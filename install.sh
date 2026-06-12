#!/usr/bin/env bash
# Farm Netwatch one-line installer for Raspberry Pi (3 and newer) / any Debian host.
#
#   curl -fsSL https://raw.githubusercontent.com/Riaan007/farm-netwatch/main/install.sh | sudo bash
#
# By default this also sets up the WireGuard CLIENT that links the site back to the
# central hub (see hub/). Supply the per-site config from the hub's wg-easy UI:
#
#   # download it from wg-easy, then either:
#   WG_CONF_B64="$(base64 -w0 site.conf)" sudo -E bash -c \
#     'curl -fsSL https://raw.githubusercontent.com/Riaan007/farm-netwatch/main/install.sh | bash'
#   # or host it somewhere and pass WG_CONF_URL=...  / or WG_CONF=/path/to/site.conf
#
# Opt out of the hub VPN with HUB_VPN=0. Add Kuma with COMPOSE_PROFILES=kuma.
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/Riaan007/farm-netwatch/main"
IMAGE="${NETWATCH_IMAGE:-ghcr.io/riaan007/farm-netwatch:latest}"
DIR="${NETWATCH_DIR:-/opt/netwatch}"
PORT="${NETWATCH_PORT:-8090}"
PROFILES="${COMPOSE_PROFILES:-}"

# --- Central-hub VPN (on by default) ----------------------------------------
HUB_VPN="${HUB_VPN:-1}"          # 1 = link this site to the hub over WireGuard
WG_CONF_URL="${WG_CONF_URL:-}"   # URL to the per-site wg0 config
WG_CONF_B64="${WG_CONF_B64:-}"   # base64 of the per-site wg0 config
WG_CONF="${WG_CONF:-}"           # path to a local per-site wg0 config

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*"; }
err() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || err "Please run with sudo/root."

# --- Docker -----------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  say "Installing Docker (get.docker.com)…"
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker || true
fi
if ! docker compose version >/dev/null 2>&1; then
  err "Docker Compose v2 plugin not found. Update Docker, then re-run."
fi

# --- Files ------------------------------------------------------------------
say "Installing to $DIR"
mkdir -p "$DIR/data"
curl -fsSL "$REPO_RAW/docker-compose.yml" -o "$DIR/docker-compose.yml"

# .env pins the image so `docker compose` pulls the prebuilt multi-arch image
# instead of building from source. TZ defaults to the Pi's own timezone.
TZONE="${TZ:-$(cat /etc/timezone 2>/dev/null || echo UTC)}"
cat > "$DIR/.env" <<EOF
NETWATCH_IMAGE=$IMAGE
NETWATCH_PORT=$PORT
TZ=$TZONE
${WG_HOST:+WG_HOST=$WG_HOST}
EOF

# Strip the local "build:" block so a Pi without the source never builds.
sed -i '/^    build:/,/^      context: \./d' "$DIR/docker-compose.yml" || true

# --- Central-hub WireGuard client -------------------------------------------
HUB_READY=0
if [ "$HUB_VPN" = "1" ]; then
  WG_DIR="$DIR/data/wg-client/wg_confs"
  TARGET="$WG_DIR/wg0.conf"
  mkdir -p "$WG_DIR"
  if [ -n "$WG_CONF_B64" ]; then
    echo "$WG_CONF_B64" | base64 -d > "$TARGET" && say "Hub VPN config installed (WG_CONF_B64)."
  elif [ -n "$WG_CONF_URL" ]; then
    curl -fsSL "$WG_CONF_URL" -o "$TARGET" && say "Hub VPN config downloaded."
  elif [ -n "$WG_CONF" ] && [ -f "$WG_CONF" ]; then
    cp "$WG_CONF" "$TARGET" && say "Hub VPN config copied from $WG_CONF."
  elif [ -f "$TARGET" ]; then
    say "Existing hub VPN config kept."
  fi
  if [ -s "$TARGET" ]; then
    chmod 600 "$TARGET"
    case ",$PROFILES," in *,wg-client,*) ;; *) PROFILES="${PROFILES:+$PROFILES,}wg-client";; esac
    HUB_READY=1
  fi
fi

# --- Up ---------------------------------------------------------------------
cd "$DIR"
say "Pulling images…"
COMPOSE_PROFILES="$PROFILES" docker compose pull
say "Starting…"
COMPOSE_PROFILES="$PROFILES" docker compose up -d

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
say "Farm Netwatch is running."
echo "   Setup wizard:  http://${IP:-<pi-ip>}:$PORT/setup"
echo "   Dashboard:     http://${IP:-<pi-ip>}:$PORT/"
echo

if [ "$HUB_VPN" = "1" ] && [ "$HUB_READY" = "1" ]; then
  sleep 3
  VPNIP=$(ip -4 -o addr show wg0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 || true)
  say "Linked to the central hub over WireGuard."
  echo "   This site's hub (VPN) address: ${VPNIP:-<bringing up… check: ip addr show wg0>}"
  echo "   On the HUB, register this site by that 10.8.0.x address (port $PORT)."
elif [ "$HUB_VPN" = "1" ]; then
  warn "Hub VPN is set up but NO config was supplied yet — this site is not linked."
  echo "   Finish linking it to the central hub:"
  echo "     1. On the hub's wg-easy UI, create a client for this site and download its .conf"
  echo "        (it must have AllowedIPs = 10.8.0.0/24 and PersistentKeepalive = 25)."
  echo "     2. Save it as:  $DIR/data/wg-client/wg_confs/wg0.conf"
  echo "     3. cd $DIR && docker compose --profile wg-client up -d"
  echo "   (Or re-run this installer with WG_CONF_B64=… / WG_CONF_URL=… set.)"
fi
echo
echo "   Update later:  cd $DIR && docker compose pull && docker compose up -d"
