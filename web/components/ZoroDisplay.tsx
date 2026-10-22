"use client";

import { useEffect, useRef, useState } from "react";
import { useDataChannel, useRoomContext } from "@livekit/components-react";
import { HudPanel } from "./HudPanel";
import { useWidgetLimit } from "./WidgetLimit";
import styles from "./ZoroDisplay.module.css";

// Mirrors zoro_agent.publish_display: {kind, data}. Voice stays primary —
// this surface shows the detail Zoro deliberately doesn't read aloud.
const DISPLAY_TOPIC = "zoro.display";

type SearchSource = { title: string; url: string; content: string };
type SearchData = {
  query: string;
  answer: string;
  results: SearchSource[];
};

type CalEvent = {
  summary: string;
  all_day: boolean;
  start: string;
  when: string;
};
type CalendarData = { hours: number; events: CalEvent[] };

type Mail = { from: string; subject: string; snippet: string };
type InboxData = { messages: Mail[] };

type WeatherData = {
  place: string;
  temp: number;
  condition: string;
  wind: number;
  high: number;
  low: number;
};

type LocationData = {
  city: string;
  region: string;
  country: string;
  timezone: string;
} | null;
type NewsItem = { title: string; link: string; source: string };
type MorningWeather = {
  place: string;
  temp: number;
  condition: string;
  high: number;
  low: number;
  rain: string;
} | null;
type BriefingData = {
  time: string;
  location: LocationData;
  weather: MorningWeather;
  news: NewsItem[];
};

type Note = { stamp: string; text: string };
type Reminder = { text: string; due_ms: number };
type NotesData = { notes: Note[]; reminders: Reminder[] };
type ProactiveData = { text: string };
type VisionData = { answer: string; image_url: string; question: string };

type YTResult = {
  id: string;
  title: string;
  channel: string;
  url: string;
  thumbnail: string;
};
type YouTubeData =
  | { mode: "results"; query: string; results: YTResult[] }
  | { mode: "transcript"; video: YTResult; text: string }
  | { mode: "summary"; video: YTResult; summary: string };

type WhatsAppChat = {
  id: string;
  name: string;
  is_group: boolean;
  unread_count: number;
  last_body: string;
  last_ts: number;
};
type WhatsAppData = { mode: "unread"; chats: WhatsAppChat[] };

type ShellData = {
  command: string;
  exit_code: number;
  stdout: string;
  stderr: string;
};

type GHNotification = {
  repo: string;
  reason: string;
  type: string;
  title: string;
  url: string;
  unread: boolean;
  updated_at: string;
};
type GHPRListItem = {
  number: number;
  title: string;
  url: string;
  repo: string;
  draft: boolean;
  created_at: string;
};
type GHPRDetails = {
  number: number;
  title: string;
  state: string;
  draft: boolean;
  review_decision: string;
  mergeable: string;
  url: string;
  base: string;
  head: string;
  author: string;
  checks_total: number;
  checks_counts: Record<string, number>;
};
type GHCreated = { url: string; title: string };
type GitHubData =
  | { mode: "notifications"; items: GHNotification[] }
  | { mode: "prs"; items: GHPRListItem[] }
  | { mode: "pr"; pr: GHPRDetails }
  | { mode: "created"; pr: GHCreated };

type ClaudeData = {
  task: string;
  status: "running" | "done" | "failed";
  lines: string[];
  result: string;
};

type NowPlayingData =
  | {
      track: string;
      artists: string;
      album: string;
      art_url: string;
      is_playing: boolean;
      progress_ms: number;
      duration_ms: number;
      device: string;
      track_url: string;
      empty?: undefined;
    }
  | { empty: true };

type BrowserData = {
  url?: string;
  title?: string;
  screenshot?: string;
  action?: string;
  status?: string;
  mode?: string;
  waiting_for_user?: boolean;
  is_headed?: boolean;
  auth_required?: boolean;
  reason?: string;
};

type DisplayCard =
  | { kind: "search"; data: SearchData }
  | { kind: "calendar"; data: CalendarData }
  | { kind: "inbox"; data: InboxData }
  | { kind: "weather"; data: WeatherData }
  | { kind: "briefing"; data: BriefingData }
  | { kind: "notes"; data: NotesData }
  | { kind: "proactive"; data: ProactiveData }
  | { kind: "vision"; data: VisionData }
  | { kind: "nowplaying"; data: NowPlayingData }
  | { kind: "claude"; data: ClaudeData }
  | { kind: "github"; data: GitHubData }
  | { kind: "shell"; data: ShellData }
  | { kind: "whatsapp"; data: WhatsAppData }
  | { kind: "youtube"; data: YouTubeData }
  | { kind: "system"; data: any }
  | { kind: "browser"; data: BrowserData };

type WidgetKind = DisplayCard["kind"];

// One widget instance: the latest payload received for its kind, plus a
// timestamp used to cache-bust the vision screenshot and to sort the grid.
type Widget<K extends WidgetKind = WidgetKind> = Extract<
  DisplayCard,
  { kind: K }
> & { ts: number };

