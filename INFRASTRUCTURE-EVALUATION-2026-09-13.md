# Netwatch infrastructure evaluation

Senior network administration perspective · 13 September 2026

**Recommendation:** keep Netwatch Hub as the operator workspace and Farm Netwatch as the site appliance. Keep WireGuard for restricted management connectivity. Assign one authoritative monitoring engine to each check, centralize incidents and make collection health visible. Prioritize customer isolation and authenticated site access before expanding remote automation.

This evaluation builds on the earlier source and selected runtime review. It is a proposed design, not a new full deployment audit. Changes below have not been deployed. Client hardware constraint: a low-cost Pi 5 with a 1 GB RAM budget and SD card or USB flash storage. Site count and staffing remain unconfirmed; effort estimates are planning ranges, not commitments.

## Managed scope and MikroTik SSH integration

**Current decision: defer Zabbix.** It could help the service operator with reusable network metrics, trends and dependency alerts, but it is not required for MikroTik SSH management. For this project, prioritize the existing Netwatch stack and a focused MikroTik integration. Reconsider a central monitoring engine only when a measured requirement exceeds the existing solution. Earlier references to a Zabbix pilot are optional future alternatives, not the rollout baseline.

### Monitor only the equipment under support

Use an explicit managed-asset list: cameras, NVRs, routers, switches, wireless bridges/APs and other network equipment included in the client service. Exclude unrelated phones, laptops, printers and household/office devices from routine checks, alerts, reports and billable counts. Keep minimal Pi/VPN self-health because failure of the monitoring path affects visibility.

Discovery is an onboarding/manual tool limited to approved network ranges. Discovered assets enter an unapproved list and do not automatically become monitored. Record scope as managed, discovered/unapproved or excluded; authorize support actions only for managed targets. A DHCP/ARP table may be used on demand to diagnose a camera address issue without making every table entry a monitored client asset.

### Add a MikroTik device panel to Netwatch Hub

| Function | Proposed behavior |
|---|---|
| Overview | RouterOS version/model, uptime, CPU/memory, supported health sensors and last successful read. |
| Interfaces | Link state, negotiated speed, errors and counter-derived traffic rates on relevant ports. |
| Camera network diagnosis | On-demand DHCP leases, ARP, bridge host table and relevant VLAN/route information. |
| Router-origin diagnostics | Bounded ping/traceroute from the MikroTik to distinguish router-to-camera failures from Pi-to-camera failures. |
| Backups | Redacted text export for comparison; protected recovery backup with model/version metadata and verified transfer. |
| Operator SSH | An authorized interactive terminal session to the selected router, with expiry and session metadata. |
| Reviewed changes | Explicit actions for DHCP reservations, interface comments, selected interface/PoE controls where supported, and planned reboot/configuration changes. |
| Advanced diagnostics | Torch, bandwidth tests and wireless scans only on demand, with duration/output limits and model/version capability checks. Some diagnostics consume resources or interrupt service. |

