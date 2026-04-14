# LXC ISP Monitor

A 24/7 internet connectivity monitor that runs inside a Proxmox LXC container, built on top of the public [pinging.net](https://pinging.net) service by [Ben Hansen](https://github.com/benhansen-io/pinging).

---

## What This Is

[pinging.net](https://pinging.net) is an open-source connectivity monitoring service that simultaneously runs multiple tests to tell you whether you are online and how healthy your connection is:

- **WebRTC UDP ping** — a data channel ping every second, behaves like a traditional ICMP ping
- **HTTP test** — a POST with a random body every 30 seconds, verifies round-trip correctness
- **DNS check** — a GET to a random subdomain every 30 seconds, verifies DNS resolution end-to-end

The original project is designed to be used from a browser. This repo packages a Python daemon that replicates the same three tests from the command line (using [aiortc](https://github.com/aiortc/aiortc) for WebRTC and [httpx](https://www.python-h.org/httpx/) for HTTP/DNS), stores results in SQLite, and serves a local web dashboard — all inside a lightweight Proxmox LXC container that runs continuously.

The result is a persistent, always-on uptime monitor for your ISP connection, viewable at `http://<lxc-ip>:8080`.

---

## Repository Layout

```
app.py                               # FastAPI daemon — monitoring loops + REST API
requirements.txt                     # Python dependencies
setup.sh                             # Manual install script (Debian 12/13 LXC)
dashboard/
  index.html                         # Dashboard UI
  index.css
  index.js
proxmox/
  pinging-monitor.json               # Community-Scripts app manifest
  ct/pinging-monitor.sh              # community-scripts conformant ct entry-point
  install/pinging-monitor-install.sh # community-scripts conformant install script
  testing/                           # ⚠ DELETE before submitting PR to community-scripts
    deploy.sh                        # Standalone deploy script for testing from this repo
```

---

## Quick Install (Proxmox)

From the Proxmox host shell:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/geekosphere-net/lxc-isp-monitoring/main/proxmox/testing/deploy.sh)
```

This creates a Debian 13 LXC with 1 CPU / 512 MB RAM / 4 GB disk, installs the monitor, and starts it automatically.

**Dashboard:** `http://<lxc-ip>:8080`  
**Logs:** `journalctl -u pinging-monitor -f` (inside the LXC)

### Overriding defaults

```bash
CT_ID=120 CT_STORAGE=local-zfs CT_BRIDGE=vmbr1 bash <(curl -fsSL ...)
```

Available environment variables: `CT_ID`, `CT_HOSTNAME`, `CT_STORAGE`, `CT_TMPL_STORAGE`, `CT_DISK`, `CT_RAM`, `CT_CPU`, `CT_BRIDGE`, `CT_OS`, `CT_VERSION`.

---

## Manual Install (any Debian 12/13 machine or LXC)

```bash
git clone https://github.com/geekosphere-net/lxc-isp-monitoring.git
cd lxc-isp-monitoring
bash setup.sh
```

---

## Configuration

Edit the systemd service unit inside the LXC:

```bash
nano /etc/systemd/system/pinging-monitor.service
systemctl daemon-reload && systemctl restart pinging-monitor
```

| Environment Variable | Default | Description |
|---|---|---|
| `TARGET_HOST` | `https://pinging.net` | Base URL for HTTP and WebRTC pings |
| `DB_PATH` | `/var/lib/pinging-monitor/monitor.db` | SQLite database path |
| `PING_INTERVAL` | `1.0` | Seconds between HTTP/WebRTC pings |
| `DNS_INTERVAL` | `30.0` | Seconds between DNS checks |
| `RETENTION_DAYS` | `30` | Days of history to keep (older rows pruned hourly) |
| `WEBRTC_ENABLED` | `true` | Set to `false` to disable WebRTC pings (HTTP + DNS still run) |

> **Note:** WebRTC pings require outbound UDP to `pinging.net:8888`. If your network blocks UDP egress, HTTP and DNS tests still run.

---

## Dashboard

The local dashboard (served from `dashboard/`) shows:

- **Status cards** — live HTTP / WebRTC / DNS state with current RTT
- **Statistics** — uptime %, packet loss %, min/avg/max RTT across selectable 1h / 24h / 7d windows
- **24h timeline** — 1 cell per minute, colour-coded: good / degraded / down / no data
- **Recent outages** — table of gaps > 5 s with no successful ping, last 7 days

---

## How It Works

`app.py` is a FastAPI application. On startup it launches four async background tasks:

1. **`http_ping_loop`** — POSTs a random number to `/api/ping`, verifies the body is echoed back, records RTT
2. **`webrtc_ping_loop`** — negotiates a WebRTC session via `/new_rtc_session`, then sends timestamp pings over a UDP data channel; reconnects with exponential backoff on failure
3. **`dns_check_loop`** — GETs `https://{rand}dns-check.pinging.net/api/dns-check`, verifies the random number is echoed back
4. **`prune_loop`** — deletes rows older than `RETENTION_DAYS` once per hour

All results land in a `pings` table in SQLite. The REST API (`/api/results`, `/api/stats`, `/api/outages`) queries this table and the dashboard polls it every 30 seconds.

---

## Proxmox Community-Scripts Conformance

This repo follows the [Proxmox Community-Scripts](https://community-scripts.org) conventions:

| Requirement | Status |
|---|---|
| `ct/` entry-point script with `#!/usr/bin/env bash` shebang | ✅ |
| Sources `build.func` from the community-scripts repo | ✅ |
| Declares `APP`, `var_tags`, `var_cpu`, `var_ram`, `var_disk`, `var_os`, `var_version`, `var_unprivileged` | ✅ |
| Calls `header_info`, `variables`, `color`, `catch_errors`, `start`, `build_container`, `description` | ✅ |
| Implements `update_script()` with up-to-date check and stop/update/start cycle | ✅ |
| `install/` script sources `$FUNCTIONS_FILE_PATH` | ✅ |
| Install script calls `setting_up_container`, `network_check`, `update_os`, `motd_ssh`, `customize`, `cleanup_lxc` | ✅ |
| Uses `msg_info` / `msg_ok` / `msg_error` for all user-facing output | ✅ |
| Uses `$STD` prefix to suppress verbose command output | ✅ |
| JSON manifest (`pinging-monitor.json`) with name, slug, type, resources, notes | ✅ |

---

## Credits

- [pinging.net](https://pinging.net) / [benhansen-io/pinging](https://github.com/benhansen-io/pinging) — the upstream connectivity testing service this monitor targets
- [community-scripts/ProxmoxVE](https://github.com/community-scripts/ProxmoxVE) — Proxmox helper script framework and conventions

## License

MIT
