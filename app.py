"""
ISP Monitor — standalone connectivity daemon + local web dashboard.

Runs three monitoring loops concurrently:
  - HTTP pings  (every 1s)  → POST https://pinging.net/api/ping
  - WebRTC pings (every 1s) → UDP via pinging.net WebRTC data channel
  - DNS checks  (every 30s) → GET https://{rand}dns-check.pinging.net/api/dns-check

Results are stored in SQLite and served via a local FastAPI dashboard.
"""

import asyncio
import json
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
# ---------------------------------------------------------------------------
# Monkey-patch: add RSA cipher suites to aiortc's DTLS SSL context.
#
# aiortc defaults to ECDHE-ECDSA-only ciphers, but pinging.net's
# webrtc-unreliable server presents an RSA certificate.  A server with an
# RSA cert cannot use any ECDSA cipher suite, so the handshake immediately
# fails with an empty handshake_failure alert (no matching cipher).
# Browsers work because they offer both ECDSA and RSA variants.
# Intercepting set_cipher_list() adds the RSA equivalents transparently,
# without modifying the installed aiortc package.
# ---------------------------------------------------------------------------
try:
    from OpenSSL import SSL as _SSL

    _orig_set_cipher_list = _SSL.Context.set_cipher_list

    def _patched_set_cipher_list(self, cipher_list: bytes) -> None:
        if isinstance(cipher_list, str):
            cipher_list = cipher_list.encode()
        if b"ECDHE-ECDSA" in cipher_list and b"ECDHE-RSA" not in cipher_list:
            rsa_suites = b":".join(
                s.replace(b"ECDSA", b"RSA")
                for s in cipher_list.split(b":")
                if b"ECDSA" in s
            )
            cipher_list = cipher_list + b":" + rsa_suites
        _orig_set_cipher_list(self, cipher_list)

    _SSL.Context.set_cipher_list = _patched_set_cipher_list  # type: ignore[method-assign]
    logger.info("Patched aiortc DTLS cipher list to include RSA suites")
except Exception as _e:
    logger.warning("Could not patch DTLS cipher list: %s", _e)

# ---------------------------------------------------------------------------
# Monkey-patch: tolerate the pinging.net server certificate's zero serial
# number.  The webrtc-unreliable Rust crate generates certs with serial=0,
# which is invalid per RFC 5280.  cryptography 42+ raises ValueError when
# loading such a cert; older versions emit a DeprecationWarning.  Either way
# the failure happens inside aiortc's DTLS fingerprint-verification step,
# causing an immediate connection failure after the handshake succeeds.
#
# We patch OpenSSL.crypto.X509.to_cryptography() — the method aiortc calls
# to get the peer cert as a cryptography object for fingerprint checking —
# to suppress the exception and return a usable object.
# ---------------------------------------------------------------------------
try:
    import warnings as _warnings
    from OpenSSL import crypto as _crypto

    _orig_to_cryptography = _crypto.X509.to_cryptography

    def _patched_to_cryptography(self):  # type: ignore[no-untyped-def]
        with _warnings.catch_warnings():
            _warnings.filterwarnings("ignore", category=DeprecationWarning)
            _warnings.filterwarnings("ignore", category=UserWarning)
            try:
                return _orig_to_cryptography(self)
            except Exception:
                # Fall back: load raw DER bytes via a relaxed backend path so
                # aiortc can still compute the SHA-256 fingerprint.
                from cryptography.x509 import load_der_x509_certificate
                der = _crypto.dump_certificate(_crypto.FILETYPE_ASN1, self)
                return load_der_x509_certificate(der)

    _crypto.X509.to_cryptography = _patched_to_cryptography  # type: ignore[method-assign]
    logger.info("Patched OpenSSL X509.to_cryptography to tolerate zero serial number")
except Exception as _e:
    logger.warning("Could not patch X509.to_cryptography: %s", _e)

