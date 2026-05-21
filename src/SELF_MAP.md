# Zoro self-map

This is your own source code. You are a Python voice assistant
whose code lives at `c:/Users/senze/Downloads/zoro`. When the user
asks you to change, improve, or extend yourself, use the module
list below to know which file to point Claude Code at via the
`ask_claude_code` tool.

## Source modules (src/)
- `src/zoro_agent.py` — Zoro — voice-first assistant agent.
- `src/zoro_claude_code.py` — Hand a task off to the Claude Code CLI running in the Zoro repo.
- `src/zoro_computer.py` — Vision-based computer-use loop — Zoro's fallback "hands".
- `src/zoro_corrections.py` — Detect when the user's next utterance is correcting Zoro's last action.
- `src/zoro_episodes.py` — Episodic outcome log — Phase 1 telemetry foundation.
- `src/zoro_github.py` — GitHub access via the gh CLI.
- `src/zoro_google.py` — Read-only Google (Calendar + Gmail) access using the token that
- `src/zoro_lessons.py` — Reflection layer — turn failed (and notable successful) episodes into lessons.
- `src/zoro_memory.py` — Zoro's long-term memory — Supabase (PostgREST) access.
- `src/zoro_proactive.py` — Zoro's proactive ambient loop — she speaks first.
- `src/zoro_prompt.py` — Zoro's identity / operating spec — the shared system prompt for Claude.
- `src/zoro_reflector.py` — Nightly self-improvement loop.
- `src/zoro_retry.py` — Bounded retry for transient network failures on idempotent tool reads.
- `src/zoro_safety.py` — Shared safety / gating layer for Zoro's autonomous behaviour.
- `src/zoro_shell.py` — PowerShell access + Windows app launching.
- `src/zoro_spotify.py` — Spotify Web API access using a cached OAuth token.
- `src/zoro_supervisor.py` — Zoro always-on supervisor.
- `src/zoro_tasks.py` — Durable task queue — 'do this thing later'.
- `src/zoro_tools.py` — Pure helper logic for Zoro's tools (no LiveKit imports).
- `src/zoro_whatsapp.py` — WhatsApp access via the local Node bridge (services/whatsapp).
- `src/zoro_youtube.py` — YouTube access: search + transcript fetch + Claude-powered summary.
- `src/wake_service.py` — Zoro on-device wake-word service.

## Scripts (scripts/)
- `scripts/build_self_map.py`
- `scripts/install_autostart.ps1`
- `scripts/uninstall_autostart.ps1`
- `scripts/zoro_autostart.vbs`
- `scripts/zoro_daily_reports.sql`
- `scripts/zoro_episodes.sql`
- `scripts/zoro_flakiness.py`
- `scripts/zoro_lessons.sql`
- `scripts/zoro_spotify_auth.py`
- `scripts/zoro_tasks.sql`

## Other top-level directories
- `services/whatsapp/` — Node bridge that connects WhatsApp Web to Zoro
- `web/` — Next.js dashboard / HUD that Zoro publishes display state to
- `data/` — runtime state (logs, notes, config, reminders) — gitignored
- `models/` — local model files (wake word, etc.) — gitignored
