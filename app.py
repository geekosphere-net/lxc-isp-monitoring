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
    channel: RTCDataChannel = pc.createDataChannel(
        "webudp", ordered=False, maxRetransmits=0
    )

    channel_open = asyncio.Event()
    message_queue: asyncio.Queue[str] = asyncio.Queue()

    @pc.on("connectionstatechange")
    async def on_connection_state_change() -> None:
        logger.info("WebRTC connection state → %s", pc.connectionState)

    @channel.on("open")
    def on_open() -> None:
        logger.info("WebRTC data channel open")
        channel_open.set()

    @channel.on("message")
    def on_message(data: str) -> None:
        message_queue.put_nowait(data)

    @channel.on("close")
    def on_close() -> None:
        logger.info("WebRTC data channel closed")

    try:
        # Build SDP offer. We need at least one local host candidate in the SDP
        # so the server knows our address and can initiate DTLS back to us.
        # Waiting for full ICE gathering bloats the SDP with STUN/TURN
        # server-reflexive candidates and causes a 413 from pinging.net.
        # A short sleep lets the local host candidate be gathered (takes <100ms)
        # without collecting expensive reflexive candidates.
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        await asyncio.sleep(0.5)

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{TARGET_HOST}/new_rtc_session",
                params={
                    "num_successful": _num_successful,
                    "num_timeout": _num_timeout,
                },
                content=pc.localDescription.sdp.encode(),
                headers={"content-type": "application/sdp"},
            )
            resp.raise_for_status()
            server_response = resp.json()

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

        # Ping loop — send timestamp ms, receive echo as last line of response
        first_ping = True
        while channel.readyState == "open":
            ts_ms = int(time.time() * 1000)
            # Send LOC? on first ping to get server location info in the response
            msg = f"LOC?\n{ts_ms}" if first_ping else str(ts_ms)
            first_ping = False
            t0 = time.monotonic()
            channel.send(msg)

            try:
                response = await asyncio.wait_for(message_queue.get(), timeout=5.0)
                rtt_ms = (time.monotonic() - t0) * 1000
                # Last line of response is the echoed timestamp
                echoed = response.strip().split("\n")[-1]
                if echoed == str(ts_ms):
                    _num_successful += 1
                    await _record("webrtc", True, rtt_ms)
                    logger.debug("WebRTC ping OK  %.1f ms", rtt_ms)
                else:
                    _num_timeout += 1
                    await _record("webrtc", False, error="echo mismatch")
            except asyncio.TimeoutError:
                _num_timeout += 1
                await _record("webrtc", False, error="timeout")
                logger.debug("WebRTC ping timeout")

            await asyncio.sleep(PING_INTERVAL)

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
        try:
            await _webrtc_session()
        except Exception as exc:
            logger.error("WebRTC session error: %s", exc)
            await _record("webrtc", False, error=str(exc)[:200])
        logger.info("WebRTC reconnecting in %.0fs", backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60.0)


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

    outages: list[dict] = []
    GAP_MS = 5000

    if timestamps:
        if timestamps[0] - cutoff > GAP_MS:
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


# Serve the static dashboard — must be mounted last so API routes take precedence
DASHBOARD_DIR = Path(__file__).parent / "dashboard"
app.mount("/", StaticFiles(directory=str(DASHBOARD_DIR), html=True), name="static")
