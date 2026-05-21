"""Print Zoro's tool-flakiness report from zoro_episodes.

Usage (from project root):
    .venv/Scripts/python.exe scripts/zoro_flakiness.py [--days 7]

Reads SUPABASE_URL / SUPABASE_SERVICE_KEY from .env (same as the agent).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

# Windows consoles default to cp1252 and choke on the dash/arrow chars below.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)
sys.path.insert(0, str(ROOT / "src"))

import zoro_episodes  # noqa: E402


def _fmt_pct(x: float) -> str:
    return f"{x * 100:5.1f}%"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--failures", type=int, default=10,
                    help="how many recent failures to list")
    args = ap.parse_args()

    stats = zoro_episodes.tool_stats(args.days)
    if not stats:
        print(f"No episodes in the last {args.days} days "
              "(or Supabase isn't reachable).")
        return 0

    print(f"\nTool flakiness — last {args.days} days "
          f"({sum(s['total'] for s in stats)} episodes)\n")
    print(f"{'tool':<22} {'total':>6} {'ok':>5} {'err':>5} "
          f"{'abrt':>5} {'err%':>7}")
    print("-" * 56)
    for s in stats:
        print(f"{s['tool']:<22} {s['total']:>6} {s['success']:>5} "
              f"{s['error']:>5} {s['aborted']:>5} "
              f"{_fmt_pct(s['error_rate']):>7}")

    fails = zoro_episodes.recent_failures(args.failures)
    if fails:
        print(f"\nRecent failures (last {len(fails)}):\n")
        for f in fails:
            when = (f.get("created_at") or "")[:19].replace("T", " ")
            tool = f.get("tool", "?")
            task = (f.get("task") or "").strip()
            err = (f.get("error") or f.get("result_summary") or "").strip()
            print(f"  {when}  {tool:<20} {task[:60]}")
            if err:
                print(f"     ↳ {err[:100]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
