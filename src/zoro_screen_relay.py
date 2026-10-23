"""Zoro Secure Encrypted Screen Relay.

Enables the headless Linux agent (running in Azure Container Apps) to capture
and describe the user's active Windows desktop without opening any listening
network ports on the Windows laptop.

Architecture:
  - Transport: Azure Redis Enterprise via TLS (rediss://) - 100% encrypted in transit.
  - Zero Open Ports: The Windows laptop connects OUTBOUND to Azure Redis.
    No local TCP/UDP ports or public endpoints are ever exposed.
  - Authentication: HMAC-SHA256 request & response signing with replay protection.
  - Session Isolation: Attaches to winsta0\\default in the user's logged-in Windows session.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import io
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("zoro.screen_relay")

# Redis keys
REQ_QUEUE = "zoro:relay:screen:requests"
RESP_PREFIX = "zoro:relay:screen:response:"
HEARTBEAT_KEY = "zoro:relay:screen:heartbeat"
HEARTBEAT_TTL = 15  # seconds

ZORO_ROOT = Path(__file__).resolve().parent.parent


def get_shared_secret() -> bytes:
    """Retrieve or derive the relay shared secret."""
    secret = os.environ.get("ZORO_RELAY_SECRET")
    if secret:
        return secret.encode("utf-8")
    # Deterministically derive secret from REDIS_URL if not set explicitly
    redis_url = os.environ.get("REDIS_URL", "")
    if redis_url:
        return hashlib.sha256(b"zoro-screen-relay-secret:" + redis_url.encode("utf-8")).digest()
    return b"zoro-screen-relay-fallback-secret-2026"


def sign_payload(data: str) -> str:
    secret = get_shared_secret()
    return hmac.new(secret, data.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_signature(data: str, signature: str) -> bool:
    expected = sign_payload(data)
    return hmac.compare_digest(expected, signature)


def attach_to_user_desktop() -> bool:
    """Ensure current thread is attached to winsta0\\default on Windows."""
    if sys.platform != "win32":
        return True
    try:
        user32 = ctypes.windll.user32
        # Open winsta0
        hwinsta = user32.OpenWindowStationW("winsta0", False, 0x10000000)
        if hwinsta:
            user32.SetProcessWindowStation(hwinsta)
        # Open default desktop
        hdesk = user32.OpenDesktopW("default", 0, False, 0x10000000)
        if hdesk:
            user32.SetThreadDesktop(hdesk)
        return True
    except Exception as e:
        logger.warning(f"Failed to attach to winsta0\\default: {e}")
        return False


def capture_local_desktop(max_edge: int = 1568, fmt: str = "png") -> Tuple[str, str, int, int]:
    """Capture the primary monitor on Windows, resize, and return (b64, media_type, width, height)."""
    attach_to_user_desktop()

    import mss
    from PIL import Image

    with mss.mss() as sct:
        mon = sct.monitors[1]  # Primary monitor
        shot = sct.grab(mon)

    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    w, h = img.size
    scale = min(1.0, max_edge / float(max(w, h)))
    if scale < 1.0:
        new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
        img = img.resize((new_w, new_h), Image.LANCZOS)
    else:
        new_w, new_h = w, h

    buf = io.BytesIO()
    if fmt.lower() in ("jpg", "jpeg"):
        img.save(buf, format="JPEG", quality=85)
        media_type = "image/jpeg"
    else:
        # Fast PNG compression for real-time responsiveness
        img.save(buf, format="PNG", compress_level=1)
        media_type = "image/png"

    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return b64, media_type, new_w, new_h


def _get_redis_client():
    import redis

    url = os.environ.get("REDIS_URL")
    if not url:
        raise ValueError("REDIS_URL environment variable is not set")
    return redis.Redis.from_url(url, decode_responses=True, socket_timeout=5.0)


# --- Client Side (Executed by Worker in Linux Cloud or Local Agent) ---

def is_relay_online() -> bool:
    """Check if the Windows screen relay heartbeat is active."""
    try:
        r = _get_redis_client()
        return bool(r.exists(HEARTBEAT_KEY))
    except Exception:
        return False


def request_screen_from_relay(max_edge: int = 1568, timeout: float = 15.0, fmt: str = "png") -> Tuple[str, str]:
    """Request a screenshot from the user's logged-in Windows laptop over Redis."""
    r = _get_redis_client()
    req_id = str(uuid.uuid4())
    ts = time.time()
    sign_data = f"{req_id}:{ts:.3f}"
    sig = sign_payload(sign_data)

    payload = {
        "req_id": req_id,
        "timestamp": ts,
        "max_edge": max_edge,
        "format": fmt,
        "sig": sig,
    }

    resp_key = f"{RESP_PREFIX}{req_id}"

    # Push to queue
    r.rpush(REQ_QUEUE, json.dumps(payload))

    # Wait for response with timeout
    start = time.time()
    while time.time() - start < timeout:
        item = r.blpop(resp_key, timeout=min(2.0, max(0.5, timeout - (time.time() - start))))
        if item:
            _, raw_data = item
            resp = json.loads(raw_data)
            # Verify response signature
            resp_sig_data = f"{req_id}:{resp.get('status')}"
            if not verify_signature(resp_sig_data, resp.get("sig", "")):
                raise ValueError("Screen relay response failed HMAC signature verification")
            if not resp.get("ok"):
                raise RuntimeError(resp.get("error", "Unknown relay capture error"))
            return resp["b64"], resp["media_type"]

    raise TimeoutError(
        f"Screen capture timed out after {timeout:.1f}s. "
        "Ensure the Zoro Screen Relay is running in your logged-in Windows session."
    )


