#!/usr/bin/env bash
# Farm Netwatch — guided installer / setup wizard for a farm SITE Raspberry Pi.
#
# It (1) PREFLIGHTS the Pi, (2) installs any missing prerequisites (Docker,
# Compose, the WireGuard kernel module), (3) pulls Netwatch + Uptime Kuma and
# links the central-hub VPN, and (4) brings the stack up and hands off to the
# dashboard setup wizard. A fresh Pi goes from bare to fully-joined in one run.
#
#   curl -fsSL https://raw.githubusercontent.com/Riaan007/farm-netwatch/main/install.sh | sudo bash
#   sudo bash install.sh --check          # preflight only — changes nothing
#
# The hub's "Add site" wizard generates a one-paste version of this with the VPN
# config embedded (WG_CONF_B64=… ASSUME_YES=1 curl … | sudo -E bash).
#
# Env / flags:  --check  --yes/-y  --no-kuma  --no-vpn
#   HUB_VPN=0            skip the central-hub VPN client
#   NO_KUMA=1            skip Uptime Kuma
#   WG_CONF_B64 | WG_CONF_URL | WG_CONF   per-site WireGuard config
#   NETWATCH_DIR (/opt/netwatch)  NETWATCH_PORT (8090)  KUMA_PORT (3001)
set -euo pipefail

REPO_RAW="${NETWATCH_REPO_RAW:-https://raw.githubusercontent.com/Riaan007/farm-netwatch/main}"
IMAGE="${NETWATCH_IMAGE:-ghcr.io/riaan007/farm-netwatch:latest}"
DIR="${NETWATCH_DIR:-/opt/netwatch}"
PORT="${NETWATCH_PORT:-8090}"
KUMA_PORT="${KUMA_PORT:-3001}"
PROFILES="${COMPOSE_PROFILES:-}"
HUB_VPN="${HUB_VPN:-1}"
NO_KUMA="${NO_KUMA:-0}"
ASSUME_YES="${ASSUME_YES:-0}"
CHECK_ONLY=0
WG_CONF_URL="${WG_CONF_URL:-}"; WG_CONF_B64="${WG_CONF_B64:-}"; WG_CONF="${WG_CONF:-}"

for a in "${@:-}"; do
  case "$a" in
    --check) CHECK_ONLY=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    --no-kuma) NO_KUMA=1 ;;
    --no-vpn) HUB_VPN=0 ;;
    -h|--help) sed -n '2,20p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
  esac
done

# ---- pretty output ---------------------------------------------------------
B=$'\033[1m'; R=$'\033[0m'; GRN=$'\033[1;32m'; YEL=$'\033[1;33m'; RED=$'\033[1;31m'; BLU=$'\033[1;34m'
hdr()  { printf '\n%s== %s ==%s\n' "$BLU" "$*" "$R"; }
say()  { printf '%s==>%s %s\n' "$BLU" "$R" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$GRN" "$R" "$*"; }
warn() { printf '  %s!%s %s\n' "$YEL" "$R" "$*"; WARNS=$((WARNS+1)); }
bad()  { printf '  %s✗%s %s\n' "$RED" "$R" "$*"; FAILS=$((FAILS+1)); }
die()  { printf '%sERROR:%s %s\n' "$RED" "$R" "$*" >&2; exit 1; }
WARNS=0; FAILS=0

# Prompt yes/no. Uses /dev/tty so it still works when the script is piped from
# curl, as long as a terminal is attached. Unattended (no tty / --yes) = default.
ask() { # $1 question  $2 default(Y|N) -> echoes y|n
  local def="${2:-Y}" ans
  if [ "$ASSUME_YES" = 1 ] || [ ! -e /dev/tty ]; then
    [ "${def^^}" = "Y" ] && echo y || echo n; return
  fi
  read -rp "$1 [$( [ "${def^^}" = Y ] && echo 'Y/n' || echo 'y/N')] " ans </dev/tty || ans=""
  ans="${ans:-$def}"
  case "${ans^^}" in Y*) echo y ;; *) echo n ;; esac
}

