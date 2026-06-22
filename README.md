# Farm Network Asset Identifier (Netwatch)

A self-contained network scanner + dashboard for a farm/site LAN, packaged as a
single Docker container that runs on **Raspberry Pi 3 and newer** (and any
Debian/amd64 host). It discovers every device on the network, identifies what
each one is (camera / NVR / access point / router / server / IoT), tracks
uptime over time, and pushes a phone alert when something new joins or a known
device drops offline.

## Highlights

- **Real discovery** — `nmap` ARP sweep on the local segment (gets MAC + vendor)
  plus ICMP/TCP discovery for remote/routed subnets.
- **Two scan depths** — fast *quick scan* every interval (top ports), and an
  on-demand *deep scan* (`-p- -sV -O`: all 65535 ports + service/version + OS
  guess) for the best possible identification.
- **Smart identification** — MAC vendor (offline OUI table + online lookup),
  reverse DNS, HTTP title/`Server` banner, and port fingerprints combined into a
  device type. Unknown devices show up as **Mystery Nodes** you can name once.
- **Uptime history** — every scan is recorded in SQLite; the device panel shows
  24h / 7d / 30d uptime and a sparkline. Great for spotting a camera that keeps
  dropping.
- **Per-device latency graph (Uptime-Kuma style)** — Netwatch samples each
  **watched/named** device's latency every ~60s into a short-retention table, so the
  hub's device row shows a smooth latency line + up/down bars with **30m / 1h / 12h /
  24h** ranges (replaces the old coarse uptime line and the standalone Kuma panel).
  Config: `scan.heartbeat_*`.
- **Multi-subnet + fixed IP (Network settings)** — give the Pi **extra IP addresses**
  so it sits on several LANs at once and ARP-scans each (additive — never touches the
  main link; re-applied on reboot; can auto-add a scan target). Add **routed** ranges
  for networks reachable via the gateway. And switch a connection **DHCP→fixed IP** with
  an **auto-revert safety**: a bad change auto-reverts to DHCP after ~2 min (and on the
  next boot) unless you confirm — so a remote Pi can't lock itself out. The fixed-IP
  control needs the **opt-in NetworkManager image** (keeps `:latest` lean):
  `docker compose -f docker-compose.yml -f docker-compose.netcfg.yml up -d --build`.
  Extra IPs + routed ranges work on the default image.
- **Site internet badge + Kuma link** — the site dashboard header shows a live
  **Internet** badge (gateway / upstream / DNS) and a **Kuma ↗** link to the site's
  own Uptime Kuma.
- **Offline alerts both ways (ntfy)** — the **hub** alerts (🔔 Alerts) when a farm
  **site drops off** the hub (and recovers); each **site** alerts when its **VPN link
  to the hub** goes down. Each is independently toggleable (hub: *Notify when a site
  goes offline*; site Settings: *Alert when the hub link goes offline*).
- **Internet-uptime monitors (default)** — when Kuma is configured, Netwatch
  auto-creates **Gateway + 8.8.8.8 + 1.1.1.1** ping monitors and a **google.com DNS**
  monitor (tagged *Internet*), so you can tell apart "no link", "no upstream", and
  "DNS broken". The hub site page shows an **Internet** badge from Netwatch's own
  checks (works even without a published Kuma status page).
- **Reachability-based presence** — a device is "online" only if it actually
  responds (an open port **or** an ICMP ping), not merely an ARP reply. This stops
  a sleeping/half-disconnected Wi-Fi device (whose NIC still answers ARP) from
  showing online. Toggle with `scan.require_reachable` (default on). The hub's
  per-site **History** button shows the resulting offline/online events per IP.
