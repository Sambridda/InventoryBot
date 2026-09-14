# Discord Inventory Bot

A Discord bot that manages a small company's inventory through a **draft → review → confirm** workflow. Supports manual entry, bulk paste, Excel round-tripping, and PDF ingestion via Gemini AI.

Built for a Raspberry Pi deployment, developed on Windows. SQLite backend, no external servers required except Gemini for PDF parsing.

---

## Features

- **Draft tickets** — every change to inventory is proposed as a Draft first. Nothing hits the actual stock until an Inventory Manager clicks Confirm.
- **Import / Export** — imports add to stock, exports subtract. Same draft workflow for both.
- **Bulk editing** — paste many rows at once via a modal, or download the draft as `.xlsx`, edit in Excel, and upload it back.
- **Stock check on export** — before confirming an export, the draft shows whether the target item actually exists and whether there's enough on hand. Informational, not blocking.
- **Fuzzy duplicate detection** — imports are checked against existing inventory using `rapidfuzz`. Likely duplicates appear as a warning in the draft embed.
- **PDF ingestion** — drop a bill or quotation into `/import`, Gemini 3.6 Flash reads it (including handwriting), and produces a draft automatically. Falls back gracefully if the AI is unreachable.
- **Excel import / export** — bulk load initial inventory from a spreadsheet; download any view as `.xlsx`.
- **24-hour draft expiry** — drafts that sit unconfirmed for a day flip to `Expired` after a reminder ping. Nothing is auto-deleted.
- **Channel lock** — all commands restricted to a single `#Inventory` channel.
- **Role gate** — confirm, cancel, and edit actions require the Inventory Manager role.

---

## Tech Stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| Discord | `discord.py` 2.5+ |
| Database | SQLite (file-based, no server) |
| Excel | `openpyxl` |
| Fuzzy matching | `rapidfuzz` |
| AI extraction | Google Gemini (`google-genai` SDK) |
| Config | `python-dotenv` |

Designed to run on a Raspberry Pi 4 (4GB RAM) alongside a Windows machine hosting local GPU inference (not yet implemented — currently cloud-only via Gemini).

---

## Setup

### 1. Install Python dependencies

```bash
pip install discord.py python-dotenv openpyxl rapidfuzz google-genai
```

### 2. Create a Discord bot

1. Go to https://discord.com/developers/applications → **New Application**.
2. **Bot** tab → Reset Token → copy the token.
3. **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Embed Links`, `Read Message History`
4. Open the generated URL, invite the bot to your server.
5. In Discord, create a role called **Inventory Manager** and assign it to yourself.
6. Enable Developer Mode (User Settings → Advanced), then:
   - Right-click your server → Copy Server ID
   - Right-click the `#Inventory` channel → Copy Channel ID
   - Right-click the Inventory Manager role → Copy Role ID

### 3. Get a Gemini API key

https://aistudio.google.com/apikey → **Create API key**. Free tier is generous.

### 4. Create `.env`

In the project root:

```ini
DISCORD_TOKEN=your_bot_token
GUILD_ID=your_server_id
INVENTORY_MANAGER_ROLE_ID=your_role_id
INVENTORY_CHANNEL_ID=your_channel_id
GEMINI_API_KEY=your_gemini_key
```

### 5. Run

```bash
python bot.py
```

You should see `Logged in as ... Commands synced.`

### 6. (Optional) Load initial inventory from Excel

If you have an existing inventory spreadsheet, edit `import_excel.py` to match your column headers, then:

```bash
python bot.py        # creates the DB schema on first run — Ctrl+C after it prints "Logged in"
python import_excel.py "path/to/your.xlsx"
python bot.py        # run again
```

---

## Commands

### Creating drafts

| Command | Purpose |
|---|---|
| `/add` | New Import draft, single item |
| `/remove` | New Export draft, single item |
| `/addbulk` | New Import draft, many items via pasted text |
| `/removebulk` | New Export draft, many items via pasted text |
| `/import` | Upload a PDF → auto-extract as an Import draft (Manager only) |
| `/export` | Upload a PDF → auto-extract as an Export draft (Manager only) |

### Reading inventory

| Command | Purpose |
|---|---|
| `/inventory` | Browse confirmed stock, paginated, with Excel download |
| `/lookup <query>` | Search by keyword; matches name, designation, specs, or location |

### Maintenance

| Command | Purpose |
|---|---|
| `/diag` | Check Gemini connectivity and SDK versions |

---

## Draft Workflow

When you run `/add`, `/addbulk`, `/import`, or any creation command, a draft embed is posted with buttons:

**Row 1**
- `➕ Add Item` — pick Designation, Location, Currency from dropdowns → modal for Name, Specs, Qty, Price, Party
- `📝 Edit Items` — modal pre-filled with all current items as pipe-separated text. Submit to replace the draft's contents.
- `📊 Download .xlsx` — get the current items as a spreadsheet
- `📤 Upload .xlsx` — upload an edited spreadsheet to replace the draft's contents

**Row 2**
- `✏️ Edit Specs` — pick a single item, edit only its specifications
- `✅ Confirm` — commit the draft. Inventory updates.
- `❌ Cancel` — delete the draft, no effect on inventory