NEED_ROOT_MSG="re-run with sudo"
[ "$CHECK_ONLY" = 1 ] || [ "$(id -u)" = 0 ] || die "must run as root ($NEED_ROOT_MSG)."

# ============================================================================
hdr "1/5  Preflight — can this Pi run Netwatch?"
APT=0; command -v apt-get >/dev/null 2>&1 && APT=1
WANT_KUMA=1; [ "$NO_KUMA" = 1 ] && WANT_KUMA=0

# OS
if [ -r /etc/os-release ]; then . /etc/os-release; fi
case " ${ID:-} ${ID_LIKE:-} " in
  *debian*|*ubuntu*|*raspbian*) ok "OS: ${PRETTY_NAME:-Debian-based}" ;;
  *) [ "$APT" = 1 ] && warn "OS ${PRETTY_NAME:-unknown}: not Debian-family but apt present" \
                    || warn "OS ${PRETTY_NAME:-unknown}: no apt — only Docker's own installer will work" ;;
esac

# Architecture
case "$(uname -m)" in
  aarch64|arm64)  ok "Arch: arm64 (Pi 3/4/5 64-bit)";  ARCH=arm64 ;;
  armv7l)         ok "Arch: armv7 (Pi 3 32-bit)";       ARCH=armv7 ;;
  x86_64|amd64)   ok "Arch: amd64";                     ARCH=amd64 ;;
  armv6l)         bad "Arch: armv6 (Pi 1 / Zero) — Docker images are not built for it"; ARCH=armv6 ;;
  *)              bad "Arch: $(uname -m) — unsupported (need arm64/armv7/amd64)"; ARCH=other ;;
esac

# WireGuard kernel support (needed for the hub VPN client)
WG_OK=0
if [ -d /sys/module/wireguard ] || modprobe -n wireguard >/dev/null 2>&1 || modinfo wireguard >/dev/null 2>&1; then
  WG_OK=1; ok "WireGuard: kernel module available"
elif [ "$HUB_VPN" != 1 ]; then
  ok "WireGuard: skipped (hub VPN disabled)"
elif [ "$APT" = 1 ]; then
  warn "WireGuard: module missing — will install the 'wireguard' package"
else
  bad "WireGuard: module missing and no apt to install it (set HUB_VPN=0 to skip)"
fi

# RAM
MEM_MB=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)
if [ "$MEM_MB" -ge 900 ]; then ok "RAM: ${MEM_MB} MB"
elif [ "$MEM_MB" -ge 512 ]; then warn "RAM: ${MEM_MB} MB — fine, but tight with Uptime Kuma"
elif [ "$MEM_MB" -gt 0 ]; then
  warn "RAM: ${MEM_MB} MB — low; Uptime Kuma will be skipped"; WANT_KUMA=0
fi

# Disk (free space where $DIR will live, fall back to /)
DROOT="$DIR"; [ -d "$DROOT" ] || DROOT="$(dirname "$DIR")"; [ -d "$DROOT" ] || DROOT=/
FREE_GB=$(df -Pk "$DROOT" 2>/dev/null | awk 'NR==2{printf "%.1f", $4/1048576}')
if awk "BEGIN{exit !(${FREE_GB:-0} >= 2)}"; then ok "Disk: ${FREE_GB} GB free at $DROOT"
else warn "Disk: only ${FREE_GB:-?} GB free at $DROOT — images need ~1.5 GB"; fi

# Internet reachability — bash /dev/tcp so this works before curl is installed
NET_OK=0
RAWHOST=$(printf '%s' "$REPO_RAW" | sed -E 's#https?://([^/]+).*#\1#')
for hp in "ghcr.io:443" "get.docker.com:443" "${RAWHOST}:443"; do
  if timeout 6 bash -c "exec 3<>/dev/tcp/${hp%%:*}/${hp##*:}" 2>/dev/null; then NET_OK=1; else warn "unreachable: $hp"; fi
done
[ "$NET_OK" = 1 ] && ok "Internet: reachable" || bad "Internet: cannot reach ghcr.io / docker / github"

