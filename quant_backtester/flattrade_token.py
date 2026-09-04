#!/usr/bin/env python
"""Mint a Flattrade session token.

Flattrade issues tokens through a browser redirect, so this cannot be fully
automated: it starts a small local server, opens the login page, catches the
redirect carrying the `request_code`, and exchanges it for a token.

Tokens expire daily, so this is a once-a-morning step.

    python quant_backtester/flattrade_token.py

Credentials are read from `configfile.ini` `[flattrade]`, or from the
environment (FLATTRADE_API_KEY / FLATTRADE_API_SECRET / FLATTRADE_CLIENT_ID),
which takes precedence. The resulting token is written to
`quant_backtester/state/flattrade_token.json` — a file that is git-ignored and
should stay that way.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import sys
import time
import webbrowser
from datetime import datetime
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quant_backtester.src.broker.flattrade import (  # noqa: E402
    automated_login,
    build_login_url,
    exchange_request_code,
)

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
TOKEN_PATH = ROOT / "state" / "flattrade_token.json"

SUCCESS_PAGE = b"""<!doctype html><meta charset="utf-8">
<title>Flattrade connected</title>
<body style="font-family:system-ui;background:#0a0d16;color:#e7ecf6;
             display:flex;align-items:center;justify-content:center;height:100vh">
<div style="text-align:center">
  <h1 style="color:#34d399">Connected</h1>
  <p style="color:#95a0be">Token saved. You can close this tab and return to the terminal.</p>
</div></body>"""

FAILURE_PAGE = b"""<!doctype html><meta charset="utf-8">
<title>Flattrade login failed</title>
<body style="font-family:system-ui;background:#0a0d16;color:#e7ecf6;
             display:flex;align-items:center;justify-content:center;height:100vh">
<div style="text-align:center">
  <h1 style="color:#f43f5e">No request code</h1>
  <p style="color:#95a0be">The redirect carried no <code>code</code> parameter.</p>
