#!/usr/bin/env bash
# Farm Netwatch — guided installer for the CENTRAL HUB (multi-site dashboard +
# WireGuard server) on a fresh Raspberry Pi / Debian box.
#
# It (1) PREFLIGHTS the machine, (2) installs any missing prerequisites (Docker,
# Compose, the WireGuard kernel module), (3) pulls the prebuilt hub stack — hub
# dashboard + wg-easy VPN server + Caddy site proxy — and writes its .env
# (asking for your DDNS name and passwords; anything left blank is generated),
# and (4) brings the stack up. Optionally it ALSO installs a local Netwatch
# SITE so the hub Pi scans its own LAN, pre-registered as the "home" card.
#
#   curl -fsSL https://raw.githubusercontent.com/Riaan007/farm-netwatch/main/install-hub.sh | sudo bash
#   sudo bash install-hub.sh --check      # preflight only — changes nothing
#   WG_HOST=hub.ddns.net ASSUME_YES=1 curl -fsSL …/install-hub.sh | sudo -E bash   # unattended
#   (env vars must survive sudo — use `sudo -E` or `sudo WG_HOST=… bash`)
#
# Env / flags:  --check  --yes/-y  --no-site
#   WG_HOST=hub.example.com  public DDNS name/IP the sites dial in to (else asked)
#   WG_PASSWORD=…            wg-easy admin password    (else asked / generated)
#   HUB_PASSWORD=…           hub dashboard password    (else asked / generated)
#   LOCAL_SITE=0             skip the local Netwatch site (same as --no-site)
#   HUB_DIR (/opt/netwatch-hub)   HUB_PORT (8091)
#
# Moving an existing hub to this machine: run this installer, then
#   cd /opt/netwatch-hub && docker compose down
#   copy the OLD hub's data/ directory over ./data   (WireGuard server keys +
#   clients, site registry, backups — sites reconnect without re-enrolling)
#   docker compose up -d
#   Note: the copied data/ keeps the OLD hub dashboard password (it lives in
#   data/hub/hub.json; .env's HUB_PASSWORD only seeds a first boot). wg-easy's
#   admin password stays the NEW one from .env.
set -euo pipefail

REPO_RAW="${NETWATCH_REPO_RAW:-https://raw.githubusercontent.com/Riaan007/farm-netwatch/main}"
DIR="${HUB_DIR:-/opt/netwatch-hub}"
HUB_PORT="${HUB_PORT:-8091}"
WG_UI_PORT=51821
WG_UDP_PORT=51820
PROXY_RANGE_LO=8200; PROXY_RANGE_HI=8231     # must match hub/docker-compose.yml
TCP_RANGE_LO=8300;   TCP_RANGE_HI=8331       # must match hub/docker-compose.yml
WG_EASY_IMAGE="ghcr.io/wg-easy/wg-easy:14"
WG_HOST="${WG_HOST:-}"
WG_PASSWORD="${WG_PASSWORD:-}"
HUB_PASSWORD="${HUB_PASSWORD:-}"
LOCAL_SITE="${LOCAL_SITE:-}"
ASSUME_YES="${ASSUME_YES:-0}"
CHECK_ONLY=0

for a in "${@:-}"; do
  case "$a" in
    --check) CHECK_ONLY=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    --no-site) LOCAL_SITE=0 ;;
    -h|--help) { sed -n '2,31p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; } \
      || echo "Piped run — see the header of install-hub.sh on GitHub for the options."; exit 0 ;;
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

# A usable terminal must be OPENABLE — /dev/tty exists as a node even when the
# process has no controlling terminal (ssh without -t, cron); only the open fails.
HAS_TTY=0; { : </dev/tty; } 2>/dev/null && HAS_TTY=1

# Prompt yes/no. Uses /dev/tty so it still works when the script is piped from
# curl, as long as a terminal is attached. Unattended (no tty / --yes) = default.
ask() { # $1 question  $2 default(Y|N) -> echoes y|n
  local def="${2:-Y}" ans
  if [ "$ASSUME_YES" = 1 ] || [ "$HAS_TTY" != 1 ]; then
    [ "${def^^}" = "Y" ] && echo y || echo n; return
  fi
  read -rp "$1 [$( [ "${def^^}" = Y ] && echo 'Y/n' || echo 'y/N')] " ans </dev/tty || ans=""
  ans="${ans:-$def}"
  case "${ans^^}" in Y*) echo y ;; *) echo n ;; esac
}