function fmtCountdown(ms: number): string {
  if (ms <= 0) return "due now";
  const s = Math.round(ms / 1000);
  if (s < 60) return `in ${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `in ${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `in ${h}h ${m % 60}m`;
}

/** Counts a number up from 0 on mount so metrics "spin up" like an
 * instrument readout rather than snapping into place. */
function useCountUp(target: number, ms = 600): number {
  const [v, setV] = useState(0);
  useEffect(() => {
    let raf = 0;
    const start = performance.now();
    const tick = (now: number) => {
      const p = Math.min(1, (now - start) / ms);
      // easeOutCubic
      setV(target * (1 - Math.pow(1 - p, 3)));
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, ms]);
  return v;
}

function Temp({ value, big = false }: { value: number; big?: boolean }) {
  const v = useCountUp(value);
  return (
    <span className={big ? styles.weatherTemp : undefined}>
      {Math.round(v)}°
    </span>
  );
}

/** Reminder list that re-renders every second so countdowns stay live. */
function ReminderList({ reminders }: { reminders: Reminder[] }) {
  const [, force] = useState(0);
  const ticking = useRef(false);
  ticking.current = reminders.some((r) => r.due_ms - Date.now() > -2000);

  useEffect(() => {
    const id = setInterval(() => {
      if (ticking.current) force((n) => n + 1);
    }, 1000);
    return () => clearInterval(id);
  }, []);

  return (
    <ul className={styles.sources}>
      {reminders.map((r, i) => {
        const remaining = r.due_ms - Date.now();
        return (
          <li key={i} className={styles.event}>
            <span className={styles.when}>{fmtCountdown(remaining)}</span>
            <span className={styles.eventTitle}>{r.text}</span>
          </li>
        );
      })}
    </ul>
  );
}

function fmtClock(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, "0")}`;
}

function NowPlayingCard({
  data,
  ts,
  onClose,
}: {
  data: NowPlayingData;
  ts: number;
  onClose: () => void;
}) {
  // Tick the progress bar locally between server publishes so it feels
  // alive (Spotify only repushes on tool calls, not every second).
  const [now, setNow] = useState<number>(() => Date.now());
  useEffect(() => {
    if (data.empty || !data.is_playing) return;
    const id = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(id);
  }, [data, ts]);

  if (data.empty) {
    return (
      <HudPanel
        key="nowplaying"
        kind="SPOTIFY"
        meta="IDLE"
        ariaLabel="Nothing playing"
        onClose={onClose}
      >
        <p className={styles.answer}>Nothing playing.</p>
      </HudPanel>
    );
  }

  const elapsed = data.is_playing
    ? Math.min(data.duration_ms, data.progress_ms + (now - ts))
    : data.progress_ms;
  const pct = data.duration_ms
    ? Math.min(100, (elapsed / data.duration_ms) * 100)
    : 0;

  return (
    <HudPanel
      key="nowplaying"
      kind="SPOTIFY"
      meta={data.is_playing ? "▶ PLAYING" : "❚❚ PAUSED"}
      ariaLabel="Now playing"
      onClose={onClose}
    >
      <div className={styles.npRow}>
        {data.art_url && (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            className={styles.npArt}
            src={data.art_url}
            alt={`${data.album} album art`}
          />
        )}
        <div className={styles.npText}>
          {data.track_url ? (
            <a
              className={styles.npTrack}
              href={data.track_url}
              target="_blank"
              rel="noopener noreferrer"
            >
              {data.track}
            </a>
          ) : (
            <span className={styles.npTrack}>{data.track}</span>
          )}
          <span className={styles.npArtists}>{data.artists}</span>
          {data.album && <span className={styles.npAlbum}>{data.album}</span>}
          {data.device && (
            <span className={styles.npDevice}>ON {data.device}</span>
          )}
        </div>
      </div>
      <div className={styles.npProgress}>
        <div
          className={styles.npProgressFill}
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className={styles.npTimes}>
        <span>{fmtClock(elapsed)}</span>
        <span>{fmtClock(data.duration_ms)}</span>
      </div>
    </HudPanel>
  );
}

function ghReasonGlyph(reason: string): string {
  switch (reason) {
    case "mention":
      return "@";
    case "review_requested":
      return "✓";
    case "team_mention":
      return "@@";
    case "assign":
      return "◆";
    case "author":
      return "▲";
    case "comment":
      return "💬";
    case "ci_activity":
      return "⚙";
    case "state_change":
      return "↻";
    default:
      return "•";
  }
}

function YouTubeCard({
  data,
  onClose,
}: {
  data: YouTubeData;
  onClose: () => void;
}) {
  if (data.mode === "results") {
    const { query, results } = data;
    return (
      <HudPanel
        key="youtube"
        kind="YOUTUBE"
        meta={
          results.length
            ? `${results.length} HIT${results.length !== 1 ? "S" : ""}`
            : "NO HITS"
        }
        ariaLabel="YouTube results"
        onClose={onClose}
      >
        <p className={styles.ccTask}>{query}</p>
        {results.length === 0 ? (
          <p className={styles.answer}>Nothing found.</p>
        ) : (
          <ul className={styles.sources}>
            {results.map((r) => (
              <li key={r.id} className={styles.ytRow}>
                {r.thumbnail && (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    className={styles.ytThumb}
                    src={r.thumbnail}
                    alt={`Thumbnail for ${r.title}`}
                  />
                )}
                <div className={styles.ytText}>
                  <a
                    href={r.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className={styles.sourceTitle}
                  >
                    {r.title}
                  </a>
                  <span className={styles.sourceHost}>{r.channel}</span>
                </div>
              </li>
            ))}
          </ul>
        )}
      </HudPanel>
    );
  }

  if (data.mode === "transcript") {
    const v = data.video;
    return (
      <HudPanel
        key="youtube"
        kind="YOUTUBE TRANSCRIPT"
        meta={v.channel || "TRANSCRIPT"}
        ariaLabel="YouTube transcript"
        onClose={onClose}
      >
        <p className={styles.ccTask}>
          {v.url ? (
            <a
              href={v.url}
              target="_blank"
              rel="noopener noreferrer"
              className={styles.sourceTitle}
            >
              {v.title}
            </a>
          ) : (
            v.title
          )}
        </p>
        <div className={styles.ytTranscript}>{data.text}</div>
      </HudPanel>
    );
  }

  // mode === "summary"
  const v = data.video;
  return (
    <HudPanel
      key="youtube"
      kind="YOUTUBE SUMMARY"
      meta={v.channel || "SUMMARY"}
      ariaLabel="YouTube summary"
      onClose={onClose}
    >
      <p className={styles.ccTask}>
        {v.url ? (
          <a
            href={v.url}
            target="_blank"
            rel="noopener noreferrer"
            className={styles.sourceTitle}
          >
            {v.title}
          </a>
        ) : (
          v.title
        )}
      </p>
      <p className={styles.answer}>{data.summary}</p>
    </HudPanel>
  );
}

function WhatsAppCard({
  data,
  onClose,
}: {
  data: WhatsAppData;
  onClose: () => void;
}) {
  const chats = data.chats;
  const total = chats.reduce((s, c) => s + (c.unread_count || 0), 0);
  return (
    <HudPanel
      key="whatsapp"
      kind="WHATSAPP"
      meta={
        chats.length
          ? `${total} UNREAD / ${chats.length} CHATS`
          : "CLEAR"
      }
      ariaLabel="WhatsApp unread"
      onClose={onClose}
    >
      {chats.length === 0 ? (
        <p className={styles.answer}>No unread WhatsApp messages.</p>
      ) : (
        <ul className={styles.sources}>
          {chats.map((c) => (
            <li key={c.id} className={styles.source}>
              <span className={styles.sourceTitle}>
                {c.name}
                {c.is_group && (
                  <span className={styles.allDay}> · group</span>
                )}
                {c.unread_count > 1 && (
                  <span className={styles.sourceHost}>
                    {"  "}
                    {c.unread_count} new
                  </span>
                )}
              </span>
              {c.last_body && (
                <p className={styles.sourceSnippet}>{c.last_body}</p>
              )}
            </li>
          ))}
        </ul>
      )}
    </HudPanel>
  );
}

function ShellCard({
  data,
  onClose,
}: {
  data: ShellData;
  onClose: () => void;
}) {
  const ok = data.exit_code === 0;
  return (
    <HudPanel
      key="shell"
      kind="POWERSHELL"
      meta={ok ? "✓ EXIT 0" : `× EXIT ${data.exit_code}`}
      accent={ok ? undefined : "gold"}
      ariaLabel="PowerShell command result"
      onClose={onClose}
    >
      <p className={styles.ccTask}>{data.command}</p>
      {data.stdout && (
        <>
          <p className={styles.sectionLabel}>stdout</p>
          <div className={styles.ccLog}>
            <div className={styles.ccLogLine}>{data.stdout}</div>
          </div>
        </>
      )}
      {data.stderr && (
        <>
          <p className={styles.sectionLabel}>stderr</p>
          <div className={styles.ccLog}>
            <div className={styles.ccLogLine}>{data.stderr}</div>
          </div>
        </>
      )}
      {!data.stdout && !data.stderr && (
        <p className={styles.answer}>(no output)</p>
      )}
    </HudPanel>
  );
}

function GitHubCard({
  data,
  onClose,
}: {
  data: GitHubData;
  onClose: () => void;
}) {
  if (data.mode === "notifications") {
    const items = data.items;
    const unread = items.filter((n) => n.unread).length;
    return (
      <HudPanel
        key="github"
        kind="GITHUB"
        meta={items.length ? `${unread} UNREAD / ${items.length}` : "CLEAR"}
        ariaLabel="GitHub notifications"
        onClose={onClose}
      >
        {items.length === 0 ? (
          <p className={styles.answer}>No notifications.</p>
        ) : (
          <ul className={styles.sources}>
            {items.map((n, i) => (
              <li
                key={i}
                className={styles.source}
                style={{ opacity: n.unread ? 1 : 0.55 }}
              >
                <span className={styles.ghRow}>
                  <span className={styles.ghGlyph} aria-hidden>
                    {ghReasonGlyph(n.reason)}
                  </span>
                  {n.url ? (
                    <a
                      href={n.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className={styles.sourceTitle}
                    >
                      {n.title}
                    </a>
                  ) : (
                    <span className={styles.sourceTitle}>{n.title}</span>
                  )}
                </span>
                <span className={styles.sourceHost}>
                  {n.repo} · {n.reason.replace("_", " ")} · {n.type}
                </span>
              </li>
            ))}
          </ul>
        )}
      </HudPanel>
    );
  }

  if (data.mode === "prs") {
    const items = data.items;
    const ready = items.filter((p) => !p.draft).length;
    return (
      <HudPanel
        key="github"
        kind="GITHUB PRs"
        meta={
          items.length
            ? `${ready} READY / ${items.length}`
            : "NONE OPEN"
        }
        ariaLabel="Open pull requests"
        onClose={onClose}
      >
        {items.length === 0 ? (
          <p className={styles.answer}>No open pull requests.</p>
        ) : (
          <ul className={styles.sources}>
            {items.map((p) => (
              <li key={p.url} className={styles.source}>
                <a
                  href={p.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className={styles.sourceTitle}
                >
                  #{p.number} · {p.title}
                </a>
                <span className={styles.sourceHost}>
                  {p.repo}
                  {p.draft && " · draft"}
                </span>
              </li>
            ))}
          </ul>
        )}
      </HudPanel>
    );
  }

  if (data.mode === "pr") {
    const pr = data.pr;
    const counts = pr.checks_counts || {};
    return (
      <HudPanel
        key="github"
        kind="GITHUB PR"
        meta={`#${pr.number} · ${pr.state}${pr.draft ? " · DRAFT" : ""}`}
        ariaLabel="Pull request detail"
        onClose={onClose}
      >
        <p className={styles.ccTask}>
          {pr.url ? (
            <a
              href={pr.url}
              target="_blank"
              rel="noopener noreferrer"
              className={styles.sourceTitle}
            >
              {pr.title}
            </a>
          ) : (
            pr.title
          )}
        </p>
        <ul className={styles.sources}>
          <li className={styles.event}>
            <span className={styles.when}>BRANCH</span>
            <span className={styles.eventTitle}>
              {pr.head} → {pr.base}
            </span>
          </li>
          <li className={styles.event}>
            <span className={styles.when}>AUTHOR</span>
            <span className={styles.eventTitle}>{pr.author}</span>
          </li>
          <li className={styles.event}>
            <span className={styles.when}>REVIEWS</span>
            <span className={styles.eventTitle}>
              {(pr.review_decision || "PENDING").replace("_", " ")}
            </span>
          </li>
          <li className={styles.event}>
            <span className={styles.when}>MERGEABLE</span>
            <span className={styles.eventTitle}>{pr.mergeable}</span>
          </li>
          <li className={styles.event}>
            <span className={styles.when}>CHECKS</span>
            <span className={styles.eventTitle}>
              {pr.checks_total === 0
                ? "none"
                : Object.entries(counts)
                    .map(([k, v]) => `${v} ${k.toLowerCase()}`)
                    .join(", ")}
            </span>
          </li>
        </ul>
      </HudPanel>
    );
  }

  // mode === "created"
  return (
    <HudPanel
      key="github"
      kind="GITHUB PR"
      meta="✓ OPENED"
      ariaLabel="Pull request opened"
      onClose={onClose}
    >
      <p className={styles.ccTask}>{data.pr.title}</p>
      <a
        className={styles.sourceTitle}
        href={data.pr.url}
        target="_blank"
        rel="noopener noreferrer"
      >
        {data.pr.url}
      </a>
    </HudPanel>
  );
}

