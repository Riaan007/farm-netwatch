# Netwatch control center

An additive operator view for the existing hub. No extra software or polling loop is installed on the client Pi.

## Open

- Open `hub/app/static/control-center.html` directly in a browser for the offline example workspace. All example records are fictional and clearly labeled.
- After deploying the updated hub image, sign into the hub and open `/control-center`. The classic hub has a link to this route; `/` and existing site pages remain available.
- `/control-center?demo=1` explicitly selects example mode in a served environment. Live errors never fall back to example data.

Deployed on 13 September 2026 at http://192.168.88.250:8091/control-center with the existing dark navy/cyan/emerald theme. Only the hub application container was recreated; the VPN gateway, proxy and site agent remained running. The new route uses the existing session guard; existing security findings are not fixed by this UI.

## Working features

Overview, client site list, managed inventory, current attention signals, MikroTik inventory view, configuration backup metadata, search/site/state filters, device and site details, filtered CSV export, responsive layout and keyboard-accessible dialogs. No CDN, external font or frontend package dependency is needed at runtime.

Live data comes from `/api/hub/overview`, per-site `/devices`, and (on request) `/backups`. These endpoints read central cached inventory and stored backup metadata. Fetch concurrency is capped at three. A visible browser refreshes snapshots once per minute; no new scans or client changes are requested by this page. Existing diagnostic pages are linked for authorized use.

The existing boolean `watch` flag is the temporary explicit scope selector, combined with a camera/recorder/network category or type. Unwatched and unrelated assets are excluded. This is a UI filter, not a new authorization boundary or fleet-wide monitoring-policy change. The underlying scanner and alert behavior are unchanged. A dedicated persisted managed-asset flag remains future work.

Missing, failed, unreachable, disabled or stale inventory is shown as unknown rather than healthy. Inventory older than ten minutes is treated as stale, in addition to the backend stale flag. A site's missing visibility suppresses individual device-down signals in this view. Fresh unreachable managed devices and appliance warnings populate the attention view. These are observations, not a persistent incident workflow. Summary cards describe the whole fleet; filters apply to the displayed lists.

Backups are displayed by age, not labeled restore-tested. Actual records are the existing sensitive Netwatch configuration bundles, not MikroTik configuration backups. Device recording health is not inferred from ping. MikroTik identification uses existing vendor/model/name information; RouterOS identity, host keys and capabilities have not been probed.

## Deliberately pending

MikroTik SSH sessions and router-origin diagnostics, secure relay changes, router configuration backups, configuration edits, persistent incidents/acknowledgements, tenant RBAC, managed-scope enforcement in collectors, and recording-health integrations. Buttons for unimplemented capabilities are disabled and labeled. This page does not execute POST/DELETE requests.

## Validation

`tests/control-center.cjs` uses Playwright with a local Chromium executable. Install Playwright in a development environment, then run:

```sh
CHROMIUM_PATH=/usr/bin/chromium node tests/control-center.cjs
```

If Playwright is installed outside normal module resolution, set `PLAYWRIGHT_MODULE` to its absolute package path. The test writes example desktop/mobile screenshots to `docs/` and validates demo navigation, filters, CSV download, dialogs, backups, managed scope, escaped device names, duplicate LAN addresses across sites, stale/partial data, expired authentication, and GET-only operation against mocked hub endpoints. In addition, post-deployment checks verified hub health, anonymous page redirection/API rejection, authenticated live data loading, navigation, dark theme and the classic hub link. The test account session was logged out afterwards.

## Deployment and rollback

The deployed image layers only the hub server route and two HTML pages onto the previously running image, preserving its dependencies and other application files. The normal source Dockerfile includes these files on a future full build. `control-center-deployment.json` records the retained rollback image.

To roll back, tag that recorded rollback image as `farm-netwatch-hub:local`, then run `docker compose up -d --no-deps hub` in the hub directory. No database migration or client update was performed.