# Prompt for a line of input (echoed). Empty when unattended.
ask_line() { # $1 prompt -> echoes answer ("" if no tty)
  local ans=""
  if [ "$ASSUME_YES" != 1 ] && [ "$HAS_TTY" = 1 ]; then
    read -rp "$1 " ans </dev/tty || ans=""
  fi
  printf '%s' "$ans"
}

# Prompt for a secret (not echoed). Empty when unattended.
ask_secret() { # $1 prompt -> echoes answer ("" if no tty)
  local ans=""
  if [ "$ASSUME_YES" != 1 ] && [ "$HAS_TTY" = 1 ]; then
    read -rsp "$1 " ans </dev/tty || ans=""
    printf '\n' >/dev/tty
  fi
  printf '%s' "$ans"
}

# head first so tr never gets SIGPIPE (which set -o pipefail would fatal on).
gen_pw() { head -c 512 /dev/urandom | tr -dc 'A-Za-z0-9' | head -c 20; }

NEED_ROOT_MSG="re-run with sudo"
[ "$CHECK_ONLY" = 1 ] || [ "$(id -u)" = 0 ] || die "must run as root ($NEED_ROOT_MSG)."

# ============================================================================
hdr "1/6  Preflight — can this machine run the hub?"
APT=0; command -v apt-get >/dev/null 2>&1 && APT=1

# OS
if [ -r /etc/os-release ]; then . /etc/os-release; fi
case " ${ID:-} ${ID_LIKE:-} " in
  *debian*|*ubuntu*|*raspbian*) ok "OS: ${PRETTY_NAME:-Debian-based}" ;;
  *) [ "$APT" = 1 ] && warn "OS ${PRETTY_NAME:-unknown}: not Debian-family but apt present" \
                    || warn "OS ${PRETTY_NAME:-unknown}: no apt — only Docker's own installer will work" ;;
esac

# Architecture — the hub image is 64-bit only (bcrypt has no 32-bit ARM wheels).
case "$(uname -m)" in
  aarch64|arm64)  ok "Arch: arm64 (Pi 3/4/5 64-bit)" ;;
  x86_64|amd64)   ok "Arch: amd64" ;;
  armv7l|armv6l)  bad "Arch: 32-bit ARM — the hub image is built for arm64/amd64 only (use a 64-bit OS)" ;;
  *)              bad "Arch: $(uname -m) — unsupported (need arm64/amd64)" ;;
esac

# WireGuard kernel support — required: wg-easy runs the VPN server in-kernel.
WG_OK=0
if [ -d /sys/module/wireguard ] || modprobe -n wireguard >/dev/null 2>&1 || modinfo wireguard >/dev/null 2>&1; then
  WG_OK=1; ok "WireGuard: kernel module available"
elif [ "$APT" = 1 ]; then
  warn "WireGuard: module missing — will install the 'wireguard' package"
else
  bad "WireGuard: module missing and no apt to install it — the VPN server cannot run"
fi

# RAM — hub + wg-easy + Caddy are light; the optional local site adds Kuma.
MEM_MB=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)
if [ "$MEM_MB" -ge 900 ]; then ok "RAM: ${MEM_MB} MB"
elif [ "$MEM_MB" -gt 0 ]; then warn "RAM: ${MEM_MB} MB — tight; skip the local site (--no-site) on this box"; fi

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

