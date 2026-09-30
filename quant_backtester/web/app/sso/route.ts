/**
 * Admit a reader who signed in on Pramana.
 *
 * A ticket is good once. Spent ids are kept in memory: without that, anyone
 * who read the ticket out of browser history or a proxy log could replay it
 * for the rest of its 45-second life.
 */
import { NextResponse } from "next/server";
import { COOKIE, SESSION_SECONDS, signSession, verifyTicket } from "@/lib/session";

export const dynamic = "force-dynamic";

const spent = new Map<string, number>();
const SPENT_MAX = 512;

export async function GET(request: Request) {
  const secret = process.env.PRAMANA_SSO_SECRET;
  if (!secret) {
    return new NextResponse("This lab is not paired with an account system.", { status: 404 });
  }

  const url = new URL(request.url);
  const ticket = url.searchParams.get("ticket") ?? "";
  const claims = await verifyTicket(ticket, secret, "lab");
  if (!claims || spent.has(claims.jti)) {
    return new NextResponse("That sign-in link is not valid any more. Sign in again.", { status: 403 });
  }

  if (spent.size >= SPENT_MAX) {
    const oldest = [...spent.entries()].sort((a, b) => a[1] - b[1]).slice(0, SPENT_MAX / 4);
    oldest.forEach(([k]) => spent.delete(k));
  }
  spent.set(claims.jti, Date.now());

  // Redirect to the host the reader actually used. request.url reports
  // "localhost" even when the browser came in on 127.0.0.1, and the cookie
  // is scoped to the host it was set on -- so a redirect to the other name
  // would land the reader on the Lab without the session it just received.
  const host = request.headers.get("host") ?? url.host;
  const res = NextResponse.redirect(new URL("/lab/", `${url.protocol}//${host}`));
  res.cookies.set(COOKIE, await signSession(secret, claims.sub), {
    httpOnly: true, sameSite: "lax", path: "/lab", maxAge: SESSION_SECONDS,
  });
  return res;
}
