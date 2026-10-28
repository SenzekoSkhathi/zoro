"""Detect when the user's next utterance is correcting Zoro's last action.

Used to upgrade prior episodes from outcome='success' to 'corrected', which
closes the telemetry loop: silent wrong-answers stop looking like wins.

Cheap pattern-matcher only — no LLM call on the voice loop's hot path. If
the regex layer misses too many real corrections, Phase 2.1 can add a
Claude Haiku classifier behind it.
"""

from __future__ import annotations

import re

# Patterns the user says when something Zoro did was wrong. Anchored to the
# start of the utterance (after stripping filler) — mid-sentence "no" inside
# longer thoughts isn't a correction signal.
_CORRECTION_PATTERNS = [
    # outright rejections
    r"^no[,.!\s]+(?:not |that|wait|wrong|stop|undo)",
    r"^no+[,.!\s]+",
    r"^nope[,.!\s]",
    r"^wrong\b",
    r"^stop\b.*(?:that|doing|it)",
    r"^undo\b",
    r"^cancel that\b",
    # "I said / I meant / I asked"
    r"^i (?:said|meant|asked|wanted)\b",
    r"^that('?s| is) not what i\b",
    r"^that('?s| is) (?:wrong|not right|incorrect|the wrong)\b",
    # "you did / you got"
    r"^you (?:got|did|opened|picked|chose|sent|typed|clicked) the wrong\b",
    r"^you (?:didn'?t|did not)\b.*(?:hear|understand|get)",
    # "try again / again"
    r"^try (?:again|that again)\b",
    r"^do (?:it )?(?:again|over)\b",
    # "actually" only when it leads a correction (heuristic: short utterance)
    r"^actually,?\s+(?:i|that|no)\b",
    # "not X, Y"
    r"^not that\b",
    r"^not the\b",
    # explicit wrong-target callouts
    r"^(?:wrong|other) (?:file|email|app|window|one|person|account|folder)\b",
]

_COMPILED = [re.compile(p, re.IGNORECASE) for p in _CORRECTION_PATTERNS]

# Tools whose effects are read-only / cheap-to-redo — corrections against
# them are noise (user just refined their query). Don't tag these.
_NOISY_TOOLS = {
    "web_search", "describe_screen", "check_calendar", "check_email",
    "get_current_time", "where_am_i", "recall", "read_my_notes",
    "list_folder", "find_files",
}

# How long after a tool fires can a follow-up still count as correcting it.
# Beyond this, the user is starting a new thread, not fixing the last one.
CORRECTION_WINDOW_SECONDS = 90.0


def is_correction(utterance: str) -> bool:
    """Quick pattern check — does this utterance look like the user
    correcting what just happened?"""
    if not utterance:
        return False
    text = utterance.strip().lower()
    if not text:
        return False
    # Strip a leading filler so "uh, no that's wrong" still matches.
    text = re.sub(r"^(?:uh|um|er|ah|so|okay|ok|well)[,.\s]+", "", text)
    return any(p.search(text) for p in _COMPILED)


def should_consider_for(tool_name: str) -> bool:
    """Skip read-only tools — a 'no' after web_search is the user refining
    the search, not flagging a failure."""
    return tool_name not in _NOISY_TOOLS
