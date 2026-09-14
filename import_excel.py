Maps:
    SKU             -> name
    Product Name    -> specifications
    Product Type    -> designation
    Location        -> location     (Office -> HQ, edit LOCATION_MAP to change)
    Qty in Stock    -> quantity
    Unit Price      -> price        (parses "42.09 USD" -> 42.09)
    Supplier / Brand -> party

Skipped columns: Min Stock Level, Total Price, Status, Last Updated.

All rows are inserted as ONE Confirmed Import ticket.
"""
import os
import re
import sys
import sqlite3
from datetime import datetime

import openpyxl

DB_NAME = "inventory.db"

# Excel value -> bot location. Keys are lowercased for matching.
LOCATION_MAP = {
    "office": "HQ",
    "hq": "HQ",
    "birgunj": "Birgunj",
    "warehouse": "Warehouse",
}
DEFAULT_LOCATION = "HQ"

# Expected header names (lowercase, exact match).
HEADER_MAP = {
    "name": ["sku"],
    "specifications": ["product name"],
    "designation": ["product type"],
    "location": ["location"],
    "quantity": ["qty in stock", "qty", "quantity"],
    "price": ["unit price", "price", "rate"],
    "party": ["supplier / brand", "supplier", "brand"],
}


def parse_number(s):
    """Extract the first number from a string like '42.09 USD' or '1.0'."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"-?\d+(?:\.\d+)?", str(s))
    if not m:
        return None
    return float(m.group())


def find_idx(headers, keys):
    low = [h.lower().strip() if h else "" for h in headers]
    for key in keys:
        if key in low:
            return low.index(key)
    return None


def cell_str(row, idx):
    if idx is None or idx >= len(row) or row[idx] is None:
        return ""
    v = row[idx]
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def main(path):
    if not os.path.exists(path):
        print(f"File not found: {path}")
        sys.exit(1)
    if not os.path.exists(DB_NAME):
        print(f"DB not found ({DB_NAME}). Run bot.py once first so it creates the schema.")
        sys.exit(1)

    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        print("Excel file is empty.")
        sys.exit(1)

    headers = list(rows[0])
    print(f"Detected headers: {headers}")

    idx = {k: find_idx(headers, v) for k, v in HEADER_MAP.items()}
    missing = [k for k in ("name", "designation", "quantity") if idx[k] is None]
    if missing:
        print(f"Missing required columns: {missing}")
        print("Edit HEADER_MAP at the top of this script to match your headers.")
        sys.exit(1)

    items = []
    errors = []
    skipped_blanks = 0

    for i, row in enumerate(rows[1:], start=2):
        name = cell_str(row, idx["name"])
        designation = cell_str(row, idx["designation"])
        specs = cell_str(row, idx["specifications"])
        party = cell_str(row, idx["party"])
        raw_loc = cell_str(row, idx["location"])

        if not name and not specs and not designation:
            skipped_blanks += 1
            continue

        if not name:
            # fall back to Product Name if SKU is missing
            name = specs[:60] if specs else ""

        if not name:
            errors.append(f"Row {i}: no name (SKU) and no Product Name")
            continue
        if not designation:
            errors.append(f"Row {i}: missing Product Type")
            continue

        qty = parse_number(cell_str(row, idx["quantity"]))
        if qty is None or qty <= 0:
            errors.append(f"Row {i}: bad quantity '{cell_str(row, idx['quantity'])}'")
            continue
        qty = int(qty)

        price = parse_number(cell_str(row, idx["price"])) or 0.0
        if price < 0:
            errors.append(f"Row {i}: negative price")
            continue

        loc_key = raw_loc.lower()
        location = LOCATION_MAP.get(loc_key, DEFAULT_LOCATION)
        if loc_key and loc_key not in LOCATION_MAP:
            errors.append(f"Row {i}: unknown location '{raw_loc}' -> using {DEFAULT_LOCATION}")

        items.append((name, designation, specs, qty, price, party, location))

    print(f"\nParsed {len(items)} valid rows.")
    print(f"Skipped {skipped_blanks} blank rows.")
    if errors:
        print(f"{len(errors)} row(s) had issues:")
        for e in errors[:30]:
            print("  " + e)
    if not items:
        sys.exit(1)

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    try:
        c.execute("ALTER TABLE items ADD COLUMN location TEXT")
    except sqlite3.OperationalError:
        pass

    total = sum(q * p for (_, _, _, q, p, _, _) in items)
    date = datetime.now().strftime("%Y-%m-%d")
    c.execute("""INSERT INTO tickets (transaction_type, cost, date, status, authority, confirmed_at)
                 VALUES ('Import', ?, ?, 'Confirmed', 'Excel Import', datetime('now'))""",
              (total, date))
    tid = c.lastrowid
    c.executemany("""INSERT INTO items
                (ticket_id, name, designation, specifications, quantity, price, party, location, currency)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
               [(tid, *it, "USD") for it in items])
    conn.commit()
    conn.close()

    print(f"\nImported {len(items)} items as Ticket #{tid} (total Rs. {total:.2f}).")
    print("Run the bot and use /inventory to confirm.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python import_excel.py "MEPL Inventory Tracker.xlsx"')
        sys.exit(1)
    main(sys.argv[1])
