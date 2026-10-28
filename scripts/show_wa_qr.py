"""Regenerate data/wa_qr.png from the WhatsApp bridge's current QR.

The bridge (services/whatsapp/bridge.js) exposes the latest pairing QR at
GET /qr while it's unauthenticated. WhatsApp rotates that QR every ~20s, so
this loops, re-rendering the PNG every few seconds, and exits once the bridge
reports it has linked (status "ready"/"authenticated"). Pair it with the
auto-refreshing data/wa_qr.html viewer.
"""
from __future__ import annotations

import io
import json
import time
import urllib.request
from pathlib import Path

import qrcode

BRIDGE = "http://127.0.0.1:8767"
OUT = Path(__file__).resolve().parent.parent / "data" / "wa_qr.png"


def _get(path: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{BRIDGE}{path}", timeout=4) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001 - bridge may blip between restarts
        return None


def main() -> None:
    deadline = time.time() + 1800  # give up after 30 minutes
    while time.time() < deadline:
        st = _get("/status") or {}
        if st.get("status") in ("ready", "authenticated"):
            print(f"linked ({st.get('status')}) — stopping")
            return
        qr = (_get("/qr") or {}).get("qr")
        if qr:
            img = qrcode.make(qr)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            OUT.write_bytes(buf.getvalue())
        time.sleep(3)
    print("timed out waiting for link")


if __name__ == "__main__":
    main()
