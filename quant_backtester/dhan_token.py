#!/usr/bin/env python3
"""Dhan access-token management.

    python quant_backtester/dhan_token.py --check          # status + validity
    python quant_backtester/dhan_token.py --renew          # extend an ACTIVE token
    python quant_backtester/dhan_token.py --refresh        # full consent flow
    python quant_backtester/dhan_token.py --totp           # headless PIN + TOTP
    python quant_backtester/dhan_token.py --set <token>    # store one you already have

TWO WAYS TO GET A TOKEN, and the difference decides which you can use.

`--totp` is the one to automate. It calls `POST /app/generateAccessToken`
with the client id, PIN and a code generated from the TOTP seed, and works
from any state — expired, missing, or active. One request, no browser. This
is the cron entry.

`--renew` calls `GET /v2/RenewToken` to extend an *active* token by another
24 hours. Note it does NOT accept tokens minted by `--totp` (Dhan answers
"Renewal of token not allowed for this token type"), and it cannot revive an
expired one. It is only useful for a portal-generated token you want to keep
alive; otherwise prefer `--totp`.

`--refresh` runs the app-consent flow, which works from any state:

    1. POST auth.dhan.co/app/generate-consent   (app_id + app_secret) -> consentAppId
    2. browser login at auth.dhan.co/login/consentApp-login           -> tokenId
    3. POST auth.dhan.co/app/consumeApp-consent (app_id + app_secret) -> accessToken

Steps 1 and 3 are automated here. Step 2 is a real login with 2FA and must be
done by a human — this script opens the page and catches the redirect, but it
will never type your credentials.

Tokens are written to state/dhan_token.json (git-ignored, mode 0600) and are
never printed to the terminal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from quant_backtester.src.broker.dhan import (  # noqa: E402
    DhanClient,
    _clean,
    decode_token_claims,
    token_expiry,
)

TOKEN_FILE = ROOT / "state" / "dhan_token.json"
CONFIG_FILE = ROOT.parent / "configfile.ini"
AUTH_BASE = "https://auth.dhan.co"
API_BASE = "https://api.dhan.co/v2"
PORTAL_URL = "https://web.dhan.co/"
PORTAL_PATH = "Profile -> DhanHQ Trading APIs"

#: Dhan tokens last ~24h, so "renew soon" is measured in hours, not days.
WARN_HOURS = 6

_LANDING = b"""<!doctype html><meta charset="utf-8">
<body style="background:#05080f;color:#e6edf5;font-family:system-ui;padding:3rem">
<h2 style="color:#00ff9c">Dhan authorisation received</h2>
<p>You can close this tab and return to the terminal.</p></body>"""


# ==========================================================================
# CREDENTIALS
# ==========================================================================


def app_credentials() -> tuple[str, str, str]:
    """The API key/secret pair and client id, from the [dhan] section."""
    import configparser

    parser = configparser.ConfigParser()
    if CONFIG_FILE.exists():
        parser.read(CONFIG_FILE)

    def get(*names: str) -> str:
        for section in ("dhan", *parser.sections()):
            if not parser.has_section(section):
                continue
            items = {k.lower(): v for k, v in parser.items(section)}
            for name in names:
                value = _clean(items.get(name))
                if value:
                    return value
        return ""

    return (
        os.environ.get("DHAN_API_KEY") or get("api_key", "dhan_api_key"),
        os.environ.get("DHAN_API_SECRET") or get("api_secret", "dhan_api_secret"),
        os.environ.get("DHAN_CLIENT_ID") or get("client_id", "dhan_client_id"),
    )


def totp_credentials() -> tuple[str, str, str]:
    """Client id, PIN and TOTP seed for the headless login."""
    import configparser

    parser = configparser.ConfigParser()
    if CONFIG_FILE.exists():
        parser.read(CONFIG_FILE)

    def get(*names: str) -> str:
        for section in ("dhan", *parser.sections()):
            if not parser.has_section(section):
                continue
            items = {k.lower(): v for k, v in parser.items(section)}
            for name in names:
                value = _clean(items.get(name))
                if value:
                    return value
        return ""

    return (
        os.environ.get("DHAN_CLIENT_ID") or get("client_id", "dhan_client_id"),
        os.environ.get("DHAN_PIN") or get("pin", "dhan_pin"),
        os.environ.get("DHAN_TOTP_SECRET") or get("totp", "dhan_totp"),
    )


def _mask(token: str) -> str:
    """Tokens are credentials. Show only enough to tell two apart."""
    return f"{token[:6]}…{token[-4:]} ({len(token)} chars)" if len(token) > 20 else "…"


def load_stored() -> dict:
    if TOKEN_FILE.exists():
        try:
            return json.loads(TOKEN_FILE.read_text())
        except Exception:  # noqa: BLE001
            return {}
    return {}


def store(token: str, client_id: str, source: str = "consent") -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    expires = token_expiry(token)
    TOKEN_FILE.write_text(
        json.dumps(
            {
                "token": token,
                "client_id": client_id,
                "issued_at": datetime.now().isoformat(timespec="seconds"),
                "expires_at": expires.isoformat(timespec="seconds") if expires else None,
                "source": source,
            },
            indent=2,
        )
    )
    TOKEN_FILE.chmod(0o600)  # owner-only: this is a credential


def resolve_token() -> tuple[str, str, str]:
    """The token the adapters would use, and where it came from."""
    env = (os.environ.get("DHAN_ACCESS_TOKEN") or "").strip()
    if env:
        return env, (os.environ.get("DHAN_CLIENT_ID") or "").strip(), "environment"

    stored = load_stored()
    if stored.get("token"):
        return stored["token"], stored.get("client_id", ""), "state/dhan_token.json"

    from quant_backtester.src.broker.dhan import _from_credentials_file

    token, client = _from_credentials_file()
    return token, client, "configfile.ini"


# ==========================================================================
# STATUS
# ==========================================================================


def check() -> int:
    token, client_id, source = resolve_token()
    if not token:
        print("✗ No Dhan access token found.")
        print("  Run: python quant_backtester/dhan_token.py --totp")
        return 2

    claims = decode_token_claims(token)
    expires = token_expiry(token)
    client_id = client_id or str(claims.get("dhanClientId", ""))

    print(f"source      : {source}")
    print(f"token       : {_mask(token)}")
    print(f"client id   : {client_id or 'unknown'}")
    print(f"consumer    : {claims.get('tokenConsumerType', 'unknown')}")

    if expires:
        remaining = expires - datetime.now()
        hours = remaining.total_seconds() / 3600
        print(f"expires     : {expires:%Y-%m-%d %H:%M}  ({hours:.1f} hours)")
        if remaining.total_seconds() <= 0:
            print("\n✗ EXPIRED — and an expired token cannot be renewed.")
            print("  Run: python quant_backtester/dhan_token.py --totp")
            return 1
        if hours <= WARN_HOURS:
            print(f"\n⚠ Under {WARN_HOURS}h left. Run `--totp` for a fresh 24 hours.")
    else:
        print("expires     : unknown (no exp claim)")

    print("\nverifying against the live API…")
    try:
        profile = DhanClient(token, client_id).profile()
        print("✓ Dhan accepted the token.")
        for key in ("dhanClientId", "tokenValidity", "activeSegment", "dataPlan"):
            if key in profile:
                print(f"   {key:16} {profile[key]}")
        return 0
    except Exception as exc:  # noqa: BLE001 - a status check must never crash
        print(f"✗ Rejected: {exc}")
        return 1


def set_token(token: str, client_id: str = "") -> int:
    token = _clean(token)
    if token.count(".") != 2:
        print("✗ That is not a Dhan JWT (expected three dot-separated parts).")
        return 2

    claims = decode_token_claims(token)
    client_id = client_id or str(claims.get("dhanClientId", "")) or resolve_token()[1]
    expires = token_expiry(token)
    if expires and expires <= datetime.now():
        print(f"✗ Refusing to store a token that expired at {expires:%Y-%m-%d %H:%M}.")
        return 2

    if not _verify_and_store(token, client_id, "manual"):
        return 1
    return 0


def _verify_and_store(token: str, client_id: str, source: str) -> bool:
    """Never store a token that does not work — catch it here, not at the
    first order."""
    print("verifying…")
    try:
        DhanClient(token, client_id).profile()
    except Exception as exc:  # noqa: BLE001
        print(f"✗ Dhan rejected it: {exc}")
        print("  Nothing was written.")
        return False

    store(token, client_id, source)
    expires = token_expiry(token)
    print(f"✓ Stored in {TOKEN_FILE.relative_to(ROOT.parent)} (mode 0600)")
    if expires:
        hours = (expires - datetime.now()).total_seconds() / 3600
        print(f"  Valid until {expires:%Y-%m-%d %H:%M}  ({hours:.1f} hours)")
    return True


# ==========================================================================
# RENEW — one request, active tokens only
# ==========================================================================


def renew() -> int:
    import requests

    token, client_id, source = resolve_token()
    if not token:
        print("✗ No token to renew. Run --refresh.")
        return 2

    expires = token_expiry(token)
    if expires and expires <= datetime.now():
        print(f"✗ The current token expired at {expires:%Y-%m-%d %H:%M}.")
        print("  Expired tokens cannot be renewed — run --refresh instead.")
        return 1

    client_id = client_id or str(decode_token_claims(token).get("dhanClientId", ""))
    print(f"renewing the token from {source}…")
    try:
        response = requests.get(
            f"{API_BASE}/RenewToken",
            headers={"access-token": token, "dhanClientId": client_id, "Accept": "application/json"},
            timeout=25,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"✗ Request failed: {exc}")
        return 1

    if response.status_code >= 400:
        body = response.text[:250]
        print(f"✗ HTTP {response.status_code}: {body}")
        if "not allowed for this token type" in body:
            # Measured: RenewToken rejects tokens minted by /generateAccessToken.
            print("\n  Renewal does not apply to TOTP-generated tokens.")
            print("  Just mint a fresh one instead — it is a single request:")
            print("      python quant_backtester/dhan_token.py --totp")
        else:
            print("  If the token has lapsed, use --totp or --refresh.")
        return 1

    try:
        data = response.json()
    except ValueError:
        print(f"✗ Non-JSON response: {response.text[:200]}")
        return 1

    fresh = _clean(data.get("accessToken") or data.get("access_token") or "")
    if not fresh:
        print(f"✗ No accessToken in the response: {json.dumps(data)[:250]}")
        return 1

    # Renewal invalidates the old token immediately, so a failure to store
    # here would leave nothing usable. Store first, then report.
    return 0 if _verify_and_store(fresh, client_id, "renew") else 1


# ==========================================================================
# TOTP — headless, no browser
# ==========================================================================


def _tradehull_token(client_id: str, pin: str, seed: str) -> str | None:
    """Mint a token through Dhan-Tradehull, if it is installed.

    Returns None when unavailable so the caller falls through to the direct
    API call. Tradehull caches a token per day under ./Dependencies; that
    cache is read too, which also respects Dhan's "once every 2 minutes"
    limit on token generation.
    """
    try:
        from Dhan_Tradehull import Tradehull
    except ImportError:
        return None

    try:
        Tradehull(ClientCode=client_id, mode="pin_totp", pin=pin, totp_secret=seed)
    except Exception as exc:  # noqa: BLE001 - fall through to the direct call
        logger_msg = str(exc)[:120]
        print(f"Tradehull login did not complete ({logger_msg}); trying the direct API")
        return None

    # Tradehull writes `YYYY-MM-DD|token|expiry`.
    import glob

    files = sorted(glob.glob("Dependencies/token_*.txt"))
    for path in reversed(files):
        parts = Path(path).read_text().strip().split("|")
        if len(parts) >= 2 and parts[1].strip():
            print("Token obtained via Dhan-Tradehull.")
            return parts[1].strip()
    return None


def totp_login() -> int:
    """Mint a token from client id + PIN + a generated TOTP code.

    The seed never leaves this process and neither the PIN nor the generated
    code is ever printed — a leaked TOTP seed is a permanent second factor
    compromise, not a one-off.
    """
    import pyotp
    import requests

    client_id, pin, seed = totp_credentials()
    missing = [n for n, v in (("client_id", client_id), ("pin", pin), ("totp", seed)) if not v]
    if missing:
        print(f"✗ Missing from the [dhan] section of configfile.ini: {', '.join(missing)}")
        return 2

    try:
        code = pyotp.TOTP(seed).now()
    except Exception as exc:  # noqa: BLE001
        print(f"✗ Could not generate a TOTP code from the configured seed: {exc}")
        print("  The seed should be the base32 string shown when TOTP was set up.")
        return 2

    # Dhan-Tradehull performs the same PIN+TOTP exchange but is the
    # reference implementation the user runs, and it succeeds where a bare
    # call to /app/generateAccessToken has been rejected. Prefer it when
    # installed, and fall back to the direct call so this module keeps
    # working without the extra dependency.
    via_tradehull = _tradehull_token(client_id, pin, seed)
    if via_tradehull:
        return 0 if _verify_and_store(via_tradehull, client_id, "totp") else 1

    print(f"requesting an access token for {client_id} (PIN + TOTP)…")
    try:
        response = requests.post(
            f"{AUTH_BASE}/app/generateAccessToken",
            params={"dhanClientId": client_id, "pin": pin, "totp": code},
            headers={"Accept": "application/json"},
            timeout=25,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"✗ Request failed: {exc}")
        return 1

    if response.status_code >= 400:
        # Never echo the request: the URL carries the PIN and the TOTP code.
        print(f"✗ HTTP {response.status_code}: {response.text[:250]}")
        print("  Common causes: TOTP not enabled for API use in the Dhan profile,")
        print("  a wrong PIN, or clock drift between this machine and Dhan.")
        return 1

    try:
        data = response.json() or {}
    except ValueError:
        print(f"✗ Non-JSON response: {response.text[:200]}")
        return 1

    token = _clean(data.get("accessToken") or data.get("access_token") or "")
    if not token:
        print(f"✗ No accessToken in the response: {json.dumps(data)[:250]}")
        return 1

    resolved = str(data.get("dhanClientId") or client_id)
    return 0 if _verify_and_store(token, resolved, "totp") else 1


# ==========================================================================
# REFRESH — the consent flow
# ==========================================================================


class _RedirectHandler(BaseHTTPRequestHandler):
    token_id: str | None = None

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        found = params.get("tokenId") or params.get("token_id")
        if found:
            _RedirectHandler.token_id = found[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_LANDING)

    def log_message(self, *args) -> None:
        """Silenced: the redirect URL is a credential in transit."""


class _DualStackServer(HTTPServer):
    """Bind both stacks. On macOS `localhost` often resolves to ::1, and a
    v4-only listener simply never sees the redirect."""

    address_family = __import__("socket").AF_INET6

    def server_bind(self) -> None:
        import socket

        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def _capture_token_id(port: int, timeout: int = 600) -> str | None:
    import time

    _RedirectHandler.token_id = None
    try:
        server: HTTPServer = _DualStackServer(("::", port), _RedirectHandler)
    except OSError:
        server = HTTPServer(("0.0.0.0", port), _RedirectHandler)

    server.timeout = 1
    deadline = time.monotonic() + timeout
    # Deadline-based, not iteration-based: stray favicon requests must not
    # burn the budget and leave the listener dead while the user is still
    # logging in.
    with server:
        while time.monotonic() < deadline:
            server.handle_request()
            if _RedirectHandler.token_id:
                return _RedirectHandler.token_id
    return None


def refresh(port: int, timeout: int, manual: bool) -> int:
    import requests
    import webbrowser

    api_key, api_secret, client_id = app_credentials()
    missing = [
        n for n, v in (("api_key", api_key), ("api_secret", api_secret), ("client_id", client_id))
        if not v
    ]
    if missing:
        print(f"✗ Missing from the [dhan] section of configfile.ini: {', '.join(missing)}")
        return 2

    headers = {"app_id": api_key, "app_secret": api_secret, "Accept": "application/json"}

    # -- step 1: consent -------------------------------------------------
    print("1/3  requesting consent…")
    try:
        response = requests.post(
            f"{AUTH_BASE}/app/generate-consent?client_id={client_id}",
            headers=headers, timeout=25,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"✗ Request failed: {exc}")
        return 1
    if response.status_code >= 400:
        print(f"✗ HTTP {response.status_code}: {response.text[:250]}")
        print("  Check api_key/api_secret in the [dhan] section.")
        return 1

    consent_id = (response.json() or {}).get("consentAppId")
    if not consent_id:
        print(f"✗ No consentAppId returned: {response.text[:250]}")
        return 1
    print(f"     consent {consent_id}")

    # -- step 2: the human bit -------------------------------------------
    login_url = f"{AUTH_BASE}/login/consentApp-login?consentAppId={consent_id}"
    print("\n2/3  log in to Dhan in the browser (this script never sees your")
    print("     credentials — it only waits for the redirect).\n")
    print(f"     {login_url}\n")

    token_id = None
    if manual:
        print("     Paste the full redirect URL (or just the tokenId) here.")
        raw = input("     > ").strip()
        if "tokenId=" in raw:
            token_id = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query).get("tokenId", [None])[0]
        else:
            token_id = raw or None
    else:
        try:
            webbrowser.open(login_url)
        except Exception:  # noqa: BLE001
            pass
        print(f"     listening on port {port} for up to {timeout // 60} minutes…")
        print("     (if your app redirects elsewhere, re-run with --manual)")
        token_id = _capture_token_id(port, timeout)

    if not token_id:
        print("\n✗ No tokenId received.")
        print("  Re-run with --manual and paste the redirect URL yourself.")
        return 1
    print(f"     tokenId {token_id[:8]}…")

    # -- step 3: exchange -------------------------------------------------
    print("\n3/3  exchanging for an access token…")
    try:
        response = requests.post(
            f"{AUTH_BASE}/app/consumeApp-consent?tokenId={token_id}",
            headers=headers, timeout=25,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"✗ Request failed: {exc}")
        return 1
    if response.status_code >= 400:
        print(f"✗ HTTP {response.status_code}: {response.text[:250]}")
        return 1

    data = response.json() or {}
    token = _clean(data.get("accessToken", ""))
    if not token:
        print(f"✗ No accessToken in the response: {json.dumps(data)[:250]}")
        return 1

    resolved_id = str(data.get("dhanClientId") or client_id)
    name = data.get("dhanClientName")
    if name:
        print(f"     authorised as {name} ({resolved_id})")
    return 0 if _verify_and_store(token, resolved_id, "consent") else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect, renew, refresh and store the Dhan access token.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="report status and verify (default)")
    group.add_argument("--renew", action="store_true", help="extend an ACTIVE token by 24h")
    group.add_argument("--totp", action="store_true", help="headless login with PIN + TOTP")
    group.add_argument("--refresh", action="store_true", help="run the full consent flow")
    group.add_argument("--set", metavar="TOKEN", help="store a token you already have")
    parser.add_argument("--client-id", default="", help="client id, if not in the token")
    parser.add_argument("--port", type=int, default=8080, help="redirect listener port")
    parser.add_argument("--timeout", type=int, default=600, help="seconds to wait for the redirect")
    parser.add_argument(
        "--manual", action="store_true",
        help="paste the redirect URL instead of listening for it",
    )
    args = parser.parse_args()

    if args.set:
        return set_token(args.set, args.client_id)
    if args.totp:
        return totp_login()
    if args.renew:
        return renew()
    if args.refresh:
        return refresh(args.port, args.timeout, args.manual)
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
