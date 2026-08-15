#!/usr/bin/env bash
#
# WineLog installer for Ubuntu.
#
#   git clone https://github.com/sudds65/WineLog.git
#   cd WineLog
#   sudo ./deploy/install.sh
#
# Re-running it is safe: it updates the code in place, reinstalls dependencies
# and restarts the service without touching your database.
#
# Options:
#   --admin NAME     create this login (prompts for the password)
#   --port N         port to serve on               [8071, or 443 with --https]
#   --host ADDR      address to bind                [0.0.0.0]
#   --https          serve HTTPS, self-signed certificate unless --tls-cert
#   --tls-cert FILE  use this certificate instead of a generated one
#   --tls-key FILE   the matching private key
#   --with-nginx     put nginx in front of it, on 80 (or 443 with --https)
#   --domain NAME    server_name and certificate name [the machine's hostname]
#   --no-seed        skip loading the sample receipt
#
# Ports 80 and 443 work without running the app as root: the systemd unit
# grants CAP_NET_BIND_SERVICE to the unprivileged winelog account.
#
set -euo pipefail

APP_DIR=/opt/winelog
DATA_DIR=/var/lib/winelog
ENV_FILE=/etc/winelog.env
TLS_DIR=/etc/winelog/tls
SERVICE_USER=winelog

ADMIN_USER=""
PORT=""
HOST=0.0.0.0
WITH_NGINX=0
DOMAIN=""
SEED=1
TLS=0
TLS_CERT=""
TLS_KEY=""
REDIRECT_PORT=0

BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; OFF=$'\033[0m'
say()  { printf '%s\n' "${BOLD}==>${OFF} $*"; }
warn() { printf '%s\n' "${YELLOW}!${OFF}   $*"; }
die()  { printf '%s\n' "${RED}✗${OFF}   $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --admin)      ADMIN_USER="${2:-}"; shift 2 ;;
    --port)       PORT="${2:-}"; shift 2 ;;
    --host)       HOST="${2:-}"; shift 2 ;;
    --domain)     DOMAIN="${2:-}"; shift 2 ;;
    --https)      TLS=1; shift ;;
    --tls-cert)   TLS_CERT="${2:-}"; TLS=1; shift 2 ;;
    --tls-key)    TLS_KEY="${2:-}"; TLS=1; shift 2 ;;
    --with-nginx) WITH_NGINX=1; shift ;;
    --no-seed)    SEED=0; shift ;;
    -h|--help)    awk 'NR>2 && /^#/ {sub(/^# ?/, ""); print; next} NR>2 {exit}' "$0"; exit 0 ;;
    *)            die "unknown option: $1" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "run with sudo: sudo ./deploy/install.sh"

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "$SOURCE_DIR/manage.py" ]] || die "run this from inside the cloned repo"

if { [[ -n "$TLS_CERT" ]] && [[ -z "$TLS_KEY" ]]; } || \
   { [[ -z "$TLS_CERT" ]] && [[ -n "$TLS_KEY" ]]; }; then
  die "--tls-cert and --tls-key go together"
fi

# Asking for 443 is asking for HTTPS; nothing useful listens there in the clear.
if [[ "$PORT" == "443" ]]; then TLS=1; fi

# ── where things listen ──────────────────────────────────────────────────
# Without nginx the app binds the public port itself (443 by default under
# --https). With nginx the app stays on localhost and nginx takes 80/443.
if [[ $WITH_NGINX -eq 1 ]]; then
  HOST=127.0.0.1
  [[ -n "$PORT" ]] || PORT=8071
  EXPOSED_PORT=$([[ $TLS -eq 1 ]] && echo 443 || echo 80)
  if [[ "$PORT" == "$EXPOSED_PORT" ]]; then
    die "with --with-nginx, --port is the port nginx forwards to, so it can't be $EXPOSED_PORT — the port nginx itself listens on. Leave --port off, or use something like 8071."
  fi
else
  [[ -n "$PORT" ]] || PORT=$([[ $TLS -eq 1 ]] && echo 443 || echo 8071)
  EXPOSED_PORT="$PORT"
  # Someone typing just the hostname arrives on 80; bounce them to HTTPS.
  if [[ $TLS -eq 1 && "$PORT" == "443" ]]; then REDIRECT_PORT=80; fi
fi

