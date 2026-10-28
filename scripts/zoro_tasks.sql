-- Durable task queue — things Zoro should do later, not just speak.
-- Different from reminders (which just say a string at time T): a task
-- carries enough context that, when it fires, Zoro initiates the work
-- using her current tools (compose an email, run a search, etc.).
--
-- Status lifecycle:
--   pending    -> queued, not yet due
--   fired      -> due_at passed, directive sent to the agent
--   done       -> Zoro marked it complete after handling it
--   cancelled  -> user cancelled before it fired

create table if not exists zoro_tasks (
  id          bigserial primary key,
  created_at  timestamptz not null default now(),
  due_at      timestamptz not null,
  status      text        not null default 'pending',
  title       text        not null,
  detail      text,
  fired_at    timestamptz,
  done_at     timestamptz
);

create index if not exists zoro_tasks_due_idx
  on zoro_tasks (status, due_at);