# Ports free
port_busy() { command -v ss >/dev/null 2>&1 && ss -ltnH 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$1\$"; }
if command -v ss >/dev/null 2>&1; then
  port_busy "$PORT" && warn "Port $PORT (dashboard) is in use — set NETWATCH_PORT" || ok "Port $PORT free"
  if [ "$WANT_KUMA" = 1 ]; then
    port_busy "$KUMA_PORT" && warn "Port $KUMA_PORT (Kuma) is in use — set KUMA_PORT" || ok "Port $KUMA_PORT free"
  fi
fi

printf '\n  summary: %s%d ok-with-warnings%s, %s%d blocking%s\n' "$YEL" "$WARNS" "$R" "$RED" "$FAILS" "$R"
if [ "$FAILS" -gt 0 ]; then
  echo "  Resolve the ✗ items above and re-run."
  [ "$CHECK_ONLY" = 1 ] && exit 1 || die "preflight failed."
fi
if [ "$CHECK_ONLY" = 1 ]; then say "Preflight only (--check): nothing was changed."; exit 0; fi
if [ "$WARNS" -gt 0 ] && [ "$(ask 'Warnings above. Continue anyway?' Y)" = n ]; then
  die "aborted by user."
fi

# ============================================================================
hdr "2/5  Installing prerequisites"
apt_done=0
apt_install() { [ "$APT" = 1 ] || die "need apt to install: $*"; [ "$apt_done" = 1 ] || { apt-get update -qq; apt_done=1; }; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" >/dev/null; }

command -v curl >/dev/null 2>&1 || { say "Installing curl…"; apt_install curl ca-certificates; }
ok "curl present"

if ! command -v docker >/dev/null 2>&1; then
  say "Installing Docker (get.docker.com)…"; curl -fsSL https://get.docker.com | sh; systemctl enable --now docker || true
fi
docker --version >/dev/null 2>&1 && ok "Docker $(docker --version | awk '{print $3}' | tr -d ,)" || die "Docker install failed."

if ! docker compose version >/dev/null 2>&1; then
  say "Installing the Docker Compose plugin…"; apt_install docker-compose-plugin || true
fi
docker compose version >/dev/null 2>&1 && ok "Compose $(docker compose version --short 2>/dev/null)" \
  || die "Docker Compose v2 not available — update Docker, then re-run."

if [ "$HUB_VPN" = 1 ] && [ "$WG_OK" != 1 ]; then
  say "Installing WireGuard…"; apt_install wireguard || apt_install wireguard-tools || true
  if modprobe wireguard 2>/dev/null || [ -d /sys/module/wireguard ]; then
    grep -qx wireguard /etc/modules 2>/dev/null || echo wireguard >> /etc/modules
    ok "WireGuard module loaded"
  else
    warn "WireGuard module still unavailable — the VPN client may not come up"
  fi
elif [ "$HUB_VPN" = 1 ]; then ok "WireGuard ready"; fi

# ============================================================================
hdr "3/5  Fetching app files & configuration"
mkdir -p "$DIR/data"
curl -fsSL "$REPO_RAW/docker-compose.yml" -o "$DIR/docker-compose.yml"
ok "docker-compose.yml → $DIR"
curl -fsSL "$REPO_RAW/docker-compose.health.yml" -o "$DIR/docker-compose.health.yml"
ok "docker-compose.health.yml → $DIR (health add-on)"
# Strip the local "build:" block so a Pi without the source never builds.
sed -i '/^    build:/,/^      context: \./d' "$DIR/docker-compose.yml" || true

# Decide active compose profiles (persisted in .env so every `up -d` keeps them)
[ "$WANT_KUMA" = 1 ] && case ",$PROFILES," in *,kuma,*) ;; *) PROFILES="${PROFILES:+$PROFILES,}kuma";; esac

