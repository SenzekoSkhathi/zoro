import { NextResponse } from "next/server";
import { Pool } from "pg";
import { isAuthed } from "@/lib/auth";

// Reads Zoro's own telemetry from Azure PostgreSQL server-side and hands
// the web HUD a compact snapshot of "how she's doing": today's success rate,
// recent activity, open lessons, and last night's self-improvement reflection.
// Not cached: this is live instrument data, must reflect the latest rows.
export const dynamic = "force-dynamic";

declare global {
  // eslint-disable-next-line no-var
  var __zoroPgPool: Pool | undefined;
}

function getPool(): Pool | null {
  const connectionString =
    process.env.AZURE_POSTGRES_URL || process.env.DATABASE_URL;
  if (!connectionString) return null;

  if (!global.__zoroPgPool) {
    global.__zoroPgPool = new Pool({
      connectionString,
      ssl: { rejectUnauthorized: false },
      connectionTimeoutMillis: 5000,
      max: 10,
      idleTimeoutMillis: 30000,
    });
  }
  return global.__zoroPgPool;
}

type EpisodeRow = { tool: string; outcome: string; latency_ms: number | null };
type LessonRow = { lesson: string; tool: string };
type ReportRow = { summary: string; report_date: string; episode_stats: unknown };

export async function GET() {
  // Private activity data — gate behind the same session as /api/token.
  if (!(await isAuthed())) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  const pool = getPool();
  if (!pool) {
    return NextResponse.json(
      { error: "telemetry database backend not configured" },
      { status: 503 },
    );
  }

  try {
    const [epsRes, lessonsRes, reportsRes] = await Promise.all([
      pool.query<EpisodeRow>(
        `SELECT tool, outcome, latency_ms
         FROM zoro.zoro_episodes
         WHERE created_at >= NOW() - INTERVAL '7 days'
         ORDER BY created_at DESC
         LIMIT 2000;`
      ),
      pool.query<LessonRow>(
        `SELECT lesson, tool
         FROM zoro.zoro_lessons
         WHERE status = 'open'
         ORDER BY created_at DESC
         LIMIT 5;`
      ),
      pool.query<ReportRow>(
        `SELECT summary, report_date::text as report_date, episode_stats
         FROM zoro.zoro_daily_reports
         ORDER BY report_date DESC
         LIMIT 1;`
      ),
    ]);

      const rows = epsRes.rows ?? [];
      // Don't let voice-plumbing events skew the success rate the user sees.
      const real = rows.filter((r) => r.tool !== "false_interruption");
      const success = real.filter((r) => r.outcome === "success").length;
      const error = real.filter((r) => r.outcome === "error").length;
      const aborted = real.filter((r) => r.outcome === "aborted").length;
      const corrected = real.filter((r) => r.outcome === "corrected").length;
      const total = real.length;

      // Per-tool counts -> the few most-used tools, for a live "top systems" readout.
      const byTool = new Map<string, number>();
      for (const r of real) byTool.set(r.tool, (byTool.get(r.tool) ?? 0) + 1);
      const topTools = [...byTool.entries()]
        .sort((a, b) => b[1] - a[1])
        .slice(0, 5)
        .map(([tool, count]) => ({ tool, count }));

      const latencies = real
        .map((r) => r.latency_ms)
        .filter((n): n is number => typeof n === "number" && n > 0)
        .sort((a, b) => a - b);
      const medianLatency = latencies.length
        ? latencies[Math.floor(latencies.length / 2)]
        : null;

      const report = reportsRes.rows?.[0] ?? null;

      return NextResponse.json({
        window_days: 7,
        counts: { total, success, error, aborted, corrected },
        success_rate: total ? success / total : null,
        median_latency_ms: medianLatency,
        top_tools: topTools,
        open_lessons: (lessonsRes.rows ?? []).map((l) => ({
          lesson: l.lesson,
          tool: l.tool,
        })),
        last_report: report
          ? { summary: report.summary, date: report.report_date }
          : null,
      });
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "telemetry fetch failed" },
      { status: 502 },
    );
  }
}