function ClaudeCard({
  data,
  onClose,
}: {
  data: ClaudeData;
  onClose: () => void;
}) {
  // Pin the log to the bottom as new lines stream in.
  const logRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [data.lines.length]);

  const badge =
    data.status === "running"
      ? "▶ RUNNING"
      : data.status === "done"
      ? "✓ DONE"
      : "× FAILED";

  return (
    <HudPanel
      key="claude"
      kind="CLAUDE"
      meta={badge}
      accent={data.status === "failed" ? "gold" : undefined}
      ariaLabel="Claude Code session"
      onClose={onClose}
    >
      <p className={styles.ccTask}>{data.task}</p>
      <div className={styles.ccLog} ref={logRef}>
        {data.lines.map((l, i) => (
          <div
            key={i}
            className={
              l.startsWith("→ ") ? styles.ccLogTool : styles.ccLogLine
            }
          >
            {l}
          </div>
        ))}
        {data.status === "running" && data.lines.length === 0 && (
          <div className={styles.ccLogLine}>Starting Claude…</div>
        )}
      </div>
      {data.result && (
        <>
          <p className={styles.sectionLabel}>
            {data.status === "failed" ? "Error" : "Result"}
          </p>
          <p className={styles.answer}>{data.result}</p>
        </>
      )}
    </HudPanel>
  );
}

