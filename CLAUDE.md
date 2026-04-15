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
- `GET /api/hourly?hours=N` — per-hour avg RTT / uptime / loss for HTTP and WebRTC (default 24 h)
- `GET /api/daily?days=N` — per-day avg RTT / uptime / loss for HTTP and WebRTC (default 30 days)
- `GET /api/outages?days=N` — outage events (gaps > 5 s with no successful ping)
- `/` — static dashboard (mounted last)

### Dashboard (`dashboard/`)

Vanilla HTML/CSS/JS — no build step. Two-tab interface:

**Real-Time tab** (default):
- Status bar — HTTP / WebRTC / DNS dots (green = up, red = down) with latest RTT; 30 s refresh countdown
- Stats header — Last / Min / Max / Avg / Loss for the active probe (HTTP | WebRTC toggle); updates every 30 s
- Live grid — 12 rows × 60 cells; each cell = 5 s average of all pings in that window; 1 hour of history; newest row at top; grid re-renders every 1 s from cached data; data fetched every 5 s
  - Green < 100 ms · Yellow 100–300 ms · Orange > 300 ms · Red ≥ 50% loss · Grey = no data
  - Hover any cell for a tooltip: time range · avg RTT · loss %

**Historical tab**:
- Statistics panel — uptime %, avg/min/max RTT, packet loss for HTTP / WebRTC / DNS; 1 h / 24 h / 7 d window selector
- Latency heatmap — hourly strip (24 cells, last 24 h) + daily calendar grid (week rows × day-of-week columns, last 30 days); HTTP | WebRTC probe toggle applies to both
- Recent outages — table of gaps > 5 s with no successful ping, last 7 days

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

---

## WebRTC — Fully Working (resolved 2026-04-14)

All three monitoring loops (HTTP, WebRTC, DNS) are confirmed working on CT 113.

### The problems we fixed and why

pinging.net's server uses [webrtc-unreliable](https://github.com/kyren/webrtc-unreliable)
v0.6, a Rust crate that is data-channel-only and non-standard in several ways. aiortc is
designed for media WebRTC and assumes a compliant peer. Four incompatibilities needed
monkey-patching, applied at module level in `app.py` before any session is created:

| # | Problem | Root cause | Fix |
|---|---|---|---|
| 1 | SDP 413 Payload Too Large | aiortc embeds ICE candidates + 3 fingerprints in the offer (~1 kB); pinging.net rejects anything over ~500 B | Strip `a=candidate:`, `a=end-of-candidates`, and non-sha-256 `a=fingerprint:` lines before POSTing; replace `a=setup:actpass` with `a=setup:active` |
| 2 | `DTLS handshake failed (error )` — empty | aiortc offers only ECDHE-ECDSA ciphers; server has an RSA cert → `handshake_failure` alert | Patch `SSL.Context.set_cipher_list` to append RSA equivalents of every ECDSA suite |
| 3 | `CryptographyDeprecationWarning` + connection fail | Server cert has serial=0 (invalid per RFC 5280); cryptography 42+ raises on load | Patch `OpenSSL.crypto.X509.to_cryptography` to catch the exception and reload via raw DER |
| 4 | `DTLS handshake failed (no SRTP profile negotiated)` | aiortc unconditionally requires SRTP negotiation even for pure data-channel connections; pinging.net never offers `use_srtp` | Patch `RTCDtlsTransport._setup_srtp` to unconditionally return (SRTP keying material is unused for SCTP) |

All four patches are in the `# Monkey-patch` blocks near the top of `app.py`.