RouterOS supports CLI management over SSH. Available commands and output differ by RouterOS version, installed packages and hardware, so discover capabilities and maintain version-aware parsers before enabling action buttons. Do not assume every router has PoE output, Wi-Fi or identical health sensors. [RouterOS CLI documentation](https://help.mikrotik.com/docs/spaces/ROS/pages/328134/Command+Line+Interface).

### Keep work central and the Pi small

Preferred management path: technician → authenticated Hub → WireGuard → restricted site relay → MikroTik SSH. The central SSH client verifies the router host key and authenticates end-to-end; the Pi only relays permitted traffic. Do not expose relay ports to the general LAN, use unrestricted forwarding or enable SSH agent forwarding. The existing relay implementation needs its reviewed access-control fixes before reuse.

Keep management credentials centrally protected; use separate per-device/site keys and a separate minimally privileged identity if the Pi needs autonomous read-only collection. Batch a small number of reads every 5–15 minutes, limit SSH concurrency and output size, and collect intensive diagnostics only on demand. The Pi does not need Zabbix or another monitoring daemon for this. Optional SNMPv3 can later provide routine counters if supported and authorized; SSH remains sufficient for the initial integration.

### Separate observation from changes

Create a custom RouterOS monitoring group with the minimum tested policies, beginning with SSH and read. Diagnostic policies such as test and sniff grant broader capabilities and should be separate where practical. Do not assume the built-in read group is sufficiently restricted: MikroTik documents that it includes sensitive and reboot permissions. RouterOS policy grants are coarse; Hub action restrictions add workflow control but do not reduce the power of a compromised write-capable router credential. [RouterOS user policies](https://manual.mikrotik.com/docs/authentication-authorization-accounting/user/).

Use distinct named technician access for interactive administration and a separate controlled change identity for automation. Log the operator, customer, device, action, redacted result and time. Do not offer raw write commands to ordinary monitoring users. Treat a terminal as privileged access, not as a read-only diagnostic button.

For risky routing/firewall/address changes, capture pre-change configuration, show a reviewable change and require a working recovery path. RouterOS Safe Mode can help with supported interactive changes, but it is not a universal rollback guarantee for every automated command or firmware operation. Test actual failure behavior on the supported versions before depending on it.

A text export is useful for diffs but is not a complete recovery copy: MikroTik documents exclusions such as passwords, certificates and SSH keys. Keep appropriate encrypted binary backups and separately cover excluded artifacts as required; validate restoration on the intended model/version. Do not restore a binary backup across arbitrary models or versions. [Configuration management](https://manual.mikrotik.com/docs/getting-started/configuration-management/).

**Delivery order:** managed-asset filtering; MikroTik read-only overview and bounded diagnostics; configuration export/backup; authorized terminal access; then a small set of reviewed change actions. Inventory models and RouterOS versions before implementation. These are proposed capabilities, not deployed functionality.

## Client hardware constraint: lean site appliance

**This constraint supersedes the earlier recommendation for a Zabbix proxy at each site.** Design for 1 GB RAM and flash storage. Keep central monitoring services off the client appliance. No additional client hardware is required by this proposal; supported device count must be measured on the actual hardware.

| Runs at the client | Runs centrally |
|---|---|
| Minimal headless OS; one lean Netwatch service; WireGuard client | Hub, Kuma, dashboards and incident processing; Zabbix deferred |
| Lightweight scheduled probes, selected device API/SNMP reads | Long-term metrics, searchable logs, reports and automation orchestration |
| Small bounded telemetry queue and configuration | Backup archives, audit retention, identity management and customer records |

Do not install a Zabbix proxy, Prometheus server, Grafana, Loki, local Kuma or wg-easy on the standard client image. WireGuard should have one owner, preferably the host service; the UI calls a restricted helper when changes are necessary. Retain Docker only if measured overhead fits the budget; it is not necessary to rewrite the deployment immediately. Avoid running both container and host VPN managers.

Proposed initial limits, to validate rather than promise:

- Keep at least 200 MB of RAM available during normal polling and test peak discovery, reconnect and update workloads. Record memory pressure and out-of-memory events. Avoid a large swap file on flash; swap is not a substitute for a fitting workload.
- Send one compact batch of observations every 60 seconds. Poll critical availability every 60 seconds; slower metrics every 5–15 minutes. Use onboarding/manual discovery on approved ranges; do not repeatedly inventory unrelated devices. Keep deep scans disabled by default and run only one scan/task at a time. Tune for actual device count and link capacity.
- Bound the local telemetry queue to 32 MB and at most 24 hours, whichever is reached first. These caps do not guarantee 24-hour coverage: retention depends on sample volume. Prefer state changes over repetitive healthy samples when space is scarce; count drops and expose gaps centrally.
- Batch persistent writes, initially every 1–5 minutes, with immediate persistence reserved for critical configuration/security changes. A sudden power failure can lose the unflushed telemetry batch; make that tradeoff explicit. Use bounded transactions and power-loss-safe configuration replacement; test storage recovery after abrupt shutdown.
- Limit local diagnostic logs to approximately 20 MB and rotate them. Do not retain packet captures, video, photographs or long-term metric history locally by default. Disable continuous device Syslog ingestion on the standard image.
- Alert centrally on storage pressure, queue drops, last successful collection, clock errors and appliance restart loops. Bound queue/database journal growth and keep free-space headroom for updates.

Receive telemetry through authenticated HTTPS over the VPN using a unique site identity. The agent reads local devices, so central services do not need broad access to every client subnet; this also avoids ambiguity when farms reuse LAN addresses. Integrate collected metrics with the central engine rather than adding another collector process to every Pi. This is a proposed adapter/queue change, not functionality already implemented.

During WAN loss, the appliance continues lightweight checks and bounded buffering. The hub reports visibility lost. After reconnection it accepts deduplicated observations with their original timestamps and shows any dropped interval as unknown. Local operation does not guarantee remote notifications during a complete connectivity failure.

**Acceptance gate:** benchmark a representative client inventory for 72 hours on the exact 1 GB/flash setup, including a disconnected period, queue saturation, reconnect, scan, update and power-loss recovery. Check memory, free space, write volume, poll delays and data gaps before setting a supported device limit.

## 1. Centralized Monitoring & Telemetry

### Give each component a clear job

| Component | Recommended responsibility |
|---|---|
| Netwatch Hub | Customer/site inventory, incident ownership, fleet health, remote task results and monthly service reports. |
| Farm Netwatch | Local discovery, farm-specific device diagnostics, safe management tasks and offline collection. |
| Uptime Kuma, central only | Selected availability checks; migrate existing local checks only after validating their central replacements and observation path. |
| Zabbix, deferred | Future option only if measured monitoring needs justify an additional central system. |
| Syslog collector, central only | Selected device/service events where an approved forwarding path exists; no dedicated local collector by default. |

For the constrained client, start with the existing Netwatch agent collecting lightweight observations and sending them centrally. Defer Zabbix; reconsider it only on central infrastructure if its templates and incident handling later justify the integration work. Its proxy capability is useful on larger hardware but is excluded from this client design. [Zabbix proxy documentation](https://www.zabbix.com/documentation/current/en/manual/distributed_monitoring/proxies).

Do not introduce a Prometheus server at each site. If application metrics later justify Prometheus centrally, reuse a bounded agent/export path with explicit failure handling. Choose one authoritative engine per check and avoid duplicate collection.

### Collect service health and collection health

| Area | Useful measurements | Starting interval |
|---|---|---|
| Network equipment | SNMPv3 authPriv where supported: interface utilization, errors/discards, device uptime, temperature, PoE status and radio signal/noise/capacity | 1–5 minutes |
| WAN | Gateway reachability, loss/latency to independent upstreams, DNS and HTTPS checks | 60 seconds |
| Cameras/NVRs | Reachability, channel/recording faults, disk health and storage state through supported device APIs | 1–5 minutes |
| Site appliance | CPU, memory, free disk, clock offset, restart count, storage errors and local queue age | 1–5 minutes |
| Netwatch services | Last successful scan, heartbeat age, poll scheduling delay, task backlog, API errors and backup age | 60 seconds |
| VPN | Active probe, handshake age interpreted with keepalive, and traffic counters | 60 seconds |

Treat legacy SNMP as a restricted read-only service on the management network. Limit polling to approved inventory; do not continually deep-scan all ports. A camera replying to ping is not proof that footage is being recorded. Unsupported recording checks must show “unverified.”

Make central Syslog optional for devices with a configured management route or a lightweight, rate-limited forwarding path. Do not add a dedicated site logging stack. Prefer selected fault/state events over debug streams. Where devices support only local UDP Syslog, collection may be omitted on this hardware profile; record that visibility limitation. UDP losses cannot be recovered downstream. Normalize customer/site/device identity and timestamps, synchronize clocks, redact credentials and enforce customer-specific access centrally.

Start with three dashboard views:

- **Daily operations:** unacknowledged incidents, lost visibility, overdue backups, failed tasks and expiring certificates.
- **Site investigation:** topology, WAN/VPN/appliance states, affected devices, recent changes and correlated logs.
- **Monthly client report:** service availability, monitoring coverage, unknown periods, incidents resolved and maintenance performed.

Show healthy, degraded, down, unknown and maintenance separately. Replace received-sample averages with timestamped availability intervals and publish collection coverage alongside availability. On central storage only, begin with provisional retention of 30 days detailed metrics, 12 months aggregates and 30 days searchable operational logs; measure storage and support needs before extending. Give audit records their own retention policy.

## 2. Proactive Alerting

### Alert on service impact, then attach diagnostic evidence

Model dependencies explicitly: gateway → backhaul radio → switch → cameras. If a radio fails, group the affected cameras into its incident. Preserve downstream observations and re-evaluate after recovery; suppression should reduce notifications, not erase evidence. Zabbix supports trigger dependencies; Prometheus Alertmanager supports grouping, inhibition, silences and routing. Choose one incident source for each service. [Zabbix dependencies](https://www.zabbix.com/documentation/current/en/manual/config/triggers/dependencies), [Alertmanager](https://prometheus.io/docs/alerting/latest/alertmanager/).

These are initial tuning values, not universal limits:

| Condition | Starting rule | Handling |
|---|---|---|
| Site heartbeat missing | Three missed 60-second observations | “Visibility lost”; do not assert a complete client outage. |
| WAN quality degraded | Packet loss above 5% over 5 minutes with sufficient samples | Investigate sustained service impact; baseline by link type. |
| Appliance storage low | Less than 15% free for 15 minutes; escalate below 5% | Combine with write rate and estimated time to full. |
| Configuration backup stale | No successful daily backup for 26 hours | Create a support task; escalate if recovery exposure persists. |
| Recording failure | Device API explicitly reports a fault, confirmed where safe | High priority based on client coverage and affected channels. |
| Flapping device | Repeated transitions, e.g. five within 30 minutes | One instability incident rather than repeated outage pages. |

Require stable recovery, initially two successful checks. Use different recovery thresholds for noisy metrics. Schedule maintenance silences with an owner and expiry. Do not treat NVR circular storage utilization like an ordinary disk-full alarm.

Every alert should include the client/site, affected service, first failure, last good observation, evidence, runbook link and assigned owner. Separate detection, acknowledgement, work started and restoration timestamps.

Use a simple severity workflow: critical service impact goes to the staffed responder; persistent degradation enters the support queue; capacity and maintenance issues enter a daily digest. As a pilot, escalate an unacknowledged critical incident after 15 minutes **during covered hours and only to an actually staffed backup contact**. Notification delivery is not acknowledgement. Acknowledgement stops duplicate paging but does not close the incident.

Review noisy rules weekly: count actionable incidents, false positives, repeats and unacknowledged time. Test notification delivery and run an independent external check of the hub and its alert pipeline. Prometheus recommends actionable symptoms and monitoring the monitoring system itself. [Alerting guidance](https://prometheus.io/docs/practices/alerting/).

## 3. Remote Management & Automation

### Turn existing onboarding into controlled enrollment

The existing installer and VPN enrollment provide a useful starting point. Extend them with a short-lived, single-use token bound to one customer/site. Generate the appliance's private keys locally, validate server identity and enroll only into the assigned policy. Keep enrollment secrets out of shared scripts and persistent shell history. Reinstallation must not silently produce duplicate sites or monitors.

Use versioned site templates for approved subnets, device roles, monitor intervals, alert policies and software versions. Keep desired inventory centrally and discovered inventory separately until approved. Automated provisioning begins after the appliance has basic power/network access; retain a documented local bootstrap path for sites without working DHCP or internet.

### Expand backups beyond Netwatch settings

The existing daily site bundles help restore configuration, but do not cover the complete monitoring history or Kuma database. Add device configuration backups where vendor APIs/SSH support reliable exports. Track success, age, checksum and configuration differences. Encrypt secret-bearing backups, store an independent offsite copy and test restores onto spare equipment. Capture backups before and after important changes. Keep secrets out of ordinary Git repositories and diff displays.

### Make bulk work bounded and reviewable

Begin automation with inventory checks, configuration export, backup validation and read-only diagnostics. Then add staged application updates. Use Ansible or an equivalent managed runner with tenant-scoped inventory, pinned automation content and restricted credentials. Ansible's `serial` execution supports batches rather than changing every host at once. [Execution strategies](https://docs.ansible.com/projects/ansible/latest/playbook_guide/playbooks_strategies.html).

Each task needs a target list, preflight result, timeout, concurrency cap, unique job ID, operator, result and rollback instructions. Retry only operations known to be safe to repeat. Expire queued changes so that a device reconnecting next week cannot execute an obsolete repair.

Roll out changes to one lab appliance, one pilot site, then a small batch before the remainder. Stop on failed health checks. Never update all upstream radios or VPN gateways together. Require explicit change review for addressing, routing, firewall, firmware and disruptive device actions; a dry run does not prove hardware changes are safe. Disable the current ntfy command listener by default and move actions into authenticated, audited workflows.

## 4. Operational Resilience & Security

### Close the known access gaps first

The earlier review found an unauthenticated site API exposing credential/configuration operations, a shared hub administrator identity, open relay listeners and permissive forwarding in the VPN container's FORWARD chain. That chain is not proof of end-to-end exposure, but it does not establish customer separation.

- Require authentication for site reads and writes, using separate scoped machine credentials for hub collection. Protect configuration exports as secrets.
- Add named users, MFA, administrator/technician/customer-viewer roles and server-side customer checks on every endpoint, report and job.
- Enforce default-deny cross-client forwarding. Permit only required hub/site services and technician access to assigned targets. Restrict IPv4 and IPv6 paths; WireGuard AllowedIPs is not a substitute for authorization.
- Separate management, cameras/IoT and user/guest networks with routed ACLs. A VLAN without filtering is insufficient. Restrict the probe to necessary device protocols.
- Place remote relays behind an authenticated management path; enforce expiry, per-target rules and revocation. Protect usage of a tunnel as well as its creation.
- Separate privileged network operations from the web process. Use production serving with one explicit owner for background scanners and pollers.

### Recover reliably before adding complex failover

Use dedicated, maintained central infrastructure, UPS coverage at key sites, independent monitoring and encrypted offsite recovery copies. Separate VPN gateway lifecycle from Hub/proxy lifecycle so routine VPN maintenance does not unnecessarily recreate all management services.

Test a spare-appliance restore and hub recovery. Record achieved recovery time and data loss. Consider dual WAN and redundant central services only where the service commitment justifies their cost. A second VPN endpoint needs a tested routing, identity and failover design; a second container on the same host is not failure-domain redundancy.

Record who accessed which client, read/exported secrets, opened tunnels, changed configuration, restored backups and ran jobs. Include timestamp, reason, result and redacted before/after changes. Forward security audit records to a separate restricted store with tamper-resistant retention. Ordinary debug logs are not an audit trail. Test offboarding across user accounts, device keys, active sessions, job permissions and stored credentials.

## 5. Quick Wins vs. Long-Term Enhancements

Effort refers to a focused engineering workstream, excluding procurement and fleet rollout. Access-control and firewall changes still need a tested recovery path and compatibility checks.

| Group | Recommendation | Indicative effort | Administrative payoff / completion evidence |
|---|---|---|---|
| Quick win | Standard customer/site/device naming, ownership and support hours | Hours–2 days | Faster triage; every managed asset maps to an owner and service. |
| Quick win | Disable ntfy remote commands; restrict existing management ports | 1–3 days | Smaller exposure; authorized polling/support still works and untrusted access fails. |
| Quick win | Backup freshness alerts, external hub check and incident runbook links | 1–3 days | Failures surface before a client call; injected failures create assigned incidents. |
| Quick win | Maintenance windows, persistence and stable recovery thresholds | 2–5 days | Fewer repeat notifications; measure reduction against the pilot baseline. |
| Near term | Authenticated site API and scoped hub credentials | 1–2 weeks | Protects secrets and changes; unauthorized operations fail without breaking collection. |
| Near term | Client isolation rules and restricted tunnel access | 1–2 weeks | Client A cannot access client B; authorized technician paths and recovery remain functional. |
| Near term | Named accounts, MFA and customer permissions across APIs/jobs | 2–4 weeks | Traceable support access; revoked and unassigned accounts fail access tests. |
| Near term | Central telemetry pilot, useful dashboards and dependency alerts | 1–3 weeks | One incident per upstream fault; device evidence and logs available in one investigation flow. |
| Near term | Tested backups and staged Kuma/wg-easy migrations | 1–3 weeks | Known recovery procedure and verified integration behavior on candidate releases. |
| Longer term | Durable telemetry replay, truthful coverage reporting and task queue | 3–6 weeks | WAN interruptions preserve observations without executing stale changes. |
| Longer term | Secure enrollment, desired-state templates and staged fleet automation | 3–6 weeks | Repeatable onboarding and bulk work with recorded per-site results. |
| Conditional | Redundant central services, alternate connectivity and failover | 4–8+ weeks | Adopt only when demonstrated recovery limits conflict with contracted service needs. |

**Recommended order:** immediate access containment and independent monitoring; authenticated site access and client isolation; incident quality and backup restoration; centralized telemetry pilot; controlled automation; then redundancy where justified. Do not let a quick-win label delay critical access controls.

For the first 30-day pilot, track support minutes per site, actionable-alert ratio, monitoring coverage, overdue backups, median acknowledgement time and successful remote resolution rate. Use these measurements to decide whether the improvements actually simplify administration and support a profitable monthly service.
