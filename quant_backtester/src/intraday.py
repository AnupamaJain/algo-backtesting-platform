"""Intraday session bars and Initial Balance (IB) structure.

Everything else in this platform works on daily bars. Session-structure
strategies cannot: an Initial Balance is defined by the first N minutes of a
trading day, so it needs intraday candles and an explicit notion of "session".
This module is that layer, and it is deliberately separate — its own cache
directory, its own config — so intraday and daily data can never be silently
mistaken for each other.

What it provides:

    * `IntradayDataManager`  — fetch/cache 5-minute bars from the broker
    * `build_initial_balance` — the IB high/low and which side formed first
    * `first_break`          — which IB extreme was exceeded first, after the
                               window closed
    * `run_first_break_study` — the conditional frequency, with a CI

THE ORDERING PROBLEM. The interesting question ("when the IB high forms
first, does the low break first?") is a question about the ORDER of two
events. An OHLC bar records four prices but not the path between them, so
when a single bar's high exceeds the IB high AND its low undercuts the IB
low, the order is genuinely unknowable at this resolution. Guessing — or
silently defaulting to one side — manufactures the very statistic the study
is trying to measure. Such bars are counted as `ambiguous` and excluded from
the frequency, and the count is reported so the reader can judge whether the
exclusion matters. Same for the IB window itself: if the IB high and low are
set by the same bar, "which formed first" is undefined, not a coin flip.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "IntradayConfig",
    "IntradayDataManager",
    "InitialBalance",
    "BreakOutcome",
    "SessionOutcome",
    "build_initial_balance",
    "first_break",
    "analyse_session",
    "run_first_break_study",
    "wilson_interval",
]

BAR_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]

#: Side labels, used for both "formed first" and "broke first".
HIGH, LOW = "HIGH", "LOW"


# ==========================================================================
# CONFIG
# ==========================================================================


@dataclass(frozen=True)
class IntradayConfig:
    """Everything the intraday layer needs, loaded from intraday.yaml."""

    provider: str
    exchange: str
    #: Broker auth settings (token file, auto-login) — passed straight
    #: through to the adapter so credentials stay in config, never here.
    auth: dict
    interval: str
    cache_dir: Path
    max_days_per_request: int
    timezone: str
    session_open: time
    session_close: time
    ib_minutes: int
    break_measure: str
    straddle_policy: str
    instruments: list[dict]
    lookback_days: int
    confidence_level: float
    results_dir: Path

    @property
    def aliases(self) -> list[str]:
        return [i.get("alias", i["symbol"]) for i in self.instruments]

    def symbol_for(self, alias: str) -> str:
        for item in self.instruments:
            if item.get("alias", item["symbol"]) == alias:
                return item["symbol"]
        raise KeyError(f"no instrument configured with alias {alias!r}")


def load_intraday_config(path: str | Path) -> IntradayConfig:
    """Read intraday.yaml. Fails loudly on anything missing."""
    import yaml

    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    base_dir = path.resolve().parent.parent  # quant_backtester/
    src, sess, ib, study = raw["source"], raw["session"], raw["initial_balance"], raw["study"]

    measure = str(ib["break_measure"]).upper()
    if measure not in ("WICK", "CLOSE"):
        raise ValueError(f"break_measure must be WICK or CLOSE, got {measure!r}")
    policy = str(ib["straddle_policy"]).lower()
    if policy not in ("ambiguous", "skip"):
        raise ValueError(f"straddle_policy must be 'ambiguous' or 'skip', got {policy!r}")

    def _resolve(raw_path: str) -> Path:
        p = Path(raw_path)
        return p if p.is_absolute() else base_dir / p

    return IntradayConfig(
        provider=src["provider"],
        exchange=src["exchange"],
        auth=dict(src.get("auth", {})),
        interval=str(src["interval"]),
        cache_dir=_resolve(src["cache_dir"]),
        max_days_per_request=int(src["max_days_per_request"]),
        timezone=sess["timezone"],
        session_open=_parse_time(sess["open"]),
        session_close=_parse_time(sess["close"]),
        ib_minutes=int(ib["minutes"]),
        break_measure=measure,
        straddle_policy=policy,
        instruments=list(raw["instruments"]),
        lookback_days=int(study["lookback_days"]),
        confidence_level=float(study["confidence_level"]),
        results_dir=_resolve(study["results_dir"]),
    )


def _parse_time(value: str) -> time:
    hours, minutes = str(value).split(":")
    return time(int(hours), int(minutes))


# ==========================================================================
# DATA ACQUISITION
# ==========================================================================


class IntradayDataManager:
    """Fetches and caches intraday bars, one file per symbol+interval.

    Mirrors `HistoricalDataManager`'s contract (cache-first, graceful
    fallback, injected fetcher for tests) but never shares its cache
    directory — a daily file and a 5-minute file with the same name would be
    an extremely expensive mix-up.
    """

    def __init__(self, config: IntradayConfig, fetcher=None) -> None:
        self.config = config
        # Dependency inversion: tests inject a fake, so no test needs a token.
        self._fetcher = fetcher or _flattrade_fetcher(config)
        self.config.cache_dir.mkdir(parents=True, exist_ok=True)

    def get_bars(
        self,
        alias: str,
        start: date,
        end: date,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """Intraday bars for one instrument over [start, end], tz-aware."""
        path = self._cache_path(alias)
        cached = self._read_cache(path)

        if not force_refresh and cached is not None and _covers(cached, start, end):
            logger.info("Using cached intraday bars for %s (%s rows)", alias, len(cached))
            return _slice(cached, start, end, self.config.timezone)

        try:
            fresh = self._download(alias, start, end)
        except Exception as exc:  # noqa: BLE001 - fall back to cache below
            if cached is not None and not cached.empty:
                logger.warning(
                    "Intraday download failed for %s (%s); using cache", alias, exc
                )
                return _slice(cached, start, end, self.config.timezone)
            raise

        merged = fresh if cached is None else _merge(cached, fresh)
        self._write_cache(path, merged)
        logger.info("Fetched %s intraday bars for %s", len(fresh), alias)
        return _slice(merged, start, end, self.config.timezone)

    # -- internals ------------------------------------------------------

    def _download(self, alias: str, start: date, end: date) -> pd.DataFrame:
        """Pull in chunks, because the feed caps the span of one request."""
        symbol = self.config.symbol_for(alias)
        tz = self.config.timezone
        frames: list[pd.DataFrame] = []
        chunk = max(1, self.config.max_days_per_request)

        cursor = start
        while cursor <= end:
            stop = min(cursor + timedelta(days=chunk - 1), end)
            begin_ts = pd.Timestamp(
                datetime.combine(cursor, self.config.session_open), tz=tz
            )
            end_ts = pd.Timestamp(
                datetime.combine(stop, self.config.session_close), tz=tz
            )
            rows = self._fetcher(symbol, begin_ts, end_ts, self.config.interval)
            if rows:
                frames.append(normalize_bars(rows, tz))
            cursor = stop + timedelta(days=1)

        if not frames:
            return _empty_frame(tz)
        return _merge(*frames)

    def _cache_path(self, alias: str) -> Path:
        safe = alias.replace("/", "_").replace(" ", "_")
        return self.config.cache_dir / f"{safe}_{self.config.interval}m.csv"

    def _read_cache(self, path: Path) -> pd.DataFrame | None:
        if not path.exists():
            return None
        try:
            frame = pd.read_csv(path, index_col=0, parse_dates=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unreadable intraday cache %s: %s", path, exc)
            return None
        if frame.empty:
            return None
        frame.index = _to_tz(frame.index, self.config.timezone)
        return frame.sort_index()

    def _write_cache(self, path: Path, frame: pd.DataFrame) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(path)


def normalize_bars(rows: list[dict], tz: str) -> pd.DataFrame:
    """Convert Noren TPSeries records into the canonical bar frame.

    Noren returns newest-first records with stringly-typed prices and
    `dd-mm-YYYY HH:MM:SS` timestamps in exchange-local time.
    """
    records = []
    for row in rows:
        stamp = row.get("time") or row.get("ssboe")
        if stamp is None:
            continue
        try:
            when = (
                pd.to_datetime(int(stamp), unit="s", utc=True).tz_convert(tz)
                if str(stamp).isdigit()
                else pd.to_datetime(stamp, format="%d-%m-%Y %H:%M:%S").tz_localize(tz)
            )
        except Exception:  # noqa: BLE001 - a malformed row must not kill the pull
            logger.debug("Skipping intraday row with bad timestamp: %r", stamp)
            continue
        records.append(
            {
                "timestamp": when,
                "Open": _num(row.get("into")),
                "High": _num(row.get("inth")),
                "Low": _num(row.get("intl")),
                "Close": _num(row.get("intc")),
                "Volume": _num(row.get("intv") or row.get("v")),
            }
        )

    if not records:
        return _empty_frame(tz)

    frame = pd.DataFrame.from_records(records).set_index("timestamp").sort_index()
    frame = frame[~frame.index.duplicated(keep="last")]
    # A bar missing any price is unusable for high/low structure.
    return frame.dropna(subset=["Open", "High", "Low", "Close"])


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return np.nan


def _empty_frame(tz: str) -> pd.DataFrame:
    index = pd.DatetimeIndex([], tz=tz, name="timestamp")
    return pd.DataFrame(columns=BAR_COLUMNS, index=index, dtype=float)


def _to_tz(index: pd.DatetimeIndex, tz: str) -> pd.DatetimeIndex:
    index = pd.DatetimeIndex(index)
    return index.tz_localize(tz) if index.tz is None else index.tz_convert(tz)


def _merge(*frames: pd.DataFrame) -> pd.DataFrame:
    combined = pd.concat([f for f in frames if f is not None and not f.empty])
    if combined.empty:
        return combined
    combined = combined[~combined.index.duplicated(keep="last")]
    return combined.sort_index()


def _covers(frame: pd.DataFrame, start: date, end: date) -> bool:
    if frame.empty:
        return False
    return frame.index[0].date() <= start and frame.index[-1].date() >= end


def _slice(frame: pd.DataFrame, start: date, end: date, tz: str) -> pd.DataFrame:
    if frame.empty:
        return frame
    index = _to_tz(frame.index, tz)
    mask = (index.date >= start) & (index.date <= end)
    out = frame.loc[mask].copy()
    out.index = index[mask]
    return out


def _flattrade_fetcher(config: IntradayConfig):
    """Bind the broker client lazily — importing it must not require a token.

    The client is built once and reused: a multi-chunk pull would otherwise
    re-authenticate on every request, which is both slow and a good way to
    get rate-limited.
    """
    holder: dict = {}

    def client():
        if "c" not in holder:
            from .broker.flattrade import FlattradeAuth, FlattradeClient

            session = FlattradeAuth("flattrade", config.auth).authenticate()
            holder["c"] = FlattradeClient(
                token=session.access_token or "", client_id=session.user_id or ""
            )
        return holder["c"]

    def fetch(symbol: str, start: pd.Timestamp, end: pd.Timestamp, interval: str):
        return client().get_intraday_bars(
            config.exchange,
            symbol,
            start.to_pydatetime(),
            end.to_pydatetime(),
            interval=interval,
        )

    return fetch


# ==========================================================================
# INITIAL BALANCE STRUCTURE
# ==========================================================================


@dataclass(frozen=True)
class InitialBalance:
    """The first N minutes of a session, and how it was built."""

    session_date: date
    high: float
    low: float
    #: Which extreme was established first — or None when one bar set both,
    #: which makes the ordering undefined rather than 50/50.
    formed_first: str | None
    high_time: pd.Timestamp
    low_time: pd.Timestamp
    num_bars: int

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def mid(self) -> float:
        return self.low + 0.5 * self.range

    def level(self, fraction: float) -> float:
        """A retracement level, 0.0 = IB low, 1.0 = IB high."""
        return self.low + fraction * self.range


@dataclass(frozen=True)
class BreakOutcome:
    """Which IB extreme gave way first, after the IB window closed."""

    #: HIGH, LOW, None (never broke), or "AMBIGUOUS" (one bar breached both).
    side: str | None
    time: pd.Timestamp | None
    price: float | None

    @property
    def is_resolved(self) -> bool:
        return self.side in (HIGH, LOW)


@dataclass
class SessionOutcome:
    """One trading day, reduced to the facts the study needs."""

    session_date: date
    ib: InitialBalance | None
    brk: BreakOutcome | None
    note: str = ""
    extras: dict = field(default_factory=dict)


def build_initial_balance(
    session: pd.DataFrame, ib_minutes: int, session_open: time
) -> InitialBalance | None:
    """Construct the IB from the opening window of one session.

    Returns None when the session has no bars inside the window — a holiday,
    a late open, or a gap in the feed. Silently substituting the whole day's
    range would corrupt every downstream statistic.
    """
    if session.empty:
        return None

    day = session.index[0].date()
    tz = session.index.tz
    start = pd.Timestamp(datetime.combine(day, session_open), tz=tz)
    finish = start + pd.Timedelta(minutes=int(ib_minutes))

    # Left-closed, right-open: a bar stamped exactly at the boundary belongs
    # to the period it opens, not the one it closes.
    window = session.loc[(session.index >= start) & (session.index < finish)]
    if window.empty:
        return None

    high_time = window["High"].idxmax()
    low_time = window["Low"].idxmin()
    if high_time == low_time:
        # One bar set both extremes. The order inside it is unknowable.
        formed_first = None
    else:
        formed_first = HIGH if high_time < low_time else LOW

    return InitialBalance(
        session_date=day,
        high=float(window["High"].max()),
        low=float(window["Low"].min()),
        formed_first=formed_first,
        high_time=high_time,
        low_time=low_time,
        num_bars=len(window),
    )


def first_break(
    session: pd.DataFrame,
    ib: InitialBalance,
    session_open: time,
    ib_minutes: int,
    break_measure: str = "WICK",
) -> BreakOutcome:
    """Find which IB extreme is exceeded first after the IB window closes.

    `break_measure` selects what counts: WICK uses the bar's high/low (any
    trade beyond the level), CLOSE requires the bar to close beyond it.
    """
    tz = session.index.tz
    start = pd.Timestamp(datetime.combine(ib.session_date, session_open), tz=tz)
    after = session.loc[session.index >= start + pd.Timedelta(minutes=int(ib_minutes))]
    if after.empty:
        return BreakOutcome(side=None, time=None, price=None)

    if break_measure == "CLOSE":
        above = after["Close"] > ib.high
        below = after["Close"] < ib.low
    else:
        above = after["High"] > ib.high
        below = after["Low"] < ib.low

    first_up = above.idxmax() if above.any() else None
    first_down = below.idxmax() if below.any() else None

    if first_up is None and first_down is None:
        return BreakOutcome(side=None, time=None, price=None)
    if first_down is None:
        return BreakOutcome(HIGH, first_up, ib.high)
    if first_up is None:
        return BreakOutcome(LOW, first_down, ib.low)
    if first_up < first_down:
        return BreakOutcome(HIGH, first_up, ib.high)
    if first_down < first_up:
        return BreakOutcome(LOW, first_down, ib.low)

    # Same bar broke both ways. OHLC does not record which came first, and
    # guessing here would fabricate the study's headline number.
    return BreakOutcome("AMBIGUOUS", first_up, None)


def analyse_session(
    session: pd.DataFrame, config: IntradayConfig
) -> SessionOutcome:
    """Reduce one session to its IB and first-break facts."""
    if session.empty:
        return SessionOutcome(session_date=date.min, ib=None, brk=None, note="no bars")

    day = session.index[0].date()
    ib = build_initial_balance(session, config.ib_minutes, config.session_open)
    if ib is None:
        return SessionOutcome(day, None, None, note="no bars in IB window")
    if ib.range <= 0:
        return SessionOutcome(day, ib, None, note="degenerate IB (zero range)")

    outcome = first_break(
        session, ib, config.session_open, config.ib_minutes, config.break_measure
    )
    return SessionOutcome(day, ib, outcome)


def split_sessions(bars: pd.DataFrame) -> list[pd.DataFrame]:
    """Group a continuous bar frame into per-day sessions."""
    if bars.empty:
        return []
    return [group for _, group in bars.groupby(bars.index.date, sort=True)]


# ==========================================================================
# THE STUDY
# ==========================================================================


def wilson_interval(successes: int, trials: int, confidence: float = 0.95):
    """Wilson score interval for a binomial proportion.

    Wilson rather than the normal approximation because these samples are
    small — a few dozen sessions — and the naive interval misbehaves badly
    there, which is exactly where an unjustified edge would hide.
    """
    if trials == 0:
        return (float("nan"), float("nan"))
    from statistics import NormalDist

    z = NormalDist().inv_cdf(1 - (1 - confidence) / 2)
    phat = successes / trials
    denom = 1 + z**2 / trials
    centre = (phat + z**2 / (2 * trials)) / denom
    margin = z * np.sqrt(phat * (1 - phat) / trials + z**2 / (4 * trials**2)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def run_first_break_study(
    sessions: list[SessionOutcome], confidence: float = 0.95
) -> pd.DataFrame:
    """The conditional question: given which IB side formed first, which side
    breaks first?

    Only sessions where BOTH the formation order and the break order are
    unambiguous can answer it. Everything else is counted and reported rather
    than quietly dropped, because the excluded count is the reader's only way
    to judge how much the answer depends on what was thrown away.
    """
    rows = []
    for condition in (HIGH, LOW):
        pool = [
            s
            for s in sessions
            if s.ib is not None and s.ib.formed_first == condition and s.brk is not None
        ]
        resolved = [s for s in pool if s.brk.is_resolved]
        ambiguous = sum(1 for s in pool if s.brk.side == "AMBIGUOUS")
        no_break = sum(1 for s in pool if s.brk.side is None)

        broke_high = sum(1 for s in resolved if s.brk.side == HIGH)
        broke_low = len(resolved) - broke_high
        n = len(resolved)
        lo_ci, hi_ci = wilson_interval(broke_low, n, confidence)

        rows.append(
            {
                "formed_first": condition,
                "sessions": len(pool),
                "resolved": n,
                "ambiguous": ambiguous,
                "no_break": no_break,
                "broke_high": broke_high,
                "broke_low": broke_low,
                "pct_broke_low": round(100 * broke_low / n, 2) if n else np.nan,
                "ci_low": round(100 * lo_ci, 2) if n else np.nan,
                "ci_high": round(100 * hi_ci, 2) if n else np.nan,
                # A 50% baseline inside the interval means the data cannot
                # distinguish this "edge" from a coin flip.
                "beats_coinflip": bool(n and (lo_ci > 0.5 or hi_ci < 0.5)),
            }
        )
    return pd.DataFrame(rows)


def sessions_to_frame(sessions: list[SessionOutcome]) -> pd.DataFrame:
    """Flatten sessions for inspection and artifact writing."""
    rows = []
    for s in sessions:
        rows.append(
            {
                "date": s.session_date,
                "ib_high": s.ib.high if s.ib else np.nan,
                "ib_low": s.ib.low if s.ib else np.nan,
                "ib_range": s.ib.range if s.ib else np.nan,
                "ib_mid": s.ib.mid if s.ib else np.nan,
                "ib_bars": s.ib.num_bars if s.ib else 0,
                "formed_first": s.ib.formed_first if s.ib else None,
                "broke_first": s.brk.side if s.brk else None,
                "break_time": s.brk.time if s.brk else None,
                "note": s.note,
            }
        )
    return pd.DataFrame(rows)
