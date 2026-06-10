#!/usr/bin/env bash
# Farm Netwatch one-line installer for Raspberry Pi (3 and newer) / any Debian host.
#
#   curl -fsSL https://raw.githubusercontent.com/Riaan007/farm-netwatch/main/install.sh | sudo bash
#
# Re-running updates to the latest image. Override before piping, e.g.:
#   COMPOSE_PROFILES=tailscale  sudo -E bash install.sh
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/Riaan007/farm-netwatch/main"
IMAGE="${NETWATCH_IMAGE:-ghcr.io/riaan007/farm-netwatch:latest}"
DIR="${NETWATCH_DIR:-/opt/netwatch}"
PORT="${NETWATCH_PORT:-8090}"
PROFILES="${COMPOSE_PROFILES:-}"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
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
# instead of trying to build from source on the Pi. TZ defaults to the Pi's own
# timezone so the dashboard's server-side times match the wall clock.
TZONE="${TZ:-$(cat /etc/timezone 2>/dev/null || echo UTC)}"
cat > "$DIR/.env" <<EOF
NETWATCH_IMAGE=$IMAGE
NETWATCH_PORT=$PORT
TZ=$TZONE
${WG_HOST:+WG_HOST=$WG_HOST}
EOF

# Strip the local "build:" block when running from the prebuilt image so a Pi
# without the source tree never attempts a local build.
sed -i '/^    build:/,/^      context: \./d' "$DIR/docker-compose.yml" || true

# --- Up ---------------------------------------------------------------------
cd "$DIR"
say "Pulling image…"
COMPOSE_PROFILES="$PROFILES" docker compose pull
say "Starting…"
COMPOSE_PROFILES="$PROFILES" docker compose up -d

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
say "Farm Netwatch is running."
echo "   Open the setup wizard:  http://${IP:-<pi-ip>}:$PORT/setup"
echo "   Dashboard:              http://${IP:-<pi-ip>}:$PORT/"
[ -n "$PROFILES" ] && echo "   VPN profile enabled:    $PROFILES"
echo
echo "   Update later:  cd $DIR && docker compose pull && docker compose up -d"