function SystemCard({ data, onClose }: { data: any; onClose: () => void }) {
  const { battery, cpu_percent, memory, disk, summary } = data || {};
  return (
    <HudPanel kind="SYSTEM" meta="DIAGNOSTICS" ariaLabel="System Telemetry" onClose={onClose}>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "10px", marginBottom: "12px" }}>
        <div style={{ background: "rgba(10, 25, 35, 0.6)", padding: "10px", borderRadius: "6px", border: "1px solid rgba(79, 227, 255, 0.2)" }}>
          <div style={{ fontSize: "0.68rem", color: "#74aec4", letterSpacing: "0.15em", textTransform: "uppercase" }}>Battery</div>
          <div style={{ fontSize: "1.3rem", fontWeight: 700, color: (battery?.percent || 100) < 20 ? "#ff5c77" : "#a6e9ff" }}>
            {battery?.available ? `${battery.percent}%` : "AC Power"}
          </div>
          <div style={{ fontSize: "0.72rem", color: "#5fb4cf" }}>
            {battery?.available ? (battery.power_plugged ? "Charging" : "Discharging") : "Desktop PC"}
          </div>
        </div>

        <div style={{ background: "rgba(10, 25, 35, 0.6)", padding: "10px", borderRadius: "6px", border: "1px solid rgba(79, 227, 255, 0.2)" }}>
          <div style={{ fontSize: "0.68rem", color: "#74aec4", letterSpacing: "0.15em", textTransform: "uppercase" }}>CPU Usage</div>
          <div style={{ fontSize: "1.3rem", fontWeight: 700, color: (cpu_percent || 0) > 80 ? "#ffaa40" : "#a6e9ff" }}>
            {Math.round(cpu_percent || 0)}%
          </div>
          <div style={{ fontSize: "0.72rem", color: "#5fb4cf" }}>Active Utilization</div>
        </div>

        <div style={{ background: "rgba(10, 25, 35, 0.6)", padding: "10px", borderRadius: "6px", border: "1px solid rgba(79, 227, 255, 0.2)" }}>
          <div style={{ fontSize: "0.68rem", color: "#74aec4", letterSpacing: "0.15em", textTransform: "uppercase" }}>Memory (RAM)</div>
          <div style={{ fontSize: "1.3rem", fontWeight: 700, color: (memory?.percent || 0) > 85 ? "#ffaa40" : "#a6e9ff" }}>
            {Math.round(memory?.percent || 0)}%
          </div>
          <div style={{ fontSize: "0.72rem", color: "#5fb4cf" }}>
            {memory?.used_gb} GB / {memory?.total_gb} GB
          </div>
        </div>

        <div style={{ background: "rgba(10, 25, 35, 0.6)", padding: "10px", borderRadius: "6px", border: "1px solid rgba(79, 227, 255, 0.2)" }}>
          <div style={{ fontSize: "0.68rem", color: "#74aec4", letterSpacing: "0.15em", textTransform: "uppercase" }}>Disk (C:)</div>
          <div style={{ fontSize: "1.3rem", fontWeight: 700, color: (disk?.percent || 0) > 90 ? "#ff5c77" : "#a6e9ff" }}>
            {disk?.free_gb} GB free
          </div>
          <div style={{ fontSize: "0.72rem", color: "#5fb4cf" }}>
            {Math.round(disk?.percent || 0)}% utilized
          </div>
        </div>
      </div>
      {summary && <p className={styles.answer} style={{ fontSize: "0.85rem", opacity: 0.9 }}>{summary}</p>}
    </HudPanel>
  );
}

