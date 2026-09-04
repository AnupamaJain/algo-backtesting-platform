"""Flattrade integration (NSE / BSE / NFO / MCX).

Flattrade runs the NorenApi platform. Two distinct pieces are needed:

  1. **Token exchange** — an OAuth-style redirect. The operator opens
     `https://auth.flattrade.in/?app_key=<api_key>`, logs in, and is redirected
     back with a `request_code`. That code plus a SHA256 digest is exchanged
     for a session token at `https://authapi.flattrade.in/trade/apitoken`.

  2. **The trading API** — a Noren REST service. Every call is a form POST of
     `jData=<json>&jKey=<token>` to `https://piconnect.flattrade.in/PiConnectAPI`.

Scope note: Flattrade covers **Indian markets only**. It cannot price SPY,
QQQ or BTC-USD; symbols must be NSE/BSE/NFO instruments such as RELIANCE-EQ or
NIFTY. `config/universe_india.yaml` holds a matching universe.

Instrument identity: Noren addresses instruments by a numeric `token` scoped to
an exchange, not by ticker. `SearchScrip` resolves a symbol to that token and
the result is cached, because every quote and history call needs it and the
mapping does not change intraday.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from .adapter import BrokerAdapter, BrokerCapabilities
from .auth import AuthStrategy, Session
from .exceptions import (
    AuthError,
    BrokerUnavailable,
    InstrumentNotFound,
    OrderRejected,
    RateLimited,
)
from .models import (
    AccountSnapshot,
    InstrumentType,
    OrderStatus,
    OrderType,
    ProductType,
    Side,
    UnifiedInstrument,
    UnifiedOrder,
    UnifiedPosition,
    UnifiedQuote,
)
from .quotes import QuoteProvider

logger = logging.getLogger(__name__)

AUTH_URL = "https://auth.flattrade.in/"
SESSION_URL = "https://authapi.flattrade.in/auth/session"
FTAUTH_URL = "https://authapi.flattrade.in/ftauth"
TOKEN_URL = "https://authapi.flattrade.in/trade/apitoken"
API_HOST = "https://piconnect.flattrade.in/PiConnectAPI"

ROUTES = {
    "searchscrip": "/SearchScrip",
    "getquotes": "/GetQuotes",
    "tpseries": "/TPSeries",
    "eod": "/EODChartData",
    "positions": "/PositionBook",
    "orderbook": "/OrderBook",
    "placeorder": "/PlaceOrder",
    "cancelorder": "/CancelOrder",
    "modifyorder": "/ModifyOrder",
    "limits": "/Limits",
    "singleorderhistory": "/SingleOrdHist",
}

# Platform vocabulary <-> Noren's single-letter codes.
PRODUCT_TO_NOREN = {
    ProductType.INTRADAY: "I",
    ProductType.CARRYFORWARD: "M",
    ProductType.DELIVERY: "C",
    ProductType.COVER: "H",
}
NOREN_TO_PRODUCT = {v: k for k, v in PRODUCT_TO_NOREN.items()}

PRICE_TYPE_TO_NOREN = {
    OrderType.MARKET: "MKT",
    OrderType.LIMIT: "LMT",
    OrderType.STOP: "SL-MKT",
    OrderType.STOP_LIMIT: "SL-LMT",
}

# Noren status strings are inconsistent across brokers on this platform, so
# unknown values fall back to PENDING rather than being guessed at.
NOREN_TO_STATUS = {
    "COMPLETE": OrderStatus.COMPLETE,
    "OPEN": OrderStatus.OPEN,
    "PENDING": OrderStatus.PENDING,
    "TRIGGER_PENDING": OrderStatus.OPEN,
    "CANCELED": OrderStatus.CANCELLED,
    "CANCELLED": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
    "PARTIALLY FILLED": OrderStatus.PARTIAL,
}


def build_login_url(api_key: str) -> str:
    """The URL the operator must open to authorise this application."""
    return f"{AUTH_URL}?app_key={api_key}"


def exchange_request_code(api_key: str, api_secret: str, request_code: str) -> str:
    """Swap a redirect `request_code` for a session token.

    The digest is SHA256 over the concatenation `api_key + request_code +
    api_secret` — order matters, and getting it wrong returns a generic
    failure rather than a useful message, so it is isolated here.
    """
    digest = hashlib.sha256(f"{api_key}{request_code}{api_secret}".encode()).hexdigest()
    try:
        response = requests.post(
            TOKEN_URL,
            json={"api_key": api_key, "request_code": request_code, "api_secret": digest},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise BrokerUnavailable(f"token exchange failed: {exc}", broker="flattrade") from exc

    payload = response.json() if response.content else {}
    token = payload.get("token")
    if not token:
        raise AuthError(
            f"token exchange rejected: {payload.get('emsg') or payload}", broker="flattrade"
        )
    return token


def automated_login(
    *,
    api_key: str,
    api_secret: str,
    client_id: str,
    password: str,
    totp_secret: str = "",
    second_factor: str = "",
) -> str:
    """Log in and return a session token, with no browser step.

    The browser flow and this one hit the same endpoint: Flattrade's login page
    POSTs to `/ftauth` with a SHA256 password and a second factor, and receives
    a `RedirectURL` carrying the request code. Doing it directly removes the
    daily manual step, which is the difference between a scheduled system and
    one that needs a human at 9am.

    `totp_secret` is the authenticator seed (preferred — it generates a fresh
    code each run). `second_factor` is an escape hatch for accounts still on
    PAN/DOB, or for pasting a one-off OTP.

    Credentials are passed in, never read from a tracked file: callers resolve
    them from the environment or a 0600 local file.
    """
    if totp_secret:
        try:
            import pyotp
        except ImportError as exc:  # pragma: no cover
            raise AuthError(
                "pyotp is required for TOTP login: pip install pyotp", broker="flattrade"
            ) from exc
        factor = pyotp.TOTP(totp_secret.replace(" ", "")).now()
    elif second_factor:
        factor = second_factor.upper()
    else:
        raise AuthError(
            "no second factor: set FLATTRADE_TOTP_SECRET (preferred) or "
            "FLATTRADE_SECOND_FACTOR",
            broker="flattrade",
        )

    # STEP 1: establish a session and obtain `sid`.
    #
    # This is the step whose absence produced "Session invalid" on every
    # attempt. The login page opens a session before it authenticates, and
    # /ftauth validates the `Sid` it is given — sending an empty one is
    # rejected with a message that reads like an EXPIRED session rather than
    # a missing one, which is why it was mistaken for a credential problem.
    #
    # The cookies from this call matter too, so everything below shares one
    # requests.Session rather than issuing independent posts.
    browser_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/99.0.4844.74 Safari/537.36"
        ),
        "Referer": "https://auth.flattrade.in/",
    }
    session = requests.Session()
    try:
        sid_response = session.post(SESSION_URL, headers=browser_headers, timeout=30)
    except requests.RequestException as exc:
        raise BrokerUnavailable(
            f"could not open a login session: {exc}", broker="flattrade"
        ) from exc

    sid = (sid_response.text or "").strip().strip('"')
    if not sid:
        raise AuthError(
            f"login session endpoint returned no sid (HTTP {sid_response.status_code})",
            broker="flattrade",
        )

    # STEP 2: authenticate against that session.
    payload = {
        "UserName": client_id,
        "Rd": "",
        # The page hashes the password client-side; the plaintext never leaves
        # this process.
        "Password": hashlib.sha256(password.encode()).hexdigest(),
        "PAN_DOB": factor,
        "App": "",
        "ClientID": "",
        "Key": "",
        "APIKey": api_key,
        "Sid": sid,
        # "Y" replaces any existing session for this account. Without it a
        # second login the same day is refused because the first still holds.
        "Override": "Y",
        "Source": "AUTHPAGE",
    }

    try:
        response = session.post(
            FTAUTH_URL,
            json=payload,
            timeout=30,
            headers={
                **browser_headers,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://auth.flattrade.in",
            },
        )
    except requests.RequestException as exc:
        raise BrokerUnavailable(f"login request failed: {exc}", broker="flattrade") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise AuthError(
            f"login returned a non-JSON response (HTTP {response.status_code})",
            broker="flattrade",
        ) from exc

    if data.get("emsg"):
        raise AuthError(f"login rejected: {data['emsg']}", broker="flattrade")

    redirect = data.get("RedirectURL", "")
    if not redirect:
        raise AuthError(f"login returned no RedirectURL: {data}", broker="flattrade")

    from urllib.parse import parse_qs, urlparse

    code = parse_qs(urlparse(redirect).query).get("code", [""])[0]
    if not code:
        raise AuthError(
            f"RedirectURL carried no request code: {redirect}", broker="flattrade"
        )

    logger.info("Flattrade automated login succeeded; exchanging request code")
    return exchange_request_code(api_key, api_secret, code)


#: Accepted spellings for each credential, per source.
#:
#: Operators reasonably write these as environment-style names
#: (FLATTRADE_TOTP) or plain section keys (totp_secret). Silently ignoring a
#: credential because it was spelled differently produces a confusing "not
#: configured" error while the value sits right there in the file.
#:
#: Environment names are ALWAYS namespaced. Short aliases are accepted only
#: inside the `[flattrade]` config section, where the context disambiguates
#: them: a bare `PWD` in the environment is the shell's working directory, and
#: reading that as a password is both wrong and a way to leak a filesystem
#: path into an outbound login request.
ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "api_key": ("FLATTRADE_API_KEY",),
    "api_secret": ("FLATTRADE_API_SECRET",),
    "client_id": ("FLATTRADE_CLIENT_ID",),
    "password": ("FLATTRADE_PASSWORD",),
    "totp_secret": ("FLATTRADE_TOTP_SECRET", "FLATTRADE_TOTP"),
    "second_factor": ("FLATTRADE_SECOND_FACTOR",),
    "redirect_uri": ("FLATTRADE_REDIRECT_URI",),
}

SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "api_key": ("api_key", "flattrade_api_key", "apikey"),
    "api_secret": ("api_secret", "flattrade_api_secret", "apisecret"),
    "client_id": ("client_id", "flattrade_client_id", "user_id", "userid", "uid"),
    "password": ("password", "flattrade_password", "pwd"),
    "totp_secret": (
        "totp_secret", "flattrade_totp_secret", "flattrade_totp", "totp", "totp_key",
    ),
    "second_factor": (
        "second_factor", "flattrade_second_factor", "pan_dob", "pan", "dob",
    ),
    "redirect_uri": ("redirect_uri", "flattrade_redirect_uri"),
}


def resolve_credentials(config_path: "Path | None" = None) -> dict:
    """Collect Flattrade credentials from the environment, then config.

    Environment wins, so a deployment can override a checked-out file without
    editing it. Key lookup is alias- and case-insensitive because the same
    value is spelled several plausible ways.
    """
    import configparser
    import os

    resolved = {name: "" for name in SECTION_ALIASES}

    for name, env_names in ENV_ALIASES.items():
        for env_name in env_names:
            value = os.environ.get(env_name, "")
            if value:
                resolved[name] = value
                break

    path = Path(config_path) if config_path else Path("configfile.ini")
    if path.exists():
        parser = configparser.ConfigParser()
        parser.optionxform = str  # preserve case so aliases can match either way
        parser.read(path)
        if parser.has_section("flattrade"):
            section = {k.lower(): v for k, v in parser["flattrade"].items()}
            for name, aliases in SECTION_ALIASES.items():
                if resolved[name]:
                    continue
                for alias in aliases:
                    if section.get(alias.lower()):
                        resolved[name] = section[alias.lower()]
                        break
    return resolved


class FlattradeAuth(AuthStrategy):
    """Holds a Flattrade session token.

    The token is short-lived (one trading day) and can only be minted through
    a browser redirect, so it is supplied rather than fetched: either from the
    environment, or from a token file written by `flattrade_token.py`.
    """

    def __init__(self, broker: str, config: dict) -> None:
        super().__init__(broker, config)
        raw = config.get("token_file", "")
        if raw:
            candidate = Path(raw)
            # Relative paths are resolved against the package root so the
            # token is found regardless of the caller's working directory.
            self._token_file = (
                candidate
                if candidate.is_absolute()
                else Path(__file__).resolve().parents[2] / candidate
            )
        else:
            self._token_file = None

    def _read_token(self) -> tuple[str, str, bool]:
        """(token, client_id, is_fresh). `is_fresh` is False for a token
        issued on an earlier day — Noren invalidates them overnight."""
        import os

        token = os.environ.get(self.config.get("token_env", "FLATTRADE_TOKEN"), "")
        client_id = os.environ.get(self.config.get("client_id_env", "FLATTRADE_CLIENT_ID"), "")
        fresh = bool(token)

        if not token and self._token_file and self._token_file.exists():
            try:
                data = json.loads(self._token_file.read_text())
            except (OSError, ValueError):
                return "", client_id, False
            token = data.get("token", "")
            client_id = client_id or data.get("client_id", "")
            issued = data.get("issued_at", "")
            fresh = bool(token) and (
                not issued or datetime.fromisoformat(issued).date() == date.today()
            )
        return token, client_id, fresh

    def _login_credentials(self) -> dict:
        """Resolve login credentials via the shared alias-aware resolver.

        Nothing secret is written to a tracked file — the values live in the
        environment or a git-ignored local config.
        """
        configured = self.config.get("credentials_file")
        return resolve_credentials(Path(configured) if configured else None)

    def _auto_login(self) -> tuple[str, str]:
        """Mint a fresh token without any manual step, then cache it."""
        creds = self._login_credentials()
        missing = [
            k for k in ("api_key", "api_secret", "client_id", "password") if not creds[k]
        ]
        if missing or not (creds["totp_secret"] or creds["second_factor"]):
            raise AuthError(
                "automated login is not configured. Set FLATTRADE_PASSWORD and "
                "FLATTRADE_TOTP_SECRET (plus api_key/api_secret/client_id), or run "
                "`python quant_backtester/flattrade_token.py` for the browser flow. "
                f"Missing: {', '.join(missing) or 'second factor'}",
                broker=self.broker,
            )

        try:
            token = automated_login(
                api_key=creds["api_key"],
                api_secret=creds["api_secret"],
                client_id=creds["client_id"],
                password=creds["password"],
                totp_secret=creds["totp_secret"],
                second_factor=creds["second_factor"],
            )
        except AuthError as exc:
            # Automated login is the convenience path, not the only one. When
            # it fails the user still needs to know the browser flow exists —
            # otherwise a broker-side rejection looks like a dead end.
            raise AuthError(
                f"{exc}. Automated login failed; run "
                "`python quant_backtester/flattrade_token.py` to authorise in "
                "the browser instead.",
                broker=self.broker,
            ) from exc

        if self._token_file:
            self._token_file.parent.mkdir(parents=True, exist_ok=True)
            self._token_file.write_text(
                json.dumps(
                    {
                        "token": token,
                        "client_id": creds["client_id"],
                        "issued_at": datetime.now().isoformat(),
                        "source": "automated",
                    },
                    indent=2,
                )
            )
            self._token_file.chmod(0o600)
        return token, creds["client_id"]

    def authenticate(self) -> Session:
        token, client_id, fresh = self._read_token()

        # A stale or absent token triggers a silent re-login rather than an
        # error the operator has to act on. That is the whole point of
        # automating this: an unattended run at 9am must not need a human.
        if not token or not fresh:
            if not self.config.get("auto_login", True):
                raise AuthError(
                    "no valid Flattrade token and auto_login is disabled; run "
                    "`python quant_backtester/flattrade_token.py`",
                    broker=self.broker,
                )
            logger.info(
                "Flattrade token %s; logging in automatically",
                "missing" if not token else "expired",
            )
            token, client_id = self._auto_login()

        self._session = Session(
            broker=self.broker,
            access_token=token,
            user_id=client_id,
            expires_at=datetime.combine(date.today(), datetime.max.time()),
        )
        return self._session


class FlattradeClient:
    """Thin REST client for the Noren endpoints Flattrade exposes."""

    def __init__(self, token: str, client_id: str, timeout: float = 20.0) -> None:
        self._token = token
        self._client_id = client_id
        self._timeout = timeout
        self._session = requests.Session()
        self._token_cache: dict[tuple[str, str], str] = {}

    def _post(self, route: str, values: dict) -> dict | list:
        url = f"{API_HOST}{ROUTES[route]}"
        values = {"uid": self._client_id, **values}
        # Noren expects a form body, not JSON: `jData=<json>&jKey=<token>`.
        payload = f"jData={json.dumps(values)}&jKey={self._token}"

        try:
            response = self._session.post(url, data=payload, timeout=self._timeout)
        except requests.Timeout as exc:
            raise BrokerUnavailable(f"{route} timed out", broker="flattrade") from exc
        except requests.RequestException as exc:
            raise BrokerUnavailable(f"{route} failed: {exc}", broker="flattrade") from exc

        if response.status_code == 429:
            raise RateLimited("Flattrade rate limit hit", broker="flattrade", retry_after=1.0)
        if response.status_code >= 500:
            raise BrokerUnavailable(
                f"{route} returned HTTP {response.status_code}", broker="flattrade"
            )
        if not response.content:
            raise BrokerUnavailable(f"{route} returned an empty body", broker="flattrade")

        data = response.json()

        # Some Noren endpoints (EODChartData, TPSeries) return a list of JSON
        # *strings* rather than a list of objects — each element needs a second
        # parse. Doing it here means every list endpoint is handled once,
        # instead of each caller rediscovering the quirk.
        if isinstance(data, list):
            data = [
                json.loads(row) if isinstance(row, str) else row
                for row in data
            ]

        # Errors come back as {"stat": "Not_Ok", "emsg": "..."} with HTTP 200,
        # so status code alone is not a success signal.
        if isinstance(data, dict) and data.get("stat") not in (None, "Ok"):
            message = data.get("emsg", "unknown Flattrade error")
            lowered = message.lower()
            if "session" in lowered or "token" in lowered:
                raise AuthError(message, broker="flattrade")
            # An EMPTY book is not an error. Flattrade answers "no data" when
            # there are no positions, orders or trades to return; raising here
            # made a flat account indistinguishable from a broken one, and
            # every caller that reads the book had to guess which it was.
            if "no data" in lowered:
                logger.debug("Flattrade reports an empty result for %s", route)
                return []
            raise OrderRejected(message, broker="flattrade")
        return data

    # -- instruments ------------------------------------------------------

    def resolve_token(self, exchange: str, symbol: str) -> str:
        """Symbol to Noren instrument token, cached.

        Noren addresses instruments numerically; every quote and history call
        needs this, and the mapping is stable through the day.
        """
        key = (exchange, symbol)
        if key in self._token_cache:
            return self._token_cache[key]

        def _exact_matches(query: str) -> list[dict]:
            data = self._post("searchscrip", {"exch": exchange, "stext": query})
            values = data.get("values", []) if isinstance(data, dict) else []
            return [v for v in values if v.get("tsym", "").upper() == symbol.upper()]

        exact = _exact_matches(symbol)
        if not exact and " " in symbol:
            # Flattrade's search is capped (~25 hits) and ranks by relevance
            # to the query text -- a well-known index searched by its own
            # full display name (e.g. "NIFTY 50") can get buried behind
            # dozens of similarly-prefixed instruments ("Nifty500 ...",
            # "NiftySml250...") and never appear at all. Verified live: the
            # query "NIFTY 50" omits the real "Nifty 50" row entirely, but
            # "NIFTY" alone surfaces it. Retrying with just the leading word
            # widens the search net without weakening the match itself --
            # still requires an EXACT tsym match on the full original symbol.
            exact = _exact_matches(symbol.split()[0])

        if not exact:
            # No blind first-hit fallback: a substring search for "INFY" also
            # returns INFYNIFTY futures, and taking whatever ranked first
            # would silently trade the wrong instrument.
            raise InstrumentNotFound(
                f"{exchange}:{symbol} not found on Flattrade (no exact "
                "trading-symbol match)", broker="flattrade", symbol=symbol
            )
        token = str(exact[0]["token"])
        self._token_cache[key] = token
        return token

    def search(self, exchange: str, text: str) -> list[dict]:
        data = self._post("searchscrip", {"exch": exchange, "stext": text})
        return data.get("values", []) if isinstance(data, dict) else []

    # -- market data ------------------------------------------------------

    def get_quote(self, exchange: str, symbol: str) -> dict:
        token = self.resolve_token(exchange, symbol)
        data = self._post("getquotes", {"exch": exchange, "token": token})
        return data if isinstance(data, dict) else {}

    def get_daily_bars(
        self, exchange: str, symbol: str, start: date, end: date
    ) -> list[dict]:
        """Daily OHLCV history from EODChartData.

        Timestamps are epoch seconds and the symbol is `EXCH:TSYM`, not a
        token — the one endpoint that differs from the rest.
        """
        start_ts = int(datetime.combine(start, datetime.min.time()).timestamp())
        end_ts = int(datetime.combine(end, datetime.min.time()).timestamp())
        data = self._post(
            "eod",
            {"sym": f"{exchange}:{symbol}", "from": str(start_ts), "to": str(end_ts)},
        )
        return data if isinstance(data, list) else []

    def get_intraday_bars(
        self, exchange: str, symbol: str, start: datetime, end: datetime, interval: str = "5"
    ) -> list[dict]:
        token = self.resolve_token(exchange, symbol)
        data = self._post(
            "tpseries",
            {
                "exch": exchange,
                "token": token,
                "st": str(int(start.timestamp())),
                "et": str(int(end.timestamp())),
                "intrv": interval,
            },
        )
        return data if isinstance(data, list) else []

    # -- account ----------------------------------------------------------

    def get_positions(self) -> list[dict]:
        data = self._post("positions", {"actid": self._client_id})
        return data if isinstance(data, list) else []

    def get_limits(self) -> dict:
        data = self._post("limits", {"actid": self._client_id})
        return data if isinstance(data, dict) else {}

    def get_orderbook(self) -> list[dict]:
        data = self._post("orderbook", {})
        return data if isinstance(data, list) else []

    def place_order(self, values: dict) -> dict:
        data = self._post("placeorder", values)
        return data if isinstance(data, dict) else {}

    def cancel_order(self, order_number: str) -> dict:
        data = self._post("cancelorder", {"norenordno": order_number})
        return data if isinstance(data, dict) else {}

    def modify_order(self, values: dict) -> dict:
        data = self._post("modifyorder", values)
        return data if isinstance(data, dict) else {}

    def order_history(self, order_number: str) -> list[dict]:
        data = self._post("singleorderhistory", {"norenordno": order_number})
        return data if isinstance(data, list) else []


def _f(value, default: float | None = None) -> float | None:
    """Noren returns numbers as strings, and absent fields as '' or missing."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed


