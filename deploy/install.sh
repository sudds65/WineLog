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
#   --port N         port to serve on               [8071]
#   --host ADDR      address to bind                [0.0.0.0]
#   --with-nginx     also put nginx on port 80 in front of it
#   --domain NAME    server_name for nginx          [the machine's hostname]
#   --no-seed        skip loading the sample receipt
#
set -euo pipefail

APP_DIR=/opt/winelog
DATA_DIR=/var/lib/winelog
ENV_FILE=/etc/winelog.env
SERVICE_USER=winelog

ADMIN_USER=""
PORT=8071
HOST=0.0.0.0
WITH_NGINX=0
DOMAIN=""
SEED=1

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
    --with-nginx) WITH_NGINX=1; shift ;;
    --no-seed)    SEED=0; shift ;;
    -h|--help)    sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)            die "unknown option: $1" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "run with sudo: sudo ./deploy/install.sh"

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "$SOURCE_DIR/manage.py" ]] || die "run this from inside the cloned repo"

# nginx terminates on :80, so the app itself only needs to listen locally.
if [[ $WITH_NGINX -eq 1 ]]; then HOST=127.0.0.1; fi

# ── packages ─────────────────────────────────────────────────────────────
command -v apt-get >/dev/null 2>&1 \
  || die "this installer expects Ubuntu/Debian (no apt-get found)"

OS_NAME="$(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-unknown}")"
say "Installing system packages on ${OS_NAME}"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip rsync >/dev/null
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

# ── configuration ────────────────────────────────────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
  say "Writing $ENV_FILE"
  install -m 0644 "$APP_DIR/deploy/winelog.env.example" "$ENV_FILE"
fi
# Keep host/port in step with the flags on every run.
sed -i "s|^WINELOG_HOST=.*|WINELOG_HOST=$HOST|;
        s|^WINELOG_PORT=.*|WINELOG_PORT=$PORT|;
        s|^WINELOG_DATA_DIR=.*|WINELOG_DATA_DIR=$DATA_DIR|" "$ENV_FILE"

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
  say "Configuring nginx"
  [[ -n "$DOMAIN" ]] || DOMAIN="$(hostname -f 2>/dev/null || hostname)"
  sed -e "s|winelog.home.lan|$DOMAIN|" \
      -e "s|proxy_pass http://127.0.0.1:8071;|proxy_pass http://127.0.0.1:$PORT;|" \
      "$APP_DIR/deploy/nginx.conf.example" > /etc/nginx/sites-available/winelog
  ln -sf /etc/nginx/sites-available/winelog /etc/nginx/sites-enabled/winelog
  rm -f /etc/nginx/sites-enabled/default
  nginx -t >/dev/null 2>&1 && systemctl reload nginx \
    || warn "nginx config test failed — check: nginx -t"
fi

# ── firewall ─────────────────────────────────────────────────────────────
EXPOSED_PORT=$([[ $WITH_NGINX -eq 1 ]] && echo 80 || echo "$PORT")
if command -v ufw >/dev/null 2>&1; then
  say "Adding firewall rules for the private network"
  ufw allow OpenSSH >/dev/null 2>&1 || true
  for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16; do
    ufw allow from "$net" to any port "$EXPOSED_PORT" proto tcp >/dev/null 2>&1 || true
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
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[[ -n "$LAN_IP" ]] || LAN_IP="this-server"
URL=$([[ $WITH_NGINX -eq 1 ]] && echo "http://$DOMAIN" || echo "http://$LAN_IP:$PORT")

echo
printf '%s\n' "${GREEN}${BOLD}WineLog is running.${OFF}"
echo
echo "  Open         $URL"
if [[ -z "$ADMIN_USER" ]]; then
  echo "  Add a login  sudo winelog create-user <name>"
fi
echo "  Import PDFs  sudo winelog import ~/receipts/*.pdf"
echo "  Settings     sudo winelog config"
echo "  Totals       sudo winelog stats"
echo "  Logs         journalctl -u winelog -f"
echo "  Restart      sudo systemctl restart winelog"
echo