- **Device & IP event history** — an append-only log of every device that joins,
  goes offline, comes back, or **takes over an IP** another device used to hold,
  each with a full snapshot of the device's details (MAC, vendor, hostname, type,
  ports, model, serial). Open it from the **History** button: a **Timeline** view
  and a **By IP** view (click an IP to see every device that's ever lived there).
  The log is independent of the live device list, so it **survives even after you
  forget/prune a device** (retained ~1 year). APIs: `GET /api/events`
  (`?ip=`/`?key=`/`?type=` filters) and `GET /api/ip-history`.
- **Connection-quality test** — a per-device ping burst reporting packet loss,
  average latency, and jitter, graded excellent / good / fair / poor — so you can
  tell *how good* a link is, not just whether it's up.
- **Local discovery (WiFiman-style)** — mDNS/Bonjour, SSDP/UPnP, NetBIOS and the
  ARP cache enrich the local segment: finds devices that ignore ping, and pulls
  friendly hostnames, models, and even **serial numbers** (many UPnP cameras
  advertise their serial). No credentials needed.
- **Hikvision deep info** — a "Camera info" button (and auto, on deep scans) pulls
  model / serial / firmware straight from a Hikvision camera/NVR via ISAPI using
  its saved login.
- **Asset details + photo** — serial number, model, and a **photo** per device
  (auto-filled by discovery/ISAPI where possible, editable otherwise); the photo
  shows on the grid card and in the details panel for quick visual identification.
- **Device linking** — link a device that has two or more MACs (wired + WiFi, or
  multiple interfaces) to another so they're recognised as one unit.
- **Adaptive deep scan** — optionally auto-run a full deep scan the first time a
  device responds, to identify it accurately without deep-scanning everything
  every cycle. Scans are serialised so they never overlap.
- **Save device logins** — store a username/password/notes per device (handy for
  camera, NVR, and router web UIs). Secrets are kept out of the polled device feed,
  obfuscated at rest, and masked in the UI with reveal/copy buttons. A 🔑 marks
  devices that have a saved login.
- **Presence watch** — toggle 🔔 on any device (e.g. your phone) to get a push when
  it goes **offline** *and* when it comes **back online**.
- **Per-category alerts** — in Settings, choose per device type (camera, network,
  printer, IoT, …) whether to alert on offline, offline+online, or not at all — so
  important gear notifies while noisy IoT stays quiet. Precedence: a watched device
  always alerts; otherwise the category rule applies; otherwise the global default.
- **Push alerts + remote control** — new-device/offline alerts via [ntfy](https://ntfy.sh),
  with a **Test** button to confirm delivery. You can also reply to the topic (or tap
  an alert's action buttons) to run `ping`, `port`, `tracert`, `quickscan`, or
  `deepscan` against a device from anywhere — results come back to your phone.
- **Selective deep scan** — deep-scan a single device from its panel, or multi-select
  several devices on the grid and deep-scan just those.
- **Live dashboard** — the grid repaints automatically the moment a scan
  finishes (no manual refresh), polls faster while a scan is running, and shows a
  green "Live" indicator.
- **Stable device identity** — devices are tracked by MAC, so when one changes IP
  (DHCP reassignment) its record just moves to the new address — it is **not**
  marked offline and re-added as a new device. An occasional ARP miss won't fork a
  device either, and an IP-only host that later reveals a MAC is merged in place.
- **Uptime Kuma integration** — optionally run [Uptime Kuma](https://github.com/louislam/uptime-kuma)
  in the same stack; tick "Monitor in Uptime Kuma" on a device and Netwatch
  auto-creates a 60s **ping** monitor (tagged by category) for polished uptime
  graphs & status pages.
- **Multi-subnet** — scan several networks from one Pi; switch between them in
  the header.
- **Remote access VPN** (optional) — bring up **Tailscale** (zero-config) or
  **WireGuard** (self-hosted, `wg-easy`) to reach the cameras/gear from anywhere.
- **Offline-friendly** — Tailwind CSS and the OUI table are baked into the image;
  no CDN needed at runtime.

## Quick install (any Raspberry Pi)

The installer is a **guided wizard**: it preflights the Pi (arch, kernel
WireGuard, RAM, disk, internet, free ports), **installs anything missing**
(Docker, Compose, the WireGuard module), pulls Netwatch + Uptime Kuma, links the
central-hub VPN, and hands off to the dashboard setup wizard.

```bash
curl -fsSL https://raw.githubusercontent.com/Riaan007/farm-netwatch/main/install.sh | sudo bash
```

Check compatibility without changing anything:

```bash
curl -fsSL https://raw.githubusercontent.com/Riaan007/farm-netwatch/main/install.sh | sudo bash -s -- --check
```

When done, open **`http://<pi-ip>:8090/setup`** for the site wizard (name →
subnets → interval → alerts → VPN). Flags/env: `--check`, `--no-kuma`/`NO_KUMA=1`,
`--no-vpn`/`HUB_VPN=0`, `NETWATCH_PORT`, `KUMA_PORT`.

### Joining the central hub — one paste (recommended)

Easiest: on the **hub** open *Add site*, fill in the name, and copy the single
command it gives you. Paste it on the new Pi (**fresh or existing**) — it runs the
wizard above with this site's VPN config baked in, then the site turns green on the
hub within a minute. (Backed by the hub's wg-easy API + `_ENROLL_TEMPLATE`.)

### Joining from the browser — no SSH, no open ports

If Netwatch is already running on the site, you don't need the terminal at all.
On the hub open *Add site* and **copy the site's WireGuard config**, then on the
site's dashboard go to **Settings → 🔗 Central Hub VPN**, paste it, and click
**Connect**. Netwatch writes the config and brings the tunnel up itself
(`wg-quick`, in the host netns) — the site dials **out**, so **no inbound ports
are opened**. The card shows live status (🟢 connected / handshake age / your
`10.8.0.x` / hub reachable) and survives restarts. *Disconnect* drops the tunnel;
*Forget link* also deletes the stored config + keys. Don't also enable the
`wg-client` compose profile — only one process may own `wg0`.

Doing it by hand instead: the installer enables the **WireGuard client to the hub
by default** — supply the per-site config from wg-easy with `WG_CONF_B64="$(base64
-w0 site.conf)"`, `WG_CONF_URL=<url>`, or `WG_CONF=/path/to/site.conf`. Without one
it prints the finish step (drop the conf at
`/opt/netwatch/data/wg-client/wg_confs/wg0.conf`, then `docker compose up -d`) and
shows the site's `10.8.0.x` address to **register on the hub**. Opt out with
`HUB_VPN=0`.

## Manual / from source

```bash
git clone https://github.com/Riaan007/farm-netwatch.git
cd farm-netwatch
docker compose up -d --build          # builds locally
# dashboard on http://<host>:8090
```

## Configuration

The wizard writes `/opt/netwatch/data/config.json`. You can also edit scan
interval, primary network, and the ntfy topic from the dashboard gear menu, or
re-run the full wizard at `/setup`. All state (config, device names, uptime DB)
lives in the `./data` volume and survives image updates.

| Setting | Where | Notes |
|---|---|---|
| Scan targets (CIDRs) | wizard | local subnets auto-detected; add remote ones |
| Scan interval | wizard / gear | 5–60 min |
| Online lookups | wizard | turn off for air-gapped sites |
| ntfy topic | wizard / gear | blank = alerts off |
| History retention | wizard | default 90 days |
| VPN | wizard + compose profile | none / tailscale / wireguard |

### VPN notes

- **Tailscale**: `docker compose --profile tailscale up -d`, then
  `docker exec netwatch-tailscale tailscale up --advertise-routes=192.168.88.0/24`
  and approve the route in the Tailscale admin. No router changes needed.
- **WireGuard server** (`wg-easy`): for a standalone site. Set `WG_HOST`
  (public IP / DDNS) in `/opt/netwatch/.env`,
  `docker compose --profile wireguard up -d`, open the admin UI on **:51821**,
  add a client, and forward **UDP 51820** on your router.
- **WireGuard client** (`wg-client`): for sites reporting to a central
  [hub](#central-hub-all-sites-on-one-dashboard). **The installer enables this by
  default** (supply the per-site config — see "Linking a new site to the central
  hub" above). The site dials out to the hub — no router changes at the farm.
  Don't combine with the `wireguard` profile.

## Remote control via ntfy

Set an ntfy topic in the wizard (or the gear menu) and hit **Test** to confirm your
phone receives it. With "Allow remote commands" enabled, send any of these as a
message to the topic and the result is posted back:

| Command | Example | Does |
|---|---|---|
| `ping <ip>` | `ping 192.168.88.5` | host up/down + latency |
| `port <ip> <port>` | `port 192.168.88.5 554` | TCP port state |
| `tracert <ip>` | `tracert 192.168.88.254` | route hops |
| `test <ip>` | `test 192.168.88.5` | connection quality (loss/latency/jitter) |
| `quickscan [cidr]` | `quickscan` | fast scan |
| `deepscan <ip\|cidr>` | `deepscan 192.168.88.5` | full deep scan |
| `status` / `help` | `status` | summary / command list |

New-device and offline alerts also carry tap-to-run action buttons (Deep scan, Ping).

> Security: ntfy topics are public by default — anyone who knows the topic name can
> send these commands. They are **read-only probes** and are **restricted to IPs inside
> your configured scan networks** (external IPs are refused), but use a hard-to-guess
> topic name, or a self-hosted ntfy server with auth, and turn off "Allow remote
> commands" if you only want alerts.

## Who does what (KISS)

- **Uptime Kuma = monitoring.** It pings the IPs, shows what's up/down, keeps the
  history graphs and sends up/down alerts. Flag a device and its monitor
  **auto-follows the device's IP** (tracked by MAC) — so it keeps working when DHCP
  moves the device. Netwatch no longer sends its own offline/online ntfy (no double
  alerts); up/down is Kuma's job.
- **Netwatch = inventory + problems.** Find devices, scan, identify, and **flag
  problems** in the dashboard's **Problems** panel (and `GET /api/problems`):
  IP conflict · IP changed (DHCP drift) · device replaced at an IP · same MAC on
  multiple IPs · risky exposed ports (Telnet/FTP/…) · plus the existing new-device
  and "mystery" discovery flags. Fix problems on the network; Kuma handles uptime.

Flag devices for Kuma one at a time (a device's **Monitor in Uptime Kuma** tick) or
in bulk from **Settings → Integrations**: **Monitor all cameras / all identified**.

## Uptime Kuma integration

[Uptime Kuma](https://github.com/louislam/uptime-kuma) adds long-term uptime
graphs, public status pages, and many notification channels. Netwatch (discovery +
presence) feeds it; Kuma does the history/status-page side.

Run it alongside Netwatch:

```bash
docker compose --profile kuma up -d        # Kuma on http://<pi>:3001
```

> **Version:** pinned to **Uptime Kuma 1.23.17** (newest 1.x). Kuma **2.x** is a
> rewrite that removed the Socket.IO API this integration uses, so on 2.x Netwatch
> can't auto-create, tag, or delete monitors (push + the pull health URL still
> work, but you'd manage monitors by hand in Kuma). Stay on 1.x unless `kuma.py`
> is re-ported to a 2.x API. You can silence Kuma's "new update" prompt in its
> Settings → General.

To keep it running on every `up`, add `COMPOSE_PROFILES=kuma` to `.env`.

**Auto-provisioning (per device you select).** Open `http://<pi>:3001` once to
create the Kuma admin account. In Netwatch → Settings → **Integrations · Uptime
Kuma**, set the base URL (`http://localhost:3001`), enter the Kuma admin
**username + password**, and hit **Test**. Then open a device and tick
**"Monitor in Uptime Kuma"**.

Netwatch then **creates a Kuma ping monitor for you** (via Kuma's Socket.IO API)
pointed at the device's IP, and stores the monitor ID. **Kuma pings the device
directly every 60 s**, so you get a smooth graph and accurate uptime — Netwatch
just keeps the monitor's IP in sync if the device's address changes, and deletes
the monitor when you untick. (A *ping* monitor is used rather than a 30-min push,
which would otherwise flap between scans.)

Monitors are created **only for devices you tick** — never automatically during a
scan. If you have monitors left over from an older version, **Settings → "Fix
monitors → ping (60s)"** converts them in place.

Each monitor is **tagged with its device category** (Camera, Network, Printer,
Server, …) so you can filter/group on a Kuma status page. New monitors are tagged
at creation; **"Tag existing monitors by category"** in Settings back-fills tags
onto monitors you made earlier (idempotent — safe to run again).

**Manual / pull (no admin creds).** Under a device's "Manual / pull setup" you can
instead paste a token from a Kuma *Push* monitor you made yourself, or copy the
device's **health URL** into a Kuma *HTTP* monitor (200 = up, 503 = down).

## Central hub: all sites on one dashboard

The `hub/` stack turns one machine (e.g. the Pi behind your DDNS name) into a
**WireGuard hub + multi-site dashboard**: every farm site dials in over the VPN,
and `http://<hub>:8091` shows one card per site — reachability, device counts,
watched-device alarms, last scan, and that site's **Uptime Kuma** monitors —
with deep links into each site's own Netwatch and Kuma UIs.

```
farm sites (wg-client, dial out) ──UDP 51820──> hub (wg-easy 10.8.0.1)
        Netwatch :8090 / Kuma :3001  <──polled── hub dashboard :8091
```

No site-side app changes: the hub *pulls* each site's existing JSON API over
the tunnel. Because all farms reuse the same LAN range (192.168.88.0/24), sites
are addressed **only by their unique VPN IP** — LAN routes are never advertised.

### Hub setup (once)

```bash
cd hub && cp .env.example .env      # set WG_HOST, WG_PASSWORD_HASH, HUB_PASSWORD
docker compose up -d --build
```

1. Forward **UDP 51820** on your router to the hub machine. Do **not** forward
   8091/51821 — reach them over the VPN (or LAN); the login is defense in depth.
2. Open the hub at `http://<hub-lan-ip>:8091`, sign in, and check the
   pre-seeded **home** site goes green.
3. Open wg-easy at `http://<hub-lan-ip>:51821` and create one client per farm
   site (note each one's `10.8.0.x`), plus clients for your phone/laptop.

> **Heads-up:** the hub app shares wg-easy's network namespace (that's how it
> reaches `10.8.0.x`). Always restart the stack with `docker compose up -d`
> from `hub/` — recreating `wg-easy` alone strands the hub container.

### Per site — the wizard way (recommended)

Hub gear icon → **✨ New site wizard**: enter a slug + label, tick whether the
site runs Kuma, and the hub does everything on its side — it creates the
WireGuard client through wg-easy's API, registers the site, and shows **one
command to paste on the farm Pi** (it embeds the wg0.conf, pins the
`wg-client` compose profile, and starts the tunnel). The site card turns green
within a minute. The command can be re-issued any time from the site's
**Setup cmd** button; deleting a wizard site also removes its VPN client.
Requires `WG_PASSWORD` (plain text of the hash) in `hub/.env`.

### Per site — manual

```bash
# on the farm Pi, after downloading the site's conf from the hub's wg-easy UI:
mkdir -p /opt/netwatch/data/wg-client/wg_confs
cp site.conf /opt/netwatch/data/wg-client/wg_confs/wg0.conf
cd /opt/netwatch && docker compose --profile wg-client up -d
```

Then register the site in the hub (gear icon → "Manual add"): VPN IP
`10.8.0.x`, port 8090, and — if the site runs Kuma — Kuma URL
`http://10.8.0.x:3001`.

*Alternative without Docker:* `apt install wireguard`, drop the conf at
`/etc/wireguard/wg0.conf`, `systemctl enable --now wg-quick@wg0`. Fewer moving
parts; recommended for sites with flaky power/connectivity.

### Kuma on the hub dashboard

The hub reads each site's Kuma through its **published status page** (no Kuma
login needed). Once per site, in that site's Kuma UI: **Status Pages → New**,
slug **`farm`**, add your monitors to a group, **publish**. Enter the slug in
the hub's site settings and the monitors' heartbeat bars appear on the site
detail page.

### Opening a site's Netwatch / Kuma from a LAN browser

A site's dashboards live on its WireGuard IP (`10.8.0.x`), which a normal LAN
computer can't route — so the hub **reverse-proxies** them. A Caddy sidecar
(`hub-proxy`, sharing wg-easy's netns) serves each VPN site's Netwatch and Kuma
on a LAN port of the hub, so the **Netwatch ↗ / Kuma ↗** buttons just open in any
LAN browser — **no VPN client needed** on your computer.

- Ports are auto-assigned from the **`8200-8231`** range (published on wg-easy;
  set `HUB_PROXY_RANGE` to change it — it must match the compose `ports:` entry).
  The hub generates `data/hub/caddy/Caddyfile` and hot-reloads Caddy on changes.
- Each proxied port is gated by **Basic Auth** — log in as **`admin`** with your
  **hub password**. (If the buttons 401 with no prompt or the proxy seems off after
  upgrading, log into the hub once — that backfills the auth hash.)
- The **home** site uses a LAN IP, so it's linked directly (not proxied).
- **Security:** these ports expose the (login-less) site dashboards on your LAN
  behind the hub password. Keep them LAN-only — **do not forward `8200-8231`** to
  the internet.

### Connect to a device on a site's LAN (SSH / web / any port)

Beyond a site's own dashboards, you can reach the **devices behind it** — a
camera's web page, SSH/PuTTY, RDP, RTSP, any port — from a home-LAN computer that
is **not** a VPN client. On the hub's site page, each device row has a **Connect**
button: pick a port (or type a custom one) and the hub gives you a `host:port` to
paste into PuTTY/SSH, or a clickable link for web ports.

How it works (on-demand two-hop TCP relay): the tunnel is split (`10.8.0.0/24`
only) so the hub can't reach devices behind a site directly, and farm LANs overlap
across sites — so the **site Pi** (the only node that reaches its LAN) opens a relay
to the device, bound to its VPN address; the **hub** re-exposes it on a LAN port
(`8300-8331`, set `HUB_TCP_RANGE`). Tunnels are **on-demand and auto-close** when
idle; an *Active tunnels* panel lets you close them manually.

- **Security:** creating a tunnel requires the hub login; the site only relays to
  IPs it has actually scanned (no open relay to arbitrary hosts) and binds the relay
  to its VPN address (only the hub reaches it); the device's own login still applies.
  The `8300-8331` ports are **LAN-only — never forward them to the internet**.
- Example: SSH → `ssh -p <hubport> user@<hub-ip>` (or PuTTY `<hub-ip>:<hubport>`);
  web UI → click the `http(s)://<hub-ip>:<hubport>` link.

### Per-device diagnostics from the hub

On a site page, click a device row to expand it — alongside its uptime you get
**Diagnostics**: **📡 Ping**, **📶 Connection test** (packet loss / latency / jitter
rating), and **🔬 Deep scan** (all ports + service/version + OS). These run **on the
site** (proxied over the VPN) and show their results inline; a deep scan's findings
appear in the device list after the next refresh.

### Remote access (laptop / phone) — use the hub from anywhere

The hub's **📱 Remote** button creates a WireGuard client for a laptop or phone so you
can use everything **as if you were at the office**. Click *Add device*, then scan the
**QR** in the WireGuard phone app or **download the `.conf`** for the desktop app.

The config is **split tunnel** routing the office LAN + the VPN subnet
(`AllowedIPs = 10.8.0.0/24, <office_lan>`, auto-derived from the home site) via the
hub's wg-easy — so once connected you reach the hub at its **office address**
(`http://<hub-lan-ip>:8091`), every site's dashboards + device tunnels, and office-LAN
devices (RDP/SSH/printers), while your normal internet stays direct. No server-side
routing change is needed (wg-easy already NATs onto the office LAN).

**One-time router step:** forward **UDP 51820** to the hub. The DDNS (`WG_HOST`) is
already used as the `Endpoint`. Remove a device any time from the same panel.

### Hub troubleshooting

| Symptom | Check |
|---|---|
| site card stays red | `docker exec hub-wg-easy wg show` — recent handshake? site's wg-client logs? |
| handshake ok, card red | from hub: `docker exec netwatch-hub python -c "import requests;print(requests.get('http://10.8.0.X:8090/api/status',timeout=5).json())"` |
| Kuma panel: "no status page" | the slug isn't published, or has no monitors in a *public* group |
| sites can't reach hub | router forward UDP 51820 → hub; `WG_HOST` resolves to your WAN IP |
| site button 401 / proxy off | log into the hub once (backfills the proxy auth hash); `cat data/hub/caddy/Caddyfile`; `docker logs hub-proxy` |
| site button times out | `docker exec hub-proxy ip addr show wg0` (should show 10.8.0.1); confirm the site card is otherwise green |

## How it works

```
nmap scan ──> parse ──> identify (OUI + DNS + HTTP banner + ports) ──> device records
                                  │
              SQLite history <────┤────> ntfy alerts (new / offline)
                                  │
                       Flask API (:8090) ──> dashboard + wizard
```

| File | Role |
|---|---|
| `app/scanner.py` | scheduler, quick/deep nmap, multi-target, state |
| `app/identify.py` | MAC vendor, HTTP banner, type heuristics |
| `app/history.py` | SQLite uptime samples + rollups |
| `app/notify.py` | ntfy push |
| `app/server.py` | Flask API + serves the UI |
| `app/static/` | dashboard + setup wizard |

## Security

This is a **LAN tool**: the dashboard has no auth, so keep it on a trusted
network and reach it remotely via the VPN rather than exposing port 8090 to the
internet. Scanning is read-only discovery — it never logs into devices.

**Saved credentials** are stored in `/data/credentials.json`, obfuscated at rest
with a per-install key (`/data/secret.key`, mode 600) and kept out of the
`/api/devices` feed. Be clear-eyed about the threat model: obfuscation protects
against casual reading of the file or a backup, but anyone who can reach the
(unauthenticated) dashboard can request a stored password via the API. Only use
this on a trusted LAN / behind the VPN. Both files live in the `./data` volume —
exclude them from any public backup.

## Requirements

- Raspberry Pi 3/3B+/4/5 or any 64-bit (arm64) / 32-bit (armv7) / amd64 Linux host
- Docker with the Compose v2 plugin (the installer adds it if missing)
- Runs with `network_mode: host` + `NET_RAW`/`NET_ADMIN` (required for ARP/OS scans)