def _positive(value) -> float | None:
    """A price that must be strictly positive, else absent.

    Outside market hours Noren reports bid/ask as "0.00". Carrying that
    through as a real quote is misleading in the UI, and a zero touching the
    fill path would price a trade at nothing. Absent is the honest answer.
    """
    parsed = _f(value)
    return parsed if parsed and parsed > 0 else None


class FlattradeQuotes(QuoteProvider):
    """Live Indian-market quotes, for the paper broker or the console."""

    def __init__(self, client: FlattradeClient, exchange: str = "NSE", ttl_seconds: float = 5.0):
        self._client = client
        self._exchange = exchange
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, UnifiedQuote]] = {}

    def get_quote(self, symbol: str) -> UnifiedQuote:
        now = time.monotonic()
        cached = self._cache.get(symbol)
        if cached and now - cached[0] < self._ttl:
            return cached[1]

        exchange, tsym = _split_symbol(symbol, self._exchange)
        raw = self._client.get_quote(exchange, tsym)
        last = _f(raw.get("lp"))
        if last is None:
            raise InstrumentNotFound(
                f"Flattrade returned no price for {symbol}", broker="flattrade", symbol=symbol
            )

        # Already resolved and cached inside get_quote() above; re-reading it
        # here is a cache hit, not a second API call.
        try:
            token = self._client.resolve_token(exchange, tsym)
        except InstrumentNotFound:
            token = None

        quote = UnifiedQuote(
            symbol=symbol,
            last_price=last,
            timestamp=datetime.now(),
            bid=_positive(raw.get("bp1")),
            ask=_positive(raw.get("sp1")),
            volume=_f(raw.get("v")),
            previous_close=_positive(raw.get("c")),
            broker_token=token,
        )
        self._cache[symbol] = (now, quote)
        return quote


