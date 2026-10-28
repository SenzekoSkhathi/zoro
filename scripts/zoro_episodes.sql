-- Episodic outcome log for Zoro tool calls and computer-use runs.
-- Apply once in the Supabase SQL editor (same project as zoro_profile / zoro_memories).
--
-- Outcome semantics:
--   success    - tool returned normally and the result string doesn't look like a failure
--   error      - exception bubbled out, OR the spoken result starts with "I couldn't"/"failed"/etc.
--   aborted    - user-initiated stand-down or step-limit hit (computer-use only, for now)
--   corrected  - reserved; written later by a Phase 2 process that detects user follow-up
--                corrections referencing this episode

create table if not exists zoro_episodes (
  id            bigserial primary key,
  created_at    timestamptz not null default now(),
  kind          text        not null,           -- 'tool' | 'computer_use'
  tool          text        not null,           -- function_tool name, or 'control_computer'
  task          text,                            -- one-line human task / args summary
  args          jsonb,                           -- raw args (may be null)
  outcome       text        not null,
  latency_ms    integer,
  steps         integer,                         -- computer_use only
  result_summary text,
  error         text,
  session_id    text,
  correction_text text                          -- Phase 2: user's words when this episode was corrected
);

-- If you applied an earlier version of this file, add the column:
alter table zoro_episodes
  add column if not exists correction_text text;

create index if not exists zoro_episodes_tool_idx
  on zoro_episodes (tool, created_at desc);
create index if not exists zoro_episodes_outcome_idx
  on zoro_episodes (outcome, created_at desc);
create index if not exists zoro_episodes_task_idx
  on zoro_episodes (lower(task));
