-- Autonomous self-modification log.
-- Apply once in the Supabase SQL editor (same project as zoro_episodes).
--
-- One row per autonomous self-mod attempt (NOT the user-triggered ones
-- via ask_claude_code — those don't need quota tracking). Used for:
--   * daily cap enforcement: count attempts in the last 24h
--   * "what did Zoro do yesterday" lookback in the morning briefing
--   * audit trail when something breaks unexpectedly
--
-- Status semantics match the merge event from zoro_claude_code:
--   merged       - branch passed boot-check and fast-forwarded to main
--   rolled-back  - boot-check failed OR touched a protected path OR
--                  exceeded the small-change cap
--   no-changes   - Claude finished but didn't actually change anything
--   skipped      - precondition failed (dirty tree, no claude binary)

create table if not exists zoro_self_mod_log (
  id              bigserial primary key,
  created_at      timestamptz not null default now(),
  lesson_id       bigint references zoro_lessons (id) on delete set null,
  role            text,                              -- 'coder', 'researcher', etc
  task            text        not null,              -- the task text given to Claude
  status          text        not null,
  files_changed   integer,
  lines_changed   integer,
  branch          text,
  notes           text                                -- error message or "too large" reason
);

create index if not exists zoro_self_mod_log_recent_idx
  on zoro_self_mod_log (created_at desc);
create index if not exists zoro_self_mod_log_status_idx
  on zoro_self_mod_log (status, created_at desc);
