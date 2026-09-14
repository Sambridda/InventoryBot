"""Gemini 2.5 Flash PDF → structured items extraction (new google-genai SDK)."""
import json
import os
import re

from google import genai
from google.genai import types as genai_types


# ---------------- exceptions ----------------

class ExtractionError(Exception):
    """Base for all extraction failures. .reason is a short human-readable tag."""
    reason = "unknown"

class ConfigError(ExtractionError):
    reason = "config"

class KeyError_(ExtractionError):
    reason = "auth"

class NetworkError(ExtractionError):
    reason = "network"

class ModelError(ExtractionError):
    reason = "model"

class ParseError(ExtractionError):
    reason = "parse"

class EmptyResultError(ExtractionError):
    reason = "empty"

class MultiPartyError(ExtractionError):
    reason = "multi_party"

class MultiCurrencyError(ExtractionError):
    reason = "multi_currency"


# ---------------- prompt ----------------

PROMPT_TEMPLATE = """You are extracting structured inventory data from a document.

The document is a {transaction_type} document (for a company's internal inventory tracking).

Read the entire document (handwriting, tables, stamps — all of it). Return ONLY valid JSON matching this schema:

{{
  "party": "Name of the other party (supplier for Import, client for Export), or null if unclear",
  "currency": "3-letter currency code (USD, NPR, INR, EUR, CNY, GBP, JPY, etc.) as it appears, or 'NPR' if unclear",
  "items": [
    {{
      "name": "Short, unique identifier (SKU, model code, or compact name). Keep simple.",
      "designation": "Pick the closest from this list: {designations}",
      "specifications": "All distinguishing details, comma-separated. Dimensions, grade, colour, material, etc. Keep concise.",
      "quantity": 0,
      "price": 0.0
    }}
  ]
}}

Rules:
- Extract EVERY line item. Do not skip, do not merge.
- quantity must be a whole number (positive).
- price is a number only, no currency symbol. Use 0 if unknown.
- specifications: comma-separated, short identifiers only. Not full sentences.
- designation: MUST be one of: {designations}. If nothing fits, use "Other".
- party: read top AND bottom. Pick the party this transaction is with.
- currency: single code for the whole document. If mixed currencies, return {{"error": "multi_currency"}}.
- If more than one distinct party, return {{"error": "multi_party"}}.

Return ONLY the JSON object. No markdown, no explanation, no surrounding text."""


def _clean_json(raw):
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise ParseError("No JSON object found in model response.")
    return m.group(0)


def _build_client():
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ConfigError("GEMINI_API_KEY missing from .env")
    try:
        return genai.Client(api_key=api_key)
    except Exception as e:
        raise ConfigError(f"Could not build Gemini client: {e}") from e


# ---------------- main entry ----------------

def extract_items(pdf_bytes, transaction_type, existing_designations=None):
    """
    Returns dict: {"party": str, "currency": str, "items": [ ... ]}
    Raises a specific ExtractionError subclass on failure.
    """
    client = _build_client()

    pool = set(existing_designations or [])
    for d in ["Mechanical", "Electrical", "Plumbing", "Civil", "Consumable", "Other"]:
        pool.add(d)
    designations_str = ", ".join(sorted(pool))

    prompt = PROMPT_TEMPLATE.format(
        transaction_type=transaction_type,
        designations=designations_str,
    )

    try:
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=[
                prompt,
                genai_types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
            ],
        )
    except Exception as e:
        msg = str(e).lower()
        if "api key" in msg or "unauth" in msg or "permission" in msg or "401" in msg or "403" in msg:
            raise KeyError_(f"Gemini rejected the API key: {e}") from e
        if "not found" in msg or "unsupported" in msg or "404" in msg or "model" in msg:
            raise ModelError(f"Gemini model problem: {e}") from e
        if "timeout" in msg or "connect" in msg or "network" in msg or "dns" in msg:
            raise NetworkError(f"Could not reach Gemini: {e}") from e
        raise ExtractionError(f"Gemini call failed: {e}") from e

    raw = ""
    try:
        raw = response.text or ""
    except Exception as e:
        raise ParseError(f"Gemini returned no text: {e}") from e

    cleaned = _clean_json(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ParseError(f"Model returned invalid JSON: {e}") from e

    if isinstance(data, dict) and data.get("error") == "multi_party":
        raise MultiPartyError("Document has multiple parties.")
    if isinstance(data, dict) and data.get("error") == "multi_currency":
        raise MultiCurrencyError("Document has mixed currencies.")

    if not isinstance(data, dict) or "items" not in data:
        raise ParseError("Model response missing 'items'.")

    items = data.get("items") or []
    if not items:
        raise EmptyResultError("No items extracted.")

    party = (data.get("party") or "").strip()
    currency = (data.get("currency") or "NPR").strip().upper()[:4] or "NPR"

    cleaned_items = []
    for it in items:
        try:
            name = str(it.get("name", "")).strip()
            designation = str(it.get("designation", "")).strip() or "Other"
            specs = str(it.get("specifications", "")).strip()
            qty = int(it.get("quantity") or 0)
            price = float(it.get("price") or 0.0)
        except (ValueError, TypeError) as e:
            raise ParseError(f"Bad item row: {it} ({e})") from e
        if not name or qty <= 0:
            continue
        cleaned_items.append({
            "name": name,
            "designation": designation,
            "specifications": specs,
            "quantity": qty,
            "price": price,
        })

    if not cleaned_items:
        raise EmptyResultError("No valid items after cleaning.")

    return {"party": party, "currency": currency, "items": cleaned_items}


# ---------------- health check for /diag ----------------

def diag_ping():
    """Return (ok: bool, info: str). Runs a trivial Gemini request."""
    try:
        client = _build_client()
    except ExtractionError as e:
        return False, f"{e.__class__.__name__}: {e}"

    try:
        resp = client.models.generate_content(
            model="gemini-3.6-flash",
            contents="Reply with the single word: pong",
        )
        text = (resp.text or "").strip()
        return True, f"Model replied: {text!r}"
    except Exception as e:
        msg = str(e).lower()
        if "api key" in msg or "unauth" in msg or "401" in msg or "403" in msg:
            return False, f"KeyError: {e}"
        if "not found" in msg or "unsupported" in msg or "model" in msg:
            return False, f"ModelError: {e}"
        if "timeout" in msg or "connect" in msg or "network" in msg:
            return False, f"NetworkError: {e}"
        return False, f"Unknown: {type(e).__name__}: {e}"