# The hub image must be anonymously pullable (public GHCR package) — a private
# or not-yet-published package would only fail much later, at compose pull.
if [ "$NET_OK" = 1 ] && command -v curl >/dev/null 2>&1; then
  if curl -fsS --max-time 8 -o /dev/null "https://ghcr.io/token?scope=repository:riaan007/farm-netwatch-hub:pull" 2>/dev/null; then
    ok "Hub image: published and public on GHCR"
  else
    bad "Hub image ghcr.io/riaan007/farm-netwatch-hub is not publicly pullable (not published yet, or the GHCR package is still private)"
  fi
fi

# Ports free — the hub stack publishes quite a few
if command -v ss >/dev/null 2>&1; then
  # || true: zero listeners (fresh Pi, ssh off) makes grep exit 1 → pipefail
  # would otherwise kill the whole script here with no message.
  TCP_BUSY=$(ss -ltnH 2>/dev/null | awk '{print $4}' | grep -oE '[0-9]+$' | sort -un || true)
  UDP_BUSY=$(ss -lunH 2>/dev/null | awk '{print $4}' | grep -oE '[0-9]+$' | sort -un || true)
  tcp_busy() { printf '%s\n' "$TCP_BUSY" | grep -qx "$1"; }
  tcp_busy "$HUB_PORT"   && warn "Port $HUB_PORT (hub dashboard) is in use — set HUB_PORT" || ok "Port $HUB_PORT free (hub dashboard)"
  tcp_busy "$WG_UI_PORT" && warn "Port $WG_UI_PORT (wg-easy admin UI) is in use"           || ok "Port $WG_UI_PORT free (wg-easy UI)"
  printf '%s\n' "$UDP_BUSY" | grep -qx "$WG_UDP_PORT" \
    && warn "UDP $WG_UDP_PORT (WireGuard) is in use" || ok "UDP $WG_UDP_PORT free (WireGuard)"
  RANGE_BUSY=0
  for p in $(seq "$PROXY_RANGE_LO" "$PROXY_RANGE_HI") $(seq "$TCP_RANGE_LO" "$TCP_RANGE_HI"); do
    tcp_busy "$p" && RANGE_BUSY=$((RANGE_BUSY+1))
  done
  [ "$RANGE_BUSY" = 0 ] && ok "Ports $PROXY_RANGE_LO-$PROXY_RANGE_HI + $TCP_RANGE_LO-$TCP_RANGE_HI free (site proxy / device tunnels)" \
    || warn "$RANGE_BUSY port(s) busy in $PROXY_RANGE_LO-$PROXY_RANGE_HI / $TCP_RANGE_LO-$TCP_RANGE_HI (site proxy / device tunnels)"
fi
say "Note: sites dial IN — after install, forward UDP $WG_UDP_PORT on your router to this machine."

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
hdr "2/6  Hub settings"
# WG_HOST — the public name sites (and your phone) dial in to. Required.
while [ -z "$WG_HOST" ]; do
  WG_HOST=$(ask_line "Public DDNS name or static IP for this hub (e.g. myfarm.ddns.net):")
  [ -n "$WG_HOST" ] || { [ "$HAS_TTY" = 1 ] && [ "$ASSUME_YES" != 1 ] || die "WG_HOST is required — re-run with:  curl … | sudo WG_HOST=your.ddns.name bash"; }
done
case "$WG_HOST" in *" "*) die "WG_HOST must not contain spaces." ;; esac
ok "Sites will dial in to: $WG_HOST (UDP $WG_UDP_PORT)"

GEN_WG=0; GEN_HUB=0
# Re-run over an existing install: keep its secrets. Regenerating would rotate
# wg-easy's login (env hash, read every boot) but NOT the hub's (first-boot
# seed only) — leaving the freshly printed passwords half wrong.
unesc() { printf '%s' "$1" | sed 's/\$\$/$/g'; }
if [ -f "$DIR/.env" ]; then
  [ -n "$WG_PASSWORD" ]  || WG_PASSWORD=$(unesc "$(sed -n 's/^WG_PASSWORD=//p' "$DIR/.env")")
  [ -n "$HUB_PASSWORD" ] || HUB_PASSWORD=$(unesc "$(sed -n 's/^HUB_PASSWORD=//p' "$DIR/.env")")
  [ -z "$WG_PASSWORD$HUB_PASSWORD" ] || say "Existing install found at $DIR — keeping its passwords (edit $DIR/.env to change)."
