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
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, AsyncIterator, Optional

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


# Files autonomous self-mod must never alter. The harness, the safety
# layer, and Zoro's identity. A diff that touches any of these is
# rolled back regardless of boot-check outcome. The user can still
# edit them by hand on main.
_PROTECTED_PATHS = frozenset({
    "src/zoro_safety.py",
    "src/zoro_claude_code.py",
    "src/zoro_prompt.py",
})


async def _branch_touches_protected(branch: str) -> tuple[bool, list[str]]:
    rc, out = await _git("diff", "--name-only", f"main..{branch}")
    if rc != 0:
        return False, []
    changed = [p.strip().replace("\\", "/") for p in out.splitlines() if p.strip()]
    hit = sorted(p for p in changed if p in _PROTECTED_PATHS)
    return bool(hit), hit


async def _branch_diff_stats(branch: str) -> tuple[int, int]:
    """Return (files_changed, lines_changed) for the branch vs main."""
    rc, out = await _git("diff", "--shortstat", f"main..{branch}")
    if rc != 0 or not out.strip():
        return 0, 0
    # Example: " 2 files changed, 30 insertions(+), 4 deletions(-)"
    import re as _re
    files = lines = 0
    m = _re.search(r"(\d+) files? changed", out)
    if m:
        files = int(m.group(1))
    inserts = sum(int(x) for x in _re.findall(r"(\d+) insertions?", out))
    deletes = sum(int(x) for x in _re.findall(r"(\d+) deletions?", out))
    lines = inserts + deletes
    return files, lines


def _search_in_files(work_dir: Path, query: str, directory: str = "src") -> str:
    target_dir = (work_dir / directory).resolve()
    try:
        target_dir.relative_to(work_dir.resolve())
    except ValueError:
        return f"Error: directory {directory} is outside working directory."
    if not target_dir.exists():
        return f"Error: directory {directory} does not exist."
    matches = []
    for f in target_dir.rglob("*.py"):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
            for idx, line in enumerate(text.splitlines(), 1):
                if query.lower() in line.lower():
                    rel = f.relative_to(work_dir).as_posix()
                    matches.append(f"{rel}:{idx}: {line.strip()[:100]}")
                    if len(matches) >= 30:
                        return "\n".join(matches) + "\n...(matches capped at 30)"
        except Exception:
            continue
    return "\n".join(matches) if matches else f"No matches found for '{query}'."


def _execute_local_tool(work_dir: Path, name: str, args: dict) -> str:
    path_arg = args.get("path", ".")
    target_path = (work_dir / path_arg).resolve()
    try:
        rel = target_path.relative_to(work_dir.resolve()).as_posix()
    except ValueError:
        return f"Error: path {path_arg} is outside working directory."

    if rel in _PROTECTED_PATHS:
        return f"Error: {rel} is a protected path. Autonomous edits to it are forbidden."

    if name == "read_file":
        if not target_path.exists() or not target_path.is_file():
            return f"Error: file {path_arg} does not exist."
        try:
            content = target_path.read_text(encoding="utf-8", errors="replace")
            return content if len(content) <= 12000 else content[:12000] + "\n...(truncated)"
        except Exception as e:
            return f"Error reading {path_arg}: {e}"

    elif name == "write_file":
        content = args.get("content", "")
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(content, encoding="utf-8")
            return f"Successfully wrote {len(content)} characters to {path_arg}"
        except Exception as e:
            return f"Error writing {path_arg}: {e}"

    elif name == "edit_file":
        target = args.get("target", "")
        replacement = args.get("replacement", "")
        if not target_path.exists() or not target_path.is_file():
            return f"Error: file {path_arg} does not exist."
        try:
            content = target_path.read_text(encoding="utf-8", errors="replace")
            if target not in content:
                return f"Error: target string not found in {path_arg}."
            new_content = content.replace(target, replacement, 1)
            target_path.write_text(new_content, encoding="utf-8")
            return f"Successfully replaced target in {path_arg}."
        except Exception as e:
            return f"Error editing {path_arg}: {e}"

    elif name == "list_dir":
        if not target_path.exists() or not target_path.is_dir():
            return f"Error: directory {path_arg} does not exist."
        try:
            items = sorted(target_path.iterdir())
            summary = [
                f"{'[DIR] ' if i.is_dir() else '[FILE] '}{i.name}"
                for i in items[:50]
            ]
            return "\n".join(summary)
        except Exception as e:
            return f"Error listing {path_arg}: {e}"

    elif name == "search_in_files":
        query = args.get("query", "")
        directory = args.get("directory", "src")
        return _search_in_files(work_dir, query, directory)

    return f"Error: unknown tool {name}"