[[ "$PORT" =~ ^[0-9]+$ && "$PORT" -ge 1 && "$PORT" -le 65535 ]] \
  || die "--port must be a number from 1 to 65535, got '$PORT'"

# ── packages ─────────────────────────────────────────────────────────────
command -v apt-get >/dev/null 2>&1 \
  || die "this installer expects Ubuntu/Debian (no apt-get found)"

OS_NAME="$(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-unknown}")"
say "Installing system packages on ${OS_NAME}"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip rsync iproute2 >/dev/null
if [[ $TLS -eq 1 ]]; then apt-get install -y -qq openssl >/dev/null; fi
if [[ $WITH_NGINX -eq 1 ]]; then apt-get install -y -qq nginx >/dev/null; fi

# ── service account ──────────────────────────────────────────────────────
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  say "Creating the $SERVICE_USER service account"
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# ── code ─────────────────────────────────────────────────────────────────
say "Installing the app into $APP_DIR"
mkdir -p "$APP_DIR" "$DATA_DIR"
if [[ "$SOURCE_DIR" != "$APP_DIR" ]]; then
  rsync -a --delete \
        --exclude '.git' --exclude '.venv' --exclude 'data' \
        --exclude '__pycache__' --exclude '.pytest_cache' \
        "$SOURCE_DIR"/ "$APP_DIR"/
fi

say "Setting up the Python environment (Python $(python3 -V 2>&1 | awk '{print $2}'))"
[[ -d "$APP_DIR/.venv" ]] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# Prove the dependencies actually import. On a very new Ubuntu the Python may
# be ahead of the published wheels, in which case pip "succeeds" but the PDF
# stack is unusable — better to find out now than on the first upload.
check_deps() { "$APP_DIR/.venv/bin/python" -c \
  'import fastapi, uvicorn, pdfplumber, multipart' >/dev/null 2>&1; }

if ! check_deps; then
  warn "A dependency did not import — retrying with build tools installed"
  apt-get install -y -qq build-essential python3-dev >/dev/null
  "$APP_DIR/.venv/bin/pip" install --force-reinstall -r "$APP_DIR/requirements.txt"
  check_deps || die "dependencies could not be installed — see the output above"
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR" "$DATA_DIR"
chmod 750 "$DATA_DIR"

# ── TLS certificate ──────────────────────────────────────────────────────
[[ -n "$DOMAIN" ]] || DOMAIN="$(hostname -f 2>/dev/null || hostname)"
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

if [[ $TLS -eq 1 && -z "$TLS_CERT" ]]; then
  TLS_CERT="$TLS_DIR/winelog.crt"
  TLS_KEY="$TLS_DIR/winelog.key"
  if [[ -f "$TLS_CERT" && -f "$TLS_KEY" ]]; then
    say "Using the certificate already in $TLS_DIR"
  else
    say "Generating a self-signed certificate for $DOMAIN"
    mkdir -p "$TLS_DIR"
    ALT="DNS:$DOMAIN,DNS:localhost,IP:127.0.0.1"
    if [[ -n "$LAN_IP" ]]; then ALT="$ALT,IP:$LAN_IP"; fi
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
      -subj "/CN=$DOMAIN" -addext "subjectAltName=$ALT" \
      -keyout "$TLS_KEY" -out "$TLS_CERT" >/dev/null 2>&1 \
      || die "could not generate a certificate — see: openssl req --help"
    warn "It is self-signed, so the browser will warn once per device. Accept"
    warn "it, or drop your own certificate in with --tls-cert/--tls-key."
  fi
fi

if [[ $TLS -eq 1 ]]; then
  [[ -f "$TLS_CERT" ]] || die "no such certificate: $TLS_CERT"
  [[ -f "$TLS_KEY"  ]] || die "no such private key: $TLS_KEY"
  # nginx reads the key as root; the app reads it as the service account.
  chgrp "$SERVICE_USER" "$TLS_CERT" "$TLS_KEY" 2>/dev/null || true
  chmod 0640 "$TLS_KEY"
  chmod 0644 "$TLS_CERT"
fi

# ── configuration ────────────────────────────────────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
  say "Writing $ENV_FILE"
  install -m 0644 "$APP_DIR/deploy/winelog.env.example" "$ENV_FILE"
fi

# Update a setting in place, or append it if this env file predates it.
set_env() {
  if grep -q "^$1=" "$ENV_FILE"; then
    sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"
  fi
}

