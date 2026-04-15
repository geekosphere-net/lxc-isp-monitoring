"""
Standalone WebRTC diagnostic script for pinging.net.

Runs a single WebRTC session and exits.  Each --test variant probes a different
hypothesis about why the server opens the data channel but never echoes our pings.

Usage:
    python webrtc_test.py --test baseline        # exact replica of app.py logic
    python webrtc_test.py --test sctp-spy        # log every SCTP/channel event
    python webrtc_test.py --test keep-candidates # send ICE candidates in the offer
    python webrtc_test.py --test trickle-ice     # add a=ice-options:trickle to offer
    python webrtc_test.py --test no-loc          # skip LOC? prefix on first ping
    python webrtc_test.py --test har-diff --har ~/Downloads/pinging.net.har

    Add --duration N to wait longer for replies (default: 30s).
"""

import argparse
import asyncio
import difflib
import json
import logging
import sys
import time

import httpx

# ---------------------------------------------------------------------------
# Logging — verbose enough to see what's happening without being noisy
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
# Quiet down very chatty aiortc internals
for _noisy in ("aioice", "aiortc.rtp", "aiortc.rtcdtlstransport"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger("webrtc_test")

# ---------------------------------------------------------------------------
# Monkey-patch 1: RSA cipher suites
# (verbatim from app.py — pinging.net presents an RSA cert)
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
    logger.info("Patch 1 OK: DTLS cipher list → RSA suites added")
except Exception as _e:
    logger.warning("Patch 1 FAILED (DTLS cipher list): %s", _e)

# ---------------------------------------------------------------------------
# Monkey-patch 2: zero serial number in server certificate
# (verbatim from app.py)
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
                from cryptography.x509 import load_der_x509_certificate
                der = _crypto.dump_certificate(_crypto.FILETYPE_ASN1, self)
                return load_der_x509_certificate(der)

    _crypto.X509.to_cryptography = _patched_to_cryptography  # type: ignore[method-assign]
    logger.info("Patch 2 OK: X509.to_cryptography → tolerates serial=0")
except Exception as _e:
    logger.warning("Patch 2 FAILED (X509 serial): %s", _e)

# ---------------------------------------------------------------------------
# Monkey-patch 3: skip SRTP profile requirement
# (verbatim from app.py — data-channel-only server, SRTP unused)
# ---------------------------------------------------------------------------
try:
    from aiortc.rtcdtlstransport import RTCDtlsTransport as _RTCDtlsTransport

    def _patched_setup_srtp(self) -> None:  # type: ignore[no-untyped-def]
        return

    _RTCDtlsTransport._setup_srtp = _patched_setup_srtp  # type: ignore[method-assign]
    logger.info("Patch 3 OK: RTCDtlsTransport._setup_srtp → noop")
except Exception as _e:
    logger.warning("Patch 3 FAILED (SRTP skip): %s", _e)

# ---------------------------------------------------------------------------
# Now safe to import aiortc types
# ---------------------------------------------------------------------------
from aiortc import RTCDataChannel, RTCPeerConnection, RTCSessionDescription  # noqa: E402

TARGET = "https://pinging.net"


# ---------------------------------------------------------------------------
# Monkey-patch 4 (sctp-spy variant only): log low-level SCTP/channel events
# ---------------------------------------------------------------------------

def _apply_sctp_spy() -> None:
    """
    Wrap the data channel's internal message delivery method and the SCTP
    transport's receive path to log every event at the lowest accessible level.
    This confirms whether the server sends SCTP DATA chunks that aiortc receives
    but fails to surface, or whether it never sends DATA at all.
    """
    patched = []

    # Try to wrap RTCDataChannel._addIncomingMessage (aiortc ≥1.9)
    try:
        from aiortc.rtcsctptransport import RTCDataChannel as _DC

        _orig_add = _DC._addIncomingMessage  # type: ignore[attr-defined]

        def _spy_add(self, ppid, data):  # type: ignore[no-untyped-def]
            logger.info(
                "[SCTP-SPY] _addIncomingMessage fired: ppid=%s len=%d data=%r",
                ppid, len(data) if data else 0, data[:80] if data else b"",
            )
            return _orig_add(self, ppid, data)

        _DC._addIncomingMessage = _spy_add  # type: ignore[method-assign]
        patched.append("RTCDataChannel._addIncomingMessage")
    except (ImportError, AttributeError):
        pass

    # Try to wrap RTCSctpTransport._receive (the method called by DTLS with
    # raw SCTP bytes).  Log the first byte of each SCTP chunk to identify type:
    #   0 = DATA, 3 = SACK, 6 = ABORT, 7 = SHUTDOWN, etc.
    try:
        from aiortc.rtcsctptransport import RTCSctpTransport as _SCTP

        _orig_recv = _SCTP._receive  # type: ignore[attr-defined]

        async def _spy_receive(self, data, addr):  # type: ignore[no-untyped-def]
            # SCTP common header: 12 bytes, then one or more chunks.
            # Each chunk: type(1) flags(1) length(2) ...
            if len(data) >= 16:
                chunk_type = data[12]
                chunk_names = {0: "DATA", 3: "SACK", 6: "ABORT",
                               7: "SHUTDOWN", 8: "SHUTDOWN-ACK",
                               9: "ERROR", 10: "COOKIE-ECHO", 11: "COOKIE-ACK",
                               14: "SHUTDOWN-COMPLETE"}
                name = chunk_names.get(chunk_type, f"type={chunk_type}")
                logger.info(
                    "[SCTP-SPY] _receive: %d bytes from %s, first chunk = %s",
                    len(data), addr, name,
                )
            else:
                logger.info("[SCTP-SPY] _receive: %d bytes from %s", len(data), addr)
            return await _orig_recv(self, data, addr)

        _SCTP._receive = _spy_receive  # type: ignore[method-assign]
        patched.append("RTCSctpTransport._receive")
    except (ImportError, AttributeError) as e:
        logger.warning("[SCTP-SPY] Could not patch RTCSctpTransport._receive: %s", e)

    # Patch RTCDataChannel.emit so we see every event fired on our channel
    try:
        from pyee.base import EventEmitter as _EE

        _orig_emit = _EE.emit

        def _spy_emit(self, event, *args, **kwargs):  # type: ignore[no-untyped-def]
            if event in ("message", "open", "close", "error", "bufferedamountlow"):
                logger.info(
                    "[SCTP-SPY] EventEmitter.emit: event=%r on %s",
                    event, type(self).__name__,
                )
            return _orig_emit(self, event, *args, **kwargs)

        _EE.emit = _spy_emit  # type: ignore[method-assign]
        patched.append("pyee.EventEmitter.emit")
    except (ImportError, AttributeError) as e:
        logger.warning("[SCTP-SPY] Could not patch EventEmitter.emit: %s", e)

    logger.info("[SCTP-SPY] Active patches: %s", ", ".join(patched) if patched else "none")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_sdp(sdp: str) -> str:
    """Strip ICE candidates and non-sha-256 fingerprints (app.py baseline logic)."""
    lines = [
        line for line in sdp.splitlines()
        if not line.startswith("a=candidate:")
        and not line.startswith("a=end-of-candidates")
        and not (
            line.startswith("a=fingerprint:")
            and not line.startswith("a=fingerprint:sha-256")
        )
    ]
    return "\r\n".join(lines) + "\r\n"


def _add_trickle_ice(sdp: str) -> str:
    """Insert a=ice-options:trickle before a=ice-ufrag: (browsers always include it)."""
    lines = sdp.splitlines()
    out = []
    for line in lines:
        if line.startswith("a=ice-ufrag:") and not any(
            l.startswith("a=ice-options:") for l in out
        ):
            out.append("a=ice-options:trickle")
        out.append(line)
    return "\r\n".join(out) + "\r\n"


def _print_sdp_block(label: str, sdp: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {label}")
    print("=" * 60)
    for line in sdp.splitlines():
        print(f"  {line}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Session runner
# ---------------------------------------------------------------------------

async def run_session(variant: str, duration: int) -> None:
    pc = RTCPeerConnection()
    if variant == "negotiated":
        # Pre-negotiate on stream_id=0 — skip DATA_CHANNEL_OPEN handshake and
        # directly claim stream 0, which is where the server sends its echoes.
        channel: RTCDataChannel = pc.createDataChannel(
            "webudp", ordered=False, maxRetransmits=0, negotiated=True, id=0
        )
    else:
        channel: RTCDataChannel = pc.createDataChannel(
            "webudp", ordered=False, maxRetransmits=0
        )

    channel_open = asyncio.Event()
    message_queue: asyncio.Queue[str] = asyncio.Queue()
    received_count = 0

    # ---- event handlers ----

    @pc.on("connectionstatechange")
    async def on_cs() -> None:
        logger.info("connectionState → %s", pc.connectionState)

    @pc.on("iceconnectionstatechange")
    async def on_ice() -> None:
        logger.info("iceConnectionState → %s", pc.iceConnectionState)

    @pc.on("datachannel")
    def on_server_dc(dc: RTCDataChannel) -> None:
        logger.info("Server opened data channel: label=%r id=%s", dc.label, dc.id)

        @dc.on("message")
        def on_server_msg(data) -> None:
            logger.info("Message on SERVER-initiated channel: %r", data)
            if isinstance(data, (bytes, bytearray)):
                data = data.decode("utf-8", errors="replace")
            message_queue.put_nowait(data)

    @channel.on("open")
    def on_open() -> None:
        logger.info("Data channel OPEN (label=%r readyState=%s)", channel.label, channel.readyState)
        channel_open.set()

    @channel.on("message")
    def on_message(data) -> None:
        logger.info("on_message fired: type=%s data=%r", type(data).__name__, data)
        if isinstance(data, (bytes, bytearray)):
            data = data.decode("utf-8", errors="replace")
        message_queue.put_nowait(data)

    @channel.on("close")
    def on_close() -> None:
        logger.info("Data channel CLOSED")

    @channel.on("error")
    def on_error(err) -> None:
        logger.error("Data channel ERROR: %s", err)

    try:
        # ---- build SDP offer ----
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        raw_sdp = offer.sdp
        logger.info("Raw offer SDP (%d bytes, %d lines)", len(raw_sdp), len(raw_sdp.splitlines()))

        if variant == "keep-candidates":
            # Only strip extra fingerprints; keep all ICE candidates
            sdp_lines = [
                line for line in raw_sdp.splitlines()
                if not (
                    line.startswith("a=fingerprint:")
                    and not line.startswith("a=fingerprint:sha-256")
                )
            ]
            sdp_to_send = "\r\n".join(sdp_lines) + "\r\n"
        elif variant == "trickle-ice":
            # Strip candidates but add a=ice-options:trickle (what browsers send)
            sdp_to_send = _add_trickle_ice(_strip_sdp(raw_sdp))
        elif variant == "setup-active":
            # Replace a=setup:actpass with a=setup:active so aiortc resolves our
            # DTLS role to "client" before channel creation, forcing stream_id=0
            # (even) instead of stream_id=1 (odd). Browsers always use even IDs;
            # the server echoes on stream_id=0, so we need to match.
            sdp_to_send = _strip_sdp(raw_sdp).replace(
                "a=setup:actpass", "a=setup:active"
            )
        else:
            # baseline, sctp-spy, no-loc all use the same stripped SDP
            sdp_to_send = _strip_sdp(raw_sdp)

        _print_sdp_block(f"SDP WE SEND ({variant}, {len(sdp_to_send)} bytes)", sdp_to_send)

        # ---- signaling POST ----
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{TARGET}/new_rtc_session",
                params={"num_successful": 0, "num_timeout": 0},
                content=sdp_to_send.encode(),
                headers={"content-type": "application/sdp"},
            )
            resp.raise_for_status()
            server_response = resp.json()

        logger.info("Server JSON response keys: %s", list(server_response.keys()))
        answer_sdp = server_response["answer"]["sdp"]
        _print_sdp_block("SERVER ANSWER SDP", answer_sdp)

        answer = RTCSessionDescription(
            sdp=answer_sdp,
            type=server_response["answer"]["type"],
        )
        await pc.setRemoteDescription(answer)

        # ---- trickle ICE candidate from server ----
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
                logger.info("Added ICE candidate: %s", cand_init.get("candidate", ""))
            except Exception as exc:
                logger.debug("Could not add ICE candidate: %s", exc)

        # ---- wait for channel open ----
        try:
            await asyncio.wait_for(channel_open.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            logger.error("Data channel did NOT open within 15s — aborting")
            return

        # ---- ping / receive loops ----
        first_ping = True
        outstanding: dict[str, float] = {}

        async def _send_loop() -> None:
            nonlocal first_ping
            while channel.readyState == "open":
                ts_ms = int(time.time() * 1000)
                if variant == "no-loc":
                    msg = str(ts_ms)  # skip LOC? prefix entirely
                else:
                    msg = f"LOC?\n{ts_ms}" if first_ping else str(ts_ms)
                first_ping = False
                outstanding[str(ts_ms)] = time.monotonic()
                channel.send(msg)
                logger.debug("SEND: %r", msg)
                await asyncio.sleep(1.0)

        async def _recv_loop() -> None:
            nonlocal received_count
            deadline = time.monotonic() + duration
            while time.monotonic() < deadline and channel.readyState == "open":
                remaining = deadline - time.monotonic()
                try:
                    response = await asyncio.wait_for(
                        message_queue.get(), timeout=min(2.0, remaining)
                    )
                    echoed = response.strip().split("\n")[-1]
                    t0 = outstanding.pop(echoed, None)
                    if t0 is not None:
                        rtt_ms = (time.monotonic() - t0) * 1000
                        received_count += 1
                        print(f"\n>>> PING OK  rtt={rtt_ms:.1f} ms  (total received: {received_count})")
                    else:
                        print(f"\n>>> UNMATCHED message: {response!r}")
                except asyncio.TimeoutError:
                    pending = len(outstanding)
                    if pending:
                        logger.debug("Waiting... %d pings outstanding, no response yet", pending)

        send_task = asyncio.create_task(_send_loop())
        recv_task = asyncio.create_task(_recv_loop())
        try:
            await recv_task
        finally:
            send_task.cancel()
            await asyncio.gather(send_task, return_exceptions=True)

    finally:
        await pc.close()
        print(f"\n{'='*60}")
        print(f"  RESULT: received {received_count} echo(es) in {duration}s")
        print(f"  Variant: {variant}")
        print("=" * 60)


# ---------------------------------------------------------------------------
# har-diff: compare browser SDP offer vs ours
# ---------------------------------------------------------------------------

async def har_diff(har_path: str) -> None:
    """Parse a HAR file, extract browser's SDP offer, diff against ours."""
    with open(har_path, encoding="utf-8") as f:
        har = json.load(f)

    entries = har.get("log", {}).get("entries", [])
    rtc_entries = [
        e for e in entries
        if "/new_rtc_session" in e.get("request", {}).get("url", "")
    ]

    if not rtc_entries:
        print("No /new_rtc_session request found in HAR file.")
        print("URLs found:", [e["request"]["url"] for e in entries[:20]])
        return

    entry = rtc_entries[0]
    print(f"Found /new_rtc_session request: {entry['request']['url']}")

    post_data = entry.get("request", {}).get("postData", {})
    browser_sdp = post_data.get("text", "")
    if not browser_sdp:
        print("No postData text found in the request.")
        return

    _print_sdp_block("BROWSER SDP (from HAR)", browser_sdp)

    # Build our SDP
    pc = RTCPeerConnection()
    pc.createDataChannel("webudp", ordered=False, maxRetransmits=0)
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    our_sdp = _strip_sdp(offer.sdp)
    await pc.close()

    _print_sdp_block("OUR SDP (baseline strip)", our_sdp)

    # Unified diff
    browser_lines = browser_sdp.splitlines(keepends=True)
    our_lines = our_sdp.splitlines(keepends=True)
    diff = list(difflib.unified_diff(
        our_lines, browser_lines,
        fromfile="our-sdp", tofile="browser-sdp",
        lineterm="",
    ))
    if diff:
        print("\n" + "=" * 60)
        print("  DIFF (our SDP → browser SDP)")
        print("  Lines starting with - are in ours but not browser")
        print("  Lines starting with + are in browser but not ours")
        print("=" * 60)
        for line in diff:
            print(line, end="")
        print()
    else:
        print("\nSDPs are identical (after our stripping).")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="WebRTC diagnostic for pinging.net")
    parser.add_argument(
        "--test",
        choices=["baseline", "sctp-spy", "keep-candidates", "trickle-ice", "no-loc",
                 "setup-active", "negotiated", "har-diff"],
        required=True,
        help="Which diagnostic variant to run",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=30,
        help="Seconds to wait for responses (default: 30)",
    )
    parser.add_argument(
        "--har",
        type=str,
        default=None,
        help="Path to HAR file (required for har-diff variant)",
    )
    args = parser.parse_args()

    if args.test == "har-diff":
        if not args.har:
            parser.error("--har PATH is required for the har-diff variant")
        asyncio.run(har_diff(args.har))
        return

    if args.test == "sctp-spy":
        _apply_sctp_spy()

    print(f"\nRunning variant '{args.test}' for {args.duration}s...")
    asyncio.run(run_session(args.test, args.duration))


if __name__ == "__main__":
    main()
