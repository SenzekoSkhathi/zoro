"""Hand a task off to the Claude Code CLI running in the Zoro repo.

Fire-and-forget streaming: we spawn `claude -p "<task>"` in
stream-json mode and yield narration / tool-call / final-result events
so callers can publish live progress to the web view.

Permissions are bypassed — this is a trusted local repo and the user
reviews the diff after. If you point this at anything other than your
own repo, change that.
"""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import AsyncIterator, Optional

ZORO_ROOT = Path(__file__).resolve().parent.parent


def _claude_bin() -> str:
    # On Windows `claude` resolves to a .cmd shim under
    # %APPDATA%\npm; shutil.which finds it.
    return shutil.which("claude") or "claude"


async def run_streaming(
    task: str, cwd: Optional[Path] = None
) -> AsyncIterator[dict]:
    """Yield progress events for a Claude Code run.

    Event shapes:
        {"kind": "narration", "text": str}   — Claude's spoken thought
        {"kind": "tool",      "text": str}   — tool name being invoked
        {"kind": "result",    "text": str,
                              "ok": bool}    — final answer / failure
        {"kind": "error",     "text": str}   — process-level failure
    """
    # Claude defaults to asking clarifying questions via AskUserQuestion,
    # which makes no sense here — nobody is at the terminal. Force
    # autonomy via a system-prompt addendum AND deny the interactive
    # tools outright so Claude must commit to an answer.
    headless_addendum = (
        "You are running in fully autonomous headless mode, spawned by "
        "the user's voice assistant Zoro. There is no human at the "
        "terminal to answer questions. Never call AskUserQuestion or "
        "any other interactive prompt — make a reasonable assumption "
        "and proceed. Your final reply will be read aloud to the user, "
        "so keep it to one or two short sentences summarising what you "
        "did (or, if you genuinely could not proceed, what the blocker "
        "was). Do not narrate your tool use in the final reply."
    )
    proc = await asyncio.create_subprocess_exec(
        _claude_bin(),
        "-p",
        task,
        "--output-format",
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--append-system-prompt",
        headless_addendum,
        "--disallowedTools",
        "AskUserQuestion,ExitPlanMode",
        cwd=str(cwd or ZORO_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None
    try:
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "assistant":
                for block in ev.get("message", {}).get("content", []):
                    btype = block.get("type")
                    if btype == "text":
                        text = (block.get("text") or "").strip()
                        if text:
                            yield {"kind": "narration", "text": text}
                    elif btype == "tool_use":
                        yield {
                            "kind": "tool",
                            "text": block.get("name", "tool"),
                        }
            elif t == "result":
                yield {
                    "kind": "result",
                    "text": ev.get("result") or "",
                    "ok": ev.get("subtype") == "success",
                }
    finally:
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
        if proc.returncode and proc.returncode != 0:
            err = b""
            if proc.stderr:
                try:
                    err = await proc.stderr.read()
                except Exception:  # noqa: BLE001
                    err = b""
            yield {
                "kind": "error",
                "text": (
                    err.decode("utf-8", errors="replace").strip()
                    or f"claude exited with code {proc.returncode}"
                ),
            }