function BrowserCard({ data, onClose }: { data: BrowserData; onClose: () => void }) {
  const room = useRoomContext();
  const [isSending, setIsSending] = useState(false);
  const { url, title, screenshot, action, status, mode, waiting_for_user, auth_required, reason } = data || {};

  const isTakeover = Boolean(waiting_for_user || mode === "user_takeover" || status === "waiting_for_user");

  const handleResume = async () => {
    if (isSending || !room?.localParticipant) return;
    setIsSending(true);
    try {
      const payload = new TextEncoder().encode(JSON.stringify({ action: "browser_resume" }));
      await room.localParticipant.publishData(payload, { reliable: true, topic: "zoro.display" });
    } catch (e) {
      console.error("[ZoroDisplay] Failed to publish browser_resume:", e);
    } finally {
      setTimeout(() => setIsSending(false), 2000);
    }
  };

  const handleHandoff = async () => {
    if (isSending || !room?.localParticipant) return;
    setIsSending(true);
    try {
      const payload = new TextEncoder().encode(JSON.stringify({ action: "browser_handoff", reason: "User requested takeover" }));
      await room.localParticipant.publishData(payload, { reliable: true, topic: "zoro.display" });
    } catch (e) {
      console.error("[ZoroDisplay] Failed to publish browser_handoff:", e);
    } finally {
      setTimeout(() => setIsSending(false), 2000);
    }
  };

  return (
    <HudPanel
      kind="BROWSER"
      meta={title || "ZORO BROWSER"}
      accent={isTakeover ? "gold" : "cyan"}
      ariaLabel="Dedicated Browser View"
      onClose={onClose}
    >
      {/* Takeover Active Notice */}
      {isTakeover && (
        <div className={styles.takeoverBanner}>
          <div className={styles.takeoverHeader}>
            <div className={styles.takeoverPulse} />
            <span className={styles.takeoverBadge}>HUMAN-IN-THE-LOOP TAKEOVER ACTIVE</span>
          </div>
          <p className={styles.takeoverText}>
            A visible Chromium window is open on your desktop{reason ? ` for ${reason}` : ""}.
            Please log in, solve any CAPTCHA/2FA, and click below when finished.
          </p>
          <button
            type="button"
            className={styles.takeoverButton}
            onClick={handleResume}
            disabled={isSending}
          >
            {isSending ? "Handing back to Zoro..." : "⚡ I'm Done — Hand Back to Zoro"}
          </button>
        </div>
      )}

      {/* Proactive Auth Gate Prompt (when detected and not yet handed off) */}
      {!isTakeover && auth_required && (
        <div className={styles.authGateBanner}>
          <p className={styles.authGateText}>
            🔐 <strong>Authentication Gate Detected:</strong> Login, password, or security check required.
          </p>
          <button
            type="button"
            className={styles.authGateButton}
            onClick={handleHandoff}
            disabled={isSending}
          >
            Take Over Window
          </button>
        </div>
      )}

      {/* URL bar */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "8px",
          marginBottom: "8px",
          background: isTakeover ? "rgba(35, 25, 10, 0.7)" : "rgba(10, 25, 35, 0.7)",
          padding: "6px 10px",
          borderRadius: "4px",
          border: isTakeover
            ? "1px solid rgba(255, 179, 0, 0.35)"
            : "1px solid rgba(79, 227, 255, 0.2)",
        }}
      >
        <div
          style={{
            width: "8px",
            height: "8px",
            borderRadius: "50%",
            background: isTakeover ? "#ffb300" : "#4fe3ff",
            boxShadow: isTakeover ? "0 0 8px #ffb300" : "0 0 8px #4fe3ff",
          }}
        />
        <span
          style={{
            fontFamily: "var(--font-geist-mono), monospace",
            fontSize: "0.75rem",
            color: isTakeover ? "#ffe082" : "#a6e9ff",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {url || "about:blank"}
        </span>
      </div>

      {/* Viewport Screenshot */}
      {screenshot && (
        <div
          style={{
            position: "relative",
            borderRadius: "6px",
            overflow: "hidden",
            border: isTakeover
              ? "1px solid rgba(255, 179, 0, 0.35)"
              : "1px solid rgba(79, 227, 255, 0.25)",
            maxHeight: "400px",
          }}
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={`data:image/jpeg;base64,${screenshot}`}
            alt="Zoro browser viewport"
            style={{ width: "100%", height: "auto", display: "block" }}
          />
        </div>
      )}

      {/* Action / Status footer */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: "8px" }}>
        <span style={{ fontSize: "0.75rem", color: isTakeover ? "#ffca28" : "#74aec4", fontStyle: "italic" }}>
          {action || (isTakeover ? "Waiting for user authentication..." : "Idle")}
        </span>
        <span
          style={{
            fontSize: "0.65rem",
            padding: "2px 6px",
            background: isTakeover ? "rgba(255, 179, 0, 0.2)" : "rgba(79, 227, 255, 0.15)",
            borderRadius: "3px",
            color: isTakeover ? "#ffd54f" : "#4fe3ff",
            letterSpacing: "0.1em",
            textTransform: "uppercase",
            fontWeight: 600,
          }}
        >
          {isTakeover ? "User Takeover" : "Autonomous"}
        </span>
      </div>
    </HudPanel>
  );
}

