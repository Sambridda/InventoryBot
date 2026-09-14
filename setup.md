# Discord Inventory Bot — Setup Guide

This guide walks you through getting the bot running from scratch and points out every place you might need to customize it for your own business. Read the whole thing once before starting — some decisions (like locations and designations) are baked into the code and are much easier to change before you deploy.

---

## 1. What this is

A Discord bot for tracking inventory. You propose changes as "Drafts," and they don't count until someone with the Inventory Manager role clicks Confirm. Imports add to stock; exports subtract.

It supports:
- Manual entry of single items or bulk rows
- Excel import/export for a whole ticket
- PDF ingestion (via Google Gemini) for bills and quotations
- Search and browse commands for looking up stock

---

## 2. What you need

- A computer running Windows, macOS, or Linux.
- Python 3.11 or newer ([python.org/downloads](https://www.python.org/downloads))
  - On Windows, tick "Add Python to PATH" during installation.
- A Discord account with permission to create a server or add bots to an existing one.
- A Google AI Studio account for the Gemini API key (free tier is fine): [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
- Optional: an existing inventory spreadsheet if you want to bulk-load starting stock.

No credit card, server, or hosting is required. SQLite is built into Python.

---

## 3. Install the Python dependencies

Open a terminal in the project folder and run:

```bash
pip install -r requirements.txt
```

If `pip` isn't recognized, try `python -m pip install -r requirements.txt` instead.

---

## 4. Create the Discord bot

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications). Click **New Application**. Name it whatever you like.
2. In the sidebar click **Bot** → **Reset Token** → copy it. Save this somewhere safe; you'll only see it once.
3. In the sidebar click **OAuth2** → **URL Generator**:
   - Scopes: tick `bot` AND `applications.commands`
   - Bot Permissions: tick `Send Messages`, `Embed Links`, `Read Message History`
   - Copy the URL at the bottom and open it in your browser. Pick your server, authorize.

   > **Important:** both scopes are required. If you only pick `bot`, the bot will join but slash commands will fail with "Missing Access."

4. In Discord, enable Developer Mode: User Settings → Advanced → Developer Mode ON.
5. Create a role called **Inventory Manager** in your server and assign it to yourself (and to anyone else who should be able to confirm/edit drafts).
6. Create (or pick) a single text channel where the bot should operate. This is your inventory channel.
7. Right-click each of the following and choose **Copy ID**:
   - Your server icon → Server (Guild) ID
   - The inventory channel → Channel ID
   - The Inventory Manager role → Role ID

You should now have 4 IDs plus the bot token.

---

## 5. Get a Gemini API key

1. Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
2. Sign in with a Google account.
3. Click **Create API key** → pick a project → copy the key. It's a long string starting with `AIza`.

The free tier is enough for this bot. If you never plan to use `/import` or `/export` (the PDF commands), you can skip this step and leave `GEMINI_API_KEY` blank — everything else works without it.

---

## 6. Create the .env file

In the same folder as `bot.py`, create a file named exactly `.env` (no `.txt` extension). It should contain:

```ini
DISCORD_TOKEN=your_bot_token_here
GUILD_ID=your_server_id_here
INVENTORY_MANAGER_ROLE_ID=your_role_id_here
INVENTORY_CHANNEL_ID=your_channel_id_here
GEMINI_API_KEY=your_gemini_key_here
```

Rules:
- No quotes around values.
- No spaces before or after the `=`.
- Save as plain text, not `.rtf` or `.docx`.

If you're not using PDF import, set `GEMINI_API_KEY=` (empty).

---

## 7. Run it

```bash
python bot.py
```

You should see:

```
Logged in as YourBot#1234. Commands synced.
```

Leave that terminal window open — the bot runs as long as the window stays open. Closing it shuts down the bot.

In Discord, type `/` in your inventory channel. You should see commands like `/add`, `/remove`, `/inventory`, `/lookup`, `/addbulk`, `/removebulk`, `/import`, `/export`, `/diag`.

Test with `/diag` first — it checks that the Gemini side is wired up correctly.

---

## 8. (Optional) Load starting inventory from Excel

If you already have inventory in a spreadsheet, use `import_excel.py` to bulk-load it.

1. First run `bot.py` once so it creates the database:
   ```bash
   python bot.py
   ```
   Wait until "Commands synced," then `Ctrl+C` to stop it.

2. Open `import_excel.py` in a text editor. Find the `HEADER_MAP` dictionary near the top:
   ```python
   HEADER_MAP = {
       "name":           ["sku"],
       "specifications": ["product name"],
       "designation":    ["product type"],
       "location":       ["location"],
       "quantity":       ["qty in stock", "qty", "quantity"],
       "price":          ["unit price", "price", "rate"],
       "party":          ["supplier / brand", "supplier", "brand"],
   }
   ```
   Change the strings on the right to match your spreadsheet's column headers (case-insensitive). Multiple alternatives are supported — add more if your file has variations.

3. Also near the top is `LOCATION_MAP`. It converts what's in your spreadsheet into the bot's location names:
   ```python
   LOCATION_MAP = {
       "office":    "HQ",
       "hq":        "HQ",
       "birgunj":   "Birgunj",
       "warehouse": "Warehouse",
   }
   ```
   Edit as needed. Anything not in the map defaults to `HQ`.

4. Run:
   ```bash
   python import_excel.py "path/to/your/file.xlsx"
   ```
   It prints what it parsed and what it skipped. Fix any issues then rerun (it appends, so delete `inventory.db` first if you want a clean slate).

5. Start the bot again:
   ```bash
   python bot.py
   ```

---

## 9. Customizing for your own needs

The bot works out of the box with the defaults below, but almost everyone will want to change at least some of them. Open `bot.py` in a text editor and look for these near the top of the file.

### Locations (`bot.py`)
```python
LOCATIONS = ["HQ", "Birgunj", "Warehouse"]
```
Replace with the actual sites you track stock at. Examples: `["Main Store", "Warehouse A", "Truck 1"]` or `["Kathmandu", "Pokhara", "Birgunj"]`. Keep names short — they're shown in tables.

### Location abbreviations (`bot.py`)
```python
LOCATION_ABBR = {"HQ": "HQ", "Birgunj": "BIR", "Warehouse": "WH"}
```
These 2–3 letter abbreviations appear in the `/inventory` table to keep it from wrapping. Update whenever you change `LOCATIONS`. If you forget an entry, the full name is used.

### Designations (`bot.py`)
```python
DESIGNATIONS = ["Mechanical", "Electrical", "Plumbing", "Civil", "Consumable", "Other"]
```
These are the item categories users pick from dropdowns. Replace with the categories that make sense for your business. Examples: `["Electronics", "Tools", "Packaging", "Raw Materials"]`. Keep the list to 25 or fewer (Discord's dropdown limit).

> Note: the PDF extraction prompt also references this list when classifying items from bills. A longer or more specific list gives better AI results.

### Currency choices (`bot.py`)
```python
CURRENCY_CHOICES = ["NPR", "USD", "INR", "EUR", "GBP", "CNY", "JPY"]
```
Which currencies appear in dropdowns. Add or remove as needed. Use 3-letter ISO codes.

### Currency symbols (`bot.py`)
Maps currency codes to display symbols. The bot shows "Rs. 1,100.00" for NPR, "$294.63" for USD, etc. If you add a currency to `CURRENCY_CHOICES` that has no symbol, the display falls back to "1,100.00 XYZ".

### Draft TTL / reminders (`bot.py`)
```python
DRAFT_TTL_HOURS  = 24    # how long a draft lives
REMINDER_AT_HOURS = 20   # when the reminder ping fires
DUP_THRESHOLD    = 85    # fuzzy-match threshold (0-100)
PAGE_SIZE        = 10    # rows per page in /inventory
```
Change `DRAFT_TTL_HOURS` to 48 if your team needs longer, or to 4 if this is fast-paced. `REMINDER_AT_HOURS` must be less than `DRAFT_TTL_HOURS`.

`DUP_THRESHOLD`: higher = fewer duplicate warnings. Lower = more.

`PAGE_SIZE`: larger = more rows per page, but Discord embeds cap at 1024 characters per field. 10–12 is safe; 15+ starts breaking.

### Hardcoded "HQ" for PDF imports (`bot.py`)
Search `bot.py` for the line inside `_handle_pdf_import`:
```python
it["name"], it["designation"], it.get("specifications", ""),
int(it["quantity"]), float(it.get("price", 0.0)),
party, "HQ", currency))
```
The `"HQ"` there is where PDF-imported items are assigned by default. If your primary location is called something else (e.g. "Main Store"), change `"HQ"` to that name.

### Role name (Discord, not code)
The role must be named "Inventory Manager" — or you can name it anything you want and the bot will still work, since it looks up the role by ID from `.env`, not by name. So the role name is cosmetic. Just make sure the ID in `.env` matches the role you want users to have.

### Channel (Discord, not code)
All commands are locked to the channel ID in `.env` (`INVENTORY_CHANNEL_ID`). To use a different channel, update `.env` with the new channel ID.

To disable the channel restriction entirely (for testing), set `INVENTORY_CHANNEL_ID=0` in `.env`. Every channel will then accept commands. Not recommended for production.

### Gemini model (`ai_extract.py`)
Search `ai_extract.py` for `gemini-` — you'll find the model name used for PDF extraction and the `/diag` ping.

The default is `gemini-3.6-flash`. If Google retires that model, replace it with whatever the error message suggests. Common alternates: `gemini-3.6-flash-latest`, `...-preview`.

---

## 10. How people will use it

Read this before inviting your team. It describes the workflow they'll follow.

**For a new purchase (adding stock):**
1. Either type `/add` (single item) or `/addbulk` (many items) or `/import` (upload the bill as PDF).
2. The bot posts a Draft ticket with buttons.
3. Anyone with the Inventory Manager role can add more items, edit items via the modal or Excel round-trip, fix a single item's specs, or Confirm/Cancel.
4. On Confirm, stock updates. Nothing changes before that.

**For an outgoing sale (removing stock):**
Same as above but use `/remove`, `/removebulk`, or `/export`. The draft shows a "Stock check" section that tells you whether the item exists and whether there's enough on hand. If the item isn't there, you'll see a red ❌. You can still confirm — the bot doesn't block — but you probably want to cancel instead.

**For looking things up:**
- `/inventory` — browse everything, paginated
- `/lookup 9mm` — search by any word in name, designation, specs, or location

Both views have a "Download as Excel" button.

**Daily maintenance:**
Nothing needed. Drafts expire automatically after 24h with a reminder ping at 20h. Expired drafts are kept, not deleted.

---

## 11. Troubleshooting

| Symptom | Fix |
|---|---|
| "Missing Access" on startup | The bot lacks the `applications.commands` scope. Kick the bot from your server, then re-invite with a new URL generated with both `bot` and `applications.commands` scopes. |
| "This interaction failed" when clicking a button | Look at the terminal window where `bot.py` is running — there's a full Python traceback there. The last few lines tell you which line failed. |
| "GEMINI_API_KEY not set" or `[auth]` | Check `.env`. Ensure no quotes, no spaces, no hidden characters. Regenerate the key at aistudio.google.com/apikey and try again. |
| `[model]` errors | Google retires Gemini models periodically. The error message will suggest a replacement. Open `ai_extract.py`, search for `gemini-`, replace both occurrences. |
| PDF import says "AI unavailable" | The bot couldn't reach Gemini. Check your internet and run `/diag` for a quick health check. |
| Drafts don't expire | The expiry sweep runs every 30 minutes, and only while the bot is running. If you close the terminal, nothing expires until you start the bot again. |
| Items show two rows with the same name | Stock is grouped by name + location. If they differ in either, they show separately. Check case and spelling. |
| The bot exits silently after "Logged in" | The bottom of `bot.py` may have been truncated. It should end with `if __name__ == "__main__": client.run(TOKEN)`. |

---

## 12. Files in this project

| File | Purpose |
|---|---|
| `bot.py` | The Discord bot itself — commands, buttons, modals, database operations. |
| `ai_extract.py` | Gemini integration for PDF parsing. Only used by `/import` and `/export`. |
| `import_excel.py` | One-shot script to bulk-load inventory from a spreadsheet. Not used at runtime. |
| `inventory.db` | SQLite database. Created automatically on first run. This is your actual data — back it up periodically. |
| `.env` | Your secrets. Never commit this to git. |
| `requirements.txt` | Python dependencies. |
| `README.md` | Overview of the project. |
| `SETUP.md` | This file. |

---

## 13. Backups

Your entire inventory lives in `inventory.db`. That single file contains every ticket, every item, every transaction.

- **To back it up:** copy `inventory.db` somewhere safe.
- **To restore:** replace `inventory.db` with your backup while the bot is stopped.
- **To start fresh:** delete `inventory.db` and restart the bot. It will recreate an empty database.

There is no built-in backup mechanism. If you want scheduled backups, use your OS scheduler (Task Scheduler on Windows, cron on Linux) to copy the file daily.

---

## 14. What's not implemented (yet)

- Local AI models for offline PDF parsing. Currently the PDF commands require internet access to Gemini. Everything else works offline.
- Cross-location stock transfers. To move items from one location to another, use `/remove` on the source and `/add` on the destination.
- Web-based draft editor. Bulk editing happens inside Discord or via the Excel round-trip.
- Automatic backups. See section 13.

If you want to contribute any of these, the code is organized to make them easy additions.