fi
# The unquoted compose .env format cannot carry these (esc() handles $ itself).
pw_ok() { case "$1" in *\'*|*\"*|*'#'*|*'\'*) return 1 ;; esac; [ "$1" = "${1#[[:space:]]}" ] && [ "$1" = "${1%[[:space:]]}" ]; }
get_pw() { # $1 label -> sets REPLY_PW / GEN_FLAG; generates when blank or unattended
  local p
  while :; do
    p=$(ask_secret "$1 password (Enter = generate):")
    [ -n "$p" ] || { p=$(gen_pw); GEN_FLAG=1; break; }
    pw_ok "$p" && break
    say "Passwords may not contain ' \" # \\ or start/end with a space — try again."
    [ "$HAS_TTY" = 1 ] && [ "$ASSUME_YES" != 1 ] || die "unsupported characters in the $1 password."
  done
  REPLY_PW="$p"
}
if [ -z "$WG_PASSWORD" ]; then GEN_FLAG=0; get_pw "wg-easy admin"; WG_PASSWORD="$REPLY_PW"; GEN_WG=$GEN_FLAG; fi
if [ -z "$HUB_PASSWORD" ]; then GEN_FLAG=0; get_pw "Hub dashboard"; HUB_PASSWORD="$REPLY_PW"; GEN_HUB=$GEN_FLAG; fi
pw_ok "$WG_PASSWORD"  || die "WG_PASSWORD contains ' \" # \\ or edge whitespace — the compose .env cannot carry it."
pw_ok "$HUB_PASSWORD" || die "HUB_PASSWORD contains ' \" # \\ or edge whitespace — the compose .env cannot carry it."
ok "Passwords ready$( [ "$GEN_WG$GEN_HUB" != 00 ] && echo ' (generated ones are shown at the end — save them!)' )"

WANT_SITE="$LOCAL_SITE"
if [ -z "$WANT_SITE" ]; then
  [ "$(ask 'Also install a local Netwatch site so this Pi scans its own LAN?' Y)" = y ] && WANT_SITE=1 || WANT_SITE=0
fi

# ============================================================================
hdr "3/6  Installing prerequisites"
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

if [ "$WG_OK" != 1 ]; then
  say "Installing WireGuard…"; apt_install wireguard || apt_install wireguard-tools || true
  if modprobe wireguard 2>/dev/null || [ -d /sys/module/wireguard ]; then
    grep -qx wireguard /etc/modules 2>/dev/null || echo wireguard >> /etc/modules
    ok "WireGuard module loaded"
  else
    warn "WireGuard module still unavailable — the VPN server may not come up"
  fi
else ok "WireGuard ready"; fi

# ============================================================================
hdr "4/6  Fetching app files & configuration"
mkdir -p "$DIR/data/wg-easy" "$DIR/data/hub/caddy"
curl -fsSL "$REPO_RAW/hub/docker-compose.yml" -o "$DIR/docker-compose.yml"
ok "docker-compose.yml → $DIR"
# Strip the local "build:" block so a Pi without the source never builds.
sed -i '/^    build:/,/^      dockerfile: hub\/Dockerfile/d' "$DIR/docker-compose.yml" || true

# wg-easy wants its admin password as a bcrypt hash; its own image generates it.
say "Generating the wg-easy password hash…"
WGPW_OUT=$(docker run --rm "$WG_EASY_IMAGE" wgpw "$WG_PASSWORD")
WG_HASH=$(printf '%s' "$WGPW_OUT" | sed -n "s/^PASSWORD_HASH='\(.*\)'$/\1/p")
[ -n "$WG_HASH" ] || die "could not parse the wgpw output: $WGPW_OUT"
ok "Hash generated"

# docker compose interpolates .env values, so every literal $ must become $$.
esc() { local s="${1//$/\$\$}"; printf '%s' "$s"; }

HOME_IP=""
[ "$WANT_SITE" = 1 ] && HOME_IP=$(hostname -I 2>/dev/null | awk '{print $1}')

TZONE="${TZ:-$(cat /etc/timezone 2>/dev/null || echo UTC)}"
cat > "$DIR/.env" <<EOF
# Written by install-hub.sh. Secrets live here — keep this file root-only.
WG_HOST=$WG_HOST
WG_PASSWORD_HASH=$(esc "$WG_HASH")
WG_PASSWORD=$(esc "$WG_PASSWORD")
HUB_PASSWORD=$(esc "$HUB_PASSWORD")
HUB_PORT=$HUB_PORT
# The site installer the hub's New-site wizard points new farm Pis at.
NETWATCH_INSTALL_URL=$REPO_RAW/install.sh
# First-boot seed: register a "home" site at this LAN IP (blank = none).
HUB_HOME_IP=$HOME_IP
TZ=$TZONE
EOF
chmod 600 "$DIR/.env"
ok "Wrote $DIR/.env"

# ============================================================================
hdr "5/6  Pulling images & starting"
cd "$DIR"
say "Pulling images (this can take a few minutes the first time)…"
docker compose pull
say "Starting the stack…"
docker compose up -d

wait_for() { local n="$1" url="$2" i; for i in $(seq 1 "${3:-20}"); do curl -fsS -o /dev/null --max-time 4 "$url" 2>/dev/null && { ok "$n is up"; return 0; }; sleep 3; done; warn "$n not responding yet at $url"; return 1; }
wait_for "Hub dashboard" "http://localhost:$HUB_PORT/" 15 || true
wait_for "wg-easy UI" "http://localhost:$WG_UI_PORT/" 10 || true

if [ "$WANT_SITE" = 1 ]; then
  say "Installing the local Netwatch site (scans this Pi's own LAN)…"
  # No VPN client: the hub reaches this site by LAN IP (it shares wg-easy's
  # netns, so localhost would not work — that's what HUB_HOME_IP is for).
  curl -fsSL "$REPO_RAW/install.sh" | HUB_VPN=0 ASSUME_YES="$ASSUME_YES" NETWATCH_REPO_RAW="$REPO_RAW" bash \
    || warn "local site install did not finish — re-run later: curl -fsSL $REPO_RAW/install.sh | HUB_VPN=0 sudo -E bash"
fi

# ============================================================================
hdr "6/6  Done"
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "   Hub dashboard:   http://${IP:-<pi-ip>}:$HUB_PORT/   (login: the hub password)"
echo "   wg-easy admin:   http://${IP:-<pi-ip>}:$WG_UI_PORT/  (LAN/VPN only — do NOT forward)"
[ "$GEN_WG" = 1 ]  && echo "   ${B}Generated wg-easy password:${R}  $WG_PASSWORD"
[ "$GEN_HUB" = 1 ] && echo "   ${B}Generated hub password:${R}      $HUB_PASSWORD"
[ "$GEN_WG$GEN_HUB" != 00 ] && echo "   (Both are also stored in $DIR/.env — root-only.)"
echo
echo "   NEXT: forward ${B}UDP $WG_UDP_PORT${R} on your router → this machine (${IP:-<pi-ip>})"
echo "         and make sure ${B}$WG_HOST${R} resolves to your public IP."
echo "   Then add farm sites: hub gear icon → ${B}✨ New site wizard${R} — it prints one"
echo "   command to paste on each new farm Pi."
echo
echo "   Update later:  cd $DIR && docker compose pull && docker compose up -d"
echo "   Moving from an old hub? Stop the stack, copy the old hub's data/ over"
echo "   $DIR/data (VPN keys + sites + backups), then: docker compose up -d"
echo "   (The copied data keeps the OLD hub dashboard password; wg-easy keeps the new one.)"
