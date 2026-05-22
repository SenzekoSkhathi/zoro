import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import { SESSION_COOKIE, isAuthed } from "@/lib/auth";

export const dynamic = "force-dynamic";

// GET → { authed } so the client can decide whether to show the login gate.
export async function GET() {
  return NextResponse.json({ authed: await isAuthed() });
}

// DELETE → log out (clear the session cookie).
export async function DELETE() {
  const store = await cookies();
  store.delete(SESSION_COOKIE);
  return NextResponse.json({ ok: true });
}
