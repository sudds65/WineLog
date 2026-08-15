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
| `--port N` | Port to serve on [8071, or 443 with `--https`] |
| `--host ADDR` | Address to bind [0.0.0.0] |
| `--https` | Serve HTTPS, self-signed certificate unless `--tls-cert` |
| `--tls-cert FILE` | Use this certificate instead of a generated one |
| `--tls-key FILE` | The matching private key |
| `--with-nginx` | Put nginx in front, on 80 (or 443 with `--https`) |
| `--domain NAME` | `server_name` and certificate name [the machine's hostname] |
| `--san NAME` | Another name or IP the certificate must cover (repeatable) |
| `--no-seed` | Skip loading the sample receipt |

### Serving on 80 or 443

Plain HTTP on port 80, so the address is just the hostname:

```bash
sudo ./deploy/install.sh --admin austin --port 80
```

Or HTTPS on 443, which also leaves port 80 redirecting to it:

```bash
sudo ./deploy/install.sh --admin austin --https --domain winelog.home.lan
```

`--https` on its own writes a self-signed certificate to `/etc/winelog/tls`,
valid for the FQDN, the bare hostname, `localhost` and the machine's LAN
address. Add `--san` for any other name you'll type — an mDNS `.local` name, a
second IP. Browsers
warn once per device before you accept it; pass `--tls-cert`/`--tls-key` to use
a real one instead, or upload one from **Settings** once the app is up. Either
way the session cookie is marked `Secure` automatically — that setting follows
the certificate, so there is nothing to remember.

Neither port needs the app to run as root. The systemd unit grants it
`CAP_NET_BIND_SERVICE`, which is the one privilege it takes to bind a low port;
everything else is still dropped, and the process runs as the unprivileged
`winelog` account.

The installer refuses to continue if something else already holds the port
(Apache and nginx both like 80) and tells you what is on it.

### Using your own CA

**Settings → HTTPS certificate** takes the certificate and key your CA issued
and binds them to the running server. There is no restart and no dropped
request: uvicorn asks one SSL context for a certificate on every new
connection, so loading yours into that context is enough — reload the page and
it is already being served under the new certificate.

The upload is checked before anything is touched: that the PEM parses, that the
key is the one that goes with the certificate, that it isn't expired or
passphrase-protected, and finally that OpenSSL itself will load the pair. A
certificate that doesn't name the address you're on is held back too, since
installing it would lock your browser out — there's a tick box to go ahead
anyway if you reach the app by one of its other names. If a bind ever fails
part-way, the previous certificate is put back and stays in service.

Uploads land in `/var/lib/winelog/tls` — the data directory, the one place the
service account can write — and **take precedence over `WINELOG_TLS_CERT`**, so
uploading always beats the certificate the installer generated. The same thing
from a shell:

```bash
sudo winelog tls                          # what's being served, and until when
sudo winelog tls fullchain.pem key.pem    # install; restart to pick it up
sudo winelog tls --remove                 # drop back to winelog.env, or to HTTP
```

**Name the certificate for every address you'll use.** A browser checks the
address bar against the certificate's SANs, so a certificate issued only for
`winelog.home.lan` fails the moment someone types the IP — which phones tend to
do when DNS doesn't reach them. Ask your CA for all of them at once:

```bash
openssl req -newkey rsa:2048 -nodes -subj "/CN=winelog.home.lan" \
  -keyout winelog.key -out winelog.csr
openssl x509 -req -in winelog.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -days 397 -out winelog.crt -extfile <(printf \
  "subjectAltName=DNS:winelog.home.lan,DNS:winelog,IP:192.168.1.50")
```

The upload screen refuses a certificate that doesn't name the address you're
browsing from, and tells you which names it does carry, so this is caught
before it can lock you out rather than after.

Upload one while the app is on plain HTTP and it's stored, then served after
`sudo systemctl restart winelog` — on whatever port the app is already on. Use
`--https` to move it to 443. Because an uploaded certificate outranks
`winelog.env`, re-running the installer *without* `--https` won't turn HTTPS
back off; the installer says so and points at `winelog tls --remove`.

If nginx is terminating TLS in front, put the certificate in nginx's config
instead — the Settings screen says so rather than pretending otherwise.

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
python -m app.serve            # honours the WINELOG_* variables below
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
app instead binds localhost and nginx serves 80 (443 with `--https`), allowing
private ranges and denying the rest (`deploy/nginx.conf.example`).

To change the port later, edit `/etc/winelog.env` and restart. Moving to or
from 443 means keeping three things in step — the port, the certificate, and
the firewall — so re-running the installer with the flags you want is usually
easier than editing by hand.

**Terminating TLS somewhere else** (nginx here, or a load balancer): the app
sees plain HTTP and can't tell, so set `WINELOG_COOKIE_SECURE=true` yourself.
Leave it alone on a plain-HTTP deployment or browsers will drop the session
cookie and no one will stay signed in. The app warns at startup if these two
look mismatched.

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
export, password change, and the HTTPS certificate: what's being served, when it
expires, and a place to upload the one your CA issued.

## Configuration

All optional; defaults in brackets.

| Variable | Purpose |
|---|---|
| `WINELOG_HOST` | Address to bind [`127.0.0.1`] |
| `WINELOG_PORT` | Port to bind, 80 and 443 included [`8071`] |
| `WINELOG_TLS_CERT` | Certificate to serve HTTPS with [none — plain HTTP] |
| `WINELOG_TLS_KEY` | Its private key (an uploaded certificate wins over both) |
| `WINELOG_HTTP_REDIRECT_PORT` | Also answer HTTP here and redirect to HTTPS [`0`] |
| `WINELOG_DATA_DIR` | Database + uploads directory [`./data`] |
| `WINELOG_DB` | Database path [`$WINELOG_DATA_DIR/winelog.db`] |
| `WINELOG_UPLOAD_DIR` | Stored receipt PDFs [`$WINELOG_DATA_DIR/receipts`] |
| `WINELOG_COOKIE_SECURE` | Mark the session cookie `Secure` [on when TLS is configured] |
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
tls [<cert> [<key>]]      show the HTTPS certificate, or install one
tls --remove              delete the uploaded certificate
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
* An uploaded TLS private key is written `0600` and never leaves the server —
  the certificate details are readable in the app, the key is not
* No external requests: no CDNs, no fonts, no analytics. The UI is plain HTML,
  CSS, and JavaScript with no build step.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

110 tests: parser (page breaks, Gmail chrome, non-founders discounts, malformed
line maths), API (auth, breakeven arithmetic, receipt de-duplication, search,
insights, CSV export), serving (TLS arguments, certificate checks, the
HTTP→HTTPS redirect, cookie policy), certificates (validation, hostname
matching, rollback, and a real handshake proving the live rebind), and the
installer's shell helpers under `set -euo pipefail`.

## Layout

```
app/
  main.py             FastAPI routes
  serve.py            entry point: port, TLS, HTTP→HTTPS redirect
  tls.py              certificate upload, validation and live rebinding
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
