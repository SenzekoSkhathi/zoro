"use client";

import type { CSSProperties } from "react";
import styles from "./Orb.module.css";

export type OrbMode =
  | "idle"
  | "listening"
  | "connecting"
  | "thinking"
  | "speaking";

/**
 * Holographic Siri-style fluid 3D glass orb for Zoro.
 *
 * Visual anatomy:
 *  - Outer ethereal aura & bloom (magenta, cyan, violet, green)
 *  - Smoky 3D glass sphere with optical rim lighting and specular lens reflection
 *  - Swirling multi-color fluid ribbon petals (magenta, neon cyan, emerald green, warm amber)
 *  - Luminous high-intensity white fusion core at the epicenter
 *  - Real-time audio energy reactivity (undulates, breathes, and scales with voice volume)
 */
export function Orb({
  mode = "idle",
  energy,
  accent,
  docked = false,
}: {
  mode?: OrbMode;
  energy?: number;
  accent?: string;
  docked?: boolean;
}) {
  const reactive = energy !== undefined;
  const clampedEnergy = energy !== undefined ? Math.min(1, Math.max(0, energy)) : 0;

  const style = {
    ...(accent ? { "--accent": accent } : {}),
    "--energy": String(clampedEnergy),
  } as CSSProperties;

  return (
    <div
      className={`${styles.orbWrapper} ${docked ? styles.docked : ""}`}
      data-mode={mode}
      data-reactive={reactive ? "true" : undefined}
      style={style}
    >
      {/* Outer luminous bloom & ethereal aura */}
      <div className={styles.aura} aria-hidden />

      {/* 3D Glass Sphere Body */}
      <div className={styles.sphere}>
        {/* Ambient colored chromatic plasma in the background */}
        <div className={styles.plasma} aria-hidden />

        {/* Primary swirling fluid ribbon petals */}
        <div className={`${styles.ribbonCluster} ${styles.primaryCluster}`} aria-hidden>
          <div className={`${styles.petal} ${styles.petalMagenta}`} />
          <div className={`${styles.petal} ${styles.petalCyan}`} />
          <div className={`${styles.petal} ${styles.petalGreen}`} />
          <div className={`${styles.petal} ${styles.petalAmber}`} />
        </div>

        {/* Secondary counter-swirling ribbon petals for organic fluid depth */}
        <div className={`${styles.ribbonCluster} ${styles.secondaryCluster}`} aria-hidden>
          <div className={`${styles.petal} ${styles.petalMagentaLight}`} />
          <div className={`${styles.petal} ${styles.petalCyanLight}`} />
          <div className={`${styles.petal} ${styles.petalGreenLight}`} />
        </div>

        {/* Radiant white fusion epicenter */}
        <div className={styles.core} aria-hidden>
          <div className={styles.coreInner} />
        </div>

        {/* 3D Glass curved lens reflection & rim sheen */}
        <div className={styles.glassSheen} aria-hidden />
      </div>
    </div>
  );
}
