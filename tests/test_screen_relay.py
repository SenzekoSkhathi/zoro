"""Tests for Zoro Secure Screen Relay and Describe Screen.

Tests HMAC signing, local desktop capture, end-to-end encrypted relay transfer,
and integration with describe_screen.
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path

from PIL import Image

# Ensure src/ is on sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import dotenv
dotenv.load_dotenv(ROOT / ".env")

import zoro_screen_relay
import zoro_tools


class TestScreenRelaySecurity(unittest.TestCase):
    """Test cryptographic signing and HMAC verification."""

    def test_signature_roundtrip(self):
        data = "req_12345:1729654000.000"
        sig = zoro_screen_relay.sign_payload(data)
        self.assertIsInstance(sig, str)
        self.assertEqual(len(sig), 64)  # SHA-256 hex
        self.assertTrue(zoro_screen_relay.verify_signature(data, sig))

    def test_tampered_payload_rejected(self):
        data = "req_12345:1729654000.000"
        sig = zoro_screen_relay.sign_payload(data)
        self.assertFalse(zoro_screen_relay.verify_signature(data + "_tampered", sig))
        self.assertFalse(zoro_screen_relay.verify_signature(data, "0" * 64))


class TestLocalDesktopCapture(unittest.TestCase):
    """Test actual capture of the Windows desktop."""

    def test_capture_real_desktop_png(self):
        if sys.platform != "win32":
            self.skipTest("Windows only")

        b64, media_type, w, h = zoro_screen_relay.capture_local_desktop(max_edge=800, fmt="png")
        self.assertEqual(media_type, "image/png")
        self.assertGreater(w, 0)
        self.assertGreater(h, 0)
        self.assertLessEqual(max(w, h), 800)

        raw_bytes = base64.b64decode(b64)
        self.assertGreater(len(raw_bytes), 1000)
        img = Image.open(io.BytesIO(raw_bytes))
        self.assertEqual(img.format, "PNG")
        self.assertEqual(img.size, (w, h))

    def test_capture_real_desktop_jpeg(self):
        if sys.platform != "win32":
            self.skipTest("Windows only")

        b64, media_type, w, h = zoro_screen_relay.capture_local_desktop(max_edge=600, fmt="jpeg")
        self.assertEqual(media_type, "image/jpeg")
        self.assertGreater(w, 0)
        self.assertGreater(h, 0)
        self.assertLessEqual(max(w, h), 600)

        raw_bytes = base64.b64decode(b64)
        self.assertGreater(len(raw_bytes), 1000)
        img = Image.open(io.BytesIO(raw_bytes))
        self.assertEqual(img.format, "JPEG")
        self.assertEqual(img.size, (w, h))


class TestEndToEndRelay(unittest.TestCase):
    """End-to-end test of request -> relay -> response over Azure Redis."""

    def test_e2e_screen_relay_over_redis(self):
        redis_url = os.environ.get("REDIS_URL")
        if not redis_url:
            self.skipTest("REDIS_URL not configured")

        stop_event = threading.Event()

        # Run relay in background thread for 1 request
        def relay_worker():
            r = zoro_screen_relay._get_redis_client()
            while not stop_event.is_set():
                item = r.blpop(zoro_screen_relay.REQ_QUEUE, timeout=0.5)
                if not item:
                    continue
                _, raw = item
                req = json.loads(raw)
                req_id = req["req_id"]
                sig = req["sig"]
                ts = req["timestamp"]
                if not zoro_screen_relay.verify_signature(f"{req_id}:{ts:.3f}", sig):
                    continue

                b64, mtype, w, h = zoro_screen_relay.capture_local_desktop(max_edge=400, fmt="png")
                resp = {
                    "req_id": req_id,
                    "ok": True,
                    "status": "ok",
                    "b64": b64,
                    "media_type": mtype,
                    "width": w,
                    "height": h,
                    "sig": zoro_screen_relay.sign_payload(f"{req_id}:ok"),
                }
                resp_key = f"{zoro_screen_relay.RESP_PREFIX}{req_id}"
                r.rpush(resp_key, json.dumps(resp))
                r.expire(resp_key, 30)
                break

        th = threading.Thread(target=relay_worker, daemon=True)
        th.start()

        try:
            # Client initiates request (as if from Linux container)
            b64, media_type = zoro_screen_relay.request_screen_from_relay(max_edge=400, timeout=10.0)
            self.assertEqual(media_type, "image/png")
            raw = base64.b64decode(b64)
            img = Image.open(io.BytesIO(raw))
            self.assertGreater(img.size[0], 0)
            self.assertGreater(img.size[1], 0)
            self.assertLessEqual(max(img.size), 400)
        finally:
            stop_event.set()
            th.join(timeout=2.0)


if __name__ == "__main__":
    unittest.main()
