"use client";

import { useState } from "react";
import styles from "./LoginGate.module.css";

/**
 * Access gate for the hosted UI. Until the user enters the password (checked
 * server-side against ZORO_ACCESS_PASSWORD), the app — and crucially the
 * laptop-control token endpoint — stays locked. On success the server sets a
 * signed httpOnly session cookie and we hand control back to the app.
 */
export function LoginGate({ onAuthed }: { onAuthed: () => void }) {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy || !password) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });
      if (res.ok) {
        onAuthed();
        return;
      }
      setError(res.status === 503 ? "Access not configured." : "Incorrect.");
    } catch {
      setError("Couldn't reach the server.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className={styles.gate} onSubmit={submit}>
      <span className={styles.kicker}>● SECURE ACCESS</span>
      <h1 className={styles.title}>ZORO</h1>
      <p className={styles.sub}>Authorization required</p>
      <div className={styles.field}>
        <input
          type="password"
          className={styles.input}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Passphrase"
          autoFocus
          autoComplete="current-password"
          aria-label="Passphrase"
          aria-invalid={!!error}
        />
        <button
          type="submit"
          className={styles.button}
          disabled={busy || !password}
        >
          {busy ? "VERIFYING…" : "AUTHENTICATE"}
        </button>
      </div>
      <span className={`${styles.status} ${error ? styles.err : ""}`}>
        {error ?? (busy ? "" : " ")}
      </span>
    </form>
  );
}