# Keep the flags and the env file in step on every run.
set_env WINELOG_HOST "$HOST"
set_env WINELOG_PORT "$PORT"
set_env WINELOG_DATA_DIR "$DATA_DIR"

if [[ $TLS -eq 1 && $WITH_NGINX -eq 0 ]]; then
  set_env WINELOG_TLS_CERT "$TLS_CERT"
  set_env WINELOG_TLS_KEY "$TLS_KEY"
  set_env WINELOG_HTTP_REDIRECT_PORT "$REDIRECT_PORT"
  set_env WINELOG_COOKIE_SECURE true
else
  # nginx terminates TLS, so the app speaks plain HTTP on localhost — but the
  # browser still sees HTTPS, so the cookie must be marked Secure.
  set_env WINELOG_TLS_CERT ""
  set_env WINELOG_TLS_KEY ""
  set_env WINELOG_HTTP_REDIRECT_PORT 0
  set_env WINELOG_COOKIE_SECURE "$([[ $TLS -eq 1 ]] && echo true || echo false)"
fi

# ── database ─────────────────────────────────────────────────────────────
run_manage() { sudo -u "$SERVICE_USER" env WINELOG_DATA_DIR="$DATA_DIR" \
                 "$APP_DIR/.venv/bin/python" "$APP_DIR/manage.py" "$@"; }

say "Preparing the database"
run_manage init >/dev/null

if [[ $SEED -eq 1 ]]; then
  run_manage seed >/dev/null 2>&1 && say "Loaded the sample receipt" || true
fi

