"""Dhan (DhanHQ v2) adapter.

Implements the same `BrokerAdapter` contract as Flattrade, so selecting Dhan
is a one-line change in broker.yaml and nothing else in the platform moves.

TWO THINGS DHAN DOES DIFFERENTLY, both handled here so callers never see them:

**Instruments are numeric.** Every Dhan call identifies a contract by
`securityId`, not by symbol — order placement, quotes, history, all of it.
The mapping lives in a scrip-master CSV Dhan publishes. `DhanInstruments`
downloads it once, caches it to disk, and resolves symbols on demand. A
symbol that cannot be resolved raises `InstrumentNotFound` rather than
guessing at a number, because a wrong securityId is an order in the wrong
instrument.

**Tokens are not refreshable in code.** A self-generated Dhan access token is
minted only from an authenticated session on the web portal; there is no API
that trades credentials for a fresh one (the partner consent flow needs
partner credentials Dhan issues separately). So `DhanAuth` validates and
reports, and `dhan_token.py` walks the operator through re-issuing. An
expired token fails loudly with the exact steps rather than degrading into
confusing 401s deep inside a trading loop.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .adapter import BrokerAdapter, BrokerCapabilities
from .auth import AuthStrategy, Session
from .exceptions import (
    AuthError,
    BrokerError,
    BrokerUnavailable,
    InstrumentNotFound,
    OrderRejected,
)
from .models import (
    AccountSnapshot,
    Fill,
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

logger = logging.getLogger(__name__)

API_BASE = "https://api.dhan.co/v2"
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"

#: Dhan's exchange segment names, keyed by the exchange the platform uses.
EXCHANGE_SEGMENTS = {
    "NSE": "NSE_EQ",
    "BSE": "BSE_EQ",
    "NFO": "NSE_FNO",
    "BFO": "BSE_FNO",
    "MCX": "MCX_COMM",
    "CDS": "NSE_CURRENCY",
    # An index (NIFTY 50, INDIA VIX, ...) shares its securityId's numeric
    # space with unrelated NSE_EQ instruments -- quoting it under NSE_EQ
    # silently returns whatever equity happens to have that same id, not
    # the index. Dhan's own dedicated segment for these is IDX_I.
    "INDEX": "IDX_I",
}

_SIDE_TO_DHAN = {Side.BUY: "BUY", Side.SELL: "SELL"}
_ORDER_TYPE_TO_DHAN = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    OrderType.STOP: "STOP_LOSS_MARKET",
    OrderType.STOP_LIMIT: "STOP_LOSS",
}
_PRODUCT_TO_DHAN = {
    ProductType.INTRADAY: "INTRADAY",
    ProductType.DELIVERY: "CNC",
    ProductType.CARRYFORWARD: "MARGIN",
    ProductType.COVER: "CO",
}
_STATUS_FROM_DHAN = {
    "TRANSIT": OrderStatus.PENDING,
    "PENDING": OrderStatus.OPEN,
    "OPEN": OrderStatus.OPEN,
    "PART_TRADED": OrderStatus.PARTIAL,
    "TRADED": OrderStatus.COMPLETE,
    "EXECUTED": OrderStatus.COMPLETE,
    "CANCELLED": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.CANCELLED,
}


# ==========================================================================
# TOKEN INSPECTION
# ==========================================================================


def decode_token_claims(token: str) -> dict:
    """Read a Dhan JWT's payload without verifying it.

    Verification is the server's job; this exists so the platform can tell an
    operator *when* a token dies before it dies mid-session.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception as exc:  # noqa: BLE001 - a malformed token is not fatal here
        logger.debug("Could not decode Dhan token: %s", exc)
        return {}


def token_expiry(token: str) -> datetime | None:
    exp = decode_token_claims(token).get("exp")
    return datetime.fromtimestamp(exp) if exp else None


# ==========================================================================
# AUTH
# ==========================================================================