</div></body>"""


def load_credentials(config_path: Path) -> dict:
    """Resolve credentials through the same alias-aware resolver the runtime
    uses, so the CLI and the engine can never disagree about what is
    configured."""
    from quant_backtester.src.broker.flattrade import resolve_credentials

    creds = resolve_credentials(config_path)

    required = ("api_key", "api_secret", "client_id")
    missing = [k for k in required if not creds[k]]
    if missing:
        raise SystemExit(
            f"Missing Flattrade credentials: {', '.join(missing)}.\n"
            f"Add them to [flattrade] in {config_path} or export FLATTRADE_* variables."
        )
    return creds


class _RedirectHandler(BaseHTTPRequestHandler):
    request_code: str | None = None

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        params = parse_qs(urlparse(self.path).query)
        code = params.get("code", [None])[0]
        _RedirectHandler.request_code = code

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(SUCCESS_PAGE if code else FAILURE_PAGE)

    def log_message(self, *args) -> None:
        """Silence the default request logging — the redirect URL contains the
        request code, and it should not end up in a scrollback buffer."""


class _DualStackServer(HTTPServer):
    """Listen on IPv4 *and* IPv6.

    On macOS `localhost` frequently resolves to ::1, so a listener bound only
    to 127.0.0.1 never sees the redirect — it simply times out with no clue
    why. Binding dual-stack removes that whole class of failure.
    """

    address_family = socket.AF_INET6

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def capture_request_code(port: int, timeout: int = 900) -> str | None:
    """Serve until the redirect arrives or the deadline passes.

    The deadline is wall-clock, not an iteration count: an earlier version
    counted loop passes, so every unrelated request (a health probe, a
    favicon fetch) burned budget and the listener died early while still
    holding the socket — which looks exactly like a hang.
    """
    try:
        server: HTTPServer = _DualStackServer(("::", port), _RedirectHandler)
    except OSError:
        # Some environments refuse dual-stack; IPv4-only beats nothing.
        server = HTTPServer(("0.0.0.0", port), _RedirectHandler)

    server.timeout = 1
    deadline = time.monotonic() + timeout
    _RedirectHandler.request_code = None

    try:
        while _RedirectHandler.request_code is None and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    return _RedirectHandler.request_code


def _save_token(token: str, client_id: str, *, source: str) -> None:
    """Persist the session token at 0600 — it is a credential."""
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(
        json.dumps(
            {
                "token": token,
                "client_id": client_id,
                "issued_at": datetime.now().isoformat(),
                "source": source,
            },
            indent=2,
        )
    )
    TOKEN_PATH.chmod(0o600)


def _port_from_redirect(redirect_uri: str, fallback: int = 8765) -> int:
    """Derive the listener port from the app's configured redirect URI.

    Flattrade redirects to whatever the app registration says; binding a
    different port means the browser lands nowhere and the request code is
    lost with no error to explain it.
    """
    if not redirect_uri:
        return fallback
    parsed = urlparse(redirect_uri.rstrip("?"))
    if parsed.port:
        return parsed.port
    if parsed.scheme == "https":
        return 443
    return 80 if parsed.scheme == "http" else fallback


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(REPO / "configfile.ini"))
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="local redirect listener port (default: taken from redirect_uri)",
    )
    parser.add_argument(
        "--code",
        help="paste a request code directly, skipping the browser step",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="force the interactive browser flow instead of automated login",
    )
    args = parser.parse_args()

    creds = load_credentials(Path(args.config))
    login_url = build_login_url(creds["api_key"])

    # Automated login is the default: a system that needs a human in a browser
    # every morning is not automated. The browser flow remains available for
    # first-time setup or when a TOTP seed is unavailable.
    if not args.code and not args.browser:
        password = creds.get("password") or os.environ.get("FLATTRADE_PASSWORD", "")
        totp = creds.get("totp_secret") or os.environ.get("FLATTRADE_TOTP_SECRET", "")
        factor = creds.get("second_factor") or os.environ.get("FLATTRADE_SECOND_FACTOR", "")
        if password and (totp or factor):
            print("Logging in automatically (no browser needed)…")
            try:
                token = automated_login(
                    api_key=creds["api_key"],
                    api_secret=creds["api_secret"],
                    client_id=creds["client_id"],
                    password=password,
                    totp_secret=totp,
                    second_factor=factor,
                )
                _save_token(token, creds["client_id"], source="automated")
                print(f"\nToken saved to {TOKEN_PATH}")
                print("Automated login is configured — this will refresh itself.\n")
                return 0
            except Exception as exc:  # noqa: BLE001
                print(f"\nAutomated login failed: {exc}")
                print("Falling back to the browser flow…\n")
        else:
            # Name precisely what is absent: "not configured" when the seed is
            # already present sends the operator hunting for the wrong thing.
            lacking = []
            if not password:
                lacking.append("FLATTRADE_PASSWORD")
            if not (totp or factor):
                lacking.append("FLATTRADE_TOTP_SECRET (or FLATTRADE_TOTP)")
            print(
                "Automated login needs: " + ", ".join(lacking) + ".\n"
                + ("TOTP seed found. " if totp else "")
                + "Falling back to the browser flow.\n"
            )

    # The listener must bind the SAME port the Flattrade app redirects to, or
    # the browser lands on a dead address and the code is never captured.
    port = args.port or _port_from_redirect(creds["redirect_uri"])

    code = args.code
    if not code:
        print(f"\nRedirect listener: http://127.0.0.1:{port}/")
        print(
            "Your Flattrade app's redirect URI must point here. If it points\n"
            "somewhere else, copy the `code` from the redirected URL and re-run\n"
            "with --code <value>.\n"
        )
        print(f"Opening: {login_url}\n")
        try:
            webbrowser.open(login_url)
        except Exception:  # noqa: BLE001 - headless environments have no browser
            print("(could not open a browser automatically — open the link above)")
        print("Waiting for the redirect…")
        code = capture_request_code(port)

    if not code:
        print("\nNo request code received. Re-run with --code <value> if you have one.")
        return 1

    print("Exchanging request code for a session token…")
    token = exchange_request_code(creds["api_key"], creds["api_secret"], code)
    _save_token(token, creds["client_id"], source="browser")

    print(f"\nToken saved to {TOKEN_PATH}")
    print("It expires at end of day — re-run this tomorrow.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
