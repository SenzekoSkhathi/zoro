"use client";

import React, { useEffect, useRef, type CSSProperties } from "react";

const ORB_SCOPED_CSS = `
.zoro-orb-root {
  --orb-size: 92px;
  --energy: 0;
  position: relative;
  width: var(--orb-size);
  height: var(--orb-size);
  border-radius: 50%;
  display: grid;
  place-items: center;
  user-select: none;
  -webkit-user-select: none;
  cursor: pointer;
  touch-action: none;
  background: transparent;
  transition: transform 0.28s cubic-bezier(0.22, 1, 0.36, 1);
}

.zoro-orb-root:hover {
  transform: scale(1.045);
}

.zoro-orb-root:active {
  transform: scale(0.97);
}

.zoro-orb-root[data-dragging="true"] {
  cursor: grabbing;
  transform: scale(1.07);
}

/* Content docked state: compact floating orb in the bottom-right corner */
.zoro-orb-docked {
  position: fixed;
  bottom: 18px;
  right: 18px;
  z-index: 50;
}

.zoro-orb-canvas {
  width: 100%;
  height: 100%;
  display: block;
  pointer-events: none;
  border-radius: 50%;
}

/* Subtle precision telemetry ring when Pinned (Alt + F5 Always-on-Top) */
.zoro-orb-pin-ring {
  position: absolute;
  inset: -4px;
  border-radius: 50%;
  border: 1px dashed rgba(40, 188, 217, 0.72);
  box-shadow:
    0 0 10px rgba(40, 188, 217, 0.28),
    inset 0 0 6px rgba(24, 47, 167, 0.22);
  pointer-events: none;
  opacity: 0;
  transform: scale(0.92);
  transition:
    opacity 0.25s ease,
    transform 0.28s cubic-bezier(0.22, 1, 0.36, 1);
  animation: zoroPinOrbit 22s linear infinite;
}

.zoro-orb-root[data-pinned="true"] .zoro-orb-pin-ring {
  opacity: 1;
  transform: scale(1);
}

@keyframes zoroPinOrbit {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}

@media (prefers-reduced-motion: reduce) {
  .zoro-orb-pin-ring {
    animation: none !important;
  }
}
`;

export type OrbMode =
  | "idle"
  | "listening"
  | "connecting"
  | "thinking"
  | "speaking";

export interface AudioBands {
  energy: number;
  bass: number;
  mid: number;
  treble: number;
}

interface LatticePoint {
  x: number;
  y: number;
  z: number;
  theta: number;
  phi: number;
  idx: number;
}

// Precompute 3D Fibonacci sphere points for the Dirac Neural Lattice
const NUM_LATTICE_POINTS = 260;
const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));
const LATTICE_POINTS: LatticePoint[] = Array.from(
  { length: NUM_LATTICE_POINTS },
  (_, i) => {
    const y = 1 - (i / (NUM_LATTICE_POINTS - 1)) * 2;
    const radius = Math.sqrt(Math.max(0, 1 - y * y));
    const theta = GOLDEN_ANGLE * i;
    const x = Math.cos(theta) * radius;
    const z = Math.sin(theta) * radius;
    const phi = Math.acos(Math.max(-1, Math.min(1, y)));
    return { x, y, z, theta, phi, idx: i };
  },
);

