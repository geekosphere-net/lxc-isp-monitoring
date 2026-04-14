# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

LXC ISP Monitor is a self-hosted connectivity monitoring daemon designed to run inside a Proxmox LXC container. It continuously tests internet connectivity via HTTP pings, WebRTC UDP pings, and DNS checks against [pinging.net](https://pinging.net), stores results in SQLite, and serves a local web dashboard.

## Repository Layout

```
app.py                              # FastAPI daemon — monitoring loops + API
requirements.txt                    # Python dependencies
setup.sh                            # Manual install script (Debian 12 LXC)
dashboard/
  index.html                        # Dashboard UI
  index.css
  index.js
proxmox/
  pinging-monitor.json              # Proxmox community-scripts app manifest
  ct/pinging-monitor.sh             # Proxmox helper script (install + update entry point)
  install/pinging-monitor-install.sh # LXC container install steps
```

## Running Locally (dev)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app:app --reload --port 8080
# Dashboard: http://localhost:8080
```

## Installing in a Proxmox LXC

Run `setup.sh` inside a fresh Debian 12/13 LXC:

```bash
bash setup.sh
```

This installs dependencies, creates a Python venv at `/opt/pinging-monitor`, writes a systemd service, and starts it. The dashboard is served at `http://<lxc-ip>:8080`.

Logs: `journalctl -u pinging-monitor -f`

## Architecture

### `app.py`

FastAPI app with four async background tasks (started via `lifespan`):

- **`http_ping_loop`** — POST to `https://pinging.net/api/ping` every `PING_INTERVAL` seconds, verifies echo
- **`webrtc_ping_loop`** — maintains a WebRTC data channel session to pinging.net:8888 via aiortc; re-connects with exponential backoff on failure
- **`dns_check_loop`** — GET to a random subdomain on `dns-check.pinging.net` every `DNS_INTERVAL` seconds
- **`prune_loop`** — deletes SQLite rows older than `RETENTION_DAYS` once per hour

Results are recorded in a SQLite database (`pings` table).

API routes:
- `GET /api/results?minutes=N` — raw ping rows for the last N minutes
- `GET /api/stats?hours=N` — uptime %, packet loss %, min/avg/max RTT per test type
- `GET /api/outages?days=N` — outage events (gaps > 5 s with no successful ping)
- `/` — static dashboard (mounted last)

### Dashboard (`dashboard/`)

Vanilla HTML/CSS/JS — no build step. Polls the API every 30 seconds and renders:
- Status cards (HTTP / WebRTC / DNS) with current RTT
- Statistics table with 1h / 24h / 7d window selector
- 24h timeline grid (1 cell = 1 minute)
- Recent outages table

### Proxmox Scripts (`proxmox/`)

Follow the [community-scripts/ProxmoxVE](https://github.com/community-scripts/ProxmoxVE) conventions:
- `ct/pinging-monitor.sh` — sourced by the Proxmox helper; handles `update_script()` logic
- `install/pinging-monitor-install.sh` — runs inside the new LXC to clone the repo, set up the venv, and install the systemd service
- `pinging-monitor.json` — app manifest (name, resources, notes) for the community-scripts UI

## Configuration

All options are environment variables set in the systemd service unit (`/etc/systemd/system/pinging-monitor.service`):

| Variable | Default | Purpose |
|---|---|---|
| `TARGET_HOST` | `https://pinging.net` | Base URL for HTTP/WebRTC pings |
| `DB_PATH` | `/var/lib/pinging-monitor/monitor.db` | SQLite database path |
| `PING_INTERVAL` | `1.0` | Seconds between HTTP/WebRTC pings |
| `DNS_INTERVAL` | `30.0` | Seconds between DNS checks |
| `RETENTION_DAYS` | `30` | Days of history to retain |
| `WEBRTC_ENABLED` | `true` | Set to `false` to disable WebRTC pings (HTTP + DNS still run) |

After editing the unit file, reload with:
```bash
systemctl daemon-reload && systemctl restart pinging-monitor
```

## Dependencies

- `aiortc` — WebRTC peer connection (optional; HTTP pings still run if missing)
- `httpx` — async HTTP client
- `fastapi` + `uvicorn` — web server
- `aiosqlite` — async SQLite
