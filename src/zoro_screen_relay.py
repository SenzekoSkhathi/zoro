"""Zoro Secure Universal Host & Screen Relay.

Enables the headless Linux agent (running in Azure Container Apps) to capture
screens, run PowerShell, launch Windows apps, and control desktop UI on the
user's active Windows laptop without opening any listening network ports on
the laptop.

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
from typing import Any, Optional, Tuple

logger = logging.getLogger("zoro.host_relay")

# Redis keys with Redis Cluster hash tag {host} to ensure all relay queues hash to the same slot
REQ_QUEUE = "zoro:relay:{host}:screen:requests"
RESP_PREFIX = "zoro:relay:{host}:screen:response:"
SCREEN_REQ_QUEUE = REQ_QUEUE
SCREEN_RESP_PREFIX = RESP_PREFIX

CMD_REQ_QUEUE = "zoro:relay:{host}:cmd:requests"
CMD_RESP_PREFIX = "zoro:relay:{host}:cmd:response:"

HEARTBEAT_KEY = "zoro:relay:{host}:screen:heartbeat"
HOST_HEARTBEAT_KEY = "zoro:relay:{host}:cmd:heartbeat"
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
        hwinsta = user32.OpenWindowStationW("winsta0", False, 0x10000000)
        if hwinsta:
            user32.SetProcessWindowStation(hwinsta)
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
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
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
        import dotenv
        dotenv.load_dotenv(ZORO_ROOT / ".env")
        url = os.environ.get("REDIS_URL")
    if not url:
        raise ValueError("REDIS_URL environment variable is not set")
    return redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_timeout=20.0,
        socket_connect_timeout=10.0,
        socket_keepalive=True,
        health_check_interval=15,
        retry_on_timeout=True,
    )


# --- Client Side (Executed by Worker in Linux Cloud or Local Agent) ---

def is_host_relay_online() -> bool:
    """Check if the Windows host relay heartbeat is active."""
    try:
        r = _get_redis_client()
        return bool(r.exists(HOST_HEARTBEAT_KEY) or r.exists(HEARTBEAT_KEY))
    except Exception:
        return False


# Backwards-compatible alias
is_relay_online = is_host_relay_online


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

    r.rpush(REQ_QUEUE, json.dumps(payload))

    start = time.time()
    while time.time() - start < timeout:
        item: Any = r.blpop(resp_key, timeout=min(2.0, max(0.5, timeout - (time.time() - start))))
        if item:
            _, raw_data = item
            resp = json.loads(raw_data)
            resp_sig_data = f"{req_id}:{resp.get('status')}"
            if not verify_signature(resp_sig_data, resp.get("sig", "")):
                raise ValueError("Screen relay response failed HMAC signature verification")
            if not resp.get("ok"):
                raise RuntimeError(resp.get("error", "Unknown relay capture error"))
            return resp["b64"], resp["media_type"]

    raise TimeoutError(
        f"Screen capture timed out after {timeout:.1f}s. "
        "Ensure the Zoro Host Relay is running in your logged-in Windows session."
    )


def request_host_command(cmd_type: str, params: Optional[dict[str, Any]] = None, timeout: float = 20.0) -> dict[str, Any]:
    """Execute a host command on the user's logged-in Windows laptop over Redis."""
    r = _get_redis_client()
    req_id = str(uuid.uuid4())
    ts = time.time()
    sign_data = f"{req_id}:{cmd_type}:{ts:.3f}"
    sig = sign_payload(sign_data)

    payload = {
        "req_id": req_id,
        "cmd_type": cmd_type,
        "params": params or {},
        "timestamp": ts,
        "sig": sig,
    }

    resp_key = f"{CMD_RESP_PREFIX}{req_id}"

    r.rpush(CMD_REQ_QUEUE, json.dumps(payload))

    start = time.time()
    while time.time() - start < timeout:
        item: Any = r.blpop(resp_key, timeout=min(2.0, max(0.5, timeout - (time.time() - start))))
        if item:
            _, raw_data = item
            resp = json.loads(raw_data)
            resp_sig_data = f"{req_id}:{resp.get('status')}"
            if not verify_signature(resp_sig_data, resp.get("sig", "")):
                raise ValueError("Host relay response failed HMAC signature verification")
            if not resp.get("ok"):
                raise RuntimeError(resp.get("error", "Unknown relay command error"))
            data = resp.get("data")
            return data if isinstance(data, dict) else {}

    raise TimeoutError(
        f"Host command '{cmd_type}' timed out after {timeout:.1f}s. "
        "Ensure the Zoro Host Relay is running in your logged-in Windows session."
    )


