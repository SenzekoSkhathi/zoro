"use client";

import { useCallback, useEffect, useState } from "react";
import {
  LiveKitRoom,
  RoomAudioRenderer,
  useVoiceAssistant,
  useLocalParticipant,
  useMultibandTrackVolume,
  useDataChannel,
} from "@livekit/components-react";
import type { LocalAudioTrack } from "livekit-client";
import { useWakeService } from "@/hooks/useWakeService";
import { useTelemetry } from "@/hooks/useTelemetry";
import { Orb, type OrbMode } from "@/components/Orb";
import { ZoroDisplay } from "@/components/ZoroDisplay";
import { Transcript } from "@/components/Transcript";
import { InstrumentRail } from "@/components/InstrumentRail";
import { ToolTicker } from "@/components/ToolTicker";
import { LoginGate } from "@/components/LoginGate";
import styles from "./page.module.css";

const USER_ACCENT = "#3fd0ff"; // cyan — you are speaking
const ZORO_ACCENT = "#ffb648"; // warm gold — Zoro is responding

type Phase = "boot" | "idle" | "connecting" | "active" | "error";

const SERVER_URL = process.env.NEXT_PUBLIC_LIVEKIT_URL as string;

function LiveClock() {
  // null until mounted so server and first client render match (the time
  // only exists client-side); avoids a hydration mismatch.
  const [now, setNow] = useState<Date | null>(null);
  useEffect(() => {
    setNow(new Date());
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  if (!now) {
    return <span className={styles.clock} suppressHydrationWarning />;
  }
  const t = now.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
  const d = now
    .toLocaleDateString([], {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    })
    .replace(/\//g, ".");
  return (
    <span className={styles.clock}>
      {d}
      <i className={styles.sep} />
      {t}
    </span>
  );
}

/**
 * Ambient instrument shell — the always-on HUD that frames the whole
 * screen: engineering grid, vignette, scan, edge rulers, corner brackets,
 * a top identity strip and a bottom telemetry readout. Behind all content.
 */
function HudFrame({
  phase,
  online,
}: {
  phase: Phase;
  online: boolean;
}) {
  const link =
    phase === "active"
      ? "LIVE"
      : phase === "connecting"
        ? "SYNC"
        : "STBY";
  return (
    <div className={styles.hud} aria-hidden>
      <span className={styles.gridbg} />
      <span className={styles.vignette} />
      <span className={styles.scanline} />

      <div className={styles.frame}>
        <span className={`${styles.corner} ${styles.cornerTL}`} />
        <span className={`${styles.corner} ${styles.cornerTR}`} />
        <span className={`${styles.corner} ${styles.cornerBL}`} />
        <span className={`${styles.corner} ${styles.cornerBR}`} />
      </div>

      <span className={`${styles.ruler} ${styles.rulerL}`} />
      <span className={`${styles.ruler} ${styles.rulerR}`} />

      <div className={`${styles.bar} ${styles.barTop}`}>
        <span className={styles.sysTag}>ZORO ▸ VOICE CORE</span>
        <LiveClock />
      </div>

      <div className={`${styles.bar} ${styles.barBot}`}>
        <span className={styles.telem}>
          <i
            className={`${styles.dot} ${
              online ? styles.dotOk : styles.dotBad
            }`}
          />
          WAKE {online ? "ONLINE" : "OFFLINE"}
        </span>
        <span className={styles.telem}>LINK {link}</span>
        <span className={styles.telem}>SYS {phase.toUpperCase()}</span>
      </div>

      {/* Bottom-right OFFLINE marker — visible whenever Zoro isn't in an
          active session (wake word required to bring her back). */}
      {phase !== "active" && (
        <span className={styles.offlineMark}>OFFLINE</span>
      )}
    </div>
  );
}

export default function Home() {
  // Access gate: null = still checking the session, false = show login,
  // true = authorized. Everything (incl. the laptop-control token route)
  // stays locked until the server confirms a valid session.
  const [authed, setAuthed] = useState<boolean | null>(null);
  useEffect(() => {
    let alive = true;
    fetch("/api/session", { cache: "no-store" })
      .then((r) => r.json())
      .then((d: { authed?: boolean }) => alive && setAuthed(!!d.authed))
      .catch(() => alive && setAuthed(false));
    return () => {
      alive = false;
    };
  }, []);

  // Land straight in the listening lobby (wake word armed) instead of an
  // "Activate Zoro" gate — the gate was an artifact of the old model where
  // deactivate disarmed the wake word.
  const [phase, setPhase] = useState<Phase>("idle");
  const [token, setToken] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Browser autoplay rules block mic/TTS until a user gesture. The native
  // Tauri shell grants audio up front, so there it's primed automatically;
  // in a plain browser we surface one light "enable audio" tap.
  const [audioPrimed, setAudioPrimed] = useState(false);
  useEffect(() => {
    const w = window as unknown as Record<string, unknown>;
    if ("__TAURI_INTERNALS__" in w || "__TAURI__" in w) setAudioPrimed(true);
  }, []);
  const primeAudio = useCallback(() => {
    try {
      const Ctx =
        window.AudioContext ||
        (window as unknown as { webkitAudioContext?: typeof AudioContext })
          .webkitAudioContext;
      if (Ctx) void new Ctx().resume();
    } catch {
      /* best-effort — the gesture itself unlocks autoplay regardless */
    }
    setAudioPrimed(true);
  }, []);
  // True while Zoro has content on screen; docks the orb to the corner
  // and projects the beam, then clears back to the natural state.
  const [contentShown, setContentShown] = useState(false);
  // Briefly suppress wake-word re-activation right after a session ends,
  // so residual TTS audio bleeding into the mic can't bounce her back on.
  const [wakeGate, setWakeGate] = useState(false);

  const connect = useCallback(async () => {
    setPhase("connecting");
    try {
      // Unique room per session: every connect creates a NEW room so
      // LiveKit's automatic agent dispatch reliably fires (a fixed name
      // lingers on the server and the agent never gets re-dispatched).
      const room = `zoro-${crypto.randomUUID()}`;
      const res = await fetch(`/api/token?room=${encodeURIComponent(room)}`);
      if (!res.ok) throw new Error(`token request failed (${res.status})`);
      const data = (await res.json()) as { token: string };
      setToken(data.token);
      setPhase("active");
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to connect");
      setPhase("error");
    }
  }, []);

  // Destructure the stable (useCallback) handlers: depending on the whole
  // `wake` object would re-run the effect every render (new object literal
  // + status state changes), spamming pause/resume at the wake service.
  const {
    status: wakeStatus,
    pause: wakePause,
    resume: wakeResume,
  } = useWakeService(() => {
    setPhase((p) => (p === "idle" ? "connecting" : p));
  });

  useEffect(() => {
    if (phase === "connecting" && !token) void connect();
    if (phase === "active") wakePause();
    // Hold off on rearming the wake word while the post-session cooldown
    // is active; the gate effect below resumes once it clears.
    if (phase === "idle" && !wakeGate) wakeResume();
  }, [phase, token, connect, wakePause, wakeResume, wakeGate]);

  const activate = useCallback(() => {
    setError(null);
    setToken(null);
    setPhase("idle");
  }, []);

  const endCall = useCallback(() => {
    setToken(null);
    setPhase("idle");
    setContentShown(false);
    // Cooldown window: ignore wake events for a few seconds so any TTS
    // tail bleeding into the mic doesn't immediately bounce her back on.
    setWakeGate(true);
    setTimeout(() => setWakeGate(false), 3000);
  }, []);

  // End the session but stay in the listening lobby — the wake word stays
  // armed so "Hey Zoro" brings her right back. (Always-listening model:
  // deactivate no longer disarms; a true off is the tray "Quit"/pause.)
  const deactivate = useCallback(() => {
    setToken(null);
    setError(null);
    setPhase("idle");
    setContentShown(false);
    setWakeGate(true);
    setTimeout(() => setWakeGate(false), 3000);
  }, []);

  const serviceOnline = wakeStatus === "connected";
  const faceMode: OrbMode =
    phase === "connecting" ? "connecting" : phase === "idle" ? "listening" : "idle";

  // Ambient self-telemetry — polled here at the page root so the
  // instrument rail stays live across every phase (even idle), making the
  // app read as a running system rather than a dormant page.
  const telemetry = useTelemetry();

  // Until authorized, render only the HUD shell + the login gate — no
  // telemetry polling, no orb, no session. The dangerous endpoints 401
  // server-side regardless, but this keeps the locked state clean too.
  if (authed !== true) {
    return (
      <main className={styles.main}>
        <HudFrame phase="boot" online={serviceOnline} />
        {authed === false && <LoginGate onAuthed={() => setAuthed(true)} />}
      </main>
    );
  }

  return (
    <main className={styles.main}>
      <HudFrame phase={phase} online={serviceOnline} />
      <InstrumentRail data={telemetry} />

      {phase !== "active" && <Orb mode={faceMode} />}

      {/* Browser-only: one light tap to satisfy autoplay so wake-triggered
          sessions have working audio. Suppressed in the Tauri shell (primed
          automatically) and once the user has enabled it. */}
      {!audioPrimed && phase !== "active" && (
        <div className={styles.activateDock}>
          <button className={styles.button} onClick={primeAudio}>
            Enable audio
          </button>
        </div>
      )}

      {phase === "error" && (
        <div className={styles.panel}>
          <p className={styles.error}>{error}</p>
          <button className={styles.button} onClick={activate}>
            Try again
          </button>
        </div>
      )}

      {phase === "active" && token && (
        <LiveKitRoom
          token={token}
          serverUrl={SERVER_URL}
          connect
          audio
          video={false}
          onDisconnected={endCall}
          className={styles.room}
        >
          <RoomAudioRenderer />
          <ZoroConversation
            onEnd={endCall}
            onDeactivate={deactivate}
            onContentChange={setContentShown}
          />
        </LiveKitRoom>
      )}

      {phase !== "active" && !serviceOnline && (
        <p className={styles.heard}>
          wake service offline — run wake_service.py
        </p>
      )}
    </main>
  );
}

function avg(bands: number[]): number {
  if (!bands.length) return 0;
  return bands.reduce((a, b) => a + b, 0) / bands.length;
}

function ZoroConversation({
  onEnd,
  onDeactivate,
  onContentChange,
}: {
  onEnd: () => void;
  onDeactivate: () => void;
  onContentChange: (shown: boolean) => void;
}) {
  const { state, audioTrack } = useVoiceAssistant();
  const { microphoneTrack } = useLocalParticipant();

  const opts = { bands: 5, updateInterval: 32 };
  const userBands = useMultibandTrackVolume(
    microphoneTrack?.track as LocalAudioTrack | undefined,
    opts,
  );
  const zoroBands = useMultibandTrackVolume(audioTrack, opts);

  // Dock the orb whenever any content is on screen (a display card or the
  // chat-history panel); clears back to centre when everything is removed.
  const [cardActive, setCardActive] = useState(false);
  const [chatActive, setChatActive] = useState(false);
  const contentShown = cardActive || chatActive;

  useEffect(() => {
    onContentChange(contentShown);
  }, [contentShown, onContentChange]);

  // Listen for session-control frames the agent emits via end_session /
  // deactivate. Give the spoken confirmation ~2.4s to land before we tear
  // down the room so the user actually hears "going to sleep, Sir".
  const { message: sessionMsg } = useDataChannel("zoro.display");
  useEffect(() => {
    if (!sessionMsg) return;
    try {
      const text = new TextDecoder().decode(sessionMsg.payload);
      const parsed = JSON.parse(text) as {
        kind?: string;
        data?: { action?: string };
      };
      if (parsed?.kind !== "session") return;
      const act = parsed.data?.action;
      if (act === "end") {
        setTimeout(onEnd, 2400);
      } else if (act === "deactivate") {
        setTimeout(onDeactivate, 2400);
      }
    } catch {
      /* not our frame */
    }
  }, [sessionMsg, onEnd, onDeactivate]);

  // Aim the beam from the docked orb's centre to the panel's centre, and
  // keep it locked on through window resizes.
  const [beam, setBeam] = useState({ x: 0, y: 0, len: 0, deg: 0 });
  useEffect(() => {
    if (!contentShown) return;
    const measure = () => {
      const orbBox = 220 * 0.42; // docked Orb visual size (scale in CSS)
      const cx = window.innerWidth - 26 - orbBox / 2;
      const cy = window.innerHeight - 26 - orbBox / 2;
      const tx = window.innerWidth / 2;
      const ty = window.innerHeight / 2;
      const dx = tx - cx;
      const dy = ty - cy;
      setBeam({
        x: cx,
        y: cy,
        len: Math.hypot(dx, dy) * 0.9,
        deg: (Math.atan2(dy, dx) * 180) / Math.PI,
      });
    };
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [contentShown]);

  const zoroSpeaking = state === "speaking";
  // Boost a little so normal speech visibly drives the orb.
  const energy = Math.min(
    1,
    avg(zoroSpeaking ? zoroBands : userBands) * 3.2,
  );
  const accent = zoroSpeaking ? ZORO_ACCENT : USER_ACCENT;
  const faceMode: OrbMode = zoroSpeaking
    ? "speaking"
    : state === "thinking"
      ? "thinking"
      : "listening";

  return (
    <div className={styles.room}>
      {contentShown && (
        <span
          className={styles.beam}
          aria-hidden
          style={{
            left: beam.x,
            top: beam.y,
            width: beam.len,
            transform: `rotate(${beam.deg}deg)`,
          }}
        />
      )}
      <Orb
        mode={faceMode}
        energy={energy}
        accent={accent}
        docked={contentShown}
      />
      {!contentShown && (
        <p className={styles.state}>{state ?? "connecting"}</p>
      )}
      <ToolTicker />
      <ZoroDisplay onActiveChange={setCardActive} />
      <Transcript onActiveChange={setChatActive} />
    </div>
  );
}
