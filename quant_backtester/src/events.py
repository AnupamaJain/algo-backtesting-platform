"""Corporate-event data: earnings announcements and their surprises.

Everything else in this platform derives its signals from price alone. Those
signals have been mined flat — the null test showed a search of this size
produces a 1.7 Sharpe by luck, and nothing here beat it. An event-driven
signal is a different proposition: post-earnings announcement drift has a
documented cause (investors under-react to earnings news and the price
adjusts over subsequent weeks), a forty-year literature behind it, and it is
not something you can find by sweeping a moving-average length.

This module supplies the raw material: when each company reported, and by
how much it beat or missed.

THE LOOK-AHEAD TRAP, which is specific and severe here. An earnings
announcement carries a *timestamp*, not just a date, and most US companies
report after the close. A backtest that trades the announcement date itself
is buying at a price that already reflects news released hours later — it
will show a spectacular, entirely fictitious edge. `first_tradeable_session`
exists to make that mistake impossible: it always returns a session strictly
after the news was public.

Estimates are also revised after the fact. `Surprise(%)` as served today is
the final revised figure, which was not necessarily knowable on the day. That
is a real limitation of this data source and is documented rather than
papered over — see `KNOWN_LIMITATIONS`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "EarningsEvent",
    "EarningsCalendar",
    "first_tradeable_session",
    "KNOWN_LIMITATIONS",
]

#: Stated plainly so nobody reads a result off this data without knowing.
KNOWN_LIMITATIONS = (
    "Surprise(%) is the vendor's current figure and may reflect estimate "
    "revisions made after the announcement; a true point-in-time consensus "
    "would be preferable. The universe is also survivor-biased — it contains "
    "companies that still trade today. Both effects flatter a PEAD result, so "
    "treat the magnitude as an upper bound.",
)

#: US equities report either before the open or after the close. A timestamp
#: at or after this hour (exchange local) is treated as after-close.
AFTER_CLOSE_HOUR = 16
BEFORE_OPEN_HOUR = 9


@dataclass(frozen=True)
class EarningsEvent:
    """One announcement, and when it could first have been traded."""

    symbol: str
    announced_at: pd.Timestamp
    eps_estimate: float | None
    eps_reported: float | None
    surprise_pct: float | None

    @property
    def announced_after_close(self) -> bool:
        hour = self.announced_at.hour
        # A midnight timestamp carries no time information; assume the worst
        # (after close) so the entry is pushed a day later rather than earlier.
        if hour == 0 and self.announced_at.minute == 0:
            return True
        return hour >= AFTER_CLOSE_HOUR

    @property
    def has_surprise(self) -> bool:
        return self.surprise_pct is not None and self.surprise_pct == self.surprise_pct


def first_tradeable_session(
    event: EarningsEvent, sessions: pd.DatetimeIndex
) -> pd.Timestamp | None:
    """The first session whose CLOSE can be traded on this news.

    After-close announcements cannot be acted on until the following session.
    Before-open announcements could in principle be traded that same day, but
    this returns the next session for those too: the exact minute is not
    reliable in vendor data, and buying the session that contains the
    announcement is the single most common way a PEAD backtest invents an
    edge it does not have.
    """
    if len(sessions) == 0:
        return None

    announced = event.announced_at
    # Compare on calendar date; sessions are tz-naive daily bars.
    announced_date = announced.date() if hasattr(announced, "date") else announced
    later = sessions[sessions.date > announced_date]
    return later[0] if len(later) else None


class EarningsCalendar:
    """Fetches and caches earnings announcements per symbol.

    Cache-first with the same contract as the price loader: a network failure
    falls back to what is on disk rather than silently producing an empty
    calendar, because an empty calendar looks exactly like "this stock never
    reported" and would quietly drop the symbol from the study.
    """

    def __init__(self, cache_dir: Path, fetcher=None, max_age_days: int = 7) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._fetcher = fetcher or _yfinance_earnings
        self._max_age_days = max_age_days

    def get(self, symbol: str, force_refresh: bool = False) -> list[EarningsEvent]:
        path = self._cache_path(symbol)
        cached = self._read(path, symbol)

        if not force_refresh and cached and self._is_fresh(path):
            return cached

        try:
            rows = self._fetcher(symbol)
        except Exception as exc:  # noqa: BLE001 - a stale calendar beats none
            if cached:
                logger.warning("Earnings fetch failed for %s (%s); using cache", symbol, exc)
                return cached
            logger.error("No earnings data for %s: %s", symbol, exc)
            return []

        events = _to_events(symbol, rows)
        if not events and cached:
            logger.warning("Earnings fetch for %s returned nothing; keeping cache", symbol)
            return cached

        self._write(path, events)
        logger.info("Fetched %s earnings events for %s", len(events), symbol)
        return events

    def get_many(self, symbols: list[str], force_refresh: bool = False) -> dict:
        out = {}
        for symbol in symbols:
            events = self.get(symbol, force_refresh=force_refresh)
            if events:
                out[symbol] = events
        return out

    # -- internals ------------------------------------------------------

    def _cache_path(self, symbol: str) -> Path:
        return self._cache_dir / f"{symbol.replace('/', '_')}_earnings.json"

    def _is_fresh(self, path: Path) -> bool:
        if not path.exists():
            return False
        age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
        return age.days < self._max_age_days

    def _read(self, path: Path, symbol: str) -> list[EarningsEvent]:
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unreadable earnings cache %s: %s", path, exc)
            return []
        return [
            EarningsEvent(
                symbol=symbol,
                announced_at=pd.Timestamp(r["announced_at"]),
                eps_estimate=r.get("eps_estimate"),
                eps_reported=r.get("eps_reported"),
                surprise_pct=r.get("surprise_pct"),
            )
            for r in raw
        ]

    def _write(self, path: Path, events: list[EarningsEvent]) -> None:
        path.write_text(
            json.dumps(
                [
                    {
                        "announced_at": e.announced_at.isoformat(),
                        "eps_estimate": e.eps_estimate,
                        "eps_reported": e.eps_reported,
                        "surprise_pct": e.surprise_pct,
                    }
                    for e in events
                ],
                indent=1,
            )
        )


def _to_events(symbol: str, frame) -> list[EarningsEvent]:
    """Normalize the vendor frame, dropping anything unusable."""
    if frame is None or len(frame) == 0:
        return []

    events = []
    for stamp, row in frame.iterrows():
        try:
            when = pd.Timestamp(stamp)
        except Exception:  # noqa: BLE001
            continue
        events.append(
            EarningsEvent(
                symbol=symbol,
                announced_at=when,
                eps_estimate=_num(row.get("EPS Estimate")),
                eps_reported=_num(row.get("Reported EPS")),
                surprise_pct=_num(row.get("Surprise(%)")),
            )
        )
    # Oldest first, so downstream code can walk forward in time.
    return sorted(events, key=lambda e: e.announced_at)


def _num(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None  # reject NaN


def _yfinance_earnings(symbol: str):
    import yfinance as yf

    # The vendor caps this at 100; asking for more is rejected outright
    # rather than truncated, which silently produced an empty calendar.
    return yf.Ticker(symbol).get_earnings_dates(limit=100)
