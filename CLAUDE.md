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

---

## Active Work: WebRTC not connecting (in progress as of 2026-04-14)

**Status:** HTTP and DNS monitoring work correctly. WebRTC is still failing. The latest
`app.py` commit (`378edd6`) has diagnostic logging in place and the current best fix
attempt is deployed but not yet confirmed working.

### What we know

**The upstream browser behaviour (from [benhansen-io/pinging](https://github.com/benhansen-io/pinging) `frontend/index.ts`):**
```js
const offer = await rtcPeer.createOffer();
await rtcPeer.setLocalDescription(offer);
// Sends localDescription.sdp IMMEDIATELY — no waiting, no filtering
fetch("/new_rtc_session?...", { method: "POST", body: rtcPeer.localDescription.sdp })
```
The browser SDP has zero candidates and one `a=fingerprint:sha-256` line. Total size is
well under pinging.net's payload limit.

**The aiortc difference:**
- aiortc gathers ICE candidates eagerly **inside `createOffer()`** (before `setLocalDescription`),
  so `offer.sdp` already contains a host candidate and an srflx candidate.
- aiortc includes **three** DTLS fingerprints (sha-256, sha-384, sha-512); browsers send only sha-256.
- Together these push the SDP to ~1039 bytes, triggering a **413 Payload Too Large** from
  pinging.net's `/new_rtc_session` endpoint.

**Fix in `_webrtc_session()` (current state in app.py):**
```python
# Strip all candidates and non-sha-256 fingerprints before sending
sdp_lines = [
    line for line in offer.sdp.splitlines()
    if not line.startswith("a=candidate:")
    and not line.startswith("a=end-of-candidates")
    and not (
        line.startswith("a=fingerprint:")
        and not line.startswith("a=fingerprint:sha-256")
    )
]
sdp_to_send = "\r\n".join(sdp_lines) + "\r\n"
```
This should produce a ~480 byte SDP matching what a browser sends. **Not yet confirmed
working** — test was in flight at compact time.

**Previous failure mode (before the 413 was fixed):**
When a 200 response was received from the server, ICE negotiation completed successfully
(`ICE completed` in the aioice logs), but the WebRTC data channel never opened
(`WebRTC channel did not open within 15s`). The connection state went directly
`new → closed` (from our own `pc.close()` in the finally block) — it never reached
`connecting`, meaning DTLS never started.

### What to do after compact

1. **Check if the 413 is fixed:** Run `journalctl -u pinging-monitor -f` on CT 113
   (`192.168.4.188`). Look for `WebRTC SDP offer (NNN bytes, 0 candidate lines)` — NNN
   should be ~480. If still 413, the new code wasn't pulled; run:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/geekosphere-net/lxc-isp-monitoring/main/app.py \
     -o /opt/pinging-monitor/app.py && systemctl restart pinging-monitor
   ```

2. **If 413 is gone but channel still doesn't open:** The server response is now logged
   (`WebRTC server response: {...}`). Paste it here. Key things to check in the response:
   - `answer.sdp` — look for `a=setup:` (active = server initiates DTLS, passive = we initiate)
   - `candidate` — the ICE candidate the server sends back
   The DTLS not starting suggests either a role mismatch (server expects us to initiate but
   aiortc is waiting for server) or the server can't reach us (no candidates in our offer
   means server doesn't know our address until it sees our STUN binding requests).

3. **If DTLS is the remaining issue:** Consider patching the SDP to force
   `a=setup:active` (making aiortc initiate DTLS to the server):
   ```python
   sdp_to_send = sdp_to_send.replace("a=setup:actpass", "a=setup:active")
   ```
   This makes aiortc the DTLS client, initiating to the server after ICE — the server
   doesn't need to know our address to start DTLS.

4. **If WebRTC simply won't work with aiortc:** Set `WEBRTC_ENABLED=false` in the
   systemd service. HTTP and DNS tests are fully working and give good monitoring coverage.
   WebRTC can be revisited when submitting to community-scripts.