# ---------------------------------------------------------------------------
# Monkey-patch: skip aiortc's unconditional SRTP profile requirement.
#
# aiortc requires an SRTP profile to be negotiated in the DTLS handshake,
# even for pure data-channel (SCTP) connections where SRTP is irrelevant.
# pinging.net's webrtc-unreliable server is data-channel-only and never
# responds to the use_srtp DTLS extension, so get_selected_srtp_profile()
# returns None and aiortc fails with "no SRTP profile negotiated".
#
# Returning a dummy profile lets aiortc's check pass.  The SRTP keying
# material aiortc derives afterwards is never used for SCTP data channels.
# ---------------------------------------------------------------------------
try:
    from aiortc.rtcdtlstransport import RTCDtlsTransport as _RTCDtlsTransport

    _orig_setup_srtp = _RTCDtlsTransport._setup_srtp  # type: ignore[attr-defined]

    def _patched_setup_srtp(self) -> None:  # type: ignore[no-untyped-def]
        # pinging.net's webrtc-unreliable server is data-channel only and never
        # negotiates an SRTP profile. aiortc's _setup_srtp() unconditionally
        # requires one, but SRTP keying material is unused for SCTP data channels.
        # Unconditionally skip — SRTP is never needed for this data-channel-only
        # connection regardless of what get_selected_srtp_profile() returns.
        return

    _RTCDtlsTransport._setup_srtp = _patched_setup_srtp  # type: ignore[method-assign]
    logger.info("Patched RTCDtlsTransport._setup_srtp to skip SRTP for data-channel-only server")
except Exception as _e:
    logger.warning("Could not patch RTCDtlsTransport._setup_srtp: %s", _e)

DB_PATH = Path(os.environ.get("DB_PATH", "/data/monitor.db"))
TARGET_HOST = os.environ.get("TARGET_HOST", "https://pinging.net")
PING_INTERVAL = float(os.environ.get("PING_INTERVAL", "1.0"))
DNS_INTERVAL = float(os.environ.get("DNS_INTERVAL", "30.0"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))
WEBRTC_ENABLED = os.environ.get("WEBRTC_ENABLED", "true").lower() not in ("0", "false", "no")

# Shared counters passed to the server during WebRTC session negotiation
# (mirrors what the browser frontend does)
_num_successful = 0
_num_timeout = 0

try:
    from aiortc import RTCDataChannel, RTCPeerConnection, RTCSessionDescription

    WEBRTC_AVAILABLE = True
except ImportError:
    WEBRTC_AVAILABLE = False
    logger.warning("aiortc not installed — WebRTC pings disabled (HTTP pings still run)")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