# Place the per-site hub VPN config, if supplied
HUB_READY=0
if [ "$HUB_VPN" = 1 ]; then
  WGDIR="$DIR/data/wg-client/wg_confs"; TARGET="$WGDIR/wg0.conf"; mkdir -p "$WGDIR"
  if   [ -n "$WG_CONF_B64" ]; then echo "$WG_CONF_B64" | base64 -d > "$TARGET" && say "Hub VPN config installed (WG_CONF_B64)."
  elif [ -n "$WG_CONF_URL" ]; then curl -fsSL "$WG_CONF_URL" -o "$TARGET" && say "Hub VPN config downloaded."
  elif [ -n "$WG_CONF" ] && [ -f "$WG_CONF" ]; then cp "$WG_CONF" "$TARGET" && say "Hub VPN config copied."
  fi
  if [ -s "$TARGET" ]; then
    chmod 600 "$TARGET"
    case ",$PROFILES," in *,wg-client,*) ;; *) PROFILES="${PROFILES:+$PROFILES,}wg-client";; esac
    HUB_READY=1
  fi
fi

TZONE="${TZ:-$(cat /etc/timezone 2>/dev/null || echo UTC)}"
cat > "$DIR/.env" <<EOF
NETWATCH_IMAGE=$IMAGE
NETWATCH_PORT=$PORT
TZ=$TZONE
COMPOSE_PROFILES=$PROFILES
# health add-on: SMART disk checks, /dev/watchdog, container restart watch
COMPOSE_FILE=docker-compose.yml:docker-compose.health.yml
EOF
ok "Wrote $DIR/.env  (profiles: ${PROFILES:-none})"

# ============================================================================
hdr "4/5  Pulling images & starting"
cd "$DIR"
say "Pulling images (this can take a few minutes the first time)…"
docker compose pull
say "Starting the stack…"
docker compose up -d

wait_for() { local n="$1" url="$2" i; for i in $(seq 1 "${3:-20}"); do curl -fsS -o /dev/null --max-time 4 "$url" 2>/dev/null && { ok "$n is up"; return 0; }; sleep 3; done; warn "$n not responding yet at $url"; return 1; }
wait_for "Netwatch" "http://localhost:$PORT/api/health" 12
[ "$WANT_KUMA" = 1 ] && wait_for "Uptime Kuma" "http://localhost:$KUMA_PORT/" 14
VPNIP=""
if [ "$HUB_READY" = 1 ]; then
  sleep 3
  VPNIP=$(ip -4 -o addr show wg0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 || true)
  if ping -c1 -W2 10.8.0.1 >/dev/null 2>&1; then ok "Hub VPN tunnel up (hub reachable at 10.8.0.1)"
  else warn "Hub VPN tunnel not confirmed yet (check: ip addr show wg0)"; fi
fi

# ============================================================================
hdr "5/5  Done"
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "   Dashboard / setup wizard:  http://${IP:-<pi-ip>}:$PORT/setup"
[ "$WANT_KUMA" = 1 ] && echo "   Uptime Kuma:               http://${IP:-<pi-ip>}:$KUMA_PORT/  (create the admin once)"
if [ "$HUB_VPN" = 1 ] && [ "$HUB_READY" = 1 ]; then
  echo "   Linked to the central hub. This site's VPN address: ${VPNIP:-<bringing up…>}"
  echo "   (If you added this site on the hub, it will turn green within ~1 minute.)"
elif [ "$HUB_VPN" = 1 ]; then
  warn "Hub VPN set up but no config supplied — this site is NOT linked yet."
  echo "   Finish: on the hub's wg-easy UI create a client, save its .conf as"
  echo "     $DIR/data/wg-client/wg_confs/wg0.conf   then:  (cd $DIR && docker compose up -d)"
fi
echo
echo "   Update later:  cd $DIR && docker compose pull && docker compose up -d"
if [ "$(ask 'Run the first network scan now?' Y)" = y ]; then
  curl -fsS -X POST "http://localhost:$PORT/api/trigger" -H 'Content-Type: application/json' -d '{"mode":"quick"}' >/dev/null 2>&1 \
    && say "Scan started — open the dashboard to watch it." || warn "Could not trigger the scan (finish the setup wizard first)."
fi