function hexToRgb(hex?: string): [number, number, number] | null {
  if (!hex) return null;
  const clean = hex.trim().replace(/^#/, "");
  if (clean.length === 3) {
    const r = parseInt(clean[0] + clean[0], 16);
    const g = parseInt(clean[1] + clean[1], 16);
    const b = parseInt(clean[2] + clean[2], 16);
    if (!Number.isNaN(r) && !Number.isNaN(g) && !Number.isNaN(b)) {
      return [r, g, b];
    }
  } else if (clean.length === 6) {
    const r = parseInt(clean.slice(0, 2), 16);
    const g = parseInt(clean.slice(2, 4), 16);
    const b = parseInt(clean.slice(4, 6), 16);
    if (!Number.isNaN(r) && !Number.isNaN(g) && !Number.isNaN(b)) {
      return [r, g, b];
    }
  }
  return null;
}

/**
 * Dirac Neural Lattice + Spline 3D Volumetric AI Orb
 *
 * Inspired by Humandone's "AI Orb" (#02021B midnight core, #182FA7 cobalt neon glow,
 * #28BCD9 electric turquoise-cyan, #CACED2 metallic silver-white highlights) fused
 * with a 260-node 3D Fibonacci spherical point-field shader that ripples in real time
 * with 5-band voice formants.
 *
 * Interactions:
 *  - Single click: triggers `onClick` (start/interact with Zoro)
 *  - Double-click + hold/move: triggers `onStartDrag` (drag orb anywhere on screen)
 *  - `Alt + F5`: toggles `pinned` (Always-on-Top orbital indicator ring)
 */
export function Orb({
  mode = "idle",
  energy,
  bands,
  accent,
  docked = false,
  pinned = false,
  size = 92,
  listenToMic = false,
  speechSpeed = 1,
  speechTone = 0.5,
  onAudioBands,
  onClick,
  onStartDrag,
}: {
  mode?: OrbMode;
  energy?: number;
  /** Live audio frequency spectrum bands (e.g. 5-band array from useMultibandTrackVolume) */
  bands?: number[];
  accent?: string;
  docked?: boolean;
  /** Whether the window is pinned Always-on-Top (Alt + F5) */
  pinned?: boolean;
  size?: number;
  listenToMic?: boolean;
  /** Speaking tempo multiplier (e.g. 0.6 = slow/calm, 1.0 = natural, 1.6 = fast/excited) */
  speechSpeed?: number;
  /** AI vocal pitch/timbre (0 = deep/warm bass, 0.5 = balanced, 1 = bright/high treble) */
  speechTone?: number;
  onAudioBands?: (bands: AudioBands) => void;
  onClick?: () => void;
  /** Fired when user double-clicks and holds/moves the mouse to drag the orb */
  onStartDrag?: () => void;
}) {
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const onBandsRef = useRef(onAudioBands);
  onBandsRef.current = onAudioBands;

  const onClickRef = useRef(onClick);
  onClickRef.current = onClick;

  const onStartDragRef = useRef(onStartDrag);
  onStartDragRef.current = onStartDrag;

  // Double-click + hold/move & press-drag vs single-click state refs
  const lastPointerDownRef = useRef<number>(0);
  const pointerDownPosRef = useRef<{ x: number; y: number } | null>(null);
  const singleClickTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const suppressNextClickUntilRef = useRef<number>(0);
  const dragStartedRef = useRef<boolean>(false);

  const clampedExternalEnergy =
    energy !== undefined ? Math.min(1, Math.max(0, energy)) : undefined;

  const targetBandsRef = useRef<{
    bass: number;
    mid: number;
    treble: number;
    energy: number;
    tone: number;
    hasBands: boolean;
  }>({
    bass: 0,
    mid: 0,
    treble: 0,
    energy: 0,
    tone: speechTone,
    hasBands: false,
  });

  // Update target frequency bands whenever LiveKit multiband volume or energy changes
  useEffect(() => {
    if (bands && bands.length > 0) {
      let b = 0;
      let m = 0;
      let t = 0;

      if (bands.length >= 5) {
        b = Math.min(
          1,
          Math.max(0, ((bands[0] ?? 0) * 0.45 + (bands[1] ?? 0) * 0.55) * 3.4),
        );
        m = Math.min(
          1,
          Math.max(0, ((bands[2] ?? 0) * 0.6 + (bands[3] ?? 0) * 0.4) * 3.2),
        );
        t = Math.min(1, Math.max(0, (bands[4] ?? 0) * 3.8));
      } else if (bands.length >= 3) {
        b = Math.min(1, Math.max(0, (bands[0] ?? 0) * 3.4));
        m = Math.min(1, Math.max(0, (bands[1] ?? 0) * 3.2));
        t = Math.min(1, Math.max(0, (bands[2] ?? 0) * 3.8));
      } else {
        b = Math.min(1, Math.max(0, (bands[0] ?? 0) * 3.2));
        m = b * 0.85;
        t = b * 0.72;
      }

      const calculatedEnergy = Math.min(
        1,
        Math.max(
          clampedExternalEnergy ?? 0,
          b * 0.38 + m * 0.44 + t * 0.18,
        ),
      );

      const dynamicTone =
        b + t > 0.04 ? Math.min(1, Math.max(0, t / (b + t))) : speechTone;

      targetBandsRef.current = {
        bass: b,
        mid: m,
        treble: t,
        energy: calculatedEnergy,
        tone: dynamicTone,
        hasBands: true,
      };
    } else if (clampedExternalEnergy !== undefined) {
      const e = clampedExternalEnergy;
      targetBandsRef.current = {
        bass: e * 0.92,
        mid: e * 0.85,
        treble: e * 0.72,
        energy: e,
        tone: speechTone,
        hasBands: false,
      };
    } else {
      targetBandsRef.current = {
        bass: 0,
        mid: 0,
        treble: 0,
        energy: 0,
        tone: speechTone,
        hasBands: false,
      };
    }
  }, [bands, clampedExternalEnergy, speechTone]);

  // Optional direct Web Audio API microphone analyzer
  const micBandsRef = useRef<{
    energy: number;
    bass: number;
    mid: number;
    treble: number;
  }>({ energy: 0, bass: 0, mid: 0, treble: 0 });

  useEffect(() => {
    if (!listenToMic) return;

    let cancelled = false;
    let audioCtx: AudioContext | null = null;
    let stream: MediaStream | null = null;
    let rafId = 0;

    let smoothEnergy = 0;
    let smoothBass = 0;
    let smoothMid = 0;
    let smoothTreble = 0;

    async function startMic() {
      try {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
          return;
        }
        stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          },
        });
        if (cancelled) {
          stream.getTracks().forEach((t) => t.stop());
          return;
        }

        const AudioContextClass =
          window.AudioContext ||
          (window as unknown as { webkitAudioContext: typeof AudioContext })
            .webkitAudioContext;
        audioCtx = new AudioContextClass();
        const source = audioCtx.createMediaStreamSource(stream);
        const analyser = audioCtx.createAnalyser();
        analyser.fftSize = 256;
        analyser.smoothingTimeConstant = 0.76;
        source.connect(analyser);

        const bins = new Uint8Array(analyser.frequencyBinCount);

        const tick = () => {
          if (cancelled) return;
          analyser.getByteFrequencyData(bins);

          let bassSum = 0;
          for (let i = 1; i <= 8; i++) bassSum += bins[i];
          const rawBass = Math.min(1, Math.max(0, (bassSum / 8 - 18) / 165));

          let midSum = 0;
          for (let i = 9; i <= 36; i++) midSum += bins[i];
          const rawMid = Math.min(1, Math.max(0, (midSum / 28 - 14) / 145));

          let trebleSum = 0;
          for (let i = 37; i <= 80; i++) trebleSum += bins[i];
          const rawTreble = Math.min(
            1,
            Math.max(0, (trebleSum / 44 - 10) / 120),
          );

          const rawOverall = Math.min(
            1,
            rawBass * 0.45 + rawMid * 0.4 + rawTreble * 0.15,
          );

          const targetEnergy =
            clampedExternalEnergy !== undefined
              ? Math.max(clampedExternalEnergy, rawOverall)
              : rawOverall;

          smoothEnergy += (targetEnergy - smoothEnergy) * 0.28;
          smoothBass += (rawBass - smoothBass) * 0.3;
          smoothMid += (rawMid - smoothMid) * 0.28;
          smoothTreble += (rawTreble - smoothTreble) * 0.32;

          micBandsRef.current = {
            energy: smoothEnergy,
            bass: smoothBass,
            mid: smoothMid,
            treble: smoothTreble,
          };

          if (onBandsRef.current) {
            onBandsRef.current(micBandsRef.current);
          }

          rafId = requestAnimationFrame(tick);
        };

        rafId = requestAnimationFrame(tick);
      } catch {
        // Fallback if microphone permission is declined or unavailable
      }
    }

    void startMic();

    return () => {
      cancelled = true;
      if (rafId) cancelAnimationFrame(rafId);
      if (stream) stream.getTracks().forEach((t) => t.stop());
      if (audioCtx && audioCtx.state !== "closed") {
        audioCtx.close().catch(() => {});
      }
    };
  }, [listenToMic, clampedExternalEnergy]);

  // Main 60fps 3D Dirac Neural Lattice + Volumetric Spline Shader Loop
  useEffect(() => {
    const canvas = canvasRef.current;
    const wrapper = wrapperRef.current;
    if (!canvas || !wrapper) return;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const clampedSpeed = Math.min(2.2, Math.max(0.4, speechSpeed));
    const clampedTone = Math.min(1, Math.max(0, speechTone));

    // Smoothly interpolated RGB color channels so mode transitions are seamless
    // Default Humandone AI Orb signature palette:
    //   Midnight void: #02021B (2, 2, 27)
    //   Royal Neon Blue: #182FA7 (24, 47, 167)
    //   Electric Cyan: #28BCD9 (40, 188, 217)
    //   Silver-White Specular: #CACED2 (202, 206, 210)
    let curPrimary: [number, number, number] = [40, 188, 217];
    let curSecondary: [number, number, number] = [24, 47, 167];
    let curHighlight: [number, number, number] = [202, 206, 210];

    let smoothEnergy = 0;
    let smoothBass = 0;
    let smoothMid = 0;
    let smoothTreble = 0;
    let rotY = 0;
    let rotX = 0.22;
    let phase = 0;
    let rafId = 0;
    let lastTime = performance.now();

    const render = (now: number) => {
      const dt = Math.min(0.05, (now - lastTime) / 1000);
      lastTime = now;

      const dpr = Math.min(window.devicePixelRatio || 1, 2.5);
      const pixelSize = Math.round(size * dpr);
      if (canvas.width !== pixelSize || canvas.height !== pixelSize) {
        canvas.width = pixelSize;
        canvas.height = pixelSize;
      }

      const target = targetBandsRef.current;
      phase += dt * clampedSpeed;

      // Update smoothed audio bands
      if (listenToMic) {
        const mb = micBandsRef.current;
        smoothEnergy = mb.energy;
        smoothBass = mb.bass;
        smoothMid = mb.mid;
        smoothTreble = mb.treble;
      } else if (target.hasBands || clampedExternalEnergy !== undefined) {
        smoothEnergy += (target.energy - smoothEnergy) * 0.28;
        smoothBass += (target.bass - smoothBass) * 0.3;
        smoothMid += (target.mid - smoothMid) * 0.28;
        smoothTreble += (target.treble - smoothTreble) * 0.32;
      } else if (mode === "speaking") {
        const syllableWave =
          0.5 +
          0.32 * Math.sin(phase * 5.6) +
          0.18 * Math.sin(phase * 12.1 + 0.7);
        const phraseEnvelope = Math.max(
          0.2,
          Math.sin(phase * 1.7) * 0.55 + 0.45,
        );
        const simE = Math.min(1, syllableWave * phraseEnvelope * 0.75);
        smoothEnergy += (simE - smoothEnergy) * 0.25;
        smoothBass += (simE * (1.05 - clampedTone * 0.4) - smoothBass) * 0.25;
        smoothMid +=
          (simE * (0.75 + 0.25 * Math.sin(phase * 7.2)) - smoothMid) * 0.25;
        smoothTreble += (simE * (0.55 + clampedTone * 0.5) - smoothTreble) * 0.25;
      } else {
        const idlePulse =
          mode === "thinking"
            ? 0.28 + 0.12 * Math.sin(phase * 6.5)
            : mode === "connecting"
              ? 0.22 + 0.14 * Math.sin(phase * 4.5)
              : 0.08 + 0.05 * Math.sin(phase * 2.0);
        smoothEnergy += (idlePulse - smoothEnergy) * 0.18;
        smoothBass += (idlePulse * 0.7 - smoothBass) * 0.18;
        smoothMid += (idlePulse * 0.8 - smoothMid) * 0.18;
        smoothTreble += (idlePulse * 0.6 - smoothTreble) * 0.18;
      }

      wrapper.style.setProperty("--energy", smoothEnergy.toFixed(3));

      if (onBandsRef.current && !listenToMic) {
        onBandsRef.current({
          energy: smoothEnergy,
          bass: smoothBass,
          mid: smoothMid,
          treble: smoothTreble,
        });
      }

      // Target color palette per mode
      const customRgb = hexToRgb(accent);
      let targetPrimary: [number, number, number] = [40, 188, 217]; // #28BCD9 Cyan
      let targetSecondary: [number, number, number] = [24, 47, 167]; // #182FA7 Royal Neon Blue
      let targetHighlight: [number, number, number] = [202, 206, 210]; // #CACED2 Silver-White

      if (mode === "speaking") {
        // Blend Humandone electric cyan/cobalt with warm solar gold or custom accent when Zoro speaks
        targetPrimary = customRgb ?? [255, 182, 72];
        targetSecondary = [40, 188, 217];
        targetHighlight = [255, 244, 224];
      } else if (mode === "thinking") {
        targetPrimary = [96, 215, 255];
        targetSecondary = [88, 66, 235];
        targetHighlight = [225, 235, 255];
      } else if (mode === "connecting") {
        targetPrimary = [56, 204, 240];
        targetSecondary = [36, 72, 205];
        targetHighlight = [212, 228, 245];
      } else if (customRgb) {
        targetPrimary = customRgb;
      }

      const colorLerp = Math.min(1, dt * 6.5);
      for (let c = 0; c < 3; c++) {
        curPrimary[c] += (targetPrimary[c] - curPrimary[c]) * colorLerp;
        curSecondary[c] += (targetSecondary[c] - curSecondary[c]) * colorLerp;
        curHighlight[c] += (targetHighlight[c] - curHighlight[c]) * colorLerp;
      }

      const pR = Math.round(curPrimary[0]);
      const pG = Math.round(curPrimary[1]);
      const pB = Math.round(curPrimary[2]);

      const sR = Math.round(curSecondary[0]);
      const sG = Math.round(curSecondary[1]);
      const sB = Math.round(curSecondary[2]);

      const hR = Math.round(curHighlight[0]);
      const hG = Math.round(curHighlight[1]);
      const hB = Math.round(curHighlight[2]);

      // Advance 3D rotation
      const isThinking = mode === "thinking";
      const isConnecting = mode === "connecting";
      const spinSpeed = isThinking
        ? 2.35
        : isConnecting
          ? 1.65
          : 0.55 + smoothEnergy * 1.45 * clampedSpeed;

      rotY = (rotY + dt * spinSpeed) % (Math.PI * 2);
      rotX =
        0.26 * Math.sin(phase * 0.75) +
        (isThinking ? 0.35 * Math.cos(phase * 1.9) : smoothBass * 0.18);

      const W = pixelSize;
      const H = pixelSize;
      const cx = W * 0.5;
      const cy = H * 0.5;
      const R = W * 0.39; // Leaves room for outer atmospheric bloom without clipping

      ctx.clearRect(0, 0, W, H);

      // ── 1. Outer Atmospheric Neon Bloom (#182FA7 + #28BCD9) ───────────────
      const bloomRadius = R * (1.26 + smoothEnergy * 0.14);
      const outerGlow = ctx.createRadialGradient(
        cx,
        cy,
        R * 0.35,
        cx,
        cy,
        bloomRadius,
      );
      outerGlow.addColorStop(
        0,
        `rgba(${pR}, ${pG}, ${pB}, ${0.32 + smoothEnergy * 0.28})`,
      );
      outerGlow.addColorStop(
        0.55,
        `rgba(${sR}, ${sG}, ${sB}, ${0.24 + smoothEnergy * 0.18})`,
      );
      outerGlow.addColorStop(1, "rgba(2, 2, 27, 0)");
      ctx.fillStyle = outerGlow;
      ctx.beginPath();
      ctx.arc(cx, cy, bloomRadius, 0, Math.PI * 2);
      ctx.fill();

      // ── 2. Deep Midnight Void Sphere (#02021B) + Volumetric Spline Lobes ──
      ctx.save();
      ctx.beginPath();
      ctx.arc(cx, cy, R, 0, Math.PI * 2);
      ctx.clip();

      const sphereBase = ctx.createRadialGradient(
        cx - R * 0.18,
        cy - R * 0.22,
        R * 0.05,
        cx,
        cy,
        R,
      );
      sphereBase.addColorStop(0, "#09113a");
      sphereBase.addColorStop(0.55, "#040624");
      sphereBase.addColorStop(1, "#02021B");
      ctx.fillStyle = sphereBase;
      ctx.fillRect(0, 0, W, H);

      // Internal volumetric Spline energy waves (additive screen blend)
      ctx.globalCompositeOperation = "screen";
      for (let k = 0; k < 3; k++) {
        ctx.save();
        ctx.translate(cx, cy);
        const dir = k % 2 === 0 ? 1 : -0.85;
        ctx.rotate(rotY * dir + k * 2.09);
        const lobeRx = R * (0.72 + 0.12 * Math.sin(phase * 2.1 + k) + smoothEnergy * 0.16);
        const lobeRy = R * (0.44 + 0.1 * Math.cos(phase * 2.7 - k) + smoothBass * 0.16);
        const offset = R * 0.12;
        const lobeGrad = ctx.createRadialGradient(
          offset,
          0,
          R * 0.02,
          offset,
          0,
          lobeRx,
        );
        const usePrimary = k !== 1;
        const c0 = usePrimary ? [pR, pG, pB] : [sR, sG, sB];
        lobeGrad.addColorStop(
          0,
          `rgba(${hR}, ${hG}, ${hB}, ${0.28 + smoothEnergy * 0.28})`,
        );
        lobeGrad.addColorStop(
          0.42,
          `rgba(${c0[0]}, ${c0[1]}, ${c0[2]}, ${0.42 + smoothEnergy * 0.25})`,
        );
        lobeGrad.addColorStop(1, "rgba(2, 2, 27, 0)");
        ctx.fillStyle = lobeGrad;
        ctx.beginPath();
        ctx.ellipse(offset, 0, lobeRx, lobeRy, 0, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
      }

      // Central volumetric core radiance
      const coreR = R * (0.36 + smoothEnergy * 0.24);
      const coreGrad = ctx.createRadialGradient(cx, cy, 0, cx, cy, coreR);
      coreGrad.addColorStop(
        0,
        `rgba(255, 255, 255, ${0.55 + smoothEnergy * 0.4})`,
      );
      coreGrad.addColorStop(
        0.4,
        `rgba(${pR}, ${pG}, ${pB}, ${0.45 + smoothEnergy * 0.35})`,
      );
      coreGrad.addColorStop(1, "rgba(2, 2, 27, 0)");
      ctx.fillStyle = coreGrad;
      ctx.beginPath();
      ctx.arc(cx, cy, coreR, 0, Math.PI * 2);
      ctx.fill();

      // ── 3. 3D Dirac Neural Point-Field Lattice ────────────────────────────
      const cosY = Math.cos(rotY);
      const sinY = Math.sin(rotY);
      const cosX = Math.cos(rotX);
      const sinX = Math.sin(rotX);

      const waveAmp = 0.035 + smoothEnergy * 0.14;
      const bassAmp = smoothBass * 0.09;
      const trebleAmp = smoothTreble * 0.07;

      for (let i = 0; i < LATTICE_POINTS.length; i++) {
        const pt = LATTICE_POINTS[i];

        // Multi-band spherical harmonic surface displacement
        const harmonic =
          1 +
          Math.sin(pt.y * 6.0 + phase * 4.8) * waveAmp +
          Math.cos(pt.x * 5.0 - phase * 3.9) * bassAmp +
          Math.sin(pt.theta * 3.0 + phase * 7.2) * trebleAmp;

        const px = pt.x * harmonic;
        const py = pt.y * harmonic;
        const pz = pt.z * harmonic;

        // 3D rotation (Y axis then X axis)
        const x1 = px * cosY - pz * sinY;
        const z1 = pz * cosY + px * sinY;
        const y2 = py * cosX - z1 * sinX;
        const z2 = z1 * cosX + py * sinX;

        // Perspective projection & depth cueing (-1..1 -> 0..1)
        const depth = Math.max(0, Math.min(1, (z2 + 1.25) / 2.25));
        const perspective = 0.84 + depth * 0.16;
        const sx = cx + x1 * (R * 0.91) * perspective;
        const sy = cy + y2 * (R * 0.91) * perspective;

        const baseDotR = (0.65 + depth * 1.45) * dpr * (size / 92);
        const dotR = baseDotR * (1 + smoothEnergy * 0.32);

        // Front-hemisphere nodes shimmer with #28BCD9 / #CACED2; back nodes recede into #182FA7
        const isCrest = depth > 0.76 && i % 4 === 0;
        const rCol = isCrest ? hR : depth > 0.45 ? pR : sR;
        const gCol = isCrest ? hG : depth > 0.45 ? pG : sG;
        const bCol = isCrest ? hB : depth > 0.45 ? pB : sB;
        const alpha = Math.min(
          0.98,
          Math.max(0.1, depth * depth * (0.88 + smoothEnergy * 0.22)),
        );

        ctx.fillStyle = `rgba(${rCol}, ${gCol}, ${bCol}, ${alpha.toFixed(3)})`;
        ctx.beginPath();
        ctx.arc(sx, sy, dotR, 0, Math.PI * 2);
        ctx.fill();
      }

      // ── 4. Fresnel Glass Specular Rim & Metallic Crest (#CACED2 / #28BCD9)
      ctx.globalCompositeOperation = "source-over";

      // Top-left curved glass reflection
      const specGrad = ctx.createRadialGradient(
        cx - R * 0.34,
        cy - R * 0.36,
        R * 0.02,
        cx - R * 0.2,
        cy - R * 0.2,
        R * 0.78,
      );
      specGrad.addColorStop(0, `rgba(${hR}, ${hG}, ${hB}, 0.24)`);
      specGrad.addColorStop(0.4, `rgba(${pR}, ${pG}, ${pB}, 0.05)`);
      specGrad.addColorStop(1, "rgba(2, 2, 27, 0)");
      ctx.fillStyle = specGrad;
      ctx.fillRect(0, 0, W, H);

      // Iridescent rim stroke
      const rimGrad = ctx.createLinearGradient(
        cx - R,
        cy - R,
        cx + R,
        cy + R,
      );
      rimGrad.addColorStop(
        0,
        `rgba(${hR}, ${hG}, ${hB}, ${0.55 + smoothEnergy * 0.25})`,
      );
      rimGrad.addColorStop(
        0.5,
        `rgba(${pR}, ${pG}, ${pB}, ${0.42 + smoothEnergy * 0.35})`,
      );
      rimGrad.addColorStop(
        1,
        `rgba(${sR}, ${sG}, ${sB}, ${0.6 + smoothEnergy * 0.25})`,
      );

      ctx.strokeStyle = rimGrad;
      ctx.lineWidth = Math.max(1.2, 1.35 * dpr);
      ctx.beginPath();
      ctx.arc(cx, cy, R - ctx.lineWidth * 0.6, 0, Math.PI * 2);
      ctx.stroke();

      ctx.restore();

      rafId = requestAnimationFrame(render);
    };

    rafId = requestAnimationFrame(render);
    return () => {
      cancelAnimationFrame(rafId);
    };
  }, [
    mode,
    accent,
    size,
    speechSpeed,
    speechTone,
    listenToMic,
    clampedExternalEnergy,
  ]);

  useEffect(() => {
    const w = window as unknown as { __zoroEndDrag?: () => void };
    w.__zoroEndDrag = () => {
      dragStartedRef.current = false;
      suppressNextClickUntilRef.current = performance.now() + 380;
      if (wrapperRef.current) {
        delete wrapperRef.current.dataset.dragging;
      }
    };
    return () => {
      delete w.__zoroEndDrag;
      if (singleClickTimerRef.current) {
        clearTimeout(singleClickTimerRef.current);
      }
    };
  }, []);

  const beginNativeDrag = () => {
    if (singleClickTimerRef.current) {
      clearTimeout(singleClickTimerRef.current);
      singleClickTimerRef.current = null;
    }
    dragStartedRef.current = true;
    suppressNextClickUntilRef.current = performance.now() + 800;
    if (wrapperRef.current) {
      wrapperRef.current.dataset.dragging = "true";
    }
    onStartDragRef.current?.();
  };

  const handlePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    const now = performance.now();
    const delta = now - lastPointerDownRef.current;
    lastPointerDownRef.current = now;
    pointerDownPosRef.current = { x: e.clientX, y: e.clientY };
    dragStartedRef.current = false;

    if (delta > 0 && delta < 460) {
      // Double-click detected -> immediately start native window drag!
      beginNativeDrag();
    }
  };

  const handlePointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (dragStartedRef.current) return;
    if ((e.buttons & 1) !== 1 || !pointerDownPosRef.current) return;
    const dx = e.clientX - pointerDownPosRef.current.x;
    const dy = e.clientY - pointerDownPosRef.current.y;
    if (Math.hypot(dx, dy) > 5) {
      // User pressed and moved the mouse -> start native window drag!
      beginNativeDrag();
    }
  };

  const handlePointerUp = () => {
    pointerDownPosRef.current = null;
  };

  const handleClick = () => {
    if (
      dragStartedRef.current ||
      performance.now() < suppressNextClickUntilRef.current
    ) {
      return;
    }
    if (!onClickRef.current) return;

    if (singleClickTimerRef.current) {
      clearTimeout(singleClickTimerRef.current);
    }
    singleClickTimerRef.current = setTimeout(() => {
      singleClickTimerRef.current = null;
      if (
        !dragStartedRef.current &&
        performance.now() >= suppressNextClickUntilRef.current
      ) {
        onClickRef.current?.();
      }
    }, 260);
  };

  const style = {
    "--orb-size": `${size}px`,
    "--energy": String(clampedExternalEnergy ?? 0),
  } as CSSProperties;

  return (
    <>
      <style>{ORB_SCOPED_CSS}</style>
      <div
        ref={wrapperRef}
        className={`zoro-orb-root ${docked ? "zoro-orb-docked" : ""}`}
        data-mode={mode}
        data-pinned={pinned ? "true" : undefined}
        data-reactive={listenToMic || energy !== undefined ? "true" : undefined}
        style={style}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onDoubleClick={() => {
          if (!dragStartedRef.current) {
            beginNativeDrag();
          }
        }}
        onClick={handleClick}
        role="button"
        tabIndex={0}
        aria-label={
          pinned
            ? "Zoro AI Orb (Pinned on Top — Alt+F5 to unpin, Double-click and hold to move)"
            : "Zoro AI Orb (Click to talk, Double-click and hold to move, Alt+F5 to pin on top)"
        }
        title={
          pinned
            ? "Zoro (Pinned on Top) • Click to talk • Double-click & drag to move • Alt+F5 unpin"
            : "Zoro • Click to talk • Double-click & drag to move • Alt+F5 pin on top"
        }
        onKeyDown={(e) => {
          if (onClick && (e.key === "Enter" || e.key === " ")) {
            e.preventDefault();
            onClick();
          }
        }}
      >
        <div className="zoro-orb-pin-ring" aria-hidden />
        <canvas ref={canvasRef} className="zoro-orb-canvas" aria-hidden />
      </div>
    </>
  );
}

export default Orb;
