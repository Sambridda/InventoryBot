import os
import sqlite3
from datetime import datetime, timedelta
from io import BytesIO

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from rapidfuzz import fuzz
import openpyxl

import ai_extract

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
MANAGER_ROLE_ID = int(os.getenv("INVENTORY_MANAGER_ROLE_ID", "0"))
INVENTORY_CHANNEL_ID = int(os.getenv("INVENTORY_CHANNEL_ID", "0"))

DB_NAME = "inventory.db"
DRAFT_TTL_HOURS = 24
REMINDER_AT_HOURS = 20
DUP_THRESHOLD = 85
PAGE_SIZE = 10
MAX_PDF_BYTES = 2 * 1024 * 1024

LOCATIONS = ["HQ", "Office", "Warehouse"]
LOCATION_ABBR = {"HQ": "HQ", "Office": "Off", "Warehouse": "WH"}
DESIGNATIONS = ["Mechanical", "Electrical", "Plumbing", "Civil", "Consumable", "Other"]
CURRENCY_CHOICES = ["NPR", "USD", "INR", "EUR", "GBP", "CNY", "JPY"]

CURRENCY_SYMBOLS = {
    "NPR": "Rs. ",
    "USD": "$",
    "INR": "₹",
    "EUR": "€",
    "GBP": "£",
    "CNY": "¥",
    "JPY": "¥",
}

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
GUILD = discord.Object(id=GUILD_ID)


# ---------------- helpers ----------------

def format_money(amount, currency):
    code = (currency or "NPR").upper()
    sym = CURRENCY_SYMBOLS.get(code)
    if sym:
        return f"{sym}{amount:,.2f}"
    return f"{amount:,.2f} {code}"


# ---------------- DB ----------------

