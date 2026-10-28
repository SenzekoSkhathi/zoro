import { cookies } from "next/headers";
import { SignJWT, jwtVerify } from "jose";

// Single-user access gate for Zoro's hosted UI. The dangerous endpoints
// (/api/token mints a LiveKit join token that can drive the laptop;
// /api/telemetry exposes private activity) require a valid session cookie.
//
// Design notes:
//   - One secret to manage: ZORO_ACCESS_PASSWORD. The session JWT is signed
//     with it, so rotating the password instantly invalidates all sessions.
//   - FAIL CLOSED: if ZORO_ACCESS_PASSWORD is unset, login always fails and
//     every protected route 401s. A missing env var must never mean "open".

export const SESSION_COOKIE = "zoro_session";
// Long-lived: log in once per device and forget. Safe because the JWT is
// signed with the password — rotating ZORO_ACCESS_PASSWORD instantly
// revokes every outstanding session everywhere (the global kill switch).
const SESSION_TTL = "365d";
export const SESSION_MAX_AGE = 365 * 24 * 60 * 60;

function passwordOrNull(): string | null {
  const p = process.env.ZORO_ACCESS_PASSWORD;
  return p && p.length > 0 ? p : null;
}

function signingKey(pw: string): Uint8Array {
  // Bind the JWT signature to the password itself, so a password change
  // revokes outstanding cookies for free.
  return new TextEncoder().encode(`zoro-session::${pw}`);
}

/** Timing-safe-ish constant-time string comparison. */
export function safeEqual(a: string, b: string): boolean {
  const enc = new TextEncoder();
  const ba = enc.encode(a);
  const bb = enc.encode(b);
  // Compare against max length so length itself doesn't leak via timing.
  const len = Math.max(ba.length, bb.length);
  let diff = ba.length ^ bb.length;
  for (let i = 0; i < len; i++) {
    diff |= (ba[i] ?? 0) ^ (bb[i] ?? 0);
  }
  return diff === 0;
}

/** Verify a candidate password against the configured one. */
export function checkPassword(candidate: string): boolean {
  const pw = passwordOrNull();
  if (!pw) return false; // fail closed
  return safeEqual(candidate, pw);
}

/** Mint a signed session token (call only after checkPassword passed). */
export async function mintSession(): Promise<string | null> {
  const pw = passwordOrNull();
  if (!pw) return null;
  return new SignJWT({ scope: "zoro" })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuedAt()
    .setExpirationTime(SESSION_TTL)
    .sign(signingKey(pw));
}

/** True if the incoming request carries a valid, unexpired session cookie. */
export async function isAuthed(): Promise<boolean> {
  const pw = passwordOrNull();
  if (!pw) return false; // fail closed
  const store = await cookies();
  const token = store.get(SESSION_COOKIE)?.value;
  if (!token) return false;
  try {
    await jwtVerify(token, signingKey(pw));
    return true;
  } catch {
    return false;
  }
}