### Bulk text format

Imports: `name | designation | specs | qty | price | currency | party | location`

Exports: `name | designation | qty | price | currency | party | location`

Pipe or tab separated. Lines with `#` in the first cell are treated as headers and skipped.

Example:

```
Red button | Mechanical | 32A, IP54 | 10 | 1100 | NPR | Gwen | HQ
Copper wire | Electrical | 2.5mm² | 50 | 320 | NPR | Ram | HQ
```

---

## How Stock Is Calculated

Stock on hand = sum of all confirmed Import quantities − sum of all confirmed Export quantities, **grouped by (name, location)**.

- Names are matched case-insensitively.
- Specifications are stored but not part of the identity — items are identified by name + location.
- Exports never delete rows. Every transaction stays in the database as an audit trail.

This means if you import 100 items and later export 100, `/inventory` shows `0` but the database keeps both transactions for traceability.

---

## Database Schema

SQLite, single file `inventory.db`.

### `tickets`

| Column | Type | Notes |
|---|---|---|
| ticket_id | INTEGER PK | autoincrement |
| transaction_type | TEXT | `'Import'` or `'Export'` |
| cost | REAL | snapshotted at confirm time, never recalculated |
| date | TEXT | ISO date |
| status | TEXT | `Draft` / `Confirmed` / `Expired` |
| authority | TEXT | user who confirmed, null until confirmed |
| created_at | TEXT | ISO timestamp |
| expires_at | TEXT | 24h from creation |
| reminded_at | TEXT | 20h reminder timestamp |
| confirmed_at | TEXT | confirmation timestamp |
| channel_id | INTEGER | where the draft was posted |
| message_id | INTEGER | the draft embed's message ID |

### `items`

| Column | Type | Notes |
|---|---|---|
| item_id | INTEGER PK | autoincrement |
| ticket_id | INTEGER FK | parent ticket |
| name | TEXT | short identifier |
| designation | TEXT | category |
| specifications | TEXT | free-form description |
| quantity | INTEGER | whole numbers only |
| price | REAL | per-unit price |
| party | TEXT | supplier (import) or client (export) |
| location | TEXT | `HQ` / `Birgunj` / `Warehouse` |
| currency | TEXT | ISO code, default `NPR` |

Indexes: `ticket_id`, `name`, `status`.

---

## Design Decisions

A few things that might look odd but are deliberate:

- **Drafts are proposals, not entries.** A draft never affects stock. Only after Confirm does inventory change. This matches the spec's "no automatic decisions where a human should confirm."
- **Exports are additive, not destructive.** Removing stock records a negative row. The original import row remains. This gives you a full audit trail.
- **Names are the identity, not SKUs.** Two items with the same name + location are the same item, regardless of specification. This avoids duplicate rows when the same item is entered twice with slightly different description text.
- **No automatic canonicalization.** If you type `Bomb` and later `bomb`, the display merges them but the storage keeps both spellings. This matches the spec's "no silent merging."
- **PDF is not saved.** After Gemini reads it, the file is discarded. No document archive.
- **Currency is per-item.** A ticket is expected to be single-currency (the AI detects this); mixed currencies are rejected at extraction time.
- **Gemini only for extraction.** It never writes to the database. It produces JSON; the bot parses and creates the draft.

---

## Roadmap / Not Implemented

- **Local inference fallback** — the original spec planned local Qwen2.5-7B for PDF extraction. Currently cloud-only via Gemini. Adding Ollama support would make the bot fully offline-capable.
- **Cross-location transfers** — moving stock from HQ to Birgunj requires two tickets right now.
- **R1 / Gemini conversational bots** — this repository only covers the Inventory Bot. A separate multi-bot workspace is planned.
- **Web-based draft editor** — mentioned as a "Phase II" idea. Current editing is via Discord modals or Excel round-trip.

---

## File Layout

```
inventory-bot/
├── bot.py              # Main bot: commands, buttons, modals
├── ai_extract.py       # Gemini integration for PDF parsing
├── import_excel.py     # One-shot initial bulk load from a spreadsheet
├── inventory.db        # SQLite database (gitignored)
├── .env                # Secrets (gitignored)
├── .env.example        # Template
├── requirements.txt
└── README.md
```

Add to `.gitignore`:

```
.env
inventory.db
__pycache__/
*.pyc
*.xlsx
```

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Missing Access` on startup | Bot lacks `applications.commands` scope. Kick and re-invite with both scopes. |
| `This interaction failed` on button click | Traceback in the bot's terminal window will say which line. Common: modal field limit (max 5), select row collision (max 5 width per row). |
| `/import` says `[auth]` | Gemini API key is invalid or expired. Regenerate. |
| `/import` says `[model]` | The model name (`gemini-3.6-flash`) has been retired. Check the error message for the replacement. |
| `/lookup` shows wrong quantity | Check grouping — stock sums by name + location, ignoring specs. |
| Bot exits silently after login | The `if __name__ == "__main__":` block was likely deleted or broken. |

---

## License

Personal / hobby project. Use as you see fit.
