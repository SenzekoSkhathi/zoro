"""Sub-agent loader.

A "sub-agent" is a markdown file under `agents/` that defines a focused
role Zoro can dispatch to via Claude Code. Each file follows a tiny
convention:

    # Title              <- first line, used as display name
    One-line description <- second non-empty line
    <!-- tools: A,B,C -->
    ...
    Body of the system-prompt addendum...

The body (everything after the front matter) is passed to Claude Code
via `--append-system-prompt`. The tools comment, if present, becomes an
allow-list — every other tool is added to `--disallowedTools`.

Sub-agent execution inherits the full safety harness in
`zoro_claude_code.run_streaming` (branch → boot-check → auto-merge or
rollback), so a misbehaving role still cannot break Zoro's main.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"

# The full set of Claude Code tools we are willing to expose. Anything
# in this set but not in a role's allow-list becomes disallowed. Kept
# explicit so a new Claude Code release can't silently grant a tool to
# a role that wasn't designed to use it.
_KNOWN_TOOLS = frozenset({
    "Read", "Write", "Edit", "Glob", "Grep", "Bash",
    "WebFetch", "WebSearch",
})

_TOOLS_RE = re.compile(r"<!--\s*tools:\s*([^>]+?)\s*-->", re.IGNORECASE)


@dataclass(frozen=True)
class SubAgent:
    name: str
    title: str
    description: str
    allowed_tools: frozenset[str]
    body: str

    @property
    def disallowed_tools(self) -> list[str]:
        """Tools to deny for this role: the safe-set minus the allow-list,
        plus AskUserQuestion/ExitPlanMode which are always denied since
        Claude Code is running headless under Zoro."""
        denied = sorted(_KNOWN_TOOLS - self.allowed_tools)
        return denied + ["AskUserQuestion", "ExitPlanMode"]


def _parse(name: str, text: str) -> SubAgent:
    lines = text.splitlines()
    title = ""
    description = ""
    body_start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not title and stripped.startswith("#"):
            title = stripped.lstrip("# ").strip() or name
            continue
        if title and not description and stripped:
            description = stripped
            body_start = i + 1
            break
    body = "\n".join(lines[body_start:]).strip()

    m = _TOOLS_RE.search(text)
    if m:
        raw = {t.strip() for t in m.group(1).split(",") if t.strip()}
        allowed = frozenset(raw & _KNOWN_TOOLS)
    else:
        # No directive: default-deny writes. Read-only baseline.
        allowed = frozenset({"Read", "Glob", "Grep"})

    return SubAgent(
        name=name,
        title=title or name,
        description=description or "(no description)",
        allowed_tools=allowed,
        body=body,
    )


def list_agents() -> list[SubAgent]:
    """Every parseable agents/*.md file."""
    if not AGENTS_DIR.exists():
        return []
    out: list[SubAgent] = []
    for path in sorted(AGENTS_DIR.glob("*.md")):
        try:
            out.append(_parse(path.stem, path.read_text(encoding="utf-8")))
        except OSError:
            continue
    return out


def load_agent(name: str) -> SubAgent | None:
    """Look up one sub-agent by its file stem (e.g. 'researcher')."""
    if not name:
        return None
    safe = name.strip().lower().replace(" ", "_")
    if not re.fullmatch(r"[a-z0-9_]+", safe or ""):
        return None
    path = AGENTS_DIR / f"{safe}.md"
    if not path.is_file():
        return None
    try:
        return _parse(safe, path.read_text(encoding="utf-8"))
    except OSError:
        return None