def _split_symbol(symbol: str, default_exchange: str) -> tuple[str, str]:
    """Accept either "NSE:RELIANCE-EQ" or a bare "RELIANCE-EQ"."""
    if ":" in symbol:
        exchange, tsym = symbol.split(":", 1)
        return exchange.upper(), tsym
    return default_exchange, symbol


class FlattradeAdapter(BrokerAdapter):
    """Live Flattrade execution behind the standard BrokerAdapter interface.

    NOT simulated: `is_simulated` stays False, so every order placed through
    this adapter sits behind the live-trading gate in `OrderService`.
    """

    name = "flattrade"
    is_simulated = False
    capabilities = BrokerCapabilities(
        supports_gtt=False,
        supports_streaming=True,
        supports_short_selling=True,
        supports_options=True,
        supports_modify=True,
        fractional_quantities=False,
    )

    def __init__(self, auth: AuthStrategy, config: dict | None = None) -> None:
        super().__init__(auth, config or {})
        self._exchange = self.config.get("exchange", "NSE")
        self._client: FlattradeClient | None = None

    @property
    def client(self) -> FlattradeClient:
        session = self.authenticate()
        if self._client is None:
            self._client = FlattradeClient(
                token=session.access_token or "",
                client_id=session.user_id or self.config.get("client_id", ""),
            )
        return self._client

    # -- market data ------------------------------------------------------

    def get_quote(self, symbol: str) -> UnifiedQuote:
        return FlattradeQuotes(self.client, self._exchange).get_quote(symbol)

    def get_instruments(self) -> list[UnifiedInstrument]:
        """The configured universe, resolved to real tokens and tick sizes.

        Flattrade has no bulk instrument-dump endpoint — only per-symbol
        lookup (`searchscrip`/`getquotes`). A full exchange dump the way
        Kite or Dhan's scrip master provide is not available here, so this
        is honestly scoped to the symbols this platform actually trades
        rather than pretending to be a complete catalogue.

        One quote call per symbol resolves both the numeric token and the
        real tick size (`ti` in Flattrade's response) — guessing either
        would produce orders priced or routed against the wrong instrument.
        """
        out = []
        for symbol in self.config.get("universe", []):
            exchange, tsym = _split_symbol(symbol, self._exchange)
            try:
                quote = self.client.get_quote(exchange, tsym)
                token = quote.get("token", "")
                tick = float(quote.get("ti") or 0.05)
            except Exception as exc:  # noqa: BLE001 - one bad symbol must not fail the sync
                logger.warning("Could not resolve %s for instrument sync: %s", symbol, exc)
                continue
            out.append(
                UnifiedInstrument(
                    symbol=symbol,
                    exchange=exchange,
                    instrument_type=InstrumentType.EQUITY,
                    lot_size=1,
                    tick_size=tick,
                    broker_token=str(token),
                )
            )
        return out

    # -- orders ------------------------------------------------------------

    def place_order(self, order: UnifiedOrder) -> UnifiedOrder:
        exchange, tsym = _split_symbol(order.symbol, self._exchange)
        values = {
            "actid": self.client._client_id,
            "exch": exchange,
            "tsym": tsym,
            "qty": str(int(order.quantity)),
            "prc": str(order.limit_price or 0),
            "prd": PRODUCT_TO_NOREN[order.product],
            "trantype": "B" if order.side is Side.BUY else "S",
            "prctyp": PRICE_TYPE_TO_NOREN[order.order_type],
            "ret": "DAY",
            "remarks": order.strategy or "platform",
        }
        if order.stop_price is not None:
            values["trgprc"] = str(order.stop_price)

        response = self.client.place_order(values)
        order_number = response.get("norenordno")
        if not order_number:
            raise OrderRejected(
                response.get("emsg", "Flattrade did not return an order number"),
                broker=self.name,
            )
        order.broker = self.name
        order.broker_order_id = str(order_number)
        order.order_id = order.order_id or str(order_number)
        order.status = OrderStatus.OPEN
        order.created_at = order.created_at or datetime.now()
        order.updated_at = datetime.now()
        return order

    def cancel_order(self, order_id: str) -> UnifiedOrder:
        self.client.cancel_order(order_id)
        return self.get_order_status(order_id)

    def modify_order(
        self,
        order_id: str,
        *,
        quantity: float | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> UnifiedOrder:
        current = self.get_order_status(order_id)
        exchange, tsym = _split_symbol(current.symbol, self._exchange)
        values = {
            "norenordno": order_id,
            "exch": exchange,
            "tsym": tsym,
            "qty": str(int(quantity if quantity is not None else current.quantity)),
            "prctyp": PRICE_TYPE_TO_NOREN[current.order_type],
            "prc": str(limit_price if limit_price is not None else (current.limit_price or 0)),
        }
        if stop_price is not None:
            values["trgprc"] = str(stop_price)
        self.client.modify_order(values)
        return self.get_order_status(order_id)

    def get_order_status(self, order_id: str) -> UnifiedOrder:
        for order in self.get_orders():
            if order.broker_order_id == order_id or order.order_id == order_id:
                return order
        raise OrderRejected(f"order {order_id} not found", broker=self.name)

    def get_orders(self) -> list[UnifiedOrder]:
        return [self._to_order(raw) for raw in self.client.get_orderbook()]

    def _to_order(self, raw: dict) -> UnifiedOrder:
        quantity = _f(raw.get("qty"), 0.0) or 0.0
        order = UnifiedOrder(
            symbol=raw.get("tsym", ""),
            side=Side.BUY if raw.get("trantype") == "B" else Side.SELL,
            quantity=max(quantity, 1e-9),
            order_type=OrderType.LIMIT if raw.get("prctyp") == "LMT" else OrderType.MARKET,
            product=NOREN_TO_PRODUCT.get(raw.get("prd", "I"), ProductType.INTRADAY),
            limit_price=_f(raw.get("prc")),
            broker=self.name,
            order_id=str(raw.get("norenordno", "")),
            broker_order_id=str(raw.get("norenordno", "")),
            strategy=raw.get("remarks", "") or "",
            status=NOREN_TO_STATUS.get(str(raw.get("status", "")).upper(), OrderStatus.PENDING),
            filled_quantity=_f(raw.get("fillshares"), 0.0) or 0.0,
            average_price=_f(raw.get("avgprc")),
            status_message=raw.get("rejreason", "") or "",
        )
        return order

    # -- positions ---------------------------------------------------------

    def get_positions(self) -> list[UnifiedPosition]:
        positions = []
        for raw in self.client.get_positions():
            # Noren already signs netqty (negative == short), which matches the
            # platform convention, so no direction flag translation is needed.
            quantity = _f(raw.get("netqty"), 0.0) or 0.0
            if abs(quantity) < 1e-9:
                continue
            positions.append(
                UnifiedPosition(
                    symbol=raw.get("tsym", ""),
                    quantity=quantity,
                    average_price=_f(raw.get("netavgprc"), 0.0) or 0.0,
                    broker=self.name,
                    product=NOREN_TO_PRODUCT.get(raw.get("prd", "I"), ProductType.INTRADAY),
                    last_price=_f(raw.get("lp")),
                    realized_pnl=_f(raw.get("rpnl"), 0.0) or 0.0,
                )
            )
        return positions

    def get_account(self) -> AccountSnapshot:
        limits = self.client.get_limits()
        positions = self.get_positions()
        cash = _f(limits.get("cash"), 0.0) or 0.0
        return AccountSnapshot(
            broker=self.name,
            cash=cash,
            equity=cash + sum(p.market_value for p in positions),
            realized_pnl=sum(p.realized_pnl for p in positions),
            unrealized_pnl=sum(p.unrealized_pnl for p in positions),
            positions=positions,
            timestamp=datetime.now(),
        )


# --------------------------------------------------------------------------
# Historical data for the research pipeline
# --------------------------------------------------------------------------


def build_flattrade_downloader(client: FlattradeClient, default_exchange: str = "NSE"):
    """A downloader for `HistoricalDataManager`.

    Returns the same OHLCV+`Adj Close` frame shape the yfinance downloader
    produces, so Layers 1-4 consume Indian data without any change.

    Flattrade's EOD series is already adjusted for corporate actions, so
    `Adj Close` is set equal to the close. That keeps the back-adjustment step
    in `data_loader` a no-op (factor 1.0) rather than double-adjusting.
    """
    import pandas as pd

    def download(symbol: str, start: date, end: date) -> pd.DataFrame:
        exchange, tsym = _split_symbol(symbol, default_exchange)
        rows = client.get_daily_bars(exchange, tsym, start, end)
        if not rows:
            raise BrokerUnavailable(
                f"Flattrade returned no history for {symbol}", broker="flattrade"
            )

        records = []
        for row in rows:
            # Noren spells these differently across endpoints; accept both.
            stamp = row.get("ssboe") or row.get("time")
            close = _f(row.get("intc") or row.get("c"))
            if stamp is None or close is None:
                continue
            when = (
                datetime.fromtimestamp(int(stamp))
                if str(stamp).isdigit()
                # Flattrade spells dates "01-SEP-2026"; ssboe (epoch) is
                # preferred, this is only the fallback.
                else datetime.strptime(str(stamp).upper(), "%d-%b-%Y")
            )
            records.append(
                {
                    "Date": when.date(),
                    "Open": _f(row.get("into") or row.get("o"), close),
                    "High": _f(row.get("inth") or row.get("h"), close),
                    "Low": _f(row.get("intl") or row.get("l"), close),
                    "Close": close,
                    "Adj Close": close,
                    "Volume": _f(row.get("intv") or row.get("v"), 0.0),
                }
            )

        if not records:
            raise BrokerUnavailable(
                f"Flattrade history for {symbol} had no usable rows", broker="flattrade"
            )

        frame = pd.DataFrame(records).set_index("Date").sort_index()
        frame.index = pd.to_datetime(frame.index)
        return frame

    return download
