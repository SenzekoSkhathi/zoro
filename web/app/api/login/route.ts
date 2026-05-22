import { NextRequest, NextResponse } from "next/server";
import { cookies } from "next/headers";
import { SESSION_COOKIE, checkPassword, mintSession } from "@/lib/auth";

export const dynamic = "force-dynamic";

// POST { password } — verify against ZORO_ACCESS_PASSWORD and, on success,
// set a signed httpOnly session cookie. Wrong/empty password 401s after a
// small fixed delay to take the edge off trivial brute-forcing.
export async function POST(req: NextRequest) {
  let password = "";
  try {
    const body = (await req.json()) as { password?: string };
    password = body?.password ?? "";
  } catch {
    /* malformed body → treated as wrong password */
  }

  if (!checkPassword(password)) {
    await new Promise((r) => setTimeout(r, 400));
    return NextResponse.json({ ok: false }, { status: 401 });
  }

  const token = await mintSession();
  if (!token) {
    // Password matched but no token minted → ZORO_ACCESS_PASSWORD unset
    // (can't happen if checkPassword passed, but stay defensive).
    return NextResponse.json({ ok: false }, { status: 503 });
  }

  const store = await cookies();
  store.set(SESSION_COOKIE, token, {
    httpOnly: true,
    secure: true,
    sameSite: "lax",
    path: "/",
    maxAge: 30 * 24 * 60 * 60,
  });
  return NextResponse.json({ ok: true });
}
