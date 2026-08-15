# WineLog

A small self-hosted tracker for the Obscure Wine Co. founders membership: log what
you drink, and watch the 50% member discount pay back the membership fee.

* Ingests the Square receipt PDF (the one Gmail emails you) and pulls out the line items
* Counts **only** lines carrying `Discount: founders (50%)` — the wine and beer
* Runs on one Linux box with a single SQLite file, behind your VPN
* Works on an iPhone SE, an iPhone 16 Pro, and a 14" MacBook Pro

---

## What counts toward breakeven

Only line items that carry the founders discount marker:

```
2022 Nebel Riesling                       $5.50
Reg Price$11.00
Discount: founders (50%) (-$5.50)          ← this line makes it count
```

Food, undiscounted extras, tax, and tip are parsed and kept on the receipt for
reference, but they never touch the savings total. On the sample receipt the
$38.00 cheese board is stored and shown greyed out; the $31.00 of wine is what
moves the needle, for $15.50 saved.

Breakeven target = membership fee + any tax you paid on it (Settings). It starts
at $1,500.00 — add the tax you paid there to make the target exact.

## Quick start

```bash
git clone <this repo> /opt/winelog && cd /opt/winelog
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python manage.py init
python manage.py create-user austin        # prompts for a password (10+ chars)
python manage.py seed                      # loads app/seed_data.json

uvicorn app.main:app --host 127.0.0.1 --port 8071
```

Open <http://127.0.0.1:8071> and sign in.

### Loading your receipts

`manage.py seed` loads `app/seed_data.json`, which holds the one receipt we had
in full detail (#XzEm, Aug 9 2026 — the Nebel Riesling and the Bava Ruche).
Everything else goes in the same way you'll add receipts from here on:

```bash
python manage.py import ~/receipts/*.pdf     # files or a whole directory
python manage.py import receipt.pdf --dry-run  # parse and print, save nothing
```

or drop the PDF into the **Add** tab in the browser, check the lines it found,
and hit *Log this receipt*.

Re-importing the same receipt is safe: receipts are de-duplicated on the Square
receipt number and on the file's SHA-256, so a double upload is rejected rather
than double-counted.

> The four rows in `breakeven_for_obscure.xlsx` ("two glasses of wine", etc.) are
> deliberately **not** seeded — they were summary lines, and the app tracks real
> receipts and their line items instead. Import those receipt PDFs and the same
> totals rebuild themselves with the actual wines named.

## Deploying on the local server

```bash
sudo useradd --system --home /opt/winelog winelog
sudo mkdir -p /var/lib/winelog && sudo chown winelog:winelog /var/lib/winelog
sudo cp deploy/winelog.service /etc/systemd/system/
sudo systemctl enable --now winelog
```

`deploy/nginx.conf.example` puts it behind nginx with the LAN/VPN ranges
allowed and everything else denied. The service binds `127.0.0.1` by default, so
nothing is exposed until you put a proxy in front of it.

**Serving over HTTPS?** Set `WINELOG_COOKIE_SECURE=true` so the session cookie is
marked `Secure`. Leave it `false` on plain HTTP or the browser will drop the
cookie and you'll never stay signed in.

### Backups

Everything is in `$WINELOG_DATA_DIR` (`/var/lib/winelog` under systemd):

```bash
sqlite3 /var/lib/winelog/winelog.db ".backup '/backup/winelog-$(date +%F).db'"
```

`receipts/` beside it holds the uploaded PDFs. **Settings → Export CSV** gives you
a flat file of every line item if you want the data elsewhere.

## Using it

**Dashboard** — how much you've saved, how much is left, and whether you're on
pace. The chart plots cumulative savings against a straight "even pace" run to
breakeven by the membership end date; above the grey line means you're ahead.

**Purchases** — every visit broken out by receipt, with each line item, what it
listed for, and what the discount saved.

**Add** — upload a receipt PDF or type a purchase in by hand. The upload screen
shows what the parser found and lets you untick anything that shouldn't count
before saving.

**Search & insights** — free-text search over item names with date, type, and
sort filters, plus answers to the questions worth asking:

* the priciest thing you ordered (and what it cost after the discount)
* the biggest single saving
* the best visit and the best month
* what you order most often — vintages collapse, so a 2022 and a 2023 of the
  same wine count as two orders of that wine

**Settings** — membership fee and tax, term dates, default discount rate, CSV
export, password change.

## Configuration

All optional; defaults in brackets.

| Variable | Purpose |
|---|---|
| `WINELOG_DATA_DIR` | Database + uploads directory [`./data`] |
| `WINELOG_DB` | Database path [`$WINELOG_DATA_DIR/winelog.db`] |
| `WINELOG_UPLOAD_DIR` | Stored receipt PDFs [`$WINELOG_DATA_DIR/receipts`] |
| `WINELOG_COOKIE_SECURE` | Mark the session cookie `Secure` [`false`] |
| `WINELOG_SESSION_DAYS` | How long a login lasts [`30`] |
| `WINELOG_MAX_UPLOAD_BYTES` | Upload size cap [`10485760`] |
| `WINELOG_LOGIN_MAX_ATTEMPTS` | Failed logins before lockout [`8`] |
| `WINELOG_LOGIN_LOCKOUT_SECONDS` | Lockout length [`300`] |

## Admin CLI

```
python manage.py init                    # create the database
python manage.py create-user <name>      # add a login
python manage.py set-password <name>     # change one (signs out that user's devices)
python manage.py list-users
python manage.py seed [--force]          # load app/seed_data.json
python manage.py import <pdf|dir> [...]  # ingest receipts
python manage.py stats                   # breakeven summary in the terminal
```

## Security notes

This is built for a VPN-only network, not the open internet:

* Passwords are PBKDF2-HMAC-SHA256, 240k rounds, per-user salt
* Sessions are random opaque tokens in SQLite — a logout or password change
  revokes them immediately, everywhere
* Failed logins lock an account for 5 minutes after 8 tries
* Cookies are `HttpOnly` + `SameSite=Lax`; writes additionally require a custom
  header, which a cross-origin page cannot set without a CORS preflight
* Uploads are checked for the PDF magic bytes and size-capped
* No external requests: no CDNs, no fonts, no analytics. The UI is plain HTML,
  CSS, and JavaScript with no build step.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

48 tests: parser (page breaks, Gmail chrome, non-founders discounts, malformed
line maths) and API (auth, breakeven arithmetic, receipt de-duplication, search,
insights, CSV export).

## Layout

```
app/
  main.py             FastAPI routes
  receipt_parser.py   Square PDF → line items
  service.py          storage + breakeven maths
  search.py           search and insights queries
  auth.py             passwords and sessions
  db.py               SQLite schema and migrations
  config.py           environment configuration
  seed_data.json      receipt-level seed
  static/             the web app (index.html, app.css, app.js)
deploy/               systemd unit + nginx example
manage.py             admin CLI
tests/
```
