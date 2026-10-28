"use client";

import { useCallback, useEffect, useRef, useState } from "react";
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
import { WidgetLimitProvider } from "@/components/WidgetLimit";
import { InstrumentRail } from "@/components/InstrumentRail";
import { ToolTicker } from "@/components/ToolTicker";
import { LoginGate } from "@/components/LoginGate";
import styles from "./page.module.css";

const USER_ACCENT = "#3fd0ff"; // cyan — you are speaking
const ZORO_ACCENT = "#ffb648"; // warm gold — Zoro is responding

type Phase = "boot" | "idle" | "connecting" | "active" | "error";

const SERVER_URL = process.env.NEXT_PUBLIC_LIVEKIT_URL as string;

async function callTauri<T = unknown>(
  cmd: string,
  args?: Record<string, unknown>,
): Promise<T | null> {
  if (typeof window === "undefined") return null;
  const w = window as unknown as {
    __TAURI_INTERNALS__?: {
      invoke?: (cmd: string, args?: Record<string, unknown>) => Promise<T>;
    };
  };
  if (w.__TAURI_INTERNALS__?.invoke) {
    try {
      return await w.__TAURI_INTERNALS__.invoke(cmd, args);
    } catch (e) {
      console.warn(`[Tauri] call "${cmd}" failed:`, e);
    }
  }
  return null;
}

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
  const [inShell, setInShell] = useState(false);
  const [isOverlay, setIsOverlay] = useState(false);
  const [alwaysOnTop, setAlwaysOnTop] = useState(false);
  useEffect(() => {
    const w = window as unknown as Record<string, unknown>;
    const ua = navigator.userAgent || "";
    const searchParams = new URLSearchParams(window.location.search);
    const overlayParam = searchParams.get("overlay") === "true";
    const shell =
      "__TAURI_INTERNALS__" in w ||
      "__TAURI__" in w ||
      ua.includes("ZoroShell") ||
      searchParams.get("shell") === "tauri" ||
      overlayParam;
    if (shell) {
      setAudioPrimed(true);
      setInShell(true);
      setIsOverlay(true);
      document.documentElement.dataset.overlay = "true";
      document.body.dataset.overlay = "true";
    }
  }, []);

  useEffect(() => {
    if (inShell) {
      void callTauri<boolean>("get_always_on_top").then((val) => {
        if (typeof val === "boolean") setAlwaysOnTop(val);
      });
    }
  }, [inShell]);

  const toggleAlwaysOnTop = useCallback(async () => {
    const next = await callTauri<boolean>("toggle_always_on_top");
    if (typeof next === "boolean") {
      setAlwaysOnTop(next);
    } else {
      setAlwaysOnTop((prev) => !prev);
    }
  }, []);

  const handleHideWindow = useCallback(async () => {
    await callTauri("hide_window");
  }, []);

  const handleStartDrag = useCallback(() => {
    void callTauri("start_drag");
  }, []);

  // Keyboard shortcuts & native pin state bridge:
  //   Alt + F5 -> Toggle Always-on-Top (Pin)
  //   Alt + F4 -> Hide window to tray
  useEffect(() => {
    const w = window as unknown as { __zoroSetPinned?: (val: boolean) => void };
    w.__zoroSetPinned = (val: boolean) => {
      setAlwaysOnTop(!!val);
    };

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.altKey && e.key === "F5") {
        e.preventDefault();
        void toggleAlwaysOnTop();
      } else if (e.altKey && e.key === "F4") {
        e.preventDefault();
        void handleHideWindow();
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      delete w.__zoroSetPinned;
    };
  }, [toggleAlwaysOnTop, handleHideWindow]);

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

  // Dynamic window sizing for the desktop shell:
  // Compact 240x240 for Orb-only mode; expands to 500x860 when cards/chat open.
  useEffect(() => {
    if (!isOverlay) return;
    const targetW = contentShown ? 500 : 240;
    const targetH = contentShown ? 860 : 240;
    void callTauri("set_window_size", { width: targetW, height: targetH });
  }, [isOverlay, contentShown]);
  // Briefly suppress wake-word re-activation right after a session ends,
  // so residual TTS audio bleeding into the mic can't bounce her back on.
  const [wakeGate, setWakeGate] = useState(false);
  const [deactivated, setDeactivated] = useState(false);
  const wakeGateRef = useRef(false);
  const deactivatedRef = useRef(false);
  wakeGateRef.current = wakeGate;
  deactivatedRef.current = deactivated;

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
    mute: wakeMute,
    unmute: wakeUnmute,
  } = useWakeService(() => {
    if (wakeGateRef.current || deactivatedRef.current) return;
    setPhase((p) => (p === "idle" ? "connecting" : p));
  });

  useEffect(() => {
    if (phase === "connecting" && !token) void connect();
    if (phase === "connecting" || phase === "active" || wakeGate) wakePause();
    // Hold off on rearming the wake word while the post-session cooldown
    // or explicit deactivation is active.
    if (phase === "idle" && !wakeGate && !deactivated) wakeResume();
  }, [phase, token, connect, wakePause, wakeResume, wakeGate, deactivated]);

  // Native wake bridge: in the Tauri shell the page can't reach the local
  // ws:// wake socket (mixed content), so the Rust side calls this global
  // when the wake service relaunches the app with --wake. Same effect as a
  // websocket wake. Only armed once authorized so a stray call can't start
  // an (unauthorized) session.
  useEffect(() => {
    if (authed !== true) return;
    const w = window as unknown as { __zoroWake?: () => void };
    w.__zoroWake = () => {
      if (wakeGateRef.current || deactivatedRef.current) return;
      setPhase((p) => (p === "idle" ? "connecting" : p));
    };
    return () => {
      delete w.__zoroWake;
    };
  }, [authed]);

  const activate = useCallback(() => {
    setError(null);
    setToken(null);
    setDeactivated(false);
    wakeUnmute();
    setPhase("idle");
  }, [wakeUnmute]);

  const endCall = useCallback(() => {
    setToken(null);
    setPhase("idle");
    setContentShown(false);
    // Cooldown window: ignore wake events for a few seconds so any TTS
    // tail bleeding into the mic doesn't immediately bounce her back on.
    setWakeGate(true);
    setTimeout(() => setWakeGate(false), 3500);
  }, []);

  // Disarm wake word until the user explicitly clicks the Orb / Tap to Speak.
  const deactivate = useCallback(() => {
    setToken(null);
    setError(null);
    setPhase("idle");
    setContentShown(false);
    setDeactivated(true);
    wakeMute();
    setWakeGate(true);
    setTimeout(() => setWakeGate(false), 3500);
  }, [wakeMute]);

  // In the Tauri shell wake arrives via the native bridge, not the (blocked)
  // ws:// socket, so treat the shell as wake-capable for the status readout.
  const serviceOnline = wakeStatus === "connected" || inShell;
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
      <main className={styles.main} data-overlay={isOverlay ? "true" : undefined}>
        {!isOverlay && <HudFrame phase="boot" online={serviceOnline} />}
        {authed === false && <LoginGate onAuthed={() => setAuthed(true)} />}
      </main>
    );
  }

  return (
    <main className={styles.main} data-overlay={isOverlay ? "true" : undefined}>
      {!isOverlay && <HudFrame phase={phase} online={serviceOnline} />}
      {!isOverlay && <InstrumentRail data={telemetry} />}

      {phase !== "active" && (
        <div className={isOverlay ? styles.overlayContainer : undefined}>
          <Orb
            mode={faceMode}
            pinned={alwaysOnTop}
            onStartDrag={handleStartDrag}
            onClick={() => {
              void primeAudio();
              setDeactivated(false);
              wakeUnmute();
              if (phase === "idle") setPhase("connecting");
            }}
          />
        </div>
      )}

      {phase !== "active" && !isOverlay && (
        <div className={styles.activateDock}>
          <button
            className={styles.button}
            onClick={() => {
              void primeAudio();
              setDeactivated(false);
              wakeUnmute();
              if (phase === "idle") setPhase("connecting");
            }}
          >
            {phase === "connecting" ? "Connecting..." : "Tap to Speak"}
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
          audio={{
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          }}
          video={false}
          onDisconnected={endCall}
          className={styles.room}
          data-overlay={isOverlay ? "true" : undefined}
        >
          <RoomAudioRenderer />
          <ZoroConversation
            onEnd={endCall}
            onDeactivate={deactivate}
            onContentChange={setContentShown}
            isOverlay={isOverlay}
            alwaysOnTop={alwaysOnTop}
            onStartDrag={handleStartDrag}
          />
        </LiveKitRoom>
      )}

      {phase !== "active" && !serviceOnline && !isOverlay && (
        <p className={styles.heard}>
          wake service offline — click orb or tap to speak
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
  isOverlay = false,
  alwaysOnTop = false,
  onStartDrag,
}: {
  onEnd: () => void;
  onDeactivate: () => void;
  onContentChange: (shown: boolean) => void;
  isOverlay?: boolean;
  alwaysOnTop?: boolean;
  onStartDrag?: () => void;
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
      const orbSize = 92; // docked Siri Orb diameter
      const cx = window.innerWidth - 18 - orbSize / 2;
      const cy = window.innerHeight - 18 - orbSize / 2;
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
  const activeBands = zoroSpeaking ? zoroBands : userBands;
  // Boost a little so normal speech visibly drives the orb.
  const energy = Math.min(
    1,
    avg(activeBands) * 3.2,
  );
  const accent = zoroSpeaking ? ZORO_ACCENT : USER_ACCENT;
  const faceMode: OrbMode = zoroSpeaking
    ? "speaking"
    : state === "thinking"
      ? "thinking"
      : "listening";

  return (
    <div className={styles.room} data-overlay={isOverlay ? "true" : undefined}>
      {contentShown && !isOverlay && (
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
      <WidgetLimitProvider>
        <ZoroDisplay onActiveChange={setCardActive} />
        <Transcript onActiveChange={setChatActive} />
      </WidgetLimitProvider>
      <div className={isOverlay ? styles.overlayContainer : undefined}>
        <Orb
          mode={faceMode}
          energy={energy}
          bands={activeBands}
          accent={accent}
          docked={contentShown && !isOverlay}
          pinned={alwaysOnTop}
          onStartDrag={onStartDrag}
        />
      </div>
      {!contentShown && !isOverlay && (
        <p className={styles.state}>{state ?? "connecting"}</p>
      )}
      {!isOverlay && <ToolTicker />}
    </div>
  );
}
