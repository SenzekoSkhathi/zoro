"use client";

import React, { useEffect, useRef, type CSSProperties } from "react";

const styles = {
  orbWrapper: "siri-orb-wrapper",
  docked: "siri-orb-docked",
  aura: "siri-orb-aura",
  sphere: "siri-orb-sphere",
  plasma: "siri-orb-plasma",
  ribbonCluster: "siri-orb-ribbonCluster",
  primaryCluster: "siri-orb-primaryCluster",
  secondaryCluster: "siri-orb-secondaryCluster",
  petal: "siri-orb-petal",
  petalMagenta: "siri-orb-petalMagenta",
  petalCyan: "siri-orb-petalCyan",
  petalGreen: "siri-orb-petalGreen",
  petalAmber: "siri-orb-petalAmber",
  petalMagentaLight: "siri-orb-petalMagentaLight",
  petalCyanLight: "siri-orb-petalCyanLight",
  petalGreenLight: "siri-orb-petalGreenLight",
  core: "siri-orb-core",
  coreInner: "siri-orb-coreInner",
  glassSheen: "siri-orb-glassSheen",
};

const ORB_SCOPED_CSS = `
.siri-orb-wrapper {
  --accent: #3fd0ff;
  --energy: 0;        /* 0..1 overall live voice syllable energy */
  --bass: 0;          /* 0..1 low-frequency vocal warmth */
  --mid: 0;           /* 0..1 mid-frequency vocal formants */
  --treble: 0;        /* 0..1 high-frequency sibilance & air */
  --tone: 0.5;        /* 0..1 vocal pitch balance (0 = warm/deep, 1 = bright/high) */
  --flow-a: 0deg;     /* continuous organic rotation phase for primary petals */
  --flow-b: 0deg;     /* counter-rotating phase for secondary petals */
  --wobble-x: 0px;    /* subtle internal fluid drift X */
  --wobble-y: 0px;    /* subtle internal fluid drift Y */
  --orb-size: 92px;

  position: relative;
  width: var(--orb-size);
  height: var(--orb-size);
  border-radius: 50%;
  display: grid;
  place-items: center;
  user-select: none;
  cursor: pointer;
  background: radial-gradient(
    circle at 50% 50%,
    #050208 0%,
    #020003 72%,
    #010002 100%
  );
  box-shadow:
    0 0 calc(16px + var(--energy) * 14px) rgba(68, 118, 204, calc(0.46 + var(--energy) * 0.24)),
    0 0 calc(34px + var(--energy) * 22px) rgba(42, 82, 160, calc(0.26 + var(--energy) * 0.18)),
    0 10px 28px rgba(0, 0, 0, 0.88),
    inset 0 0 4px rgba(110, 165, 255, 0.14);
  transition:
    transform 0.35s cubic-bezier(0.22, 1, 0.36, 1),
    box-shadow 0.08s linear;
}

.siri-orb-wrapper:hover {
  transform: scale(1.04);
}

.siri-orb-wrapper:active {
  transform: scale(0.96);
}

/* Content docked state: compact floating orb in the bottom-right corner matching screenshot */
.siri-orb-docked {
  position: fixed;
  bottom: 18px;
  right: 18px;
  z-index: 50;
}

/* ── Outer Atmospheric Cool-Blue Halo ──────────────────────────────────── */
.siri-orb-aura {
  position: absolute;
  inset: -10px;
  border-radius: 50%;
  pointer-events: none;
  background: radial-gradient(
    circle,
    rgba(78, 136, 230, 0.38) 0%,
    rgba(56, 104, 192, 0.22) 52%,
    transparent 76%
  );
  filter: blur(10px);
  opacity: calc(0.72 + var(--energy) * 0.28);
  transform: scale(calc(1 + var(--energy) * 0.1));
  animation: auraBreathe 4.8s ease-in-out infinite alternate;
  transition:
    transform 0.08s linear,
    opacity 0.08s linear;
}

/* ── Inner 3D Glass Sphere (68.5% of outer black housing — stays steady) ── */
.siri-orb-sphere {
  position: relative;
  width: 68.5%;
  height: 68.5%;
  border-radius: 50%;
  overflow: hidden;
  background:
    radial-gradient(
      circle at 18% 52%,
      rgba(55, 95, 195, calc(0.4 + var(--mid) * 0.2)) 0%,
      transparent 48%
    ),
    radial-gradient(
      circle at 58% 88%,
      rgba(165, 118, 110, calc(0.36 + var(--bass) * 0.2)) 0%,
      transparent 52%
    ),
    radial-gradient(
      circle at 82% 38%,
      rgba(48, 86, 74, calc(0.32 + var(--treble) * 0.2)) 0%,
      transparent 48%
    ),
    radial-gradient(
      circle at 36% 18%,
      rgba(110, 22, 52, calc(0.42 + var(--bass) * 0.22)) 0%,
      transparent 50%
    ),
    radial-gradient(
      circle at 50% 50%,
      #160d1e 0%,
      #0c0712 68%,
      #060309 100%
    );
  box-shadow:
    inset 2.5px 0 5px rgba(82, 128, 235, 0.62),
    inset -1.5px -2.5px 6px rgba(210, 168, 152, 0.65),
    inset 1px 2.5px 5px rgba(145, 32, 68, 0.52),
    inset -2px 1.5px 5px rgba(88, 138, 118, 0.45),
    inset 0 0 12px rgba(0, 0, 0, 0.75);
  display: grid;
  place-items: center;
}

/* ── Subtle Ambient Rim Refraction ─────────────────────────────────────── */
.siri-orb-plasma {
  position: absolute;
  inset: 0;
  border-radius: 50%;
  pointer-events: none;
  background: conic-gradient(
    from 190deg at 50% 50%,
    rgba(70, 120, 235, 0.28) 0deg,
    rgba(165, 28, 72, 0.32) 75deg,
    rgba(32, 68, 56, 0.22) 155deg,
    rgba(195, 150, 138, 0.36) 245deg,
    rgba(155, 42, 88, 0.28) 310deg,
    rgba(70, 120, 235, 0.28) 360deg
  );
  filter: blur(5px);
  opacity: calc(0.72 + var(--energy) * 0.28);
  transform: rotate(var(--flow-a));
}

/* ── Swirling Siri Petal Clusters (inside the steady sphere) ───────────── */
.siri-orb-ribbonCluster {
  position: absolute;
  inset: 0;
  pointer-events: none;
}

.siri-orb-primaryCluster {
  transform:
    translate3d(var(--wobble-x), var(--wobble-y), 0)
    rotate(var(--flow-a))
    scale(calc(0.96 + var(--energy) * 0.16));
  transition: transform 0.04s linear;
}

.siri-orb-secondaryCluster {
  transform:
    translate3d(calc(var(--wobble-y) * -0.7), calc(var(--wobble-x) * 0.7), 0)
    rotate(var(--flow-b))
    scale(calc(0.95 + var(--energy) * 0.14));
  transition: transform 0.04s linear;
}

/* ── Sculpted Silk-Glass Siri Petals ───────────────────────────────────── */
.siri-orb-petal {
  position: absolute;
  pointer-events: none;
  mix-blend-mode: screen;
  will-change: transform, border-radius;
}

/* 1. Deep Ruby-Crimson Upper-Left & Lower-Left Sweeping Lobe (responds to warm bass & tone) */
.siri-orb-petalMagenta {
  top: 9%;
  left: 15%;
  width: 56%;
  height: 74%;
  background: radial-gradient(
    ellipse at 58% 48%,
    rgba(255, 95, 145, 0.95) 0%,
    rgba(220, 38, 92, 0.88) 48%,
    rgba(168, 22, 68, 0.72) 82%,
    rgba(130, 14, 52, 0.18) 96%,
    transparent 100%
  );
  border-radius: 56% 44% 48% 52% / 46% 54% 46% 54%;
  box-shadow: inset 0 1px 2px rgba(255, 160, 195, 0.45);
  filter: blur(0.6px);
  transform:
    rotate(calc(-24deg - var(--bass) * 18deg))
    scaleX(calc(0.96 + var(--bass) * 0.28 + (1 - var(--tone)) * var(--energy) * 0.16))
    scaleY(calc(0.96 + var(--energy) * 0.22));
  animation: morphRuby 5.2s ease-in-out infinite alternate;
  transition: transform 0.04s linear;
}

/* 2. Electric Sky-Blue / Cyan Horizontal Left Lobe (responds to vocal mids & formants) */
.siri-orb-petalCyan {
  top: 33%;
  left: 9%;
  width: 48%;
  height: 33%;
  background: linear-gradient(
    98deg,
    rgba(88, 178, 255, 0.96) 0%,
    rgba(115, 205, 255, 0.92) 45%,
    rgba(210, 245, 255, 0.98) 88%,
    rgba(255, 255, 255, 0) 100%
  );
  border-radius: 58% 42% 40% 60% / 54% 46% 54% 46%;
  box-shadow:
    inset 1px 1px 2px rgba(205, 240, 255, 0.75),
    0 0 6px rgba(72, 168, 255, 0.35);
  filter: blur(0.45px);
  transform:
    rotate(calc(14deg + var(--mid) * 22deg))
    scaleX(calc(0.95 + var(--mid) * 0.34 + var(--tone) * var(--energy) * 0.14))
    scaleY(calc(0.96 + var(--energy) * 0.2));
  animation: morphCyan 4.4s ease-in-out infinite alternate;
  transition: transform 0.04s linear;
}

/* 3. Luminous Mint / Seafoam Emerald Right Lobe (responds to mids & bright treble) */
.siri-orb-petalGreen {
  top: 30%;
  right: 12%;
  width: 48%;
  height: 54%;
  background: radial-gradient(
    ellipse at 32% 42%,
    rgba(220, 255, 238, 0.96) 0%,
    rgba(110, 228, 172, 0.88) 38%,
    rgba(52, 182, 126, 0.76) 78%,
    rgba(28, 130, 88, 0.15) 96%,
    transparent 100%
  );
  border-radius: 42% 58% 52% 48% / 48% 56% 44% 52%;
  box-shadow: inset -1px 1px 2px rgba(190, 255, 220, 0.55);
  filter: blur(0.55px);
  transform:
    rotate(calc(18deg - var(--treble) * 20deg))
    scaleX(calc(0.96 + var(--mid) * 0.24 + var(--treble) * 0.18))
    scaleY(calc(0.96 + var(--treble) * 0.26 + var(--tone) * var(--energy) * 0.14));
  animation: morphGreen 4.8s ease-in-out infinite alternate;
  transition: transform 0.04s linear;
}

/* 4. Warm Champagne-Peach Lower Lobe (responds to warm vocal chest resonance) */
.siri-orb-petalAmber {
  bottom: 11%;
  left: 31%;
  width: 28%;
  height: 44%;
  background: linear-gradient(
    185deg,
    rgba(255, 255, 255, 0.98) 0%,
    rgba(252, 224, 210, 0.92) 42%,
    rgba(236, 186, 168, 0.84) 82%,
    rgba(205, 135, 120, 0.15) 97%,
    transparent 100%
  );
  border-radius: 46% 54% 50% 50% / 38% 38% 62% 62%;
  box-shadow: inset 0 -1px 2px rgba(255, 235, 220, 0.65);
  filter: blur(0.45px);
  transform:
    rotate(calc(8deg + var(--bass) * 14deg))
    scaleX(calc(0.96 + var(--energy) * 0.18))
    scaleY(calc(0.95 + var(--bass) * 0.3 + (1 - var(--tone)) * var(--energy) * 0.15));
  animation: morphPeach 4.6s ease-in-out infinite alternate;
  transition: transform 0.04s linear;
}

/* 5. Lower-Right Rose-Coral Cross Wing */
.siri-orb-petalMagentaLight {
  bottom: 20%;
  right: 11%;
  width: 54%;
  height: 36%;
  background: radial-gradient(
    ellipse at 32% 45%,
    rgba(255, 185, 205, 0.85) 0%,
    rgba(218, 68, 108, 0.72) 55%,
    rgba(165, 35, 72, 0.52) 86%,
    transparent 98%
  );
  border-radius: 44% 56% 48% 52% / 52% 48% 52% 48%;
  filter: blur(0.6px);
  transform:
    rotate(calc(28deg - var(--mid) * 16deg))
    scale(calc(0.96 + var(--energy) * 0.24));
  animation: morphWing 5.4s ease-in-out infinite alternate;
  transition: transform 0.04s linear;
}

/* 6. Smoked-Teal Translucent Upper-Right Glass Fin */
.siri-orb-petalCyanLight {
  top: 14%;
  right: 22%;
  width: 34%;
  height: 44%;
  background: linear-gradient(
    210deg,
    rgba(135, 185, 172, 0.42) 0%,
    rgba(95, 158, 148, 0.52) 48%,
    rgba(210, 250, 240, 0.75) 90%,
    transparent 100%
  );
  border-radius: 52% 48% 45% 55% / 58% 42% 58% 42%;
  box-shadow: inset -1px 1px 1.5px rgba(210, 245, 235, 0.4);
  filter: blur(0.5px);
  transform:
    rotate(calc(32deg + var(--treble) * 18deg))
    scale(calc(0.96 + var(--treble) * 0.28));
  animation: morphGlassFin 5.8s ease-in-out infinite alternate;
  transition: transform 0.04s linear;
}

/* 7. Delicate Vertical Ruby Filament Stem at 6 o'clock */
.siri-orb-petalGreenLight {
  bottom: 4%;
  left: 47.5%;
  width: 5.5%;
  height: 44%;
  background: linear-gradient(
    to bottom,
    rgba(255, 255, 255, 0.95) 0%,
    rgba(235, 110, 140, 0.82) 55%,
    rgba(185, 52, 82, 0.65) 90%,
    transparent 100%
  );
  border-radius: 999px;
  filter: blur(0.35px);
  transform: scaleY(calc(0.95 + var(--energy) * 0.22));
  transition: transform 0.04s linear;
}

/* ── Blazing White Intersection Core ───────────────────────────────────── */
.siri-orb-core {
  position: absolute;
  top: 52%;
  left: 49%;
  transform: translate(
    calc(-50% + var(--wobble-x) * 0.6),
    calc(-50% + var(--wobble-y) * 0.6)
  );
  width: 27%;
  height: 27%;
  border-radius: 50%;
  display: grid;
  place-items: center;
  pointer-events: none;
}

.siri-orb-coreInner {
  width: 100%;
  height: 100%;
  border-radius: 50%;
  background: radial-gradient(
    circle,
    #ffffff 0%,
    #ffffff 48%,
    rgba(235, 252, 255, 0.92) 72%,
    rgba(180, 235, 255, 0) 100%
  );
  box-shadow:
    0 0 calc(8px + var(--energy) * 16px) #ffffff,
    0 0 calc(16px + var(--energy) * 28px) rgba(215, 248, 255, 0.95),
    0 0 calc(24px + var(--bass) * 32px) rgba(120, 220, 255, 0.78);
  filter: blur(0.8px);
  transform: scale(calc(0.9 + var(--energy) * 0.45 + var(--mid) * 0.15));
  transition:
    transform 0.04s linear,
    box-shadow 0.04s linear;
}

/* ── 3D Curved Sphere Specular Rim & Inner Shadow ──────────────────────── */
.siri-orb-glassSheen {
  position: absolute;
  inset: 0;
  border-radius: 50%;
  pointer-events: none;
  background:
    radial-gradient(
      circle at 50% 50%,
      transparent 58%,
      rgba(10, 6, 16, 0.35) 86%,
      rgba(185, 155, 148, 0.22) 96%,
      rgba(255, 255, 255, 0.28) 100%
    ),
    radial-gradient(
      ellipse at 32% 20%,
      rgba(255, 255, 255, 0.14) 0%,
      transparent 45%
    );
}

/* ── Mode Modifiers (Thinking & Connecting Circling Motion) ────────────── */
.siri-orb-wrapper[data-mode="thinking"] .siri-orb-plasma {
  animation: spinCW 2s linear infinite;
  opacity: 0.92;
}

.siri-orb-wrapper[data-mode="thinking"] .siri-orb-primaryCluster {
  animation: orbitThinkingCW 1.8s linear infinite;
}

.siri-orb-wrapper[data-mode="thinking"] .siri-orb-secondaryCluster {
  animation: orbitThinkingCCW 2.4s linear infinite;
}

.siri-orb-wrapper[data-mode="thinking"] .siri-orb-core {
  animation: coreOrbitThinking 1.8s linear infinite;
}

.siri-orb-wrapper[data-mode="connecting"] .siri-orb-aura {
  animation: auraBreathe 1.2s ease-in-out infinite alternate;
}

.siri-orb-wrapper[data-mode="connecting"] .siri-orb-plasma {
  animation: spinCW 2.6s linear infinite;
  opacity: 0.88;
}

.siri-orb-wrapper[data-mode="connecting"] .siri-orb-primaryCluster {
  animation: orbitConnectingCW 2.2s linear infinite;
}

.siri-orb-wrapper[data-mode="connecting"] .siri-orb-secondaryCluster {
  animation: orbitConnectingCCW 3s linear infinite;
}

.siri-orb-wrapper[data-mode="connecting"] .siri-orb-core {
  animation: coreOrbitConnecting 2.2s linear infinite;
}

/* ── Keyframes ─────────────────────────────────────────────────────────── */
@keyframes spinCW {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}

/* Smooth circular orbital movement for Thinking & Connecting modes */
@keyframes orbitThinkingCW {
  0% {
    transform: rotate(0deg) translateX(4.5px) rotate(0deg) scale(1.02);
  }
  50% {
    transform: rotate(180deg) translateX(4.5px) rotate(180deg) scale(1.08);
  }
  100% {
    transform: rotate(360deg) translateX(4.5px) rotate(360deg) scale(1.02);
  }
}

@keyframes orbitThinkingCCW {
  0% {
    transform: rotate(360deg) translateX(3.5px) rotate(-360deg) scale(0.98);
  }
  50% {
    transform: rotate(180deg) translateX(3.5px) rotate(-180deg) scale(1.05);
  }
  100% {
    transform: rotate(0deg) translateX(3.5px) rotate(0deg) scale(0.98);
  }
}

@keyframes coreOrbitThinking {
  0% {
    transform: translate(-50%, -50%) rotate(0deg) translateX(4px);
  }
  100% {
    transform: translate(-50%, -50%) rotate(360deg) translateX(4px);
  }
}

@keyframes orbitConnectingCW {
  0% {
    transform: rotate(0deg) translateX(5.5px) rotate(0deg) scale(0.96);
  }
  50% {
    transform: rotate(180deg) translateX(5.5px) rotate(180deg) scale(1.06);
  }
  100% {
    transform: rotate(360deg) translateX(5.5px) rotate(360deg) scale(0.96);
  }
}

@keyframes orbitConnectingCCW {
  0% {
    transform: rotate(0deg) translateX(4px) rotate(-360deg) scale(0.94);
  }
  50% {
    transform: rotate(180deg) translateX(4px) rotate(-180deg) scale(1.04);
  }
  100% {
    transform: rotate(360deg) translateX(4px) rotate(0deg) scale(0.94);
  }
}

@keyframes coreOrbitConnecting {
  0% {
    transform: translate(-50%, -50%) rotate(0deg) translateX(5px);
  }
  100% {
    transform: translate(-50%, -50%) rotate(360deg) translateX(5px);
  }
}

@keyframes auraBreathe {
  0% {
    transform: scale(0.98);
    opacity: 0.7;
  }
  100% {
    transform: scale(1.03);
    opacity: 0.88;
  }
}

@keyframes morphRuby {
  0% {
    border-radius: 56% 44% 48% 52% / 46% 54% 46% 54%;
  }
  100% {
    border-radius: 46% 54% 56% 44% / 56% 44% 54% 46%;
  }
}

@keyframes morphCyan {
  0% {
    border-radius: 58% 42% 40% 60% / 54% 46% 54% 46%;
  }
  100% {
    border-radius: 44% 56% 54% 46% / 44% 56% 46% 54%;
  }
}

@keyframes morphGreen {
  0% {
    border-radius: 42% 58% 52% 48% / 48% 56% 44% 52%;
  }
  100% {
    border-radius: 54% 46% 42% 58% / 56% 44% 54% 46%;
  }
}

@keyframes morphPeach {
  0% {
    border-radius: 46% 54% 50% 50% / 38% 38% 62% 62%;
  }
  100% {
    border-radius: 54% 46% 46% 54% / 46% 46% 54% 54%;
  }
}

@keyframes morphWing {
  0% {
    border-radius: 44% 56% 48% 52% / 52% 48% 52% 48%;
  }
  100% {
    border-radius: 54% 46% 54% 46% / 46% 54% 46% 54%;
  }
}

@keyframes morphGlassFin {
  0% {
    border-radius: 52% 48% 45% 55% / 58% 42% 58% 42%;
  }
  100% {
    border-radius: 45% 55% 52% 48% / 48% 52% 48% 52%;
  }
}

@media (prefers-reduced-motion: reduce) {
  .siri-orb-aura,
  .siri-orb-plasma,
  .siri-orb-primaryCluster,
  .siri-orb-secondaryCluster,
  .siri-orb-petal,
  .siri-orb-coreInner {
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

/**
 * Compact Siri-style glass orb with live Web Audio API microphone reactivity.
 *
 * Visual anatomy matching reference screenshot:
 *  - Compact 92px pitch-black outer housing disc with cool-blue atmospheric bloom
 *  - 68.5% inner 3D glass sphere with directional sapphire, ruby, seafoam & peach rim refraction
 *  - Crisp, silk-edged translucent Siri petals (ruby, sky-cyan, mint-emerald, champagne-peach, smoked-teal fin & ruby filament)
 *  - Additive screen blending that naturally fuses into a radiant white core
 *  - Multi-band Web Audio API microphone analyzer (--energy, --bass, --mid, --treble)
 */
export function Orb({
  mode = "idle",
  energy,
  bands,
  accent,
  docked = false,
  size = 92,
  listenToMic = false,
  speechSpeed = 1,
  speechTone = 0.5,
  onAudioBands,
  onClick,
}: {
  mode?: OrbMode;
  energy?: number;
  /** Live audio frequency spectrum bands (e.g. 5-band array from useMultibandTrackVolume) */
  bands?: number[];
  accent?: string;
  docked?: boolean;
  size?: number;
  listenToMic?: boolean;
  /** Speaking tempo multiplier (e.g. 0.6 = slow/calm, 1.0 = natural, 1.6 = fast/excited) */
  speechSpeed?: number;
  /** AI vocal pitch/timbre (0 = deep/warm bass, 0.5 = balanced, 1 = bright/high treble) */
  speechTone?: number;
  onAudioBands?: (bands: AudioBands) => void;
  onClick?: () => void;
}) {
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const onBandsRef = useRef(onAudioBands);
  onBandsRef.current = onAudioBands;

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

  // Calculate target frequency metrics whenever bands, energy, or speechTone change
  useEffect(() => {
    if (bands && bands.length > 0) {
      let b = 0;
      let m = 0;
      let t = 0;

      if (bands.length >= 5) {
        // 5-band spectrum from LiveKit useMultibandTrackVolume:
        // bands[0]: Sub-bass (deep chest resonance / warmth)
        // bands[1]: Mid-bass (vocal fundamental)
        // bands[2]: Low-mid (vocal formants F1/F2)
        // bands[3]: High-mid (articulation / presence)
        // bands[4]: Treble (fricatives / sibilance / air)
        b = Math.min(1, Math.max(0, ((bands[0] ?? 0) * 0.45 + (bands[1] ?? 0) * 0.55) * 3.4));
        m = Math.min(1, Math.max(0, ((bands[2] ?? 0) * 0.60 + (bands[3] ?? 0) * 0.40) * 3.2));
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
          b * 0.38 + m * 0.44 + t * 0.18
        )
      );

      const dynamicTone =
        b + t > 0.04
          ? Math.min(1, Math.max(0, t / (b + t)))
          : speechTone;

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

  useEffect(() => {
    if (!listenToMic) return;
    const el = wrapperRef.current;
    if (!el) return;

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

          // Compute frequency bands (256 FFT -> 128 bins)
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
            Math.max(0, (trebleSum / 44 - 10) / 120)
          );

          const rawOverall = Math.min(
            1,
            rawBass * 0.45 + rawMid * 0.4 + rawTreble * 0.15
          );

          const targetEnergy =
            clampedExternalEnergy !== undefined
              ? Math.max(clampedExternalEnergy, rawOverall)
              : rawOverall;

          smoothEnergy += (targetEnergy - smoothEnergy) * 0.28;
          smoothBass += (rawBass - smoothBass) * 0.3;
          smoothMid += (rawMid - smoothMid) * 0.28;
          smoothTreble += (rawTreble - smoothTreble) * 0.32;

          if (wrapperRef.current) {
            wrapperRef.current.style.setProperty(
              "--energy",
              smoothEnergy.toFixed(3)
            );
            wrapperRef.current.style.setProperty(
              "--bass",
              smoothBass.toFixed(3)
            );
            wrapperRef.current.style.setProperty("--mid", smoothMid.toFixed(3));
            wrapperRef.current.style.setProperty(
              "--treble",
              smoothTreble.toFixed(3)
            );
          }

          if (onBandsRef.current) {
            onBandsRef.current({
              energy: smoothEnergy,
              bass: smoothBass,
              mid: smoothMid,
              treble: smoothTreble,
            });
          }

          rafId = requestAnimationFrame(tick);
        };

        rafId = requestAnimationFrame(tick);
      } catch {
        // Fallback if microphone permission is declined or unavailable
      }
    }

    startMic();

    return () => {
      cancelled = true;
      if (rafId) cancelAnimationFrame(rafId);
      if (stream) stream.getTracks().forEach((t) => t.stop());
      if (audioCtx && audioCtx.state !== "closed") {
        audioCtx.close().catch(() => {});
      }
    };
  }, [listenToMic, clampedExternalEnergy]);

  // Smooth Siri-style internal petal flow & syllable undulation (outer sphere stays steady)
  useEffect(() => {
    const el = wrapperRef.current;
    if (!el) return;

    const clampedSpeed = Math.min(2.2, Math.max(0.4, speechSpeed));
    const clampedTone = Math.min(1, Math.max(0, speechTone));
    el.style.setProperty("--tone", clampedTone.toFixed(3));

    let raf = 0;
    let angleA = 0;
    let angleB = 0;
    let phase = 0;
    let smoothCadence = 0;
    let smoothEnergy = 0;
    let smoothBass = 0;
    let smoothMid = 0;
    let smoothTreble = 0;
    let smoothTone = clampedTone;
    let lastTime = performance.now();

    const step = (now: number) => {
      const dt = Math.min(0.05, (now - lastTime) / 1000);
      lastTime = now;

      const target = targetBandsRef.current;
      phase += dt * clampedSpeed;

      let activeEnergy = smoothEnergy;
      let activeBass = smoothBass;
      let activeMid = smoothMid;
      let activeTreble = smoothTreble;

      if (!listenToMic) {
        if (target.hasBands || clampedExternalEnergy !== undefined) {
          smoothEnergy += (target.energy - smoothEnergy) * 0.28;
          smoothBass += (target.bass - smoothBass) * 0.30;
          smoothMid += (target.mid - smoothMid) * 0.28;
          smoothTreble += (target.treble - smoothTreble) * 0.32;
          smoothTone += (target.tone - smoothTone) * 0.18;

          activeEnergy = smoothEnergy;
          activeBass = smoothBass;
          activeMid = smoothMid;
          activeTreble = smoothTreble;

          el.style.setProperty("--energy", activeEnergy.toFixed(3));
          el.style.setProperty("--bass", activeBass.toFixed(3));
          el.style.setProperty("--mid", activeMid.toFixed(3));
          el.style.setProperty("--treble", activeTreble.toFixed(3));
        } else if (mode === "speaking" && smoothEnergy < 0.02) {
          const syllableWave =
            0.5 +
            0.32 * Math.sin(phase * 5.4) +
            0.18 * Math.sin(phase * 11.8 + 0.7);
          const phraseEnvelope = Math.max(0.18, Math.sin(phase * 1.6) * 0.55 + 0.45);
          activeEnergy = Math.min(1, syllableWave * phraseEnvelope * 0.75);
          activeBass = activeEnergy * (1.1 - clampedTone * 0.5);
          activeMid = activeEnergy * (0.75 + 0.25 * Math.sin(phase * 7.2));
          activeTreble = activeEnergy * (0.5 + clampedTone * 0.6);

          el.style.setProperty("--energy", activeEnergy.toFixed(3));
          el.style.setProperty("--bass", Math.min(1, activeBass).toFixed(3));
          el.style.setProperty("--mid", Math.min(1, activeMid).toFixed(3));
          el.style.setProperty("--treble", Math.min(1, activeTreble).toFixed(3));
        } else {
          smoothEnergy += (0 - smoothEnergy) * 0.22;
          smoothBass += (0 - smoothBass) * 0.22;
          smoothMid += (0 - smoothMid) * 0.22;
          smoothTreble += (0 - smoothTreble) * 0.22;
          activeEnergy = smoothEnergy;
          activeBass = smoothBass;
          activeMid = smoothMid;
          activeTreble = smoothTreble;

          el.style.setProperty("--energy", activeEnergy.toFixed(3));
          el.style.setProperty("--bass", activeBass.toFixed(3));
          el.style.setProperty("--mid", activeMid.toFixed(3));
          el.style.setProperty("--treble", activeTreble.toFixed(3));
        }
      } else {
        activeEnergy = parseFloat(el.style.getPropertyValue("--energy")) || 0;
        activeBass = parseFloat(el.style.getPropertyValue("--bass")) || 0;
        activeMid = parseFloat(el.style.getPropertyValue("--mid")) || 0;
        activeTreble = parseFloat(el.style.getPropertyValue("--treble")) || 0;
      }

      if (onBandsRef.current && !listenToMic) {
        onBandsRef.current({
          energy: activeEnergy,
          bass: activeBass,
          mid: activeMid,
          treble: activeTreble,
        });
      }

      smoothCadence += (activeEnergy - smoothCadence) * 0.22;

      // In thinking & connecting modes, perform a crisp continuous circling orbit
      const isThinking = mode === "thinking";
      const isConnecting = mode === "connecting";

      const baseDegPerSec = isThinking
        ? 210
        : isConnecting
        ? 155
        : mode === "speaking"
        ? 28 * clampedSpeed
        : 18;
      const voiceBoostDeg =
        isThinking || isConnecting ? 0 : smoothCadence * 115 * clampedSpeed;

      angleA = (angleA + dt * (baseDegPerSec + voiceBoostDeg)) % 360;
      angleB = (angleB - dt * (baseDegPerSec * 0.82 + voiceBoostDeg * 0.9)) % 360;

      // Circular orbital path when Thinking/Connecting; subtle syllable drift when Speaking/Listening
      const orbitAngleRad = (angleA * Math.PI) / 180;
      const orbitRadius = isThinking
        ? 4.8
        : isConnecting
        ? 5.8
        : smoothCadence * 2.8;
      const wobbleX =
        isThinking || isConnecting
          ? Math.cos(orbitAngleRad) * orbitRadius
          : Math.cos(phase * 4.2) * orbitRadius;
      const wobbleY =
        isThinking || isConnecting
          ? Math.sin(orbitAngleRad) * orbitRadius
          : Math.sin(phase * 3.5) * orbitRadius;

      const liveSpectralTone =
        activeBass + activeTreble > 0.05
          ? activeTreble / (activeBass + activeTreble)
          : clampedTone;
      const effectiveTone = clampedTone * 0.5 + liveSpectralTone * 0.5;

      el.style.setProperty("--flow-a", `${angleA.toFixed(2)}deg`);
      el.style.setProperty("--flow-b", `${angleB.toFixed(2)}deg`);
      el.style.setProperty("--wobble-x", `${wobbleX.toFixed(2)}px`);
      el.style.setProperty("--wobble-y", `${wobbleY.toFixed(2)}px`);
      el.style.setProperty("--tone", effectiveTone.toFixed(3));

      raf = requestAnimationFrame(step);
    };

    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [mode, speechSpeed, speechTone, listenToMic, clampedExternalEnergy]);

  const style = {
    ...(accent ? { "--accent": accent } : {}),
    "--orb-size": `${size}px`,
    "--energy": String(clampedExternalEnergy ?? 0),
    "--tone": String(Math.min(1, Math.max(0, speechTone))),
  } as CSSProperties;

  return (
    <>
      <style>{ORB_SCOPED_CSS}</style>
      <div
        ref={wrapperRef}
        className={`${styles.orbWrapper} ${docked ? styles.docked : ""}`}
        data-mode={mode}
        data-reactive={listenToMic || energy !== undefined ? "true" : undefined}
        style={style}
        onClick={onClick}
        role="button"
        tabIndex={0}
        aria-label="Voice Assistant Siri Orb"
        onKeyDown={(e) => {
          if (onClick && (e.key === "Enter" || e.key === " ")) {
            e.preventDefault();
            onClick();
          }
        }}
      >
        {/* Outer cool-blue atmospheric bloom */}
        <div className={styles.aura} aria-hidden />

        {/* Inner 3D Glass Sphere Body (68.5% of black housing) */}
        <div className={styles.sphere}>
          {/* Subtle inner rim color refraction */}
          <div className={styles.plasma} aria-hidden />

          {/* Primary sculpted Siri ribbon petals */}
          <div
            className={`${styles.ribbonCluster} ${styles.primaryCluster}`}
            aria-hidden
          >
            <div className={`${styles.petal} ${styles.petalMagenta}`} />
            <div className={`${styles.petal} ${styles.petalCyan}`} />
            <div className={`${styles.petal} ${styles.petalGreen}`} />
            <div className={`${styles.petal} ${styles.petalAmber}`} />
          </div>

          {/* Secondary counter-swirling glass fin, wing & vertical filament */}
          <div
            className={`${styles.ribbonCluster} ${styles.secondaryCluster}`}
            aria-hidden
          >
            <div className={`${styles.petal} ${styles.petalMagentaLight}`} />
            <div className={`${styles.petal} ${styles.petalCyanLight}`} />
            <div className={`${styles.petal} ${styles.petalGreenLight}`} />
          </div>

          {/* Blazing white fusion epicenter */}
          <div className={styles.core} aria-hidden>
            <div className={styles.coreInner} />
          </div>

          {/* 3D Glass curved rim sheen */}
          <div className={styles.glassSheen} aria-hidden />
        </div>
      </div>
    </>
  );
}

export default Orb;