class DhanAuth(AuthStrategy):
    """Loads a Dhan access token and refuses to hand out a dead one.

    Order of preference: the environment, then a token file, then the
    credentials config. Whichever supplies it, the token's expiry is checked
    before it is returned — an expired token produces a clear message naming
    the refresh script, not a 401 from somewhere inside an order call.
    """

    def __init__(self, broker: str, config: dict) -> None:
        super().__init__(broker, config)
        # Default to the canonical location rather than None. A config that
        # simply omits `token_file` (paper_dhan, for one) must still find the
        # token the refresh script writes — otherwise it silently falls
        # through to whatever stale value configfile.ini still holds.
        self._token_file = config.get("token_file") or "state/dhan_token.json"
        self._token_env = config.get("token_env", "DHAN_ACCESS_TOKEN")
        self._client_env = config.get("client_id_env", "DHAN_CLIENT_ID")

    def authenticate(self) -> Session:
        token, client_id, source = self._load()
        if not token:
            raise AuthError(
                "no Dhan access token found. Generate one at "
                "web.dhan.co -> Profile -> DhanHQ Trading APIs, then run "
                "`python quant_backtester/dhan_token.py --set <token>`",
                broker=self.broker,
            )

        expires_at = token_expiry(token)
        if expires_at and expires_at <= datetime.now():
            raise AuthError(
                f"the Dhan access token expired on {expires_at:%Y-%m-%d %H:%M} "
                f"(source: {source}). Dhan tokens cannot be renewed via the API; "
                "generate a new one at web.dhan.co -> Profile -> DhanHQ Trading "
                "APIs and run `python quant_backtester/dhan_token.py --set <token>`",
                broker=self.broker,
            )

        claims = decode_token_claims(token)
        self._session = Session(
            broker=self.broker,
            access_token=token,
            expires_at=expires_at,
            user_id=client_id or str(claims.get("dhanClientId", "")),
            metadata={"source": source, "consumer_type": claims.get("tokenConsumerType", "")},
        )
        return self._session

    def refresh(self) -> Session:
        """Dhan has no programmatic renewal for self-generated tokens.

        Saying so plainly is better than a retry loop that cannot succeed.
        """
        raise AuthError(
            "Dhan access tokens cannot be refreshed programmatically; a new one "
            "must be generated from the web portal. Run "
            "`python quant_backtester/dhan_token.py --check` for the current status.",
            broker=self.broker,
        )

    def _load(self) -> tuple[str, str, str]:
        import os

        token = (os.environ.get(self._token_env) or "").strip()
        client = (os.environ.get(self._client_env) or "").strip()
        if token:
            return token, client, "environment"

        if self._token_file:
            path = Path(self._token_file)
            if not path.is_absolute():
                from ..config import REPO_ROOT

                path = REPO_ROOT / path
            if path.exists():
                try:
                    data = json.loads(path.read_text())
                    if data.get("token"):
                        return data["token"], data.get("client_id", ""), f"file {path.name}"
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Unreadable Dhan token file %s: %s", path, exc)

        token, client = _from_credentials_file()
        return token, client, "configfile.ini"