# ── first login ──────────────────────────────────────────────────────────
if [[ -n "$ADMIN_USER" ]]; then
  if run_manage list-users | grep -qi "^$ADMIN_USER "; then
    warn "User '$ADMIN_USER' already exists, leaving the password alone"
  else
    say "Creating the login '$ADMIN_USER'"
    for attempt in 1 2 3; do
      read -rsp "  Password (10+ chars): " pw1; echo
      read -rsp "  Confirm password:     " pw2; echo
      if [[ "$pw1" != "$pw2" ]]; then warn "They don't match, try again"; continue; fi
      if [[ ${#pw1} -lt 10 ]]; then warn "Too short, try again"; continue; fi
      run_manage create-user "$ADMIN_USER" --password "$pw1" && break
    done
    unset pw1 pw2
  fi
fi

# ── winelog command ──────────────────────────────────────────────────────
say "Installing the 'winelog' command"
cat > /usr/local/bin/winelog <<WRAPPER
#!/usr/bin/env bash
# Runs the WineLog admin CLI as the service account.
# Installed by deploy/install.sh — edit there, not here.
set -euo pipefail
if [[ \$EUID -ne 0 ]]; then exec sudo "\$0" "\$@"; fi
exec sudo -u $SERVICE_USER env WINELOG_DATA_DIR=$DATA_DIR WINELOG_CLI=winelog \\
     $APP_DIR/.venv/bin/python $APP_DIR/manage.py "\$@"
WRAPPER
chmod 0755 /usr/local/bin/winelog

# ── service ──────────────────────────────────────────────────────────────
# Free our own ports before checking who holds them, so a re-run doesn't
# report itself as the conflict.
systemctl stop winelog >/dev/null 2>&1 || true

port_holder() {  # the program listening on a TCP port, if any
  ss -ltnpH "sport = :$1" 2>/dev/null | grep -oP 'users:\(\("\K[^"]+' | head -1
}

CHECK_PORTS="$PORT"
if [[ "$EXPOSED_PORT" != "$PORT" ]]; then CHECK_PORTS="$CHECK_PORTS $EXPOSED_PORT"; fi
if [[ $REDIRECT_PORT -gt 0 ]];   then CHECK_PORTS="$CHECK_PORTS $REDIRECT_PORT"; fi

for check in $CHECK_PORTS; do
  holder="$(port_holder "$check")"
  # nginx holding nginx's own ports is the arrangement we are installing.
  if [[ -n "$holder" ]] && ! { [[ $WITH_NGINX -eq 1 ]] && [[ "$holder" == "nginx" ]]; }; then
    die "port $check is already in use by '$holder' — stop it (sudo systemctl stop $holder) or pick another with --port N"
  fi
done

say "Installing the systemd service"
install -m 0644 "$APP_DIR/deploy/winelog.service" /etc/systemd/system/winelog.service
systemctl daemon-reload
systemctl enable --quiet winelog
systemctl restart winelog

sleep 2
systemctl is-active --quiet winelog \
  || die "the service did not start — check: journalctl -u winelog -n 40"

# ── nginx ────────────────────────────────────────────────────────────────
if [[ $WITH_NGINX -eq 1 ]]; then
  say "Configuring nginx on port $EXPOSED_PORT"

  # Mirrors deploy/nginx.conf.example; edit both if you change one.
  {
    if [[ $TLS -eq 1 ]]; then
      cat <<TLSHEAD
server {
    listen 443 ssl;
    http2 on;
    server_name $DOMAIN;

    ssl_certificate     $TLS_CERT;
    ssl_certificate_key $TLS_KEY;
    ssl_protocols TLSv1.2 TLSv1.3;
TLSHEAD
    else
      cat <<PLAINHEAD
server {
    listen 80;
    server_name $DOMAIN;
PLAINHEAD
    fi

    cat <<BODY

    # Only the VPN and LAN ranges get in. Adjust to your subnets.
    allow 10.0.0.0/8;
    allow 172.16.0.0/12;
    allow 192.168.0.0/16;
    deny  all;

    client_max_body_size 12m;

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
    }
}
BODY

    if [[ $TLS -eq 1 ]]; then
      cat <<REDIRECT

server {
    listen 80;
    server_name $DOMAIN;
    return 301 https://\$host\$request_uri;
}
REDIRECT
    fi
  } > /etc/nginx/sites-available/winelog

  ln -sf /etc/nginx/sites-available/winelog /etc/nginx/sites-enabled/winelog
  rm -f /etc/nginx/sites-enabled/default
  nginx -t >/dev/null 2>&1 && systemctl reload nginx \
    || warn "nginx config test failed — check: nginx -t"
fi

# ── firewall ─────────────────────────────────────────────────────────────
if command -v ufw >/dev/null 2>&1; then
  say "Adding firewall rules for the private network"
  ufw allow OpenSSH >/dev/null 2>&1 || true
  OPEN_PORTS="$EXPOSED_PORT"
  # The HTTP→HTTPS bounce only helps if 80 is reachable too.
  if [[ $TLS -eq 1 && "$EXPOSED_PORT" == "443" ]]; then OPEN_PORTS="$OPEN_PORTS 80"; fi
  for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16; do
    for p in $OPEN_PORTS; do
      ufw allow from "$net" to any port "$p" proto tcp >/dev/null 2>&1 || true
    done
  done
  if ! ufw status | grep -q "^Status: active"; then
    warn "ufw is installed but inactive. SSH is already allowed, so you can turn"
    warn "it on safely with:  sudo ufw enable"
  fi
else
  warn "ufw is not installed. Port $EXPOSED_PORT is open to anything that can"
  warn "reach this machine — make sure that's only your VPN/LAN."
fi

# ── done ─────────────────────────────────────────────────────────────────
[[ -n "$LAN_IP" ]] || LAN_IP="this-server"
SCHEME=$([[ $TLS -eq 1 ]] && echo https || echo http)
HOSTPART=$([[ $WITH_NGINX -eq 1 ]] && echo "$DOMAIN" || echo "$LAN_IP")

# 80 and 443 are implied by the scheme, so leave them off the printed URL.
if [[ "$SCHEME" == "https" && "$EXPOSED_PORT" == "443" ]] || \
   [[ "$SCHEME" == "http"  && "$EXPOSED_PORT" == "80"  ]]; then
  URL="$SCHEME://$HOSTPART"
else
  URL="$SCHEME://$HOSTPART:$EXPOSED_PORT"
fi

echo
printf '%s\n' "${GREEN}${BOLD}WineLog is running.${OFF}"
echo
echo "  Open         $URL"
if [[ $TLS -eq 1 && "$EXPOSED_PORT" == "443" ]]; then
  echo "               (http://$HOSTPART redirects here)"
fi
if [[ -z "$ADMIN_USER" ]]; then
  echo "  Add a login  sudo winelog create-user <name>"
fi
echo "  Import PDFs  sudo winelog import ~/receipts/*.pdf"
echo "  Settings     sudo winelog config"
echo "  Totals       sudo winelog stats"
echo "  Logs         journalctl -u winelog -f"
echo "  Restart      sudo systemctl restart winelog"
echo