function weatherGlyph(condition: string): string {
  const c = condition.toLowerCase();
  if (c.includes("thunder")) return "⛈";
  if (c.includes("snow")) return "❄";
  if (c.includes("rain") || c.includes("drizzle") || c.includes("shower"))
    return "🌧";
  if (c.includes("fog")) return "🌫";
  if (c.includes("overcast") || c.includes("cloud")) return "☁";
  if (c.includes("clear")) return "☀";
  return "🌤";
}

function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

/**
 * Subscribes to the agent's `zoro.display` data channel and renders the
 * latest content card inside the shared ARC-HUD shell. Must live inside
 * <LiveKitRoom>.
 */
export function ZoroDisplay({
  onActiveChange,
}: {
  /** Fires whenever a widget appears / the last one is dismissed, so the
   * parent can dock the orb while content is on screen. */
  onActiveChange?: (active: boolean) => void;
}) {
  // Multi-widget surface: one entry per kind, latest publish wins for
  // that kind, dismiss removes it. Order at render is oldest→newest by ts.
  const [widgets, setWidgets] = useState<
    Partial<Record<WidgetKind, Widget>>
  >({});

  // Shared two-widget limiter, spanning grid widgets *and* the chat panel.
  const { allowed, open, close: limitClose, clear: limitClear } =
    useWidgetLimit();

  const { message } = useDataChannel(DISPLAY_TOPIC);

  useEffect(() => {
    if (!message) return;
    try {
      const text = new TextDecoder().decode(message.payload);
      console.log("[ZoroDisplay] message received", message.topic, text);
      const parsed = JSON.parse(text) as
        | (DisplayCard & { refresh?: boolean })
        | { kind: "chat" }
        | { kind: "close"; data: { target: string } };
      if (!parsed?.kind) return;
      // Chat history is owned by <Transcript> — leave its frames alone.
      if (parsed.kind === "chat") return;
      if (parsed.kind === "close") {
        const t = parsed.data?.target;
        if (t === "all") {
          setWidgets({});
          limitClear();
        } else if (t) {
          setWidgets((w) => {
            if (!(t in w)) return w;
            const next = { ...w };
            delete next[t as WidgetKind];
            return next;
          });
          limitClose(t);
        }
        return;
      }
      // Anything else (e.g. session-control frames) isn't a widget — drop it.
      const widgetKinds = new Set<WidgetKind>([
        "search",
        "calendar",
        "inbox",
        "weather",
        "briefing",
        "notes",
        "proactive",
        "vision",
        "nowplaying",
        "claude",
        "github",
        "shell",
        "whatsapp",
        "youtube",
      ]);
      if (!widgetKinds.has(parsed.kind as WidgetKind)) return;
      const kind = parsed.kind as WidgetKind;
      const incoming = { ...parsed, ts: Date.now() } as Widget;
      // A `refresh` frame is a live update (e.g. the Spotify watcher tracking
      // a track change): refresh the card in place if it's still being shown,
      // but never open/resurface a card the user has already dismissed.
      if ("refresh" in parsed && parsed.refresh) {
        setWidgets((w) => (kind in w ? { ...w, [kind]: incoming } : w));
        return;
      }
      // Upsert this kind's widget with a fresh timestamp, then register the
      // open with the shared limiter. The limiter enforces the two-widget max
      // and may evict the oldest surface (a grid widget or the chat panel);
      // the reconcile effect below drops anything that falls out of `allowed`.
      setWidgets((w) => ({ ...w, [kind]: incoming }));
      open(kind);
    } catch (e) {
      console.warn("[ZoroDisplay] bad frame", e);
    }
  }, [message, open, limitClose, limitClear]);

  // On-screen widgets are exactly those the shared limiter still permits.
  // A kind the limiter evicted drops out of `allowed`, so it stops rendering
  // even though its (now stale) payload lingers in `widgets` until that kind
  // is re-published. Render order: oldest first so newly-opened widgets land
  // on the right / bottom of the grid instead of shoving established ones.
  const list = (Object.values(widgets) as Widget[])
    .filter((w) => allowed.has(w.kind))
    .sort((a, b) => a.ts - b.ts);
  const active = list.length > 0;

  useEffect(() => {
    onActiveChange?.(active);
  }, [active, onActiveChange]);

  const close = (kind: WidgetKind) => {
    limitClose(kind);
    setWidgets((w) => {
      if (!(kind in w)) return w;
      const next = { ...w };
      delete next[kind];
      return next;
    });
  };

  if (!active) return null;

  return (
    <div className={styles.grid}>
      {list.map((w) => renderWidget(w, () => close(w.kind)))}
    </div>
  );
}

