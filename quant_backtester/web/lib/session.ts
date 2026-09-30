/**
 * The Lab's session, and the ticket that opens it.
 *
 * The Lab had no authentication at all. It reads results off disk and can
 * trigger pipeline runs, and it was reachable by anyone who could reach the
 * port. That cannot ship. It now admits only a ticket minted by Pramana for
 * an operator account, with audience "lab", and keeps a signed cookie after.
 *
 * Two runtimes: the /sso route handler runs on Node and uses node:crypto;
 * the middleware runs on the edge runtime and only has Web Crypto. Both
 * derive the same HMAC-SHA256, so a cookie signed by one verifies in the
 * other. Nothing here is a JWT library -- there is not one installed, and
 * HS256 is thirty lines.
 */

export const COOKIE = "lab_session";
export const SESSION_SECONDS = 12 * 60 * 60;

function b64urlToBytes(s: string): Uint8Array {
  const pad = "=".repeat((4 - (s.length % 4)) % 4);
  const bin = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}
function bytesToB64url(b: Uint8Array): string {
  let bin = "";
  b.forEach((x) => (bin += String.fromCharCode(x)));
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function hmac(secret: string, data: string): Promise<Uint8Array> {
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
  );
  return new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(data)));
}
function equal(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

/** Verify a Pramana ticket. Returns the subject, or null for any failure --
 *  one answer for every failure, so a caller cannot learn about the secret. */
export async function verifyTicket(
  ticket: string, secret: string, audience: "lab"
): Promise<{ sub: string; jti: string } | null> {
  const parts = ticket.split(".");
  if (parts.length !== 3) return null;
  const [h, p, sig] = parts;
  const expected = await hmac(secret, `${h}.${p}`);
  if (!equal(expected, b64urlToBytes(sig))) return null;
  let claims: { sub?: string; aud?: string; exp?: number; jti?: string };
  try {
    claims = JSON.parse(new TextDecoder().decode(b64urlToBytes(p)));
  } catch { return null; }
  if (claims.aud !== audience) return null;
  if (!claims.exp || claims.exp < Date.now() / 1000) return null;
  if (!claims.sub || !claims.jti) return null;
  return { sub: claims.sub, jti: claims.jti };
}

/** Mint the session cookie value: subject.expiry.signature */
export async function signSession(secret: string, sub: string): Promise<string> {
  const exp = Math.floor(Date.now() / 1000) + SESSION_SECONDS;
  const body = `${bytesToB64url(new TextEncoder().encode(sub))}.${exp}`;
  const sig = bytesToB64url(await hmac(secret, body));
  return `${body}.${sig}`;
}

/** Verify a session cookie. Same shape of answer as verifyTicket. */
export async function verifySession(value: string | undefined, secret: string): Promise<string | null> {
  if (!value) return null;
  const parts = value.split(".");
  if (parts.length !== 3) return null;
  const [sub64, exp, sig] = parts;
  const expected = await hmac(secret, `${sub64}.${exp}`);
  if (!equal(expected, b64urlToBytes(sig))) return null;
  if (Number(exp) < Date.now() / 1000) return null;
  try { return new TextDecoder().decode(b64urlToBytes(sub64)); } catch { return null; }
}