async def _init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS pings (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      INTEGER NOT NULL,   -- Unix milliseconds
                type    TEXT    NOT NULL,   -- 'http' | 'webrtc' | 'dns'
                success INTEGER NOT NULL,   -- 1 or 0
                rtt_ms  REAL,               -- NULL on failure
                error   TEXT                -- NULL on success
            )
            """
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_pings_ts ON pings(ts)")
        await db.commit()


async def _record(
    type_: str,
    success: bool,
    rtt_ms: float | None = None,
    error: str | None = None,
) -> None:
    ts = int(time.time() * 1000)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO pings (ts, type, success, rtt_ms, error) VALUES (?, ?, ?, ?, ?)",
            (ts, type_, 1 if success else 0, rtt_ms, error),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# HTTP ping loop
# ---------------------------------------------------------------------------


async def http_ping_loop() -> None:
    global _num_successful, _num_timeout
    logger.info("HTTP ping loop starting → %s/api/ping", TARGET_HOST)
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
        while True:
            body = str(random.randint(1, 10_000_000))
            t0 = time.monotonic()
            try:
                resp = await client.post(
                    f"{TARGET_HOST}/api/ping",
                    content=body,
                    headers={"cache-control": "no-store"},
                )
                rtt_ms = (time.monotonic() - t0) * 1000
                if resp.text.strip() == body:
                    _num_successful += 1
                    await _record("http", True, rtt_ms)
                    logger.debug("HTTP ping OK  %.1f ms", rtt_ms)
                else:
                    _num_timeout += 1
                    await _record("http", False, error="body mismatch")
            except Exception as exc:
                _num_timeout += 1
                await _record("http", False, error=str(exc)[:200])
                logger.warning("HTTP ping failed: %s", exc)
            await asyncio.sleep(PING_INTERVAL)


# ---------------------------------------------------------------------------
# DNS check loop
# ---------------------------------------------------------------------------


async def dns_check_loop() -> None:
    logger.info("DNS check loop starting")
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
        while True:
            rand = random.randint(1, 10_000_000)
            url = f"https://{rand}dns-check.pinging.net/api/dns-check"
            t0 = time.monotonic()
            try:
                resp = await client.get(url, headers={"cache-control": "no-store"})
                rtt_ms = (time.monotonic() - t0) * 1000
                if resp.text.strip() == str(rand):
                    await _record("dns", True, rtt_ms)
                    logger.debug("DNS check OK  %.1f ms", rtt_ms)
                else:
                    await _record("dns", False, error="unexpected response")
            except Exception as exc:
                await _record("dns", False, error=str(exc)[:200])
                logger.warning("DNS check failed: %s", exc)
            await asyncio.sleep(DNS_INTERVAL)


# ---------------------------------------------------------------------------
# WebRTC ping loop
# ---------------------------------------------------------------------------


async def _webrtc_session() -> None:
    """
    Run one WebRTC session against pinging.net.

    Protocol mirrors the browser frontend (frontend/index.ts):
      1. Create RTCPeerConnection + data channel "webudp" (unordered, no retransmits)
      2. POST SDP offer to /new_rtc_session → receive JSON answer + ICE candidate
      3. Ping loop: send current timestamp ms, echo is the last line of the response
    """
    global _num_successful, _num_timeout

    pc = RTCPeerConnection()
    # Use id=0 (pre-negotiated, no DATA_CHANNEL_OPEN handshake) so our channel
    # lives on SCTP stream 0. pinging.net's server always sends echo replies on
    # stream 0 regardless of which stream we sent on — if our channel is on
    # stream 1 (aiortc's default), the replies are silently dropped.
    channel: RTCDataChannel = pc.createDataChannel(
        "webudp", ordered=False, maxRetransmits=0, negotiated=True, id=0
    )

    channel_open = asyncio.Event()
    message_queue: asyncio.Queue[str] = asyncio.Queue()

    @pc.on("connectionstatechange")
    async def on_connection_state_change() -> None:
        logger.info("WebRTC connection state → %s", pc.connectionState)

    @pc.on("iceconnectionstatechange")
    async def on_ice_connection_state_change() -> None:
        logger.info("WebRTC ICE state → %s", pc.iceConnectionState)

    @channel.on("open")
    def on_open() -> None:
        logger.info("WebRTC data channel open")
        channel_open.set()

    @channel.on("message")
    def on_message(data) -> None:
        logger.debug("WebRTC on_message type=%s: %r", type(data).__name__, data)
        if isinstance(data, (bytes, bytearray)):
            data = data.decode("utf-8", errors="replace")
        message_queue.put_nowait(data)

    @channel.on("close")
    def on_close() -> None:
        logger.info("WebRTC data channel closed")

    try:
        # Mirror the browser frontend: send the initial offer SDP with no
        # candidates (browser sends immediately before ICE gathering).
        # Also strip aiortc's extra sha-384/sha-512 fingerprints — browsers
        # only include sha-256, and the extras push the payload over
        # pinging.net's server limit causing a 413.
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        # aiortc eagerly gathers ICE candidates inside createOffer(), so
        # offer.sdp already contains host + srflx candidates and sha-384/512
        # fingerprints that browsers never send. Strip them all down to match
        # what a browser sends: no candidates, sha-256 fingerprint only.
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
        logger.info(
            "WebRTC SDP offer (%d bytes, %d candidate lines):\n%s",
            len(sdp_to_send),
            sum(1 for l in sdp_to_send.splitlines() if l.startswith("a=candidate:")),
            sdp_to_send,
        )

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{TARGET_HOST}/new_rtc_session",
                params={
                    "num_successful": _num_successful,
                    "num_timeout": _num_timeout,
                },
                content=sdp_to_send.encode(),
                headers={"content-type": "application/sdp"},
            )
            resp.raise_for_status()
            server_response = resp.json()

        logger.info("WebRTC server response: %s", server_response)

        answer = RTCSessionDescription(
            sdp=server_response["answer"]["sdp"],
            type=server_response["answer"]["type"],
        )
        await pc.setRemoteDescription(answer)

        # Add the trickle ICE candidate the server returns
        cand_init = server_response.get("candidate") or {}
        cand_str = cand_init.get("candidate", "")
        if cand_str:
            try:
                from aiortc.sdp import candidate_from_sdp

                if cand_str.startswith("candidate:"):
                    cand_str = cand_str[len("candidate:"):]
                candidate = candidate_from_sdp(cand_str)
                candidate.sdpMid = cand_init.get("sdpMid", "0")
                candidate.sdpMLineIndex = cand_init.get("sdpMLineIndex", 0)
                await pc.addIceCandidate(candidate)
            except Exception as exc:
                # Not fatal — the answer SDP usually includes the candidate too
                logger.debug("Could not add ICE candidate: %s", exc)

        # Wait for data channel to open
        try:
            await asyncio.wait_for(channel_open.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            logger.warning("WebRTC channel did not open within 15s")
            await _record("webrtc", False, error="channel open timeout")
            return

        # Ping loop — mirrors browser behavior: send at PING_INTERVAL (1s) regardless
        # of pending responses, collect echoes asynchronously. The browser fires each
        # ping's 5-second timeout independently and schedules the next ping immediately;
        # our previous sequential approach only sent one ping every 6s during stalls,
        # which may not be enough to trigger server-side routing state.
        MAX_CONSECUTIVE_TIMEOUTS = 10
        first_ping = True
        # outstanding: ts_ms_str → monotonic t0 for RTT calculation
        outstanding: dict[str, float] = {}

        async def _send_loop() -> None:
            nonlocal first_ping
            while channel.readyState == "open":
                ts_ms = int(time.time() * 1000)
                msg = f"LOC?\n{ts_ms}" if first_ping else str(ts_ms)
                first_ping = False
                outstanding[str(ts_ms)] = time.monotonic()
                channel.send(msg)
                logger.debug("WebRTC send: %r", msg)
                await asyncio.sleep(PING_INTERVAL)

        async def _recv_loop() -> None:
            global _num_successful, _num_timeout
            consecutive_timeouts = 0
            while channel.readyState == "open":
                # Wait up to PING_INTERVAL for a response, then fall through to
                # expire any stale outstanding pings.
                try:
                    response = await asyncio.wait_for(
                        message_queue.get(), timeout=PING_INTERVAL
                    )
                    echoed = response.strip().split("\n")[-1]
                    t0 = outstanding.pop(echoed, None)
                    if t0 is not None:
                        rtt_ms = (time.monotonic() - t0) * 1000
                        consecutive_timeouts = 0
                        _num_successful += 1
                        await _record("webrtc", True, rtt_ms)
                        logger.debug("WebRTC ping OK  %.1f ms", rtt_ms)
                    else:
                        logger.info("WebRTC on_message (unmatched): %r", response)
                except asyncio.TimeoutError:
                    pass

                # Expire outstanding pings older than 5 seconds
                now = time.monotonic()
                for ts_str in [k for k, t0 in list(outstanding.items()) if now - t0 > 5.0]:
                    outstanding.pop(ts_str, None)
                    _num_timeout += 1
                    await _record("webrtc", False, error="timeout")
                    consecutive_timeouts += 1
                    logger.debug(
                        "WebRTC ping timeout (consecutive=%d/%d)",
                        consecutive_timeouts, MAX_CONSECUTIVE_TIMEOUTS,
                    )

                if consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
                    logger.warning(
                        "WebRTC session stalled (%d consecutive timeouts) — reconnecting",
                        consecutive_timeouts,
                    )
                    return

        send_task = asyncio.create_task(_send_loop())
        recv_task = asyncio.create_task(_recv_loop())
        try:
            await recv_task  # returns when stalled or channel closes
        finally:
            send_task.cancel()
            await asyncio.gather(send_task, return_exceptions=True)

    finally:
        await pc.close()


async def prune_loop() -> None:
    """Delete rows older than RETENTION_DAYS once per hour."""
    while True:
        cutoff = int((time.time() - RETENTION_DAYS * 86400) * 1000)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM pings WHERE ts < ?", (cutoff,))
            await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await db.commit()
        logger.info("Pruned rows older than %d days", RETENTION_DAYS)
        await asyncio.sleep(3600)


async def webrtc_ping_loop() -> None:
    if not WEBRTC_ENABLED:
        logger.info("WebRTC loop disabled (WEBRTC_ENABLED=false)")
        return
    if not WEBRTC_AVAILABLE:
        logger.info("WebRTC loop skipped (aiortc not available)")
        return

    logger.info("WebRTC ping loop starting → %s", TARGET_HOST)
    backoff = 2.0
    while True:
        ok_before = _num_successful
        try:
            await _webrtc_session()
        except Exception as exc:
            logger.error("WebRTC session error: %s", exc)
            await _record("webrtc", False, error=str(exc)[:200])
        # Reset backoff if the session produced any successful pings; only
        # grow it on pure-failure sessions so reconnects stay fast after
        # transient stalls.
        if _num_successful > ok_before:
            backoff = 2.0
        else:
            backoff = min(backoff * 2, 60.0)
        logger.info("WebRTC reconnecting in %.0fs", backoff)
        await asyncio.sleep(backoff)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    await _init_db()
    tasks = [
        asyncio.create_task(http_ping_loop(), name="http-ping"),
        asyncio.create_task(dns_check_loop(), name="dns-check"),
        asyncio.create_task(webrtc_ping_loop(), name="webrtc-ping"),
        asyncio.create_task(prune_loop(), name="prune"),
    ]
    yield
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(lifespan=lifespan)


@app.get("/api/results")
async def api_results(minutes: int = 1440) -> list[dict]:
    """Return raw ping rows for the last N minutes (default 24h)."""
    cutoff = int((time.time() - minutes * 60) * 1000)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT ts, type, success, rtt_ms, error FROM pings WHERE ts >= ? ORDER BY ts",
            (cutoff,),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


@app.get("/api/stats")
async def api_stats(hours: int = 24) -> dict:
    """Return uptime/RTT statistics per test type for the last N hours."""
    cutoff = int((time.time() - hours * 3600) * 1000)
    stats: dict = {}
    async with aiosqlite.connect(DB_PATH) as db:
        for type_ in ("http", "webrtc", "dns"):
            async with db.execute(
                """
                SELECT
                    COUNT(*)                                    AS total,
                    SUM(success)                                AS ok,
                    AVG(CASE WHEN success=1 THEN rtt_ms END)   AS avg_rtt,
                    MIN(CASE WHEN success=1 THEN rtt_ms END)   AS min_rtt,
                    MAX(CASE WHEN success=1 THEN rtt_ms END)   AS max_rtt
                FROM pings
                WHERE ts >= ? AND type = ?
                """,
                (cutoff, type_),
            ) as cursor:
                row = await cursor.fetchone()
            total = (row[0] or 0) if row else 0
            ok = (row[1] or 0) if row else 0
            avg_rtt, min_rtt, max_rtt = (row[2], row[3], row[4]) if row else (None, None, None)
            stats[type_] = {
                "total": total,
                "uptime_pct": round(ok / total * 100, 2) if total else None,
                "packet_loss_pct": round((total - ok) / total * 100, 2) if total else None,
                "avg_rtt": round(avg_rtt, 1) if avg_rtt is not None else None,
                "min_rtt": round(min_rtt, 1) if min_rtt is not None else None,
                "max_rtt": round(max_rtt, 1) if max_rtt is not None else None,
            }
    return stats


@app.get("/api/buckets")
async def api_buckets(hours: int = 24, seconds: int = 0) -> list[dict]:
    """Pre-aggregated 5-second bucket stats for the realtime grid.

    Use ``hours`` for the initial full load (default 24 h = 288 rows of 60 cells).
    Use ``seconds`` for incremental fetches (e.g. seconds=10 fetches the last
    2 bucket windows).  When ``seconds`` is non-zero it takes precedence.
    """
    if seconds > 0:
        cutoff = int((time.time() - seconds) * 1000)
    else:
        cutoff = int((time.time() - hours * 3600) * 1000)
    return await _bucket_stats(cutoff, 5_000)


@app.get("/api/outages")
async def api_outages(days: int = 7) -> list[dict]:
    """
    Return outage events for the last N days.
    An outage is a gap > 5s with no successful HTTP or WebRTC ping.
    """
    cutoff = int((time.time() - days * 86400) * 1000)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT ts FROM pings
            WHERE ts >= ? AND success = 1 AND type IN ('http', 'webrtc')
            ORDER BY ts
            """,
            (cutoff,),
        ) as cursor:
            timestamps = [row[0] for row in await cursor.fetchall()]
        # Determine whether the service was already running before the window.
        # If the earliest ping in the DB is newer than cutoff, the service simply
        # hadn't started yet — don't synthesise a fake leading-edge outage.
        async with db.execute(
            "SELECT MIN(ts) FROM pings WHERE type IN ('http', 'webrtc')"
        ) as cursor:
            row = await cursor.fetchone()
            first_ping_ts = row[0] if row and row[0] is not None else None

    outages: list[dict] = []
    GAP_MS = 5000

    if timestamps:
        if (
            first_ping_ts is not None
            and first_ping_ts < cutoff
            and timestamps[0] - cutoff > GAP_MS
        ):
            outages.append(
                {
                    "start": cutoff,
                    "end": timestamps[0],
                    "duration_s": round((timestamps[0] - cutoff) / 1000, 1),
                }
            )
        for i in range(1, len(timestamps)):
            gap = timestamps[i] - timestamps[i - 1]
            if gap > GAP_MS:
                outages.append(
                    {
                        "start": timestamps[i - 1],
                        "end": timestamps[i],
                        "duration_s": round(gap / 1000, 1),
                    }
                )

    return outages