# --- Server Side (Runs in User's Windows Session) ---

def _handle_local_host_command(cmd_type: str, params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch and execute host commands locally in the Windows user session."""
    attach_to_user_desktop()

    old_force = os.environ.pop("ZORO_FORCE_RELAY", None)
    old_cloud = os.environ.pop("ZORO_CLOUD", None)
    try:
        import importlib
        if cmd_type == "powershell":
            import zoro_shell
            importlib.reload(zoro_shell)
            cmd = str(params.get("command", ""))
            timeout = int(params.get("timeout", 180))
            return zoro_shell._local_run_powershell(cmd, timeout=timeout)

        elif cmd_type == "open_app":
            import zoro_shell
            importlib.reload(zoro_shell)
            name = str(params.get("name", ""))
            res = zoro_shell._local_open_app(name)
            return res if isinstance(res, dict) else {"name": name}

        elif cmd_type == "list_apps":
            import zoro_shell
            importlib.reload(zoro_shell)
            return {"apps": zoro_shell.list_installed_apps()}

        elif cmd_type == "mouse_click":
            import zoro_tools
            x = params.get("x")
            y = params.get("y")
            btn = str(params.get("button", "left"))
            zoro_tools.mouse_click(x, y, button=btn)
            return {"ok": True}

        elif cmd_type == "type_text":
            import zoro_tools
            text = str(params.get("text", ""))
            zoro_tools.type_text(text)
            return {"ok": True}

        elif cmd_type == "press_keys":
            import zoro_tools
            keys = params.get("keys", [])
            if isinstance(keys, list):
                zoro_tools.press_keys(keys)
            return {"ok": True}

        elif cmd_type == "manage_window":
            import zoro_tools
            action = str(params.get("action", ""))
            zoro_tools.window_action(action)
            return {"ok": True}

        elif cmd_type == "media_key":
            import zoro_tools
            action = str(params.get("action", ""))
            repeat = int(params.get("repeat", 1))
            zoro_tools.media_key(action, repeat=repeat)
            return {"ok": True}

        elif cmd_type == "open_folder":
            import zoro_tools
            target = str(params.get("target", ""))
            path = zoro_tools.open_folder(target)
            return {"path": path}

        elif cmd_type == "open_path":
            import zoro_tools
            path = str(params.get("path", ""))
            opened = zoro_tools.open_path(path)
            return {"path": opened}

        elif cmd_type == "list_folder":
            import zoro_tools
            target = str(params.get("target", ""))
            limit = int(params.get("limit", 40))
            entries = zoro_tools.list_folder(target, limit=limit)
            return {"entries": entries}

        elif cmd_type == "find_files":
            import zoro_tools
            query = str(params.get("query", ""))
            root = params.get("root")
            limit = int(params.get("limit", 15))
            files = zoro_tools.find_files(query, root=root, limit=limit)
            return {"files": files}

        elif cmd_type == "lock_screen":
            import zoro_tools
            zoro_tools.lock_screen()
            return {"ok": True}

        elif cmd_type == "sleep_pc":
            import zoro_tools
            zoro_tools.sleep_pc()
            return {"ok": True}

        else:
            raise ValueError(f"Unknown host relay command: {cmd_type}")
    finally:
        if old_force is not None:
            os.environ["ZORO_FORCE_RELAY"] = old_force
        if old_cloud is not None:
            os.environ["ZORO_CLOUD"] = old_cloud


def run_relay_service():
    """Run the universal host & screen relay listener in the logged-in Windows session."""
    import redis

    print("[host-relay] Starting Zoro Universal Host & Screen Relay in Windows session...", flush=True)
    attach_to_user_desktop()

    r = _get_redis_client()
    last_heartbeat = 0.0

    print("[host-relay] Connected to Azure Redis. Listening for screen and command requests...", flush=True)

    while True:
        now = time.time()
        # Update heartbeat every 5 seconds
        if now - last_heartbeat >= 5.0:
            try:
                val = f"online:{int(now)}"
                r.setex(HOST_HEARTBEAT_KEY, HEARTBEAT_TTL, val)
                r.setex(HEARTBEAT_KEY, HEARTBEAT_TTL, val)
                last_heartbeat = now
            except Exception as e:
                print(f"[host-relay] Heartbeat error: {e}", flush=True)

        try:
            # Block up to 2 seconds waiting on either queue
            item: Any = r.blpop([SCREEN_REQ_QUEUE, CMD_REQ_QUEUE], timeout=2.0)
            if not item:
                continue

            queue_name, raw_req = item
            req = json.loads(raw_req)
            req_id = req.get("req_id")
            ts = req.get("timestamp", 0)
            sig = req.get("sig", "")

            # Replay protection: within 60s
            if abs(time.time() - ts) > 60.0:
                print(f"[host-relay] Request {req_id} expired (timestamp drift)", flush=True)
                continue

            if queue_name == SCREEN_REQ_QUEUE:
                sign_data = f"{req_id}:{ts:.3f}"
                if not verify_signature(sign_data, sig):
                    print(f"[host-relay] Invalid signature for screen req {req_id}", flush=True)
                    continue

                resp_key = f"{RESP_PREFIX}{req_id}"
                max_edge = req.get("max_edge", 1568)
                fmt = req.get("format", "png")

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
                    print(f"[host-relay] Screen captured: {w}x{h} ({req_id[:8]})", flush=True)
                except Exception as err:
                    print(f"[host-relay] Screen capture error: {err}", flush=True)
                    err_payload = {
                        "req_id": req_id,
                        "ok": False,
                        "status": "error",
                        "error": str(err),
                        "sig": sign_payload(f"{req_id}:error"),
                    }
                    r.rpush(resp_key, json.dumps(err_payload))
                    r.expire(resp_key, 30)

            elif queue_name == CMD_REQ_QUEUE:
                cmd_type = req.get("cmd_type", "")
                params = req.get("params", {})
                sign_data = f"{req_id}:{cmd_type}:{ts:.3f}"
                if not verify_signature(sign_data, sig):
                    print(f"[host-relay] Invalid signature for cmd req {req_id}", flush=True)
                    continue

                resp_key = f"{CMD_RESP_PREFIX}{req_id}"

                try:
                    res_data = _handle_local_host_command(cmd_type, params)
                    resp_payload = {
                        "req_id": req_id,
                        "ok": True,
                        "status": "ok",
                        "data": res_data,
                        "sig": sign_payload(f"{req_id}:ok"),
                    }
                    r.rpush(resp_key, json.dumps(resp_payload))
                    r.expire(resp_key, 30)
                    print(f"[host-relay] Executed command '{cmd_type}' successfully ({req_id[:8]})", flush=True)
                except Exception as err:
                    print(f"[host-relay] Command '{cmd_type}' error: {err}", flush=True)
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
            print(f"[host-relay] Redis connection error, reconnecting in 3s: {e}", flush=True)
            time.sleep(3.0)
            try:
                r = _get_redis_client()
            except Exception:
                pass
        except Exception as e:
            print(f"[host-relay] Unexpected loop error: {e}", flush=True)
            time.sleep(1.0)


if __name__ == "__main__":
    import dotenv
    dotenv.load_dotenv(ZORO_ROOT / ".env")
    run_relay_service()
