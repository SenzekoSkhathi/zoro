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
import sys
import time
from pathlib import Path
from typing import AsyncIterator, Optional

ZORO_ROOT = Path(__file__).resolve().parent.parent


def _claude_bin() -> str:
    # On Windows `claude` resolves to a .cmd shim under
    # %APPDATA%\npm; shutil.which finds it.
    return shutil.which("claude") or "claude"


def _git_bin() -> str:
    return shutil.which("git") or "git"


async def _git(*args: str, cwd: Path = ZORO_ROOT) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        _git_bin(), *args, cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", errors="replace").strip()


async def _has_changes() -> bool:
    rc, out = await _git("status", "--porcelain")
    return rc == 0 and bool(out.strip())


async def _boot_check() -> tuple[bool, str]:
    """Compile-check every .py under src/. Cheap, catches syntax breaks.

    Not a full import test — some modules need env / network — but enough
    to refuse a rewrite that doesn't parse.
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "compileall", "-q", str(ZORO_ROOT / "src"),
        cwd=str(ZORO_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    text = out.decode("utf-8", errors="replace").strip()
    return (proc.returncode or 0) == 0, text


async def _rebuild_self_map() -> None:
    script = ZORO_ROOT / "scripts" / "build_self_map.py"
    if not script.exists():
        return
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(script), cwd=str(ZORO_ROOT),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


async def run_streaming(
    task: str, cwd: Optional[Path] = None
) -> AsyncIterator[dict]:
    """Yield progress events for a Claude Code run.

    Wraps the spawn with a self-modification safety harness:
      1. Branch off main (`zoro-self-mod/<ts>`)
      2. Run claude streaming events through
      3. Boot-check the result (compileall src/)
      4. If green: commit, fast-forward main, push, delete branch
         If red: hard-reset and abandon the branch
    Yields one extra {"kind": "merge", ...} event at the end describing
    the outcome.

    Event shapes:
        {"kind": "narration", "text": str}   — Claude's spoken thought
        {"kind": "tool",      "text": str}   — tool name being invoked
        {"kind": "result",    "text": str,
                              "ok": bool}    — final answer / failure
        {"kind": "merge",     "text": str,
                              "status": "merged"|"rolled-back"
                                       |"no-changes"|"skipped"}
        {"kind": "error",     "text": str}   — process-level failure
    """
    work_dir = cwd or ZORO_ROOT
    is_self_mod = work_dir.resolve() == ZORO_ROOT.resolve()
    branch: Optional[str] = None
    if is_self_mod:
        # Ensure we're starting from a clean main; if the working tree
        # is dirty, refuse — we don't want to entangle user-in-progress
        # edits with an autonomous rewrite.
        rc, out = await _git("status", "--porcelain")
        if rc != 0 or out.strip():
            yield {
                "kind": "error",
                "text": "working tree not clean; refusing self-mod",
            }
            return
        await _git("checkout", "main")
        branch = f"zoro-self-mod/{int(time.time())}"
        rc, out = await _git("checkout", "-b", branch)
        if rc != 0:
            yield {"kind": "error", "text": f"could not branch: {out}"}
            return
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

    if not is_self_mod or branch is None:
        return

    # Self-mod cleanup: boot-check → auto-merge or rollback.
    if not await _has_changes():
        # Claude may have already committed its own work, so check the
        # branch vs main rather than the working tree.
        rc, ahead = await _git("rev-list", "--count", f"main..{branch}")
        committed = rc == 0 and ahead.strip() not in ("", "0")
        if not committed:
            await _git("checkout", "main")
            await _git("branch", "-D", branch)
            yield {
                "kind": "merge",
                "status": "no-changes",
                "text": "no changes — nothing to merge",
            }
            return

    ok, log = await _boot_check()
    if not ok:
        # Discard the branch entirely.
        await _git("reset", "--hard", "HEAD")
        await _git("checkout", "main")
        await _git("branch", "-D", branch)
        yield {
            "kind": "merge",
            "status": "rolled-back",
            "text": f"boot check failed, rolled back: {log[:400]}",
        }
        return

    # Commit any uncommitted changes Claude left behind.
    if await _has_changes():
        await _git("add", "-A")
        await _git(
            "commit", "-m", f"zoro self-mod: {task[:72]}",
        )

    # Refresh the self-map so future sessions see any new modules.
    await _rebuild_self_map()
    if await _has_changes():
        await _git("add", "src/SELF_MAP.md")
        await _git("commit", "-m", "Refresh SELF_MAP after self-mod")

    # Fast-forward main and push.
    await _git("checkout", "main")
    rc, mout = await _git("merge", "--ff-only", branch)
    if rc != 0:
        # Shouldn't happen on a clean branch off main, but if it does,
        # leave the branch in place so the user can inspect.
        yield {
            "kind": "merge",
            "status": "skipped",
            "text": f"could not fast-forward main: {mout[:400]}",
        }
        return
    await _git("branch", "-d", branch)
    push_rc, push_out = await _git("push", "origin", "main")
    push_note = " (push failed)" if push_rc != 0 else ""
    yield {
        "kind": "merge",
        "status": "merged",
        "text": f"merged to main{push_note}",
    }