async def _bucket_stats(cutoff: int, bucket_ms: int) -> list[dict]:
    """Aggregate ping stats into fixed-size time buckets (hourly or daily)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT
              (ts / ?) * ?  AS period_ts,
              type,
              COUNT(*)      AS total,
              SUM(success)  AS ok,
              AVG(CASE WHEN success = 1 AND rtt_ms IS NOT NULL THEN rtt_ms END) AS avg_rtt,
              MIN(CASE WHEN success = 1 AND rtt_ms IS NOT NULL THEN rtt_ms END) AS min_rtt,
              MAX(CASE WHEN success = 1 AND rtt_ms IS NOT NULL THEN rtt_ms END) AS max_rtt
            FROM pings
            WHERE ts >= ? AND type IN ('http', 'webrtc')
            GROUP BY period_ts, type
            ORDER BY period_ts ASC
            """,
            (bucket_ms, bucket_ms, cutoff),
        ) as cursor:
            rows = await cursor.fetchall()

    periods: dict[int, dict] = {}
    for period_ts, type_, total, ok, avg_rtt, min_rtt, max_rtt in rows:
        if period_ts not in periods:
            periods[period_ts] = {"ts": period_ts}
        periods[period_ts][type_] = {
            "total": total,
            "ok": ok,
            "uptime_pct": round(ok / total * 100, 1) if total else None,
            "packet_loss_pct": round((total - ok) / total * 100, 2) if total else None,
            "avg_rtt": round(avg_rtt, 1) if avg_rtt is not None else None,
            "min_rtt": round(min_rtt, 1) if min_rtt is not None else None,
            "max_rtt": round(max_rtt, 1) if max_rtt is not None else None,
        }

    return sorted(periods.values(), key=lambda x: x["ts"])


@app.get("/api/hourly")
async def api_hourly(hours: int = 24) -> list[dict]:
    """Per-hour RTT/uptime stats for the last N hours (HTTP and WebRTC)."""
    cutoff = int((time.time() - hours * 3600) * 1000)
    return await _bucket_stats(cutoff, 3_600_000)


@app.get("/api/daily")
async def api_daily(days: int = 30) -> list[dict]:
    """Per-day RTT/uptime stats for the last N days (HTTP and WebRTC)."""
    cutoff = int((time.time() - days * 86400) * 1000)
    return await _bucket_stats(cutoff, 86_400_000)


# Serve the static dashboard — must be mounted last so API routes take precedence
DASHBOARD_DIR = Path(__file__).parent / "dashboard"
app.mount("/", StaticFiles(directory=str(DASHBOARD_DIR), html=True), name="static")
