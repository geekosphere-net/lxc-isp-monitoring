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
**Update:** exec into the LXC and run `update`

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

**Update:** run `update` inside the machine.

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

The local dashboard (served from `dashboard/`) is a two-tab interface:

### Real-Time tab

| Element | Detail |
|---|---|
| **Status bar** | HTTP / WebRTC / DNS indicator dots — green (up) or red (down) — with latest RTT and a 30 s refresh countdown |
| **Stats header** | Last · Min · Max · Avg · Loss for the selected probe; HTTP \| WebRTC toggle |
| **Live grid** | 12 rows × 60 cells — each cell is the 5-second average of all pings in that window, giving 1 hour of history with the newest row at top |

Cell colours follow ITU-T G.1010 thresholds:

| Colour | Meaning |
|---|---|
| Green | avg RTT < 100 ms |
| Yellow | avg RTT 100–300 ms |
| Orange | avg RTT > 300 ms |
| Red | ≥ 50 % packet loss |
| Grey | no data yet |

Hover any cell for an exact tooltip: time range · avg RTT · loss %.

### Historical tab

| Element | Detail |
|---|---|
| **Statistics** | Uptime %, avg/min/max RTT, packet loss for all three probe types; selectable 1 h / 24 h / 7 d window |
| **Hourly heatmap** | 24 cells — one per hour — for the last 24 hours, same RTT colour scale |
| **Daily calendar** | GitHub-style grid: rows = weeks, columns = Mon–Sun, last 30 days; instantly shows weekly patterns |
| **Recent outages** | Table of gaps > 5 s with no successful ping, last 7 days |

An HTTP \| WebRTC probe toggle switches both the hourly and daily heatmaps simultaneously.

---

## How It Works

`app.py` is a FastAPI application. On startup it launches four async background tasks:

1. **`http_ping_loop`** — POSTs a random number to `/api/ping`, verifies the body is echoed back, records RTT
2. **`webrtc_ping_loop`** — negotiates a WebRTC session via `/new_rtc_session`, then sends timestamp pings over a UDP data channel; reconnects with exponential backoff on failure
3. **`dns_check_loop`** — GETs `https://{rand}dns-check.pinging.net/api/dns-check`, verifies the random number is echoed back
4. **`prune_loop`** — deletes rows older than `RETENTION_DAYS` once per hour

All results land in a `pings` table in SQLite. The REST API serves five query endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /api/results?minutes=N` | Raw ping rows for the last N minutes |
| `GET /api/stats?hours=N` | Aggregate uptime / RTT / loss per probe type |
| `GET /api/hourly?hours=N` | Per-hour avg RTT / uptime / loss (default 24 h) |
| `GET /api/daily?days=N` | Per-day avg RTT / uptime / loss (default 30 days) |
| `GET /api/outages?days=N` | Outage events (gaps > 5 s, default 7 days) |

The dashboard fetches raw results every 5 s (for the live grid), refreshes the status bar and stats header every 30 s, and loads hourly/daily heatmap data on demand when the Historical tab is opened.

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

## WebRTC Implementation Notes

The original [pinging.net](https://pinging.net) client is a browser — it uses the browser's native WebRTC stack. This project replicates that with [aiortc](https://github.com/aiortc/aiortc), a Python WebRTC library. The pinging.net server uses [webrtc-unreliable](https://github.com/kyren/webrtc-unreliable) v0.6, a Rust crate that is data-channel-only and non-standard in several ways. Getting aiortc to work against it required four workarounds, all applied as monkey-patches at startup in `app.py`:

### 1 — SDP size limit (413 Payload Too Large)

**Problem:** aiortc eagerly gathers ICE candidates inside `createOffer()` and includes three DTLS fingerprint algorithms (sha-256/384/512), producing an SDP offer of ~1 kB. pinging.net's `/new_rtc_session` endpoint rejects payloads over ~500 bytes.

**Fix (in `_webrtc_session()`):** Strip all `a=candidate:` and `a=end-of-candidates` lines and all non-sha-256 `a=fingerprint:` lines before sending. Also replace `a=setup:actpass` with `a=setup:active` to ensure aiortc initiates the DTLS handshake (the server always responds with `a=setup:passive`). The stripped SDP is ~410 bytes, matching what a browser sends.

### 2 — Cipher suite mismatch (empty DTLS handshake failure)

**Problem:** aiortc configures its DTLS SSL context with ECDHE-ECDSA-only cipher suites. The pinging.net server presents an RSA certificate. A server with an RSA cert cannot negotiate any ECDSA cipher suite, so it sends a bare `handshake_failure` alert immediately — aiortc reports this as `DTLS handshake failed (error )` with an empty error string.

**Fix:** Monkey-patch `OpenSSL.SSL.Context.set_cipher_list` so that whenever aiortc sets an ECDSA-only list, the RSA equivalents (replacing `ECDSA` with `RSA` in each suite name) are appended automatically. Browsers work because they offer both families; this patch makes aiortc do the same.

### 3 — Zero serial number certificate (cryptography 42+ rejects server cert)

**Problem:** The webrtc-unreliable Rust crate generates self-signed DTLS certificates with `serial=0`, which is invalid per RFC 5280. `cryptography` 42+ raises a `ValueError` when loading such a certificate. This happens inside aiortc's DTLS fingerprint-verification step (`OpenSSL.crypto.X509.to_cryptography()`), causing the connection to fail immediately after a successful handshake.

**Fix:** Monkey-patch `OpenSSL.crypto.X509.to_cryptography` to catch the exception and fall back to loading the certificate from raw DER bytes via `cryptography.x509.load_der_x509_certificate()`, which bypasses the serial-number validation. Fingerprint verification still succeeds because the DER bytes are intact.

### 4 — Missing SRTP profile (data-channel-only server)

**Problem:** aiortc unconditionally calls `_setup_srtp()` after every DTLS handshake and raises `"DTLS handshake failed (no SRTP profile negotiated)"` if the peer didn't negotiate an SRTP profile via the `use_srtp` DTLS extension. webrtc-unreliable is a pure data-channel server and never sends `use_srtp`, so this check always fails.

**Fix:** Monkey-patch `RTCDtlsTransport._setup_srtp` to return immediately without doing anything. The SRTP keying material it would derive is only used for audio/video media tracks; an SCTP data channel runs directly over the DTLS record layer and never touches SRTP.

---

## Credits

- [pinging.net](https://pinging.net) / [benhansen-io/pinging](https://github.com/benhansen-io/pinging) — the upstream connectivity testing service this monitor targets
- [community-scripts/ProxmoxVE](https://github.com/community-scripts/ProxmoxVE) — Proxmox helper script framework and conventions

## License

MIT