async def _run_azure_coder_streaming(
    task: str,
    work_dir: Path,
    system_prompt: str,
    disallowed: list[str],
) -> AsyncIterator[dict]:
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    key = os.environ.get("AZURE_OPENAI_API_KEY")
    dep = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-6-sol")
    if not endpoint or not key:
        yield {"kind": "error", "text": "Neither claude CLI nor Azure OpenAI credentials found."}
        return

    from openai import AzureOpenAI
    client = AzureOpenAI(
        azure_endpoint=endpoint,
        api_key=key,
        api_version=os.environ.get("OPENAI_API_VERSION", "2024-10-21"),
    )

    all_tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read file contents at the given relative path",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Create or completely overwrite a file with given content",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "edit_file",
                "description": "Replace exact target string with replacement string in file",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "target": {"type": "string"},
                        "replacement": {"type": "string"},
                    },
                    "required": ["path", "target", "replacement"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_dir",
                "description": "List files and directories at given relative path",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "default": "."}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_in_files",
                "description": "Search code files for a string or identifier",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "directory": {"type": "string", "default": "src"},
                    },
                    "required": ["query"],
                },
            },
        },
    ]

    tools = [
        t for t in all_tools
        if t["function"]["name"] not in disallowed
    ]

    self_map_path = work_dir / "src" / "SELF_MAP.md"
    self_map_text = ""
    if self_map_path.exists():
        try:
            self_map_text = "\n\nRepository Architecture (SELF_MAP.md):\n" + self_map_path.read_text(encoding="utf-8")[:4000]
        except Exception:
            pass

    full_system = (
        system_prompt
        + "\n\nYou are Zoro's autonomous engineer running on Azure OpenAI. "
        "Keep changes small, clean, robust, and well-targeted. "
        "Never touch protected files (src/zoro_safety.py, src/zoro_claude_code.py, src/zoro_prompt.py). "
        "When finished, reply with a short summary of the changes you made."
        + self_map_text
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": full_system},
        {"role": "user", "content": task},
    ]

    for turn in range(10):
        try:
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: client.chat.completions.create(
                    model=dep,
                    messages=messages,
                    tools=tools if tools else None,
                    reasoning_effort="none",
                    max_completion_tokens=800,
                ),
            )
        except Exception as e:
            yield {"kind": "error", "text": f"Azure OpenAI call failed: {e}"}
            return

        choice = resp.choices[0]
        msg = choice.message
        content = (msg.content or "").strip()
        if content:
            yield {"kind": "narration", "text": content}

        if not msg.tool_calls:
            yield {"kind": "result", "text": content or "Finished changes.", "ok": True}
            return

        assistant_msg: dict[str, Any] = {"role": "assistant"}
        if content:
            assistant_msg["content"] = content
        assistant_msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in msg.tool_calls
        ]
        messages.append(assistant_msg)

        for tc in msg.tool_calls:
            fname = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            summary = args.get("path") or args.get("query") or ""
            yield {"kind": "tool", "text": f"{fname}: {summary}"}

            res = _execute_local_tool(work_dir, fname, args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": res,
            })

    yield {"kind": "result", "text": "Reached turn limit.", "ok": True}


async def run_streaming(
    task: str,
    cwd: Optional[Path] = None,
    role: Optional[str] = None,
    max_files: Optional[int] = None,
    max_lines: Optional[int] = None,
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
    disallowed = ["AskUserQuestion", "ExitPlanMode"]
    if role:
        try:
            import zoro_subagents
            sa = zoro_subagents.load_agent(role)
        except Exception:  # noqa: BLE001
            sa = None
        if sa is not None:
            headless_addendum = headless_addendum + "\n\n" + sa.body
            disallowed = sa.disallowed_tools
        else:
            yield {
                "kind": "narration",
                "text": f"(unknown sub-agent role '{role}', running ungated)",
            }
    has_claude = bool(shutil.which("claude"))
    use_azure = not has_claude or os.environ.get("ZORO_CODER_ENGINE") == "azure"

    if use_azure:
        async for ev in _run_azure_coder_streaming(
            task=task,
            work_dir=work_dir,
            system_prompt=headless_addendum,
            disallowed=disallowed,
        ):
            yield ev
    else:
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
            ",".join(disallowed),
            cwd=str(cwd or ZORO_ROOT),
            stdout=asyncio.subprocess.PIPE,
            # Merge stderr into stdout so the streaming loop below drains
            # both. A separate stderr pipe deadlocks Claude Code on long
            # `--verbose` runs once the ~64 KB stderr buffer fills.
            stderr=asyncio.subprocess.STDOUT,
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
                yield {
                    "kind": "error",
                    "text": f"claude exited with code {proc.returncode}",
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

    # Commit anything still uncommitted so the diff check below sees
    # the full set of changes Claude made.
    if await _has_changes():
        await _git("add", "-A")
        await _git("commit", "-m", f"zoro self-mod (pre-check): {task[:64]}")

    touches_protected, hit = await _branch_touches_protected(branch)
    if touches_protected:
        await _git("checkout", "main")
        await _git("branch", "-D", branch)
        yield {
            "kind": "merge",
            "status": "rolled-back",
            "text": (
                "refused: this change touched protected files "
                + ", ".join(hit)
                + ". Skhathi must edit those by hand."
            ),
        }
        return

    files_changed, lines_changed = await _branch_diff_stats(branch)
    if (max_files is not None and files_changed > max_files) or (
        max_lines is not None and lines_changed > max_lines
    ):
        await _git("checkout", "main")
        await _git("branch", "-D", branch)
        yield {
            "kind": "merge",
            "status": "rolled-back",
            "files_changed": files_changed,
            "lines_changed": lines_changed,
            "text": (
                f"refused: {files_changed} files / {lines_changed} lines "
                f"exceeds the small-change cap "
                f"(max {max_files} files, {max_lines} lines). "
                "Skhathi should review and run this manually."
            ),
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
    merged_branch = branch
    await _git("branch", "-d", branch)
    push_rc, push_out = await _git("push", "origin", "main")
    push_note = " (push failed)" if push_rc != 0 else ""
    yield {
        "kind": "merge",
        "status": "merged",
        "files_changed": files_changed,
        "lines_changed": lines_changed,
        "branch": merged_branch,
        "text": (
            f"merged to main "
            f"({files_changed} files / {lines_changed} lines){push_note}"
        ),
    }