def _from_credentials_file() -> tuple[str, str]:
    """Read the token from configfile.ini.

    The canonical home is a `[dhan]` section with unprefixed keys, matching
    how `[flattrade]` is written. Any section carrying `dhan_`-prefixed keys
    is still accepted, so a config that has not been reorganised keeps
    working rather than failing with a confusing "no token found".
    """
    import configparser

    from ..config import REPO_ROOT

    path = REPO_ROOT.parent / "configfile.ini"
    if not path.exists():
        return "", ""

    parser = configparser.ConfigParser()
    try:
        parser.read(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read %s: %s", path, exc)
        return "", ""

    if parser.has_section("dhan"):
        items = {k.lower(): v for k, v in parser.items("dhan")}
        token = _clean(items.get("access_token") or items.get("dhan_access_token"))
        if token:
            return token, _clean(items.get("client_id") or items.get("dhan_client_id"))

    # Legacy layout: dhan_* keys parked in some other section.
    for section in parser.sections():
        items = {k.lower(): v for k, v in parser.items(section)}
        token = _clean(items.get("dhan_access_token"))
        if token:
            logger.info(
                "Found Dhan credentials in [%s]; consider moving them to a "
                "[dhan] section", section,
            )
            return token, _clean(items.get("dhan_client_id"))
    return "", ""


def _clean(value: str | None) -> str:
    """Strip whitespace and any surrounding quotes.

    Values in configfile.ini are often written quoted. Passing a token with
    its quotation marks still attached produces an authentication failure
    that looks exactly like an expired token, which is a miserable thing to
    debug.
    """
    text = (value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


# ==========================================================================
# INSTRUMENTS
# ==========================================================================


class DhanInstruments:
    """Symbol -> securityId, from Dhan's published scrip master.

    Cached on disk because the file is large and changes once a day. A miss
    is an error, never a guess: an incorrect securityId would place a real
    order in the wrong instrument.
    """

    def __init__(self, cache_dir: Path, ttl_hours: float = 24.0) -> None:
        self._path = Path(cache_dir) / "dhan_scrip_master.csv"
        self._ttl = timedelta(hours=ttl_hours)
        self._frame = None

    def _load(self):
        import pandas as pd

        if self._frame is not None:
            return self._frame

        fresh = (
            self._path.exists()
            and datetime.fromtimestamp(self._path.stat().st_mtime) > datetime.now() - self._ttl
        )
        if not fresh:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                frame = pd.read_csv(SCRIP_MASTER_URL, low_memory=False)
                frame.to_csv(self._path, index=False)
                logger.info("Downloaded Dhan scrip master (%s rows)", len(frame))
            except Exception as exc:  # noqa: BLE001 - stale cache beats no cache
                if not self._path.exists():
                    raise BrokerUnavailable(
                        f"could not download the Dhan scrip master: {exc}", broker="dhan"
                    ) from exc
                logger.warning("Scrip master refresh failed (%s); using cached copy", exc)

        frame = pd.read_csv(self._path, low_memory=False)
        frame.columns = [c.strip().upper() for c in frame.columns]
        required = {"SECURITY_ID", "SYMBOL_NAME"}
        missing = required - set(frame.columns)
        if missing:
            raise BrokerError(
                f"Dhan scrip master is missing {sorted(missing)}; the published "
                "layout has changed and symbol resolution cannot be trusted",
                broker="dhan",
            )
        self._frame = frame
        return self._frame

    def option_chain(self, underlying: str, exchange: str = "NSE", instrument: str = "OPTIDX"):
        """All option rows for one underlying, real strikes and expiries.

        Used to derive the current contract dynamically -- no expiry date or
        strike is ever hardcoded; both come from whatever Dhan actually
        publishes as tradable right now.
        """
        frame = self._load()
        wanted = underlying.strip().upper()
        scoped = frame[
            (frame["UNDERLYING_SYMBOL"].astype(str).str.strip().str.upper() == wanted)
            & (frame["EXCH_ID"].astype(str).str.upper() == exchange.upper())
            & (frame["INSTRUMENT"].astype(str).str.upper() == instrument.upper())
        ]
        return scoped

    def resolve(self, symbol: str, exchange: str = "NSE") -> UnifiedInstrument:
        """Find one instrument's numeric securityId.

        Matched on the detailed master's real columns (SYMBOL_NAME /
        DISPLAY_NAME / UNDERLYING_SYMBOL), scoped to the exchange and to cash
        equity by default. The scoping matters: "RELIANCE" appears on
        thousands of rows once futures and options are included, and picking
        the wrong one would place an order in a derivative.
        """
        frame = self._load()
        wanted = _normalise(symbol)

        # Dhan's own SYMBOL_NAME is month-granular ("NIFTY-Sep2026-...")
        # and collides across every weekly expiry in that month -- useless
        # for picking one specific contract. `option_chain()`'s caller
        # instead builds a day-qualified symbol ("NIFTY-08SEP2026-29150-CE")
        # that uniquely names one expiry; recognise and resolve that format
        # directly against (underlying, expiry, strike, option_type).
        day_qualified = re.match(
            r"^([A-Z]+)-(\d{2}[A-Z]{3}\d{4})-(\d+(?:\.\d+)?)-(CE|PE)$", wanted
        )
        if day_qualified and "SM_EXPIRY_DATE" in frame.columns:
            import pandas as pd

            underlying, expiry_str, strike_str, option_type = day_qualified.groups()
            try:
                expiry = datetime.strptime(expiry_str, "%d%b%Y").date()
            except ValueError:
                expiry = None
            if expiry is not None:
                expiries = pd.to_datetime(frame["SM_EXPIRY_DATE"], errors="coerce").dt.date
                hit = frame[
                    (frame["UNDERLYING_SYMBOL"].astype(str).str.strip().str.upper() == underlying)
                    & (expiries == expiry)
                    & (frame["STRIKE_PRICE"].astype(float) == float(strike_str))
                    & (frame["OPTION_TYPE"].astype(str).str.upper() == option_type)
                ]
                if not hit.empty:
                    row = hit.iloc[0]
                    # Which segment a contract trades on is a property of
                    # the UNDERLYING (SENSEX/BANKEX -> BFO, everything else
                    # -> NFO) -- not of the caller's own configured
                    # exchange, which for a cash-equity paper adapter stays
                    # "NSE" regardless of what option this call resolves.
                    resolved_exchange = "BFO" if underlying == "SENSEX" else "NFO"
                    return UnifiedInstrument(
                        symbol=symbol,
                        exchange=resolved_exchange,
                        broker_token=str(row["SECURITY_ID"]).split(".")[0],
                        lot_size=int(_num(row.get("LOT_SIZE"), 1)),
                        tick_size=_tick_in_rupees(row.get("TICK_SIZE")),
                        instrument_type=(
                            InstrumentType.CALL if option_type == "CE" else InstrumentType.PUT
                        ),
                    )
                raise InstrumentNotFound(
                    f"{symbol!r} (expiry {expiry}, strike {strike_str}{option_type}) is not "
                    "in the Dhan scrip master", symbol=symbol,
                )

        # An exact SYMBOL_NAME or DISPLAY_NAME match (Dhan's own option/
        # future symbol e.g. "NIFTY-Sep2026-29150-CE", or an index's display
        # name e.g. "Nifty 50") is unambiguous regardless of exchange or
        # instrument-type scoping -- try it first so derivatives and indices
        # resolve correctly even though the narrowing below is equity-only.
        for column in ("SYMBOL_NAME", "DISPLAY_NAME"):
            if column not in frame.columns:
                continue
            exact = frame[frame[column].astype(str).str.strip().str.upper() == wanted]
            if not exact.empty:
                row = exact.iloc[0]
                row_instrument = str(row.get("INSTRUMENT", "")).upper()
                is_derivative = row_instrument in ("OPTIDX", "OPTSTK", "FUTIDX", "FUTSTK")
                is_index = row_instrument == "INDEX"
                option_type = str(row.get("OPTION_TYPE", "")).upper()
                inst_type = (
                    InstrumentType.CALL if option_type == "CE"
                    else InstrumentType.PUT if option_type == "PE"
                    else InstrumentType.FUTURE if is_derivative
                    else InstrumentType.EQUITY
                )
                # A resolved instrument knows its own kind better than the
                # adapter's single configured exchange -- feed get_quote()
                # the market-data segment that actually matches it.
                resolved_exchange = exchange
                if is_index:
                    resolved_exchange = "INDEX"
                elif is_derivative and exchange.upper() != "BFO":
                    resolved_exchange = "NFO"
                return UnifiedInstrument(
                    symbol=symbol,
                    exchange=resolved_exchange,
                    broker_token=str(row["SECURITY_ID"]).split(".")[0],
                    lot_size=int(_num(row.get("LOT_SIZE"), 1)),
                    tick_size=_tick_in_rupees(row.get("TICK_SIZE")),
                    instrument_type=inst_type,
                )

        rows = frame
        if "EXCH_ID" in rows.columns:
            scoped = rows[rows["EXCH_ID"].astype(str).str.upper() == exchange.upper()]
            if not scoped.empty:
                rows = scoped

        # Cash equity unless the caller clearly wants a derivative segment.
        if "INSTRUMENT" in rows.columns and exchange.upper() in ("NSE", "BSE"):
            equity = rows[rows["INSTRUMENT"].astype(str).str.upper().isin(("EQUITY", "ES"))]
            if not equity.empty:
                rows = equity

        for column in ("SYMBOL_NAME", "DISPLAY_NAME", "UNDERLYING_SYMBOL"):
            if column not in rows.columns:
                continue
            hit = rows[rows[column].astype(str).str.strip().str.upper() == wanted]
            if not hit.empty:
                row = hit.iloc[0]
                return UnifiedInstrument(
                    symbol=symbol,
                    exchange=exchange,
                    broker_token=str(row["SECURITY_ID"]).split(".")[0],
                    lot_size=int(_num(row.get("LOT_SIZE"), 1)),
                    # Dhan publishes TICK_SIZE in paise (500 -> Rs 5.00,
                    # 5 -> Rs 0.05). Passing it through unconverted would
                    # round order prices to absurd increments.
                    tick_size=_tick_in_rupees(row.get("TICK_SIZE")),
                    instrument_type=InstrumentType.EQUITY,
                )

        raise InstrumentNotFound(
            f"{symbol!r} is not in the Dhan scrip master for {exchange}; "
            "check the exact trading symbol",
            symbol=symbol,
        )


#: Series suffixes other Indian brokers append to a cash-equity symbol.
#: Dhan's master carries the bare name, so "RELIANCE-EQ" must become
#: "RELIANCE" or every symbol from the Flattrade-shaped universe misses.
_SERIES_SUFFIXES = ("-EQ", "-BE", "-BZ", "-SM", "-ST")


def _normalise(symbol: str) -> str:
    text = symbol.strip().upper()
    for suffix in _SERIES_SUFFIXES:
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def _tick_in_rupees(value) -> float:
    raw = _num(value, 5.0)
    return round(raw / 100.0, 4) if raw and raw >= 1 else float(raw or 0.05)


def _num(value, default):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result else default


# ==========================================================================
# HTTP CLIENT
# ==========================================================================


#: Dhan publishes per-endpoint ceilings. The market feed is the tight one at
#: roughly one request per second; exceeding it earns a 429 and, repeated,
#: a block on the account. Keyed by path prefix, in requests per second.
RATE_LIMITS = {
    "/marketfeed": 1.0,
    "/charts": 1.0,
    "/orders": 20.0,
    "": 5.0,   # everything else
}


class _RateLimiter:
    """Smallest thing that keeps us under a per-second ceiling.

    A sleep rather than a queue: these calls are synchronous and a trading
    loop would rather wait 200ms than be blocked by the broker for a day.
    """

    def __init__(self, limits: dict) -> None:
        self._limits = dict(limits)
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def _bucket(self, path: str) -> str:
        for prefix in sorted(self._limits, key=len, reverse=True):
            if prefix and path.startswith(prefix):
                return prefix
        return ""

    def wait(self, path: str) -> None:
        bucket = self._bucket(path)
        rate = self._limits.get(bucket, 5.0)
        if rate <= 0:
            return
        interval = 1.0 / rate
        with self._lock:
            now = time.monotonic()
            earliest = self._last.get(bucket, 0.0) + interval
            if now < earliest:
                time.sleep(earliest - now)
            self._last[bucket] = time.monotonic()


class DhanClient:
    """Thin DhanHQ v2 REST client. Knows HTTP; knows nothing about strategy."""

    def __init__(
        self,
        token: str,
        client_id: str,
        timeout: float = 20.0,
        rate_limits: dict | None = None,
        max_retries: int = 3,
    ) -> None:
        self.token = token
        self.client_id = client_id
        self._timeout = timeout
        self._limiter = _RateLimiter(rate_limits or RATE_LIMITS)
        self._max_retries = max_retries

    def _request(self, method: str, path: str, payload: dict | None = None):
        import requests

        self._limiter.wait(path)
        url = f"{API_BASE}{path}"
        headers = {
            "access-token": self.token,
            "client-id": self.client_id,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            response = requests.request(
                method, url, headers=headers, json=payload, timeout=self._timeout
            )
        except Exception as exc:  # noqa: BLE001
            raise BrokerUnavailable(f"Dhan request failed: {exc}", broker="dhan") from exc

        if response.status_code == 401:
            raise AuthError(
                "Dhan rejected the access token (expired or invalid). Run "
                "`python quant_backtester/dhan_token.py --check`",
                broker="dhan",
            )
        if response.status_code == 429:
            # Back off and retry rather than failing the caller: a rate limit
            # is a "later", not a "no". Repeated 429s get the account blocked,
            # so the wait grows.
            attempt = getattr(self, "_attempt", 0) + 1
            if attempt <= self._max_retries:
                delay = 2.0 * attempt
                logger.warning(
                    "Dhan rate-limited %s; retrying in %.0fs (%s/%s)",
                    path, delay, attempt, self._max_retries,
                )
                time.sleep(delay)
                self._attempt = attempt
                try:
                    return self._request(method, path, payload)
                finally:
                    self._attempt = attempt - 1
            raise BrokerUnavailable(
                f"Dhan rate limit exceeded on {path} after {self._max_retries} retries",
                broker="dhan",
            )
        if response.status_code >= 400:
            raise BrokerError(
                f"Dhan {method} {path} -> HTTP {response.status_code}: {response.text[:300]}",
                broker="dhan",
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise BrokerError(f"Dhan returned non-JSON: {response.text[:200]}", broker="dhan") from exc

    # -- endpoints ----------------------------------------------------

    def profile(self) -> dict:
        return self._request("GET", "/profile") or {}

    def fund_limit(self) -> dict:
        return self._request("GET", "/fundlimit") or {}

    def positions(self) -> list[dict]:
        data = self._request("GET", "/positions")
        return data if isinstance(data, list) else []

    def holdings(self) -> list[dict]:
        data = self._request("GET", "/holdings")
        return data if isinstance(data, list) else []

    def orders(self) -> list[dict]:
        data = self._request("GET", "/orders")
        return data if isinstance(data, list) else []

    def order(self, order_id: str) -> dict:
        data = self._request("GET", f"/orders/{order_id}")
        if isinstance(data, list):
            return data[0] if data else {}
        return data or {}

    def trades(self) -> list[dict]:
        data = self._request("GET", "/trades")
        return data if isinstance(data, list) else []

    def place_order(self, payload: dict) -> dict:
        return self._request("POST", "/orders", payload) or {}

    def modify_order(self, order_id: str, payload: dict) -> dict:
        return self._request("PUT", f"/orders/{order_id}", payload) or {}

    def cancel_order(self, order_id: str) -> dict:
        return self._request("DELETE", f"/orders/{order_id}") or {}

    def forever_orders(self) -> list[dict]:
        data = self._request("GET", "/forever/orders")
        return data if isinstance(data, list) else []

    def place_forever(self, payload: dict) -> dict:
        return self._request("POST", "/forever/orders", payload) or {}

    def modify_forever(self, order_id: str, payload: dict) -> dict:
        return self._request("PUT", f"/forever/orders/{order_id}", payload) or {}

    def cancel_forever(self, order_id: str) -> dict:
        return self._request("DELETE", f"/forever/orders/{order_id}") or {}

    def historical(self, payload: dict) -> dict:
        """Daily OHLCV. Dhan returns parallel ARRAYS, not a list of records."""
        return self._request("POST", "/charts/historical", payload) or {}

    def ltp(self, by_segment: dict[str, list[int]]) -> dict:
        return self._request("POST", "/marketfeed/ltp", by_segment) or {}

    def quote(self, by_segment: dict[str, list[int]]) -> dict:
        return self._request("POST", "/marketfeed/quote", by_segment) or {}


# ==========================================================================
# ADAPTER
# ==========================================================================


class DhanAdapter(BrokerAdapter):
    """Dhan, behind the same interface as every other broker."""

    name = "dhan"

    capabilities = BrokerCapabilities(
        # Dhan calls these Forever Orders (/forever/orders). Wired below, so
        # the capability is declared true and the legacy GTT monitors work.
        supports_gtt=True,
        supports_streaming=False,
        supports_short_selling=True,
        supports_options=True,
        supports_modify=True,
        supports_bracket_orders=True,
        fractional_quantities=False,
    )

    def __init__(self, auth: AuthStrategy, config: dict | None = None) -> None:
        super().__init__(auth, config)
        settings = config or {}
        self._exchange = settings.get("exchange", "NSE")
        from ..config import REPO_ROOT

        self._instruments = DhanInstruments(
            Path(settings.get("cache_dir") or (REPO_ROOT / "state"))
        )
        self._client: DhanClient | None = None

    @property
    def client(self) -> DhanClient:
        if self._client is None:
            session = self.session or self.authenticate()
            self._client = DhanClient(
                token=session.access_token or "", client_id=session.user_id or ""
            )
        return self._client

    # -- instruments --------------------------------------------------

    def resolve_instrument(self, symbol: str) -> UnifiedInstrument:
        return self._instruments.resolve(symbol, self._exchange)

    def get_instruments(self) -> list[UnifiedInstrument]:
        """Deliberately not the full 100k-row dump.

        Callers that need one instrument should resolve it by symbol; the
        console lists the configured universe, not the exchange.
        """
        universe = (self.config or {}).get("universe", [])
        found = []
        for symbol in universe:
            try:
                found.append(self.resolve_instrument(symbol))
            except InstrumentNotFound:
                logger.warning("Configured symbol %s is not in the Dhan master", symbol)
        return found

    # -- market data --------------------------------------------------

    def get_quote(self, symbol: str) -> UnifiedQuote:
        instrument = self.resolve_instrument(symbol)
        # The resolved instrument knows its own exchange (options resolve to
        # NFO even when this adapter's own config default is NSE cash), so
        # prefer it over the adapter's static setting -- one paper account
        # legitimately quotes both cash and options through the same chain.
        segment = EXCHANGE_SEGMENTS.get(
            (instrument.exchange or self._exchange).upper(), "NSE_EQ"
        )
        payload = {segment: [_as_token(instrument.broker_token)]}

        data = self.client.quote(payload)
        row = _first_quote(data, segment, instrument.broker_token)
        if not row:
            raise InstrumentNotFound(f"Dhan returned no quote for {symbol}", symbol=symbol)

        ohlc = row.get("ohlc") or {}
        return UnifiedQuote(
            symbol=symbol,
            last_price=float(row.get("last_price") or row.get("ltp") or 0.0),
            timestamp=datetime.now(),
            broker_token=instrument.broker_token,
            volume=_num(row.get("volume"), None),
            previous_close=_num(ohlc.get("close"), None),
        )

    def get_quotes(self, symbols: list[str]) -> dict[str, UnifiedQuote]:
        """One request for the whole list.

        The base class would loop and call `get_quote` per symbol, which on a
        1-request-per-second feed turns marking a 20-symbol book into a
        20-second stall. Dhan accepts many security ids per segment, so the
        batch is a single call.
        """
        segment = EXCHANGE_SEGMENTS.get(self._exchange.upper(), "NSE_EQ")
        tokens: dict[str, str] = {}
        for symbol in symbols:
            try:
                tokens[symbol] = self.resolve_instrument(symbol).broker_token
            except InstrumentNotFound:
                logger.debug("No Dhan instrument for %s", symbol)

        if not tokens:
            return {}

        data = self.client.quote({segment: [_as_token(t) for t in tokens.values()]})
        payload = (data.get("data", data) or {}).get(segment, {}) or {}

        quotes: dict[str, UnifiedQuote] = {}
        for symbol, token in tokens.items():
            row = payload.get(str(token))
            if not row:
                continue
            ohlc = row.get("ohlc") or {}
            quotes[symbol] = UnifiedQuote(
                symbol=symbol,
                last_price=float(row.get("last_price") or row.get("ltp") or 0.0),
                timestamp=datetime.now(),
                volume=_num(row.get("volume"), None),
                previous_close=_num(ohlc.get("close"), None),
            )
        return quotes

    # -- orders -------------------------------------------------------

    def place_order(self, order: UnifiedOrder) -> UnifiedOrder:
        instrument = self.resolve_instrument(order.symbol)
        payload = {
            "dhanClientId": self.client.client_id,
            "transactionType": _SIDE_TO_DHAN[order.side],
            "exchangeSegment": EXCHANGE_SEGMENTS.get(self._exchange.upper(), "NSE_EQ"),
            "productType": _PRODUCT_TO_DHAN.get(order.product, "INTRADAY"),
            "orderType": _ORDER_TYPE_TO_DHAN.get(order.order_type, "MARKET"),
            "validity": "DAY",
            "securityId": instrument.broker_token,
            "quantity": int(order.quantity),
            "price": float(order.limit_price or 0),
            "triggerPrice": float(order.stop_price or 0),
        }
        if order.tags.get("correlation_id"):
            payload["correlationId"] = str(order.tags["correlation_id"])[:25]

        data = self.client.place_order(payload)
        order_id = data.get("orderId") or data.get("orderid")
        if not order_id:
            raise OrderRejected(
                f"Dhan did not return an order id: {data}", broker=self.name
            )
        placed = order.copy_with(
            # Callers track orders by our id; the broker's is recorded
            # alongside it for reconciliation.
            order_id=order.order_id or str(order_id),
            broker_order_id=str(order_id),
            broker=self.name,
            status=_STATUS_FROM_DHAN.get(
                str(data.get("orderStatus", "")).upper(), OrderStatus.OPEN
            ),
        )

        # The place response carries an id and a status but not the fill. A
        # market order may already be complete, and a caller that reads
        # filled_quantity off the returned object must not see a stale zero —
        # so the broker's own view wins. A read-back failure is not fatal:
        # the order exists either way, and reporting it as placed is more
        # accurate than raising.
        try:
            confirmed = self.get_order_status(str(order_id))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not read back order %s: %s", order_id, exc)
            return placed
        return confirmed.copy_with(
            order_id=placed.order_id,
            strategy=order.strategy,
            tags=order.tags,
        )

    def modify_order(
        self,
        order_id: str,
        *,
        quantity: float | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> UnifiedOrder:
        current = self.get_order_status(order_id)
        payload = {
            "dhanClientId": self.client.client_id,
            "orderId": str(order_id),
            "orderType": _ORDER_TYPE_TO_DHAN.get(current.order_type, "LIMIT"),
            "validity": "DAY",
            "quantity": int(quantity if quantity is not None else current.quantity),
            "price": float(limit_price if limit_price is not None else (current.limit_price or 0)),
        }
        if stop_price is not None:
            payload["triggerPrice"] = float(stop_price)
        self.client.modify_order(str(order_id), payload)
        return self.get_order_status(order_id)

    def cancel_order(self, order_id: str) -> UnifiedOrder:
        with _as_order_rejected(order_id, self.name):
            self.client.cancel_order(str(order_id))
        return self.get_order_status(order_id)

    def get_order_status(self, order_id: str) -> UnifiedOrder:
        with _as_order_rejected(order_id, self.name):
            raw = self.client.order(str(order_id))
        if not raw:
            raise OrderRejected(f"unknown order {order_id}", broker=self.name)
        return _to_order(raw, self.name)

    def get_orders(self) -> list[UnifiedOrder]:
        return [_to_order(r, self.name) for r in self.client.orders()]

    def get_fills(self) -> list[Fill]:
        fills = []
        for row in self.client.trades():
            fills.append(
                Fill(
                    order_id=str(row.get("orderId", "")),
                    symbol=row.get("tradingSymbol") or row.get("customSymbol") or "",
                    side=Side.BUY
                    if str(row.get("transactionType", "")).upper() == "BUY"
                    else Side.SELL,
                    quantity=abs(float(_num(row.get("tradedQuantity"), 0))),
                    price=float(_num(row.get("tradedPrice"), 0)),
                    timestamp=_parse_time(row.get("exchangeTime") or row.get("createTime")),
                )
            )
        return fills

    # -- portfolio ----------------------------------------------------

    def get_positions(self) -> list[UnifiedPosition]:
        positions = []
        for row in self.client.positions():
            quantity = float(_num(row.get("netQty"), 0))
            if quantity == 0:
                continue
            positions.append(
                UnifiedPosition(
                    symbol=row.get("tradingSymbol") or row.get("customSymbol") or "",
                    quantity=quantity,
                    average_price=float(_num(row.get("costPrice") or row.get("buyAvg"), 0)),
                    broker=self.name,
                    product=_product_from_dhan(row.get("productType")),
                    last_price=_num(row.get("lastTradedPrice"), None),
                    realized_pnl=float(_num(row.get("realizedProfit"), 0)),
                )
            )
        return positions

    def get_history(self, symbol: str, start, end, interval: str = "day"):
        """Daily bars in the platform's canonical frame.

        Dhan identifies contracts numerically and answers with parallel
        arrays keyed by field, rather than the row-per-bar shape every other
        source here uses. Both differences are absorbed so callers see the
        same frame they would get from any other provider.
        """
        import pandas as pd

        instrument = self.resolve_instrument(symbol)
        payload = {
            "securityId": str(instrument.broker_token),
            "exchangeSegment": EXCHANGE_SEGMENTS.get(self._exchange.upper(), "NSE_EQ"),
            "instrument": "EQUITY",
            "expiryCode": 0,
            "fromDate": _as_date_str(start),
            "toDate": _as_date_str(end),
        }
        data = self.client.historical(payload)
        return _arrays_to_frame(data)

    # -- GTT (Dhan "Forever Orders") ----------------------------------

    def get_gtts(self) -> list[dict]:
        """Standing triggers, in Kite's GTT vocabulary.

        The legacy monitors read `condition.tradingsymbol` and `status`, so
        Dhan's flat record is reshaped rather than passed through — a monitor
        that cannot find the symbol treats the position as unprotected.
        """
        out = []
        for row in self.client.forever_orders():
            out.append(
                {
                    "id": row.get("orderId"),
                    "status": str(row.get("orderStatus", "")).lower(),
                    "condition": {
                        "exchange": row.get("exchangeSegment", ""),
                        "tradingsymbol": row.get("tradingSymbol")
                        or row.get("customSymbol")
                        or "",
                        "trigger_values": [_num(row.get("triggerPrice"), 0.0)],
                    },
                    "orders": [
                        {
                            "transaction_type": str(row.get("transactionType", "")).upper(),
                            "quantity": int(_num(row.get("quantity"), 0)),
                            "price": _num(row.get("price"), 0.0),
                            "product": str(row.get("productType", "")),
                        }
                    ],
                }
            )
        return out

    def place_gtt(
        self,
        *,
        symbol: str,
        side: str,
        quantity: int,
        trigger_price: float,
        limit_price: float | None = None,
        product: str = "CNC",
        **_ignored,
    ) -> str:
        instrument = self.resolve_instrument(symbol)
        payload = {
            "dhanClientId": self.client.client_id,
            "orderFlag": "SINGLE",
            "transactionType": "BUY" if str(side).upper() == "BUY" else "SELL",
            "exchangeSegment": EXCHANGE_SEGMENTS.get(self._exchange.upper(), "NSE_EQ"),
            "productType": _PRODUCT_TO_DHAN.get(
                {"CNC": ProductType.DELIVERY, "MIS": ProductType.INTRADAY}.get(
                    str(product).upper(), ProductType.DELIVERY
                ),
                "CNC",
            ),
            "orderType": "LIMIT" if limit_price else "MARKET",
            "validity": "DAY",
            "securityId": instrument.broker_token,
            "quantity": int(quantity),
            "price": float(limit_price or trigger_price),
            "triggerPrice": float(trigger_price),
        }
        data = self.client.place_forever(payload)
        order_id = data.get("orderId")
        if not order_id:
            raise OrderRejected(f"Dhan rejected the GTT: {data}", broker=self.name)
        return str(order_id)

    def modify_gtt(self, gtt_id: str, **changes) -> str:
        payload = {"dhanClientId": self.client.client_id, "orderId": str(gtt_id)}
        if "trigger_price" in changes:
            payload["triggerPrice"] = float(changes["trigger_price"])
        if "quantity" in changes:
            payload["quantity"] = int(changes["quantity"])
        if "limit_price" in changes:
            payload["price"] = float(changes["limit_price"])
        self.client.modify_forever(str(gtt_id), payload)
        return str(gtt_id)

    def delete_gtt(self, gtt_id: str) -> str:
        self.client.cancel_forever(str(gtt_id))
        return str(gtt_id)

    def get_account(self) -> AccountSnapshot:
        funds = self.client.fund_limit()
        cash = float(_num(funds.get("availabelBalance") or funds.get("availableBalance"), 0))
        utilized = float(_num(funds.get("utilizedAmount"), 0))
        positions = self.get_positions()
        return AccountSnapshot(
            broker=self.name,
            cash=cash,
            equity=cash + utilized,
            realized_pnl=sum(p.realized_pnl for p in positions),
            unrealized_pnl=sum(p.unrealized_pnl for p in positions),
            positions=positions,
            timestamp=datetime.now(),
        )


# ==========================================================================
# TRANSLATION
# ==========================================================================


@contextmanager
def _as_order_rejected(order_id: str, broker: str):
    """Translate a broker-side 'no such order' into the platform's own
    exception, so callers can branch on it uniformly."""
    try:
        yield
    except OrderRejected:
        raise
    except BrokerError as exc:
        text = str(exc).lower()
        if "not found" in text or "invalid" in text or "does not exist" in text:
            raise OrderRejected(f"unknown order {order_id}", broker=broker) from exc
        raise


def _as_date_str(value) -> str:
    """Dhan expects plain YYYY-MM-DD."""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def _arrays_to_frame(data: dict):
    """Turn Dhan's column-arrays response into a canonical OHLCV frame.

    An empty or partial response yields an empty frame rather than a
    misaligned one: zipping arrays of unequal length would silently pair the
    wrong price with the wrong day.
    """
    import pandas as pd

    payload = data.get("data", data) if isinstance(data, dict) else {}
    required = ("open", "high", "low", "close", "timestamp")
    if not all(k in payload for k in required):
        logger.warning("Dhan historical response missing fields: %s", sorted(payload)[:8])
        return pd.DataFrame()

    lengths = {len(payload[k]) for k in required}
    if len(lengths) != 1 or lengths == {0}:
        logger.warning("Dhan historical arrays are ragged (%s); discarding", lengths)
        return pd.DataFrame()

    index = pd.to_datetime(payload["timestamp"], unit="s", utc=True)
    # Exchange-local calendar dates, then tz-naive to match every other source.
    index = index.tz_convert("Asia/Kolkata").tz_localize(None).normalize()

    frame = pd.DataFrame(
        {
            "Open": pd.to_numeric(payload["open"], errors="coerce"),
            "High": pd.to_numeric(payload["high"], errors="coerce"),
            "Low": pd.to_numeric(payload["low"], errors="coerce"),
            "Close": pd.to_numeric(payload["close"], errors="coerce"),
            "Volume": pd.to_numeric(payload.get("volume", [0] * len(index)), errors="coerce"),
        },
        index=index,
    )
    # Dhan EOD is already corporate-action adjusted, so the factor is 1.
    frame["Adj Close"] = frame["Close"]
    frame = frame[["Open", "High", "Low", "Close", "Adj Close", "Volume"]]
    return frame.dropna(subset=["Open", "High", "Low", "Close"]).sort_index()


def build_dhan_downloader(adapter):
    """A `(symbol, start, end) -> DataFrame` callable for the data loader."""

    def download(symbol: str, start, end):
        return adapter.get_history(symbol, start, end)

    return download


def _as_token(value):
    """Dhan keys the market feed by numeric securityId.

    A non-numeric token means resolution returned something unexpected;
    passing it through unchanged is better than crashing on int(), and the
    feed will simply not match it.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _first_quote(data: dict, segment: str, token: str) -> dict:
    """Dig the one row out of Dhan's nested market-feed response."""
    payload = data.get("data", data)
    if not isinstance(payload, dict):
        return {}
    by_segment = payload.get(segment)
    if isinstance(by_segment, dict):
        return by_segment.get(str(token)) or next(iter(by_segment.values()), {}) or {}
    return {}


def _parse_time(value) -> datetime:
    if not value:
        return datetime.now()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    return datetime.now()


def _product_from_dhan(value) -> ProductType:
    return {
        "INTRADAY": ProductType.INTRADAY,
        "CNC": ProductType.DELIVERY,
        "MARGIN": ProductType.CARRYFORWARD,
        "CO": ProductType.COVER,
    }.get(str(value).upper(), ProductType.INTRADAY)


def _to_order(row: dict, broker: str) -> UnifiedOrder:
    order_type = {
        "MARKET": OrderType.MARKET,
        "LIMIT": OrderType.LIMIT,
        "STOP_LOSS": OrderType.STOP_LIMIT,
        "STOP_LOSS_MARKET": OrderType.STOP,
    }.get(str(row.get("orderType", "")).upper(), OrderType.MARKET)

    price = _num(row.get("price"), None)
    return UnifiedOrder(
        symbol=row.get("tradingSymbol") or row.get("customSymbol") or "",
        side=Side.BUY if str(row.get("transactionType", "")).upper() == "BUY" else Side.SELL,
        quantity=abs(float(_num(row.get("quantity"), 0))),
        order_type=order_type,
        # A LIMIT order is required to carry its price; Dhan omits it on some
        # rows, so fall back to the traded price rather than failing to build
        # a record of an order that genuinely exists.
        limit_price=price if order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and price
        else (price if price else None),
        stop_price=_num(row.get("triggerPrice"), None) or None,
        product=_product_from_dhan(row.get("productType")),
        # Dhan issues one id and the platform tracks orders by `order_id`,
        # so both carry it — reading an order back must produce the same id
        # placing it returned.
        order_id=str(row.get("orderId", "")),
        broker_order_id=str(row.get("orderId", "")),
        broker=broker,
        status=_STATUS_FROM_DHAN.get(str(row.get("orderStatus", "")).upper(), OrderStatus.PENDING),
        filled_quantity=float(_num(row.get("filledQty") or row.get("filled_qty"), 0)),
        average_price=_num(row.get("averageTradedPrice"), None) or 0.0,
        status_message=row.get("omsErrorDescription") or "",
        created_at=_parse_time(row.get("createTime")),
    )