def db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS tickets (
        ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
        transaction_type TEXT NOT NULL CHECK (transaction_type IN ('Import', 'Export')),
        cost REAL NOT NULL DEFAULT 0,
        date TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'Draft' CHECK (status IN ('Draft', 'Confirmed', 'Expired')),
        authority TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        expires_at TEXT,
        reminded_at TEXT,
        confirmed_at TEXT,
        channel_id INTEGER,
        message_id INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS items (
        item_id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        designation TEXT NOT NULL,
        specifications TEXT,
        quantity INTEGER NOT NULL,
        price REAL NOT NULL DEFAULT 0,
        party TEXT,
        location TEXT,
        currency TEXT DEFAULT 'NPR',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id) ON DELETE CASCADE
    )''')
    for stmt in [
        "ALTER TABLE items ADD COLUMN location TEXT",
        "ALTER TABLE items ADD COLUMN currency TEXT DEFAULT 'NPR'",
    ]:
        try:
            c.execute(stmt)
        except sqlite3.OperationalError:
            pass
    c.execute("UPDATE items SET location='HQ' WHERE location IS NULL OR location=''")
    c.execute("UPDATE items SET currency='NPR' WHERE currency IS NULL OR currency=''")
    conn.commit()
    conn.close()


def create_draft(transaction_type, channel_id):
    conn = db()
    c = conn.cursor()
    date = datetime.now().strftime("%Y-%m-%d")
    expires = (datetime.now() + timedelta(hours=DRAFT_TTL_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("INSERT INTO tickets (transaction_type, date, expires_at, channel_id) VALUES (?, ?, ?, ?)",
              (transaction_type, date, expires, channel_id))
    tid = c.lastrowid
    conn.commit()
    conn.close()
    return tid


def add_item(ticket_id, name, designation, specs, qty, price, party, location, currency="NPR"):
    conn = db()
    c = conn.cursor()
    c.execute("INSERT INTO items (ticket_id, name, designation, specifications, quantity, price, party, location, currency) "
              "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
              (ticket_id, name, designation, specs, qty, price, party, location, currency))
    conn.commit()
    conn.close()


def add_items_bulk(ticket_id, items):
    conn = db()
    c = conn.cursor()
    c.executemany(
        "INSERT INTO items (ticket_id, name, designation, specifications, quantity, price, party, location, currency) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(ticket_id, *it) for it in items])
    conn.commit()
    conn.close()


def replace_items(ticket_id, items):
    conn = db()
    c = conn.cursor()
    try:
        c.execute("DELETE FROM items WHERE ticket_id=?", (ticket_id,))
        c.executemany(
            "INSERT INTO items (ticket_id, name, designation, specifications, quantity, price, party, location, currency) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(ticket_id, *it) for it in items])
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


def get_ticket(ticket_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,))
    t = c.fetchone()
    if not t:
        conn.close()
        return None, []
    c.execute("SELECT * FROM items WHERE ticket_id=? ORDER BY item_id", (ticket_id,))
    items = [dict(r) for r in c.fetchall()]
    conn.close()
    return dict(t), items


def set_ticket_message(ticket_id, channel_id, message_id):
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE tickets SET channel_id=?, message_id=? WHERE ticket_id=?",
              (channel_id, message_id, ticket_id))
    conn.commit()
    conn.close()


def get_ticket_by_message(message_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT ticket_id FROM tickets WHERE message_id=?", (message_id,))
    row = c.fetchone()
    conn.close()
    return row["ticket_id"] if row else None


def confirm_ticket(ticket_id, authority):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT status FROM tickets WHERE ticket_id=?", (ticket_id,))
    row = c.fetchone()
    if not row or row["status"] != "Draft":
        conn.close()
        return False, 0.0
    c.execute("SELECT SUM(quantity * price) FROM items WHERE ticket_id=?", (ticket_id,))
    cost = c.fetchone()[0] or 0.0
    c.execute("UPDATE tickets SET status='Confirmed', cost=?, authority=?, confirmed_at=datetime('now') WHERE ticket_id=?",
              (cost, authority, ticket_id))
    conn.commit()
    conn.close()
    return True, cost


def cancel_ticket(ticket_id):
    conn = db()
    c = conn.cursor()
    c.execute("DELETE FROM tickets WHERE ticket_id=? AND status='Draft'", (ticket_id,))
    changed = c.rowcount
    conn.commit()
    conn.close()
    return changed > 0


def get_existing_designations():
    conn = db()
    c = conn.cursor()
    c.execute("SELECT DISTINCT designation FROM items WHERE designation IS NOT NULL AND designation != ''")
    rows = [r["designation"] for r in c.fetchall()]
    conn.close()
    return rows


def search_inventory(query):
    conn = db()
    c = conn.cursor()
    like = f"%{query.lower()}%"
    c.execute('''SELECT MIN(i.name) as name,
                 MIN(i.designation) as designation,
                 MIN(i.location) as location,
                 MAX(i.specifications) as specifications,
                 SUM(CASE WHEN t.transaction_type='Import' THEN i.quantity ELSE -i.quantity END) as qty
                 FROM items i JOIN tickets t ON i.ticket_id = t.ticket_id
                 WHERE t.status='Confirmed'
                   AND (LOWER(i.name), LOWER(COALESCE(i.location,''))) IN (
                       SELECT LOWER(i2.name), LOWER(COALESCE(i2.location,''))
                       FROM items i2 JOIN tickets t2 ON i2.ticket_id = t2.ticket_id
                       WHERE t2.status='Confirmed'
                         AND (LOWER(i2.name) LIKE ? OR LOWER(i2.designation) LIKE ?
                              OR LOWER(COALESCE(i2.specifications,'')) LIKE ?
                              OR LOWER(COALESCE(i2.location,'')) LIKE ?)
                   )
                 GROUP BY LOWER(i.name), LOWER(COALESCE(i.location,''))''',
              (like, like, like, like))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def list_all_inventory():
    conn = db()
    c = conn.cursor()
    c.execute('''SELECT MIN(i.name) as name,
                 MIN(i.designation) as designation,
                 MIN(i.location) as location,
                 MAX(i.specifications) as specifications,
                 SUM(CASE WHEN t.transaction_type='Import' THEN i.quantity ELSE -i.quantity END) as qty
                 FROM items i JOIN tickets t ON i.ticket_id = t.ticket_id
                 WHERE t.status='Confirmed'
                 GROUP BY LOWER(i.name), LOWER(COALESCE(i.location,''))
                 ORDER BY designation, name, location''')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def check_stock(name, location):
    conn = db()
    c = conn.cursor()
    c.execute('''SELECT i.name,
                 SUM(CASE WHEN t.transaction_type='Import' THEN i.quantity ELSE -i.quantity END) as qty
                 FROM items i JOIN tickets t ON i.ticket_id = t.ticket_id
                 WHERE t.status='Confirmed'
                   AND LOWER(i.name) = LOWER(?) AND LOWER(COALESCE(i.location,'')) = LOWER(?)
                 GROUP BY i.name''',
              (name, location))
    row = c.fetchone()
    conn.close()
    if not row or row["qty"] is None:
        return None, None
    return int(row["qty"]), row["name"]


def find_duplicates(name, designation):
    conn = db()
    c = conn.cursor()
    c.execute('''SELECT MIN(i.name) as name, MIN(i.designation) as designation,
                 MIN(i.location) as location, MAX(i.specifications) as specifications,
                 SUM(CASE WHEN t.transaction_type='Import' THEN i.quantity ELSE -i.quantity END) as qty
                 FROM items i JOIN tickets t ON i.ticket_id = t.ticket_id
                 WHERE t.status='Confirmed'
                 GROUP BY LOWER(i.name), LOWER(COALESCE(i.location,''))''')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    target = f"{name} {designation}".lower()
    matches = []
    for row in rows:
        cand = f"{row['name']} {row['designation']}".lower()
        score = fuzz.token_set_ratio(target, cand)
        if score >= DUP_THRESHOLD and score < 100:
            matches.append({**row, "score": score})
    return matches


# ---------------- Table formatting ----------------

def format_ticket_items_table(items, chunk_size=10):
    if not items:
        return [("Items", "*(no items yet)*")]
    header = f"{'Name':<22} {'Desig':<6} {'Qty':>4} {'Price':>12}"
    sep = "-" * len(header)
    fields = []
    for ci in range(0, len(items), chunk_size):
        chunk = items[ci:ci + chunk_size]
        lines = [header, sep]
        for item in chunk:
            name = (item.get("name") or "")[:22]
            desig = (item.get("designation") or "")[:6]
            qty = int(item.get("quantity") or 0)
            price = float(item.get("price") or 0.0)
            lines.append(f"{name:<22} {desig:<6} {qty:>4} {price:>12,.2f}")
        block = "```\n" + "\n".join(lines) + "\n```"
        fname = "Items" if ci == 0 else f"Items (part {ci // chunk_size + 1})"
        fields.append((fname, block))
    return fields


def render_inventory_codeblock(rows):
    if not rows:
        return "```\n(empty)\n```"
    header = f"{'Name':<22} {'Desig':<6} {'Loc':<4} {'Qty':>5}"
    sep = "-" * len(header)
    lines = [header, sep]
    for r in rows:
        name = (r.get("name") or "")[:22]
        desig = (r.get("designation") or "")[:6]
        loc = LOCATION_ABBR.get(r.get("location") or "HQ", "HQ")
        qty = int(r.get("qty") or 0)
        lines.append(f"{name:<22} {desig:<6} {loc:<4} {qty:>5}")
    return "```\n" + "\n".join(lines) + "\n```"


# ---------------- Bulk parsing ----------------

BULK_HEADER_KEYS = {"name", "item", "item name"}


def parse_bulk_text(text, kind="import"):
    if kind == "import":
        field_order = ["name", "designation", "specs", "qty", "price", "currency", "party", "location"]
    else:
        field_order = ["name", "designation", "qty", "price", "currency", "party", "location"]

    items = []
    errors = []
    first_data_line_seen = False

    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if not line.strip():
            continue

        if "|" in line:
            parts = [p.strip() for p in line.split("|")]
        elif "\t" in line:
            parts = [p.strip() for p in line.split("\t")]
        else:
            parts = None

        if not first_data_line_seen:
            if parts and parts[0].lower().strip() in BULK_HEADER_KEYS:
                first_data_line_seen = True
                continue
        first_data_line_seen = True

        if not parts:
            errors.append(f"Line {i}: no separator (`|` or tab) found.")
            continue

        while len(parts) < len(field_order):
            parts.append("")
        if len(parts) > len(field_order):
            errors.append(f"Line {i}: too many fields ({len(parts)}). Max {len(field_order)}.")
            continue

        row = dict(zip(field_order, parts))
        name = row["name"].strip()
        designation = row["designation"].strip()
        specs = row.get("specs", "").strip()
        party = row.get("party", "").strip()
        location = (row.get("location") or "").strip() or "HQ"
        currency = (row.get("currency") or "").strip().upper() or "NPR"
        qty_s = row["qty"].strip()
        price_s = row["price"].strip()

        if not name:
            errors.append(f"Line {i}: name is required.")
            continue
        if not designation:
            errors.append(f"Line {i}: designation is required.")
            continue
        if location not in LOCATIONS:
            errors.append(f"Line {i}: unknown location '{location}'. Use HQ / Birgunj / Warehouse.")
            continue
        try:
            qty = int(qty_s)
            if qty <= 0:
                raise ValueError()
        except ValueError:
            errors.append(f"Line {i}: quantity '{qty_s}' must be a positive whole number.")
            continue
        try:
            price = float(price_s)
            if price < 0:
                raise ValueError()
        except ValueError:
            errors.append(f"Line {i}: price '{price_s}' must be a non-negative number.")
            continue

        items.append((name, designation, specs, qty, price, party, location, currency))

    return items, errors


def ticket_to_bulk_text(items, transaction_type):
    lines = []
    for it in items:
        curr = (it.get("currency") or "NPR").upper()
        if transaction_type == "Import":
            line = " | ".join([
                it["name"],
                it["designation"],
                it.get("specifications") or "",
                str(it["quantity"]),
                f"{it['price']:.2f}",
                curr,
                it.get("party") or "",
                it.get("location") or "HQ",
            ])
        else:
            line = " | ".join([
                it["name"],
                it["designation"],
                str(it["quantity"]),
                f"{it['price']:.2f}",
                curr,
                it.get("party") or "",
                it.get("location") or "HQ",
            ])
        lines.append(line)
    return "\n".join(lines)


# ---------------- xlsx per-ticket ----------------

def make_ticket_xlsx(items, ticket_id, transaction_type):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"Ticket {ticket_id}"
    ws.append(["Name", "Designation", "Specifications", "Qty", "Price", "Currency", "Party", "Location"])
    for it in items:
        ws.append([
            it.get("name") or "",
            it.get("designation") or "",
            it.get("specifications") or "",
            int(it.get("quantity") or 0),
            float(it.get("price") or 0.0),
            (it.get("currency") or "NPR").upper(),
            it.get("party") or "",
            it.get("location") or "HQ",
        ])
    widths = [24, 16, 40, 8, 10, 10, 26, 12]
    for i, w in enumerate(widths):
        ws.column_dimensions[chr(65 + i)].width = w
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def parse_uploaded_xlsx(file_bytes, transaction_type):
    try:
        wb = openpyxl.load_workbook(BytesIO(file_bytes), data_only=True)
    except Exception as e:
        return [], [f"Could not read .xlsx: {e}"]

    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return [], ["File is empty."]

    headers = [str(h).strip() if h is not None else "" for h in rows[0]]

    def find_col(names):
        low = [n.lower() for n in names]
        for i, h in enumerate(headers):
            if h.lower() in low:
                return i
        return None

    name_i = find_col(["name", "item name", "item"])
    desig_i = find_col(["designation", "category", "desig"])
    specs_i = find_col(["specifications", "specs", "spec", "description"])
    qty_i = find_col(["qty", "quantity", "qty in stock", "stock", "on hand"])
    price_i = find_col(["price", "unit price", "rate"])
    curr_i = find_col(["currency", "curr"])
    party_i = find_col(["party", "supplier", "client", "supplier / brand"])
    loc_i = find_col(["location", "loc", "site", "warehouse"])

    if name_i is None or desig_i is None or qty_i is None:
        return [], ["Missing required columns. Need at least: Name, Designation, Qty."]

    def cell(row, idx):
        if idx is None or idx >= len(row) or row[idx] is None:
            return ""
        v = row[idx]
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        return str(v).strip()

    items = []
    errors = []

    for i, row in enumerate(rows[1:], start=2):
        name = cell(row, name_i)
        designation = cell(row, desig_i)
        specs = cell(row, specs_i)
        party = cell(row, party_i)
        location = cell(row, loc_i) or "HQ"
        currency = (cell(row, curr_i) or "NPR").upper()

        if not name and not designation:
            continue

        if not name:
            errors.append(f"Row {i}: name required.")
            continue
        if not designation:
            errors.append(f"Row {i}: designation required.")
            continue
        if location not in LOCATIONS:
            errors.append(f"Row {i}: unknown location '{location}'.")
            continue

        try:
            qty = int(float(cell(row, qty_i) or 0))
            if qty <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            errors.append(f"Row {i}: bad quantity '{cell(row, qty_i)}'.")
            continue

        try:
            price = float(cell(row, price_i) or 0)
            if price < 0:
                raise ValueError()
        except (ValueError, TypeError):
            errors.append(f"Row {i}: bad price '{cell(row, price_i)}'.")
            continue

        items.append((name, designation, specs, qty, price, party, location, currency))

    return items, errors


# ---------------- Guards ----------------

def is_manager(user: discord.Member) -> bool:
    return isinstance(user, discord.Member) and any(r.id == MANAGER_ROLE_ID for r in user.roles)


async def require_inventory_channel(interaction: discord.Interaction) -> bool:
    if INVENTORY_CHANNEL_ID and interaction.channel_id != INVENTORY_CHANNEL_ID:
        try:
            await interaction.response.send_message(
                f"⚠️ This bot only works in <#{INVENTORY_CHANNEL_ID}>.",
                ephemeral=True, delete_after=10)
        except discord.InteractionResponded:
            pass
        return False
    return True


# ---------------- Embed builders ----------------

def build_ticket_embed(ticket, items):
    tid = ticket["ticket_id"]
    ttype = ticket["transaction_type"]
    status = ticket["status"]

    if status == "Confirmed":
        color = discord.Color.green()
        title = f"Ticket #{tid} - {ttype}"
    elif status == "Expired":
        color = discord.Color.dark_grey()
        title = f"Expired Ticket #{tid} - {ttype}"
    else:
        color = discord.Color.orange() if ttype == "Import" else discord.Color.purple()
        title = f"Draft Ticket #{tid} - {ttype}"

    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="Status", value=status, inline=True)
    embed.add_field(name="Date", value=ticket["date"], inline=True)
    if ticket.get("authority"):
        embed.add_field(name="Authority", value=str(ticket["authority"]), inline=True)

    for fname, fval in format_ticket_items_table(items):
        embed.add_field(name=fname, value=fval, inline=False)

    total = sum(int(i["quantity"]) * float(i["price"]) for i in items)
    curr = (items[0].get("currency") if items else "NPR") or "NPR"

    parties = {i.get("party") or "" for i in items}
    locations = {i.get("location") or "HQ" for i in items}
    if len(parties) == 1:
        p = next(iter(parties))
        if p:
            embed.add_field(name="Party", value=p, inline=True)
    if len(locations) == 1:
        loc = next(iter(locations))
        embed.add_field(name="Location", value=loc, inline=True)

    embed.add_field(name="Total", value=format_money(total, curr), inline=False)

    if ttype == "Export" and status == "Draft" and items:
        check_lines = []
        for item in items[:10]:
            avail, matched_name = check_stock(item["name"], item["location"])
            if avail is None:
                check_lines.append(f"❌ **{item['name']}** @ {item['location']} — **not found in inventory**")
            elif avail <= 0:
                check_lines.append(f"⚠️ **{matched_name}** @ {item['location']} — on-hand is **{avail}**, removing {item['quantity']} (→ {avail - item['quantity']})")
            elif avail < item["quantity"]:
                check_lines.append(f"⚠️ **{matched_name}** @ {item['location']} — only **{avail}** on hand, removing {item['quantity']} (→ {avail - item['quantity']})")
            elif avail == item["quantity"]:
                check_lines.append(f"✓ **{matched_name}** @ {item['location']} — exactly **{avail}** on hand (will leave 0)")
            else:
                check_lines.append(f"✓ **{matched_name}** @ {item['location']} — **{avail}** on hand, removing {item['quantity']} (leaves {avail - item['quantity']})")
        embed.add_field(name="Stock check", value="\n".join(check_lines), inline=False)

    if status == "Draft":
        if ttype == "Import":
            flags = []
            seen = set()
            for item in items:
                for dup in find_duplicates(item["name"], item["designation"]):
                    key = (dup["name"], dup["location"])
                    if key in seen:
                        continue
                    seen.add(key)
                    flags.append(f"**{item['name']}** ~ **{dup['name']}** @ {dup['location']} (existing qty {dup['qty']}, sim {dup['score']:.0f}%)")
            if flags:
                embed.add_field(name="Possible duplicates", value="\n".join(flags[:5]), inline=False)
        embed.set_footer(text="Manage with the buttons below. Use Edit Items for multi-row changes.")
    elif status == "Confirmed":
        embed.set_footer(text=f"Confirmed {ticket.get('confirmed_at', '')}")
    else:
        embed.set_footer(text="Expired - no longer editable via buttons.")
    return embed


async def refresh_draft_embed(client, ticket_id):
    ticket, items = get_ticket(ticket_id)
    if not ticket or not ticket.get("channel_id") or not ticket.get("message_id"):
        return
    try:
        channel = client.get_channel(ticket["channel_id"]) or await client.fetch_channel(ticket["channel_id"])
        message = await channel.fetch_message(ticket["message_id"])
        if ticket["status"] == "Draft":
            await message.edit(embed=build_ticket_embed(ticket, items), view=DraftView())
        else:
            await message.edit(embed=build_ticket_embed(ticket, items), view=None)
    except Exception as e:
        print(f"[refresh_draft_embed] ticket {ticket_id}: {e}")


def build_inventory_page_embed(rows, page, total_pages, title="Inventory on Hand"):
    start = page * PAGE_SIZE
    chunk = rows[start:start + PAGE_SIZE]
    embed = discord.Embed(title=title, color=discord.Color.green())
    embed.add_field(
        name=f"Page {page + 1} of {total_pages} · {len(rows)} items total",
        value=render_inventory_codeblock(chunk),
        inline=False,
    )
    return embed


# ---------------- Inventory Excel export ----------------

def make_inventory_xlsx(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Inventory"
    ws.append(["Name", "Designation", "Specifications", "Location", "Quantity on Hand"])
    for r in rows:
        ws.append([r["name"], r["designation"], r.get("specifications") or "",
                   r.get("location") or "", int(r["qty"])])
    for i, w in enumerate([32, 15, 45, 12, 18]):
        ws.column_dimensions[chr(65 + i)].width = w
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


class InventoryView(discord.ui.View):
    def __init__(self, rows, title="Inventory on Hand"):
        super().__init__(timeout=600)
        self.rows = rows
        self.title = title
        self.page = 0
        self.total_pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        self._refresh_buttons()

    def _refresh_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total_pages - 1

    def build_embed(self):
        return build_inventory_page_embed(self.rows, self.page, self.total_pages, title=self.title)

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, row=0)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_inventory_channel(interaction):
            return
        if self.page > 0:
            self.page -= 1
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶ Next", style=discord.ButtonStyle.secondary, row=0)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_inventory_channel(interaction):
            return
        if self.page < self.total_pages - 1:
            self.page += 1
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="📊 Download as Excel", style=discord.ButtonStyle.success, row=0)
    async def download_excel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_inventory_channel(interaction):
            return
        buf = make_inventory_xlsx(self.rows)
        filename = f"inventory_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        await interaction.response.send_message(
            file=discord.File(buf, filename=filename), ephemeral=True)


# ---------------- Modals ----------------

class AddItemModal(discord.ui.Modal):
    """5 fields max. Designation, Location, Currency are chosen in AddItemSetupView."""
    def __init__(self, ticket_id, designation, location, currency):
        super().__init__(title=f"Add {designation} @ {location}"[:45])
        self.ticket_id = ticket_id
        self.designation = designation
        self.location = location
        self.currency = currency
        self.name_input = discord.ui.TextInput(label="Item name", max_length=100)
        self.specs_input = discord.ui.TextInput(
            label="Specifications (comma-separated)", style=discord.TextStyle.paragraph,
            required=False, max_length=500)
        self.qty_input = discord.ui.TextInput(label="Quantity (whole number)", max_length=10)
        self.price_input = discord.ui.TextInput(label="Price per unit", max_length=15)
        self.party_input = discord.ui.TextInput(label="Party (Supplier / Client)", max_length=100)
        for f in (self.name_input, self.specs_input, self.qty_input, self.price_input, self.party_input):
            self.add_item(f)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            qty = int(self.qty_input.value.strip())
            price = float(self.price_input.value.strip())
            if qty <= 0:
                raise ValueError()
        except ValueError:
            await interaction.response.send_message("Invalid quantity or price.", ephemeral=True)
            return
        add_item(self.ticket_id,
                 self.name_input.value.strip(),
                 self.designation,
                 self.specs_input.value.strip(),
                 qty, price,
                 self.party_input.value.strip(),
                 self.location,
                 self.currency)
        await refresh_draft_embed(interaction.client, self.ticket_id)
        await interaction.response.send_message("Item added.", ephemeral=True)


class BulkEditModal(discord.ui.Modal):
    def __init__(self, ticket_id, transaction_type, current_text):
        super().__init__(title=f"Rewrite items on Ticket #{ticket_id}"[:45])
        self.ticket_id = ticket_id
        self.transaction_type = transaction_type
        if transaction_type == "Import":
            label = "name|desig|specs|qty|price|curr|party|loc"
        else:
            label = "name|desig|qty|price|curr|party|loc"
        self.text_input = discord.ui.TextInput(
            label=label, style=discord.TextStyle.paragraph,
            default=current_text[:4000] if current_text else "",
            required=True, max_length=4000)
        self.add_item(self.text_input)

    async def on_submit(self, interaction: discord.Interaction):
        kind = "import" if self.transaction_type == "Import" else "export"
        items, errors = parse_bulk_text(self.text_input.value, kind=kind)
        if errors:
            await interaction.response.send_message(
                "**Nothing was changed.** Fix these rows and try again:\n" + "\n".join(errors[:15]),
                ephemeral=True)
            return
        if not items:
            await interaction.response.send_message("No valid rows — nothing changed.", ephemeral=True)
            return
        ok = replace_items(self.ticket_id, items)
        if not ok:
            await interaction.response.send_message("Database error — nothing changed.", ephemeral=True)
            return
        await refresh_draft_embed(interaction.client, self.ticket_id)
        await interaction.response.send_message(
            f"Ticket rewritten with **{len(items)}** item(s).", ephemeral=True)


class NewBulkModal(discord.ui.Modal):
    def __init__(self, transaction_type, channel_id):
        super().__init__(title=f"New {transaction_type} - bulk")
        self.transaction_type = transaction_type
        self.channel_id = channel_id
        if transaction_type == "Import":
            label = "name|desig|specs|qty|price|curr|party|loc"
            placeholder = "Red button | Mechanical | 32A, IP54 | 10 | 1100 | NPR | Gwen | HQ"
        else:
            label = "name|desig|qty|price|curr|party|loc"
            placeholder = "Red button | Mechanical | 3 | 1100 | NPR | Ram | HQ"
        self.text_input = discord.ui.TextInput(
            label=label, style=discord.TextStyle.paragraph,
            placeholder=placeholder, required=True, max_length=4000)
        self.add_item(self.text_input)

    async def on_submit(self, interaction: discord.Interaction):
        kind = "import" if self.transaction_type == "Import" else "export"
        items, errors = parse_bulk_text(self.text_input.value, kind=kind)
        if not items:
            reply = "No valid items found."
            if errors:
                reply += "\n\n" + "\n".join(errors[:10])
            await interaction.response.send_message(reply, ephemeral=True)
            return
        tid = create_draft(self.transaction_type, self.channel_id)
        add_items_bulk(tid, items)
        ticket, titems = get_ticket(tid)
        msg = await interaction.channel.send(embed=build_ticket_embed(ticket, titems), view=DraftView())
        set_ticket_message(tid, interaction.channel_id, msg.id)
        reply = f"Draft Ticket #{tid} created with **{len(items)}** item(s)."
        if errors:
            reply += "\n\nSkipped lines:\n" + "\n".join(errors[:10])
        await interaction.response.send_message(reply, ephemeral=True)


class UploadXlsxModal(discord.ui.Modal):
    def __init__(self, ticket_id, transaction_type):
        super().__init__(title=f"Upload .xlsx for Ticket #{ticket_id}"[:45])
        self.ticket_id = ticket_id
        self.transaction_type = transaction_type
        self.file_input = discord.ui.FileUpload(required=True)
        self.add_item(self.file_input)

    async def on_submit(self, interaction: discord.Interaction):
        attachment = self.file_input.value
        try:
            file_bytes = await attachment.read()
        except Exception as e:
            await interaction.response.send_message(f"Could not read file: {e}", ephemeral=True)
            return
        items, errors = parse_uploaded_xlsx(file_bytes, self.transaction_type)
        if errors:
            await interaction.response.send_message(
                "**Nothing was changed.** Fix these rows and try again:\n" + "\n".join(errors[:15]),
                ephemeral=True)
            return
        if not items:
            await interaction.response.send_message("No valid rows — nothing changed.", ephemeral=True)
            return
        ok = replace_items(self.ticket_id, items)
        if not ok:
            await interaction.response.send_message("Database error — nothing changed.", ephemeral=True)
            return
        await refresh_draft_embed(interaction.client, self.ticket_id)
        await interaction.response.send_message(
            f"Ticket rewritten with **{len(items)}** item(s) from uploaded file.", ephemeral=True)


class EditSpecsModal(discord.ui.Modal):
    def __init__(self, ticket_id, item):
        super().__init__(title=f"Specs for: {item['name'][:30]}")
        self.ticket_id = ticket_id
        self.item_id = item["item_id"]
        self.specs_input = discord.ui.TextInput(
            label="Specifications (comma-separated)",
            style=discord.TextStyle.paragraph,
            default=item.get("specifications") or "",
            required=False, max_length=500)
        self.add_item(self.specs_input)

    async def on_submit(self, interaction: discord.Interaction):
        new_specs = self.specs_input.value.strip()
        conn = db()
        c = conn.cursor()
        c.execute("UPDATE items SET specifications=? WHERE item_id=?", (new_specs, self.item_id))
        conn.commit()
        conn.close()
        await refresh_draft_embed(interaction.client, self.ticket_id)
        await interaction.response.send_message("Specs updated.", ephemeral=True)


# ---------------- Selects / Views ----------------

class _DesignationSelect(discord.ui.Select):
    def __init__(self, host):
        self.host = host
        super().__init__(
            placeholder="Designation",
            options=[discord.SelectOption(label=d) for d in DESIGNATIONS],
            min_values=1, max_values=1, row=0)

    async def callback(self, interaction: discord.Interaction):
        self.host.designation = self.values[0]
        await interaction.response.defer()


class _LocationSelect(discord.ui.Select):
    def __init__(self, host):
        self.host = host
        super().__init__(
            placeholder="Location",
            options=[discord.SelectOption(label=l) for l in LOCATIONS],
            min_values=1, max_values=1, row=1)

    async def callback(self, interaction: discord.Interaction):
        self.host.location = self.values[0]
        await interaction.response.defer()


class _CurrencySelect(discord.ui.Select):
    def __init__(self, host):
        self.host = host
        super().__init__(
            placeholder="Currency (default NPR)",
            options=[discord.SelectOption(label=c) for c in CURRENCY_CHOICES],
            min_values=1, max_values=1, row=2)

    async def callback(self, interaction: discord.Interaction):
        self.host.currency = self.values[0]
        await interaction.response.defer()


class AddItemSetupView(discord.ui.View):
    def __init__(self, ticket_id):
        super().__init__(timeout=180)
        self.ticket_id = ticket_id
        self.designation = None
        self.location = None
        self.currency = "NPR"
        self.add_item(_DesignationSelect(self))
        self.add_item(_LocationSelect(self))
        self.add_item(_CurrencySelect(self))

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.success, row=3)
    async def continue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.designation or not self.location:
            await interaction.response.send_message(
                "Pick at least a designation and a location first.", ephemeral=True)
            return
        await interaction.response.send_modal(
            AddItemModal(self.ticket_id, self.designation, self.location, self.currency))


class EditSpecsSelect(discord.ui.Select):
    def __init__(self, ticket_id, items):
        self.ticket_id = ticket_id
        options = []
        for it in items[:25]:
            options.append(discord.SelectOption(
                label=f"{it['name']} — {it['designation']}"[:100],
                value=str(it["item_id"]),
                description=(it.get("specifications") or "(no specs)")[:100]))
        super().__init__(placeholder="Pick an item to edit specs",
                         options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        item_id = int(self.values[0])
        conn = db()
        c = conn.cursor()
        c.execute("SELECT * FROM items WHERE item_id=?", (item_id,))
        row = c.fetchone()
        conn.close()
        if not row:
            await interaction.response.send_message("Item gone.", ephemeral=True)
            return
        await interaction.response.send_modal(EditSpecsModal(self.ticket_id, dict(row)))


class EditSpecsSelectView(discord.ui.View):
    def __init__(self, ticket_id, items):
        super().__init__(timeout=120)
        self.add_item(EditSpecsSelect(ticket_id, items))


class DraftView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="➕ Add Item", style=discord.ButtonStyle.primary, custom_id="draft:add", row=0)
    async def add_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_inventory_channel(interaction):
            return
        if not is_manager(interaction.user):
            await interaction.response.send_message("Inventory Manager role required.", ephemeral=True)
            return
        tid = get_ticket_by_message(interaction.message.id)
        if not tid:
            await interaction.response.send_message("Ticket not found.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Pick a designation, location, and currency, then click Continue.",
            view=AddItemSetupView(tid), ephemeral=True)

    @discord.ui.button(label="📝 Edit Items", style=discord.ButtonStyle.primary, custom_id="draft:edititems", row=0)
    async def edit_items(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_inventory_channel(interaction):
            return
        if not is_manager(interaction.user):
            await interaction.response.send_message("Inventory Manager role required.", ephemeral=True)
            return
        tid = get_ticket_by_message(interaction.message.id)
        if not tid:
            await interaction.response.send_message("Ticket not found.", ephemeral=True)
            return
        ticket, items = get_ticket(tid)
        if not ticket:
            return
        current_text = ticket_to_bulk_text(items, ticket["transaction_type"])
        await interaction.response.send_modal(
            BulkEditModal(tid, ticket["transaction_type"], current_text))

    @discord.ui.button(label="📊 Download .xlsx", style=discord.ButtonStyle.success, custom_id="draft:download", row=0)
    async def download_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_inventory_channel(interaction):
            return
        tid = get_ticket_by_message(interaction.message.id)
        if not tid:
            await interaction.response.send_message("Ticket not found.", ephemeral=True)
            return
        ticket, items = get_ticket(tid)
        if not ticket:
            return
        buf = make_ticket_xlsx(items, tid, ticket["transaction_type"])
        filename = f"ticket_{tid}_{ticket['date']}.xlsx"
        await interaction.response.send_message(file=discord.File(buf, filename=filename), ephemeral=True)

    @discord.ui.button(label="📤 Upload .xlsx", style=discord.ButtonStyle.secondary, custom_id="draft:upload", row=0)
    async def upload_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_inventory_channel(interaction):
            return
        if not is_manager(interaction.user):
            await interaction.response.send_message("Inventory Manager role required.", ephemeral=True)
            return
        tid = get_ticket_by_message(interaction.message.id)
        if not tid:
            await interaction.response.send_message("Ticket not found.", ephemeral=True)
            return
        ticket, _ = get_ticket(tid)
        if not ticket:
            return
        await interaction.response.send_modal(UploadXlsxModal(tid, ticket["transaction_type"]))

    @discord.ui.button(label="✏️ Edit Specs", style=discord.ButtonStyle.secondary, custom_id="draft:editspecs", row=1)
    async def edit_specs(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_inventory_channel(interaction):
            return
        if not is_manager(interaction.user):
            await interaction.response.send_message("Inventory Manager role required.", ephemeral=True)
            return
        tid = get_ticket_by_message(interaction.message.id)
        if not tid:
            await interaction.response.send_message("Ticket not found.", ephemeral=True)
            return
        _, items = get_ticket(tid)
        if not items:
            await interaction.response.send_message("No items on this ticket.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Pick an item to edit its specifications:",
            view=EditSpecsSelectView(tid, items), ephemeral=True)

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success, custom_id="draft:confirm", row=1)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_inventory_channel(interaction):
            return
        if not is_manager(interaction.user):
            await interaction.response.send_message("Inventory Manager role required.", ephemeral=True)
            return
        tid = get_ticket_by_message(interaction.message.id)
        if not tid:
            await interaction.response.send_message("Ticket not found.", ephemeral=True)
            return
        _, items = get_ticket(tid)
        if not items:
            await interaction.response.send_message("Cannot confirm an empty ticket.", ephemeral=True)
            return
        ok, cost = confirm_ticket(tid, str(interaction.user))
        if not ok:
            await interaction.response.send_message("Not a confirmable draft.", ephemeral=True)
            return
        ticket, items = get_ticket(tid)
        try:
            await interaction.message.edit(embed=build_ticket_embed(ticket, items), view=None)
        except Exception as e:
            print(f"[confirm edit] {e}")

        curr = (items[0].get("currency") if items else "NPR") or "NPR"
        notice = discord.Embed(title="✅ Ticket Confirmed", color=discord.Color.green(),
                               timestamp=datetime.now())
        notice.add_field(name="Ticket", value=f"#{tid}", inline=True)
        notice.add_field(name="Type", value=ticket["transaction_type"], inline=True)
        notice.add_field(name="Items", value=str(len(items)), inline=True)
        notice.add_field(name="Total", value=format_money(cost, curr), inline=True)
        notice.add_field(name="Authority", value=interaction.user.mention, inline=True)
        await interaction.response.send_message(embed=notice)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger, custom_id="draft:cancel", row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_inventory_channel(interaction):
            return
        if not is_manager(interaction.user):
            await interaction.response.send_message("Inventory Manager role required.", ephemeral=True)
            return
        tid = get_ticket_by_message(interaction.message.id)
        if not tid:
            await interaction.response.send_message("Ticket not found.", ephemeral=True)
            return
        if cancel_ticket(tid):
            try:
                await interaction.message.edit(content=f"❌ Draft Ticket #{tid} cancelled.", embed=None, view=None)
            except Exception as e:
                print(f"[cancel edit] {e}")
            await interaction.response.send_message(f"Draft Ticket #{tid} cancelled.", ephemeral=True)
        else:
            await interaction.response.send_message("Could not cancel (not a draft?).", ephemeral=True)


# ---------------- Commands ----------------

LOCATION_CHOICES = [
    app_commands.Choice(name="HQ", value="HQ"),
    app_commands.Choice(name="Birgunj", value="Birgunj"),
    app_commands.Choice(name="Warehouse", value="Warehouse"),
]


@tree.command(name="add", description="Start a new Import draft (single item)")
@app_commands.describe(
    name="Item name",
    designation="Category",
    quantity="Whole number",
    price="Price per unit",
    location="Where the items are",
    specifications="Comma-separated specs (optional)",
    party="Supplier name (optional)",
    currency="Currency code (default NPR)",
)
@app_commands.choices(location=LOCATION_CHOICES)
async def add_cmd(interaction: discord.Interaction,
                  name: str, designation: str, quantity: int, price: float,
                  location: app_commands.Choice[str],
                  specifications: str = "", party: str = "", currency: str = "NPR"):
    if not await require_inventory_channel(interaction):
        return
    if quantity <= 0:
        await interaction.response.send_message("Quantity must be a positive whole number.", ephemeral=True)
        return
    tid = create_draft("Import", interaction.channel_id)
    add_item(tid, name, designation, specifications, quantity, price, party, location.value, currency.upper())
    ticket, items = get_ticket(tid)
    msg = await interaction.channel.send(embed=build_ticket_embed(ticket, items), view=DraftView())
    set_ticket_message(tid, interaction.channel_id, msg.id)
    await interaction.response.send_message(
        f"Draft Ticket **#{tid}** created. Add more via the buttons.", ephemeral=True)


@tree.command(name="remove", description="Start a new Export draft (single item)")
@app_commands.describe(
    name="Item name (must match existing inventory)",
    designation="Category",
    quantity="Whole number",
    location="Where the items are",
    price="Sale price per unit (0 if not a sale)",
    party="Client name (optional)",
    currency="Currency code (default NPR)",
)
@app_commands.choices(location=LOCATION_CHOICES)
async def remove_cmd(interaction: discord.Interaction,
                     name: str, designation: str, quantity: int, price: float,
                     location: app_commands.Choice[str],
                     party: str = "", currency: str = "NPR"):
    if not await require_inventory_channel(interaction):
        return
    if quantity <= 0:
        await interaction.response.send_message("Quantity must be a positive whole number.", ephemeral=True)
        return
    tid = create_draft("Export", interaction.channel_id)
    add_item(tid, name, designation, "", quantity, price, party, location.value, currency.upper())
    ticket, items = get_ticket(tid)
    msg = await interaction.channel.send(embed=build_ticket_embed(ticket, items), view=DraftView())
    set_ticket_message(tid, interaction.channel_id, msg.id)
    await interaction.response.send_message(
        f"Draft Ticket **#{tid}** created. Review the **Stock check** field before confirming.", ephemeral=True)


@tree.command(name="addbulk", description="Start a new Import draft with many items at once")
async def addbulk_cmd(interaction: discord.Interaction):
    if not await require_inventory_channel(interaction):
        return
    await interaction.response.send_modal(NewBulkModal("Import", interaction.channel_id))


@tree.command(name="removebulk", description="Start a new Export draft with many items at once")
async def removebulk_cmd(interaction: discord.Interaction):
    if not await require_inventory_channel(interaction):
        return
    await interaction.response.send_modal(NewBulkModal("Export", interaction.channel_id))


@tree.command(name="import", description="Read a PDF (bill/invoice) and create an Import draft")
@app_commands.describe(document="PDF file (max 2 MB)")
async def import_pdf_cmd(interaction: discord.Interaction, document: discord.Attachment):
    if not await require_inventory_channel(interaction):
        return
    if not is_manager(interaction.user):
        await interaction.response.send_message("Inventory Manager role required.", ephemeral=True)
        return
    await _handle_pdf_import(interaction, document, "Import")


@tree.command(name="export", description="Read a PDF (quotation/invoice) and create an Export draft")
@app_commands.describe(document="PDF file (max 2 MB)")
async def export_pdf_cmd(interaction: discord.Interaction, document: discord.Attachment):
    if not await require_inventory_channel(interaction):
        return
    if not is_manager(interaction.user):
        await interaction.response.send_message("Inventory Manager role required.", ephemeral=True)
        return
    await _handle_pdf_import(interaction, document, "Export")


async def _handle_pdf_import(interaction: discord.Interaction, document: discord.Attachment, transaction_type: str):
    await interaction.response.defer(thinking=True)

    name = (document.filename or "").lower()
    if not name.endswith(".pdf"):
        await interaction.followup.send("Attachment must be a PDF.", ephemeral=True)
        return
    if document.size > MAX_PDF_BYTES:
        await interaction.followup.send(
            f"PDF is too large ({document.size / 1024 / 1024:.1f} MB). Max 2 MB.", ephemeral=True)
        return

    try:
        pdf_bytes = await document.read()
    except Exception as e:
        await interaction.followup.send(f"[pdf_read] Could not download PDF: {e}", ephemeral=True)
        return

    try:
        result = ai_extract.extract_items(
            pdf_bytes, transaction_type,
            existing_designations=get_existing_designations())
    except ai_extract.MultiPartyError:
        await interaction.followup.send(
            "[multi_party] This document has **multiple parties**. Split it and try again.", ephemeral=True)
        return
    except ai_extract.MultiCurrencyError:
        await interaction.followup.send(
            "[multi_currency] This document has **mixed currencies**. Split it and try again.", ephemeral=True)
        return
    except ai_extract.ConfigError as e:
        await interaction.followup.send(f"[config] {e}", ephemeral=True)
        return
    except ai_extract.KeyError_ as e:
        await interaction.followup.send(f"[auth] Gemini rejected the key: {e}", ephemeral=True)
        return
    except ai_extract.NetworkError as e:
        await interaction.followup.send(f"[network] Can't reach Gemini: {e}", ephemeral=True)
        return
    except ai_extract.ModelError as e:
        await interaction.followup.send(f"[model] Gemini model problem: {e}", ephemeral=True)
        return
    except ai_extract.ParseError as e:
        await interaction.followup.send(f"[parse] Model output unreadable: {e}", ephemeral=True)
        return
    except ai_extract.EmptyResultError as e:
        await interaction.followup.send(f"[empty] Nothing to extract: {e}", ephemeral=True)
        return
    except ai_extract.ExtractionError as e:
        await interaction.followup.send(f"[extraction] {type(e).__name__}: {e}", ephemeral=True)
        return

    party = result.get("party") or ""
    currency = (result.get("currency") or "NPR").upper()
    items = result["items"]

    tid = create_draft(transaction_type, interaction.channel_id)
    rows = []
    for it in items:
        rows.append((
            it["name"], it["designation"], it.get("specifications", ""),
            int(it["quantity"]), float(it.get("price", 0.0)),
            party, "HQ", currency))
    add_items_bulk(tid, rows)

    ticket, titems = get_ticket(tid)
    msg = await interaction.channel.send(embed=build_ticket_embed(ticket, titems), view=DraftView())
    set_ticket_message(tid, interaction.channel_id, msg.id)

    await interaction.followup.send(
        f"Draft Ticket **#{tid}** created from PDF ({len(items)} items, {currency}).",
        ephemeral=True)


@tree.command(name="diag", description="Check the Gemini pipe (admin only)")
async def diag_cmd(interaction: discord.Interaction):
    if not await require_inventory_channel(interaction):
        return
    if not is_manager(interaction.user):
        await interaction.response.send_message("Inventory Manager role required.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True, ephemeral=True)
    import discord as _d
    try:
        import google.genai as _g
        genai_ver = getattr(_g, "__version__", "unknown")
    except Exception as e:
        genai_ver = f"import failed: {e}"
    lines = [
        f"discord.py: `{_d.__version__}`",
        f"google-genai: `{genai_ver}`",
        f"GEMINI_API_KEY set: `{bool(os.getenv('GEMINI_API_KEY'))}`",
    ]
    ok, info = ai_extract.diag_ping()
    lines.append(f"Gemini ping: {'✅ OK' if ok else '❌ FAIL'}")
    lines.append(f"Detail: {info}")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@tree.command(name="lookup", description="Search confirmed inventory")
@app_commands.describe(query="Keyword or phrase")
async def lookup_cmd(interaction: discord.Interaction, query: str):
    if not await require_inventory_channel(interaction):
        return
    rows = search_inventory(query)
    if not rows:
        await interaction.response.send_message(f"No items found matching **{query}**.")
        return
    view = InventoryView(rows, title=f"Search results: {query}")
    await interaction.response.send_message(embed=view.build_embed(), view=view)


@tree.command(name="inventory", description="Browse confirmed inventory (paginated, with Excel export)")
async def inventory_cmd(interaction: discord.Interaction):
    if not await require_inventory_channel(interaction):
        return
    rows = list_all_inventory()
    if not rows:
        await interaction.response.send_message("Inventory is empty.")
        return
    view = InventoryView(rows)
    await interaction.response.send_message(embed=view.build_embed(), view=view)


# ---------------- Background sweep ----------------

@tasks.loop(minutes=30)
async def expiry_sweep():
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    remind_threshold = (now + timedelta(hours=DRAFT_TTL_HOURS - REMINDER_AT_HOURS)).strftime("%Y-%m-%d %H:%M:%S")

    conn = db()
    c = conn.cursor()

    c.execute('''SELECT ticket_id, channel_id FROM tickets
                 WHERE status='Draft' AND expires_at <= ? AND expires_at > ? AND reminded_at IS NULL''',
              (remind_threshold, now_str))
    for row in c.fetchall():
        if row["channel_id"]:
            try:
                ch = client.get_channel(row["channel_id"]) or await client.fetch_channel(row["channel_id"])
                await ch.send(
                    f"⏰ <@&{MANAGER_ROLE_ID}> Draft Ticket **#{row['ticket_id']}** "
                    f"expires in under {DRAFT_TTL_HOURS - REMINDER_AT_HOURS + 1} hours.")
            except Exception as e:
                print(f"[reminder] {e}")
        c.execute("UPDATE tickets SET reminded_at=? WHERE ticket_id=?", (now_str, row["ticket_id"]))

    c.execute('''SELECT ticket_id, channel_id, message_id FROM tickets
                 WHERE status='Draft' AND expires_at <= ?''', (now_str,))
    for row in c.fetchall():
        c.execute("UPDATE tickets SET status='Expired' WHERE ticket_id=?", (row["ticket_id"],))
        if row["channel_id"] and row["message_id"]:
            try:
                ch = client.get_channel(row["channel_id"]) or await client.fetch_channel(row["channel_id"])
                msg = await ch.fetch_message(row["message_id"])
                ticket, items = get_ticket(row["ticket_id"])
                await msg.edit(embed=build_ticket_embed(ticket, items), view=None)
            except Exception as e:
                print(f"[expire edit] {e}")

    conn.commit()
    conn.close()


# ---------------- Startup ----------------

client.add_view(DraftView())


@client.event
async def on_ready():
    init_db()
    if not expiry_sweep.is_running():
        expiry_sweep.start()
    tree.copy_global_to(guild=GUILD)
    await tree.sync(guild=GUILD)
    print(f"Logged in as {client.user}. Commands synced.")


if __name__ == "__main__":
    if not TOKEN or not GUILD_ID or not MANAGER_ROLE_ID:
        raise SystemExit("Missing values in .env — check DISCORD_TOKEN, GUILD_ID, INVENTORY_MANAGER_ROLE_ID.")
    client.run(TOKEN)
