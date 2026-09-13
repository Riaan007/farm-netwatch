# Netwatch Control Center

The hub's main page (`/`). Static files: `hub/app/static/control-center.html` and
`hub/app/static/cc/` (`cc.css`, `core.js`, `fleet.js`, `site.js`). No build step, no
CDN, no framework — the hub has to work on an offline farm LAN. The server stamps
asset URLs with the newest file mtime so a deploy is never hidden by browser cache.
The old card view is kept at `/classic`; `/control-center` redirects to `/`.

## Where the numbers come from

`core.js` derives every state and count in one place (`CC.siteIssues`,
`CC.siteState`, `CC.siteCounts`, `CC.devState`), so the overview, sidebar, attention
list and site pages cannot disagree.

| Signal | Source | Refresh |
|---|---|---|
| Site cards, reachability, Pi health level, Kuma, key status | `GET /api/hub/overview` | 20 s |
| Devices per site | `GET /api/hub/sites/<id>/devices` (hub snapshot) | when the hub has newer data, at most every 55 s |
| Internet from the site | `GET /api/hub/sites/<id>/internet` (live) | 60 s |
| Wireless link findings | `GET /api/hub/sites/<id>/wifi` (live) | 5 min |
| Backups | `GET /api/hub/sites/<id>/backups` | 5 min |

Polling pauses while the tab is hidden and runs three sites at a time.

- **Site states:** paused → offline → fault → needs a look → healthy (rules in the
  header comment of `core.js`).
- **Devices:** `online`; `offline` = seen within 7 days; `gone quiet` = not seen for
  7+ days (hidden by default, counted separately).
- **Key equipment** = cameras, recorders, routers, switches/APs, wireless links,
  alarms, solar/power, and anything identified as MikroTik (vendor/model/banner
  mentions MikroTik, RouterBOARD or RouterOS).
- **What went wrong in Codex's first version:** it only counted devices ticked
  *Watch* **and** in a camera/network category, so sites without watched devices
  read "0 assets · Unknown", routers/NVRs/radios were ignored, MikroTik
  (`Routerboard.com`) wasn't recognised, and "healthy" required data younger than
  10 minutes while devices refresh every 5 — sites flipped to Unknown.

## IP search

`CC.ipMatcher` (core.js) turns an address-shaped query into an octet-aware match,
used by the device tables and the History tab: `192.168.0.1` exact · `.31` ends in ·
`192.168.0.` subnet · `192.168.0.1*` widened prefix · `88.3` octet-aligned (last
octet typed is a prefix). Anything else (e.g. `31`, `camera`) is plain text search.
A full address in History asks the site for that address's whole history
(`events?ip=`) rather than filtering the newest 500. Tests: `node tests/cc-ipsearch.cjs`.

## Still done elsewhere

Editing device names, categories, watch flags and saved logins happens on the
site's own Netwatch page (linked from the site header and the device drawer).
There is no hub-password change screen, no persistent incident/acknowledge
workflow, and no MikroTik SSH integration yet.

## Check

```sh
HUB_URL=http://localhost:8091 HUB_PASSWORD=… CHROMIUM_PATH=/usr/bin/chromium \
  node tests/control-center.cjs
```

Read-only. Logs in, compares the on-screen KPIs, attention list, site headers and
device-table totals with the API, opens every route for every site, and checks
the phone layout doesn't scroll sideways. Don't commit screenshots of a live hub:
they show client names and addresses.
