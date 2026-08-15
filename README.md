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

Breakeven target = membership fee + the tax paid on it, so **$1,605.00**
($1,500.00 + 7% Polk County sales tax). The membership term runs 2026-08-07 to
2027-08-06. All of that is editable in Settings, or from the shell:

```bash
sudo winelog config                              # show current values
sudo winelog config --set membership_tax=105
sudo winelog config --set term_end=2027-08-06
```

## Setting up on Ubuntu

Two commands on a fresh Ubuntu server:

```bash
git clone https://github.com/sudds65/WineLog.git && cd WineLog
sudo ./deploy/install.sh --admin austin
```

It prompts once for the password you want, then installs Python and the
dependencies, creates a `winelog` service account, sets up the database in
`/var/lib/winelog`, installs and starts the systemd service, opens the port to
your private network in ufw, and prints the URL to open. Nothing else to do.

Re-run it any time to update — it pulls in the new code, reinstalls
dependencies, and restarts the service without touching your data.

Verified on Ubuntu 24.04 and expected to work on any current release. If the
distro's Python is newer than the published wheels for the PDF stack, the
installer detects the failed import and retries with build tools rather than
leaving you with an app that only breaks on the first upload.

**Options**

| Flag | Effect |
|---|---|
| `--admin NAME` | Create this login (prompts for the password) |
| `--port N` | Port to serve on [8071] |
| `--host ADDR` | Address to bind [0.0.0.0] |
| `--with-nginx` | Put nginx on port 80 in front, app stays on localhost |
| `--domain NAME` | `server_name` for nginx [the machine's hostname] |
| `--no-seed` | Skip loading the sample receipt |

After it finishes you get a `winelog` command for everything else:

```bash
sudo winelog import ~/receipts/*.pdf   # ingest receipts
sudo winelog config                    # show settings
sudo winelog stats                     # totals in the terminal
sudo winelog create-user sarah         # add another login
journalctl -u winelog -f               # logs
```

### Running it locally instead

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python manage.py init
python manage.py create-user austin
python manage.py seed
uvicorn app.main:app --host 127.0.0.1 --port 8071
```

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

## How the server is wired up

The installer puts the app in `/opt/winelog`, data in `/var/lib/winelog`, and
service settings in `/etc/winelog.env` — that env file is the only thing you
normally edit:

```bash
sudo nano /etc/winelog.env       # host, port, data dir, cookie policy
sudo systemctl restart winelog
```

The systemd unit runs as an unprivileged `winelog` account with
`ProtectSystem=strict`, so the app can only write its own data directory.

By default it listens on `0.0.0.0` with ufw allowing the RFC1918 ranges only.
If ufw was inactive the installer adds the rules (including SSH) but leaves it
off, and tells you — turn it on with `sudo ufw enable`. With `--with-nginx` the
app instead binds localhost and nginx serves port 80, allowing private ranges
and denying the rest (`deploy/nginx.conf.example`).

**Serving over HTTPS?** Set `WINELOG_COOKIE_SECURE=true` in `/etc/winelog.env`
so the session cookie is marked `Secure`. Leave it `false` on plain HTTP or the
browser will drop the cookie and you'll never stay signed in.

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

Under systemd these live in `/etc/winelog.env`, which also carries
`WINELOG_HOST` and `WINELOG_PORT` for the bind address.

## Admin CLI

On a server these are all reachable as `sudo winelog <command>`; locally it's
`python manage.py <command>`.

```
init                       create the database
create-user <name>         add a login
set-password <name>        change one (signs out that user's devices)
list-users
seed [--force]             load app/seed_data.json
import <pdf|dir> [...]     ingest receipts (--dry-run to preview)
config [--set KEY=VALUE]   show or change settings
stats                      breakeven summary in the terminal
```

Settings you can change with `config`: `membership_fee`, `membership_tax`,
`term_start`, `term_end`, `discount_percent`, `member_name`. Money is given in
dollars.

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

49 tests: parser (page breaks, Gmail chrome, non-founders discounts, malformed
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
deploy/
  install.sh          one-command Ubuntu setup
  winelog.service     systemd unit
  winelog.env.example service configuration
  nginx.conf.example  optional reverse proxy
manage.py             admin CLI
tests/
```
