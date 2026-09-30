/**
 * Every page and API of the Lab needs a session, except the door itself.
 *
 * Fail closed. With no PRAMANA_SSO_SECRET the Lab is not paired with an
 * account system and refuses everything, the same way the terminal's /sso
 * does. A research console that can trigger pipeline runs must not be
 * reachable by default just because someone knows the port.
 */
import { NextResponse, type NextRequest } from "next/server";
import { COOKIE, verifySession } from "@/lib/session";

// Same origin now -- the gateway mounts the Lab under Pramana's port.
const PRAMANA = process.env.NEXT_PUBLIC_PRAMANA_URL ?? "";

export async function middleware(req: NextRequest) {
  // nextUrl.pathname is reported without basePath in some Next versions and
  // with it in others; normalise so the door is found either way.
  const raw = req.nextUrl.pathname;
  const pathname = raw.startsWith("/lab") ? raw.slice(4) || "/" : raw;
  if (pathname === "/sso" || pathname.startsWith("/_next/") || pathname === "/favicon.ico") {
    return NextResponse.next();
  }

  const secret = process.env.PRAMANA_SSO_SECRET;
  if (!secret) {
    return new NextResponse(
      "This lab is not paired with an account system. Set PRAMANA_SSO_SECRET " +
      "(run-lab.sh sources vriddhix/state/sso.env) and restart.",
      { status: 503 }
    );
  }

  const who = await verifySession(req.cookies.get(COOKIE)?.value, secret);
  if (who) return NextResponse.next();

  if (pathname.startsWith("/api/")) {
    return NextResponse.json({ error: "sign in through Pramana" }, { status: 401 });
  }
  return NextResponse.redirect(`${PRAMANA}/go/lab`);
}

export const config = { matcher: ["/((?!_next/static|_next/image).*)"] };
