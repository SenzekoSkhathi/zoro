"""Check Zoro's Autonomous Engineer logs and recent task episodes.

Usage:
    .venv/Scripts/python.exe scripts/engineer_logs.py [--limit 10]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Ensure UTF-8 console output on Windows
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)

import psycopg
from psycopg.rows import dict_row


def get_engineer_episodes(limit: int = 10) -> list[dict]:
    pg_url = os.environ.get("AZURE_POSTGRES_URL") or os.environ.get("DATABASE_URL")
    if not pg_url:
        print("[!] Missing AZURE_POSTGRES_URL in .env")
        return []

    query = """
        SELECT id, created_at, kind, tool, task, outcome, latency_ms, result_summary, error
        FROM zoro.zoro_episodes
        WHERE kind IN ('claude_code', 'sub_agent')
           OR tool IN ('claude_code', 'coder', 'engineer', 'ask_claude_code')
        ORDER BY created_at DESC
        LIMIT %s;
    """
    with psycopg.connect(pg_url, connect_timeout=8, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(query, (limit,))
            return cur.fetchall()


def main():
    parser = argparse.ArgumentParser(description="View Zoro Autonomous Engineer task logs.")
    parser.add_argument("--limit", type=int, default=10, help="Number of recent tasks to display (default: 10)")
    args = parser.parse_args()

    print(f"\nFetching last {args.limit} Engineer tasks from database...\n")
    rows = get_engineer_episodes(args.limit)
    if not rows:
        print("No engineer episodes found.")
        return

    for r in rows:
        outcome = r['outcome']
        status_tag = f"[{outcome.upper()}]"
        print(f"================================================================================")
        print(f" Time:     {r['created_at']}")
        print(f" Status:   {status_tag:<12} (Latency: {r['latency_ms'] or 0}ms)")
        print(f" Role:     {r['tool']} ({r['kind']})")
        print(f" Task:     {r['task']}")
        print(f" Summary:  {r['result_summary']}")
        if r.get('error'):
            print(f" Error:    {r['error']}")
    print(f"================================================================================\n")


if __name__ == "__main__":
    main()

