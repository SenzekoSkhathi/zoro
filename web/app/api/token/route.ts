import { NextRequest, NextResponse } from "next/server";
import { SignJWT } from "jose";
import { isAuthed } from "@/lib/auth";

// We mint the LiveKit join token by hand with `jose` (no livekit-server-sdk
// dependency). LiveKit Cloud rejects tokens with an implausibly long TTL or
// no `iat`, so we issue a standard short-lived token: `iat`/`nbf` backdated
// a few minutes to absorb minor clock skew, `exp` a few hours out. The
// browser fetches a fresh token on every connect, so a short TTL is fine.
// The API secret stays server-side; the browser only ever sees the JWT.
export async function GET(req: NextRequest) {
  // This endpoint mints a token that can drive the laptop — it is the most
  // sensitive route in the app. Require a valid session cookie; fail closed.
  if (!(await isAuthed())) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  const apiKey = process.env.LIVEKIT_API_KEY;
  const apiSecret = process.env.LIVEKIT_API_SECRET;
  if (!apiKey || !apiSecret) {
    return NextResponse.json(
      { error: "LiveKit server credentials are not configured" },
      { status: 500 },
    );
  }

  const room = req.nextUrl.searchParams.get("room") ?? "zoro";
  const identity =
    req.nextUrl.searchParams.get("identity") ??
    `user-${Math.random().toString(36).slice(2, 10)}`;

  const secret = new TextEncoder().encode(apiSecret);
  const nowSec = Math.floor(Date.now() / 1000);
  const skew = 300; // 5 min backdate to tolerate minor clock skew
  const token = await new SignJWT({
    video: {
      room,
      roomJoin: true,
      canPublish: true,
      canSubscribe: true,
    },
  })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuer(apiKey) // LiveKit expects the API key as `iss`
    .setSubject(identity) // participant identity as `sub`
    .setIssuedAt(nowSec - skew) // LiveKit Cloud requires a sane `iat`
    .setNotBefore(nowSec - skew)
    .setExpirationTime(nowSec + 6 * 60 * 60) // 6h — refreshed each connect
    .sign(secret);

  return NextResponse.json({ token, identity, room });
}