function renderWidget(w: Widget, onClose: () => void) {
  if (w.kind === "search") {
    const { query, answer, results } = w.data;
    return (
      <HudPanel
        key={w.kind}
        kind="SEARCH"
        meta={query}
        ariaLabel="Search result"
        onClose={onClose}
      >
        {answer && <p className={styles.answer}>{answer}</p>}
        {results.length > 0 && (
          <>
            <p className={styles.sectionLabel}>
              Sources · {results.length}
            </p>
            <ul className={styles.sources}>
              {results.map((r, i) => (
                <li key={r.url || i} className={styles.source}>
                  <a
                    href={r.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className={styles.sourceTitle}
                  >
                    {r.title || hostOf(r.url)}
                  </a>
                  <span className={styles.sourceHost}>{hostOf(r.url)}</span>
                  {r.content && (
                    <p className={styles.sourceSnippet}>{r.content}</p>
                  )}
                </li>
              ))}
            </ul>
          </>
        )}
      </HudPanel>
    );
  }

  if (w.kind === "calendar") {
    const { hours, events } = w.data;
    return (
      <HudPanel
        key={w.kind}
        kind="CALENDAR"
        meta={`NEXT ${hours}H · ${events.length}`}
        ariaLabel="Calendar"
        onClose={onClose}
      >
        {events.length === 0 ? (
          <p className={styles.answer}>Nothing scheduled.</p>
        ) : (
          <ul className={styles.sources}>
            {events.map((e, i) => (
              <li key={i} className={styles.event}>
                <span className={styles.when}>{e.when}</span>
                <span className={styles.eventTitle}>{e.summary}</span>
                {e.all_day && <span className={styles.allDay}>all day</span>}
              </li>
            ))}
          </ul>
        )}
      </HudPanel>
    );
  }

  if (w.kind === "inbox") {
    const { messages } = w.data;
    return (
      <HudPanel
        key={w.kind}
        kind="INBOX"
        meta={messages.length ? `${messages.length} UNREAD` : "CLEAR"}
        ariaLabel="Inbox"
        onClose={onClose}
      >
        {messages.length === 0 ? (
          <p className={styles.answer}>No unread mail.</p>
        ) : (
          <ul className={styles.sources}>
            {messages.map((m, i) => (
              <li key={i} className={styles.source}>
                <span className={styles.sourceTitle}>{m.subject}</span>
                <span className={styles.sourceHost}>{m.from}</span>
                {m.snippet && (
                  <p className={styles.sourceSnippet}>{m.snippet}</p>
                )}
              </li>
            ))}
          </ul>
        )}
      </HudPanel>
    );
  }

  if (w.kind === "weather") {
    const d = w.data;
    return (
      <HudPanel
        key={w.kind}
        kind="WEATHER"
        meta={d.place}
        ariaLabel="Weather"
        onClose={onClose}
      >
        <div className={styles.weatherMain}>
          <span className={styles.weatherGlyph} aria-hidden>
            {weatherGlyph(d.condition)}
          </span>
          <Temp value={d.temp} big />
          <span className={styles.weatherCond}>{d.condition}</span>
        </div>
        <div className={styles.weatherMeta}>
          <span>
            H <Temp value={d.high} />
          </span>
          <span>
            L <Temp value={d.low} />
          </span>
          <span>WIND {d.wind} km/h</span>
        </div>
      </HudPanel>
    );
  }

  if (w.kind === "briefing") {
    const { time, location, weather, news } = w.data;
    return (
      <HudPanel
        key={w.kind}
        kind="BRIEFING"
        meta={time}
        ariaLabel="Morning briefing"
        onClose={onClose}
      >
        {location && (
          <p className={styles.briefLoc}>
            {[location.city, location.region, location.country]
              .filter(Boolean)
              .join(" · ")}
          </p>
        )}
        {weather && (
          <>
            <div className={styles.weatherMain}>
              <span className={styles.weatherGlyph} aria-hidden>
                {weatherGlyph(weather.condition)}
              </span>
              <Temp value={weather.temp} big />
              <span className={styles.weatherCond}>{weather.condition}</span>
            </div>
            <div className={styles.weatherMeta}>
              <span>
                H <Temp value={weather.high} />
              </span>
              <span>
                L <Temp value={weather.low} />
              </span>
            </div>
            {weather.rain && (
              <p className={styles.briefRain}>{weather.rain}</p>
            )}
          </>
        )}
        {news.length > 0 && (
          <>
            <p className={styles.sectionLabel}>Headlines · {news.length}</p>
            <ul className={styles.sources}>
              {news.map((n, i) => (
                <li key={n.link || i} className={styles.source}>
                  {n.link ? (
                    <a
                      href={n.link}
                      target="_blank"
                      rel="noopener noreferrer"
                      className={styles.sourceTitle}
                    >
                      {n.title}
                    </a>
                  ) : (
                    <span className={styles.sourceTitle}>{n.title}</span>
                  )}
                  {n.source && (
                    <span className={styles.sourceHost}>{n.source}</span>
                  )}
                </li>
              ))}
            </ul>
          </>
        )}
      </HudPanel>
    );
  }

  if (w.kind === "notes") {
    const { notes, reminders } = w.data;
    const meta = reminders.length
      ? `${reminders.length} TIMER${reminders.length > 1 ? "S" : ""}`
      : `${notes.length} NOTE${notes.length === 1 ? "" : "S"}`;
    return (
      <HudPanel
        key={w.kind}
        kind="NOTES"
        meta={meta}
        ariaLabel="Notes and reminders"
        onClose={onClose}
      >
        {reminders.length > 0 && (
          <>
            <p className={styles.sectionLabel}>Reminders</p>
            <ReminderList reminders={reminders} />
          </>
        )}
        {notes.length > 0 ? (
          <>
            <p className={styles.sectionLabel}>Notes</p>
            <ul className={styles.sources}>
              {notes.map((n, i) => (
                <li key={i} className={styles.source}>
                  <span className={styles.sourceTitle}>{n.text}</span>
                  {n.stamp && (
                    <span className={styles.sourceHost}>{n.stamp}</span>
                  )}
                </li>
              ))}
            </ul>
          </>
        ) : (
          reminders.length === 0 && (
            <p className={styles.answer}>No notes or reminders yet.</p>
          )
        )}
      </HudPanel>
    );
  }

  if (w.kind === "proactive") {
    return (
      <HudPanel
        key={w.kind}
        kind="● ZORO"
        accent="gold"
        ariaLabel="Zoro reaching out"
        role="status"
        onClose={onClose}
      >
        <p className={styles.answer}>{w.data.text}</p>
      </HudPanel>
    );
  }

  if (w.kind === "nowplaying") {
    return (
      <NowPlayingCard
        key={w.kind}
        data={w.data}
        ts={w.ts}
        onClose={onClose}
      />
    );
  }

  if (w.kind === "claude") {
    return <ClaudeCard key={w.kind} data={w.data} onClose={onClose} />;
  }

  if (w.kind === "github") {
    return <GitHubCard key={w.kind} data={w.data} onClose={onClose} />;
  }

  if (w.kind === "shell") {
    return <ShellCard key={w.kind} data={w.data} onClose={onClose} />;
  }

  if (w.kind === "whatsapp") {
    return <WhatsAppCard key={w.kind} data={w.data} onClose={onClose} />;
  }

  if (w.kind === "youtube") {
    return <YouTubeCard key={w.kind} data={w.data} onClose={onClose} />;
  }

  if (w.kind === "vision") {
    const { answer, image_url, question } = w.data;
    return (
      <HudPanel
        key={w.kind}
        kind="SCREEN"
        meta={question}
        ariaLabel="Screen reading"
        onClose={onClose}
      >
        {image_url && (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            className={styles.shot}
            src={`${image_url}?t=${w.ts}`}
            alt="Screenshot Zoro looked at"
          />
        )}
        <p className={styles.answer}>{answer}</p>
      </HudPanel>
    );
  }

  if (w.kind === "system") {
    return <SystemCard key={w.kind} data={w.data} onClose={onClose} />;
  }

  if (w.kind === "browser") {
    return <BrowserCard key={w.kind} data={w.data} onClose={onClose} />;
  }

  return null;
}