# --- Server Side (Runs in User's Windows Session) ---

def run_relay_service():
    """Run the screen relay listener in the logged-in Windows session."""
    import redis

    print("[screen-relay] Starting Zoro Screen Relay in Windows session...", flush=True)
    attach_to_user_desktop()

    r = _get_redis_client()
    last_heartbeat = 0.0

    print("[screen-relay] Connected to Azure Redis. Listening for capture requests...", flush=True)

    while True:
        now = time.time()
        # Update heartbeat
        if now - last_heartbeat >= 5.0:
            try:
                r.setex(HEARTBEAT_KEY, HEARTBEAT_TTL, f"online:{int(now)}")
                last_heartbeat = now
            except Exception as e:
                print(f"[screen-relay] Heartbeat error: {e}", flush=True)

        try:
            # Wait for request up to 2 seconds
            item = r.blpop(REQ_QUEUE, timeout=2.0)
            if not item:
                continue

            _, raw_req = item
            req = json.loads(raw_req)
            req_id = req.get("req_id")
            ts = req.get("timestamp", 0)
            sig = req.get("sig", "")
            max_edge = req.get("max_edge", 1568)
            fmt = req.get("format", "png")

            # Validate signature and timestamp (within 60s)
            sign_data = f"{req_id}:{ts:.3f}"
            if not verify_signature(sign_data, sig):
                print(f"[screen-relay] Invalid signature for req {req_id}", flush=True)
                continue
            if abs(time.time() - ts) > 60.0:
                print(f"[screen-relay] Request {req_id} expired (timestamp drift)", flush=True)
                continue

            resp_key = f"{RESP_PREFIX}{req_id}"

            try:
                b64, media_type, w, h = capture_local_desktop(max_edge=max_edge, fmt=fmt)
                resp_payload = {
                    "req_id": req_id,
                    "ok": True,
                    "status": "ok",
                    "b64": b64,
                    "media_type": media_type,
                    "width": w,
                    "height": h,
                    "sig": sign_payload(f"{req_id}:ok"),
                }
                r.rpush(resp_key, json.dumps(resp_payload))
                r.expire(resp_key, 30)
                print(f"[screen-relay] Captured and returned screenshot ({w}x{h}, {media_type}) for req {req_id[:8]}", flush=True)
            except Exception as err:
                print(f"[screen-relay] Capture error for req {req_id}: {err}", flush=True)
                err_payload = {
                    "req_id": req_id,
                    "ok": False,
                    "status": "error",
                    "error": str(err),
                    "sig": sign_payload(f"{req_id}:error"),
                }
                r.rpush(resp_key, json.dumps(err_payload))
                r.expire(resp_key, 30)

        except (redis.ConnectionError, redis.TimeoutError) as e:
            print(f"[screen-relay] Redis connection error, reconnecting in 3s: {e}", flush=True)
            time.sleep(3.0)
        except Exception as e:
            print(f"[screen-relay] Unexpected loop error: {e}", flush=True)
            time.sleep(1.0)


if __name__ == "__main__":
    import dotenv
    dotenv.load_dotenv(ZORO_ROOT / ".env")
    run_relay_service()
