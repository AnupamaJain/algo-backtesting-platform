"""Live IB-60 intraday trading.

`intraday.py` builds the Initial Balance from history and `ib_backtest.py`
scores it. This module runs the same logic against *today's* session and
routes the resulting trade to a broker.

The setup, unchanged from the backtest so live and historical results stay
comparable:

    1. build the IB from the first 60 minutes
    2. wait for the first break of either extreme
    3. arm the configured entry, and fill when price reaches it
    4. exit on the stop, the target, or the session close

WHAT THE RESEARCH SAYS ABOUT THIS STRATEGY. The path-dependent backtest found
no tradable edge: expectancy −0.001 R per trade over 32 fills, t = −0.01, on
81 sessions of NIFTY and BANKNIFTY. Every apparent edge in the underlying
event study turned out to be the overnight announcement gap, which is not
tradable. This module therefore defaults to `paper` and says so on every run.
It exists because a strategy that cannot be run cannot be observed forward —
not because the evidence supports trading it.

STATE LIVES ON DISK. A session's IB, its break and any open position are
persisted per instrument per day. An intraday strategy that forgets its
position on restart will re-enter one it already holds, and at 10:20 that is
a doubled position rather than a fresh one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from .ib_backtest import IBTradeConfig, _levels
from .intraday import (
    IntradayConfig,
    IntradayDataManager,
    analyse_session,
    build_initial_balance,
    first_break,
    split_sessions,
)

logger = logging.getLogger(__name__)

__all__ = ["SessionState", "OrbisLiveEngine"]


@dataclass
class SessionState:
    """What this instrument has done today."""

    instrument: str
    session_date: str
    ib_high: float | None = None
    ib_low: float | None = None
    formed_first: str | None = None
    broke_first: str | None = None
    #: armed | filled | closed | none
    phase: str = "none"
    direction: str | None = None
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    fill_price: float | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    order_id: str | None = None
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def is_open(self) -> bool:
        return self.phase == "filled"


class StateStore:
    """One JSON file per instrument per session."""

    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, instrument: str, session: date) -> Path:
        safe = instrument.replace("/", "_").replace(" ", "_")
        return self._dir / f"orbis_{safe}_{session:%Y-%m-%d}.json"

    def load(self, instrument: str, session: date) -> SessionState:
        path = self._path(instrument, session)
        if path.exists():
            try:
                return SessionState(**json.loads(path.read_text()))
            except Exception as exc:  # noqa: BLE001
                # Losing state silently would re-enter a held position.
                logger.error("Unreadable ORBIS state %s: %s", path, exc)
                raise
        return SessionState(instrument=instrument, session_date=session.isoformat())

    def save(self, state: SessionState) -> None:
        state.updated_at = datetime.now().isoformat(timespec="seconds")
        session = date.fromisoformat(state.session_date)
        self._path(state.instrument, session).write_text(
            json.dumps(asdict(state), indent=1)
        )


class OrbisLiveEngine:
    """Runs the IB-60 setup against today's bars and places the trade."""

    def __init__(
        self,
        config: IntradayConfig,
        trade_config: IBTradeConfig,
        data: IntradayDataManager,
        service=None,
        state_dir: Path | None = None,
    ) -> None:
        self._config = config
        self._trade = trade_config
        self._data = data
        self._service = service
        self._store = StateStore(state_dir or (Path("quant_backtester") / "state" / "orbis"))

    def run(self, instrument: str, session: date | None = None, dry_run: bool = True) -> dict:
        """One pass for one instrument. Safe to call repeatedly."""
        session = session or date.today()
        state = self._store.load(instrument, session)

        bars = self._data.get_bars(instrument, session, session, force_refresh=True)
        if bars.empty:
            return self._report(state, "no bars for this session yet")

        sessions = split_sessions(bars)
        if not sessions:
            return self._report(state, "no session data")
        today = sessions[-1]

        # The IB is only final once the window has fully elapsed. Live, at
        # 09:45, `build_initial_balance` would happily return a HALF-formed
        # balance from the bars so far — and arming on that is trading a
        # level that is still moving.
        window_end = pd.Timestamp(
            datetime.combine(session, self._config.session_open), tz=today.index.tz
        ) + pd.Timedelta(minutes=self._config.ib_minutes)
        if today.index[-1] < window_end:
            remaining = (window_end - today.index[-1]).total_seconds() / 60
            return self._report(
                state, f"IB window has not completed ({remaining:.0f} min remaining)"
            )

        ib = build_initial_balance(today, self._config.ib_minutes, self._config.session_open)
        if ib is None:
            return self._report(state, "no bars inside the IB window")
        state.ib_high, state.ib_low = ib.high, ib.low
        state.formed_first = ib.formed_first

        outcome = first_break(
            today, ib, self._config.session_open, self._config.ib_minutes,
            self._config.break_measure,
        )
        state.broke_first = outcome.side
        if not outcome.is_resolved:
            self._store.save(state)
            reason = "no break yet" if outcome.side is None else "break was ambiguous"
            return self._report(state, reason)

        # Arm on the first resolved break.
        if state.phase == "none":
            direction, entry, stop, target = _levels(ib, outcome.side, self._trade)
            state.direction, state.entry = direction, entry
            state.stop, state.target = stop, target
            state.phase = "armed"
            logger.info(
                "%s armed %s: entry %.2f stop %.2f target %.2f",
                instrument, direction, entry, stop, target,
            )

        # Only bars strictly after the break can fill or manage the trade.
        after = today.loc[today.index > outcome.time]
        if after.empty:
            self._store.save(state)
            return self._report(state, "armed; waiting for the next bar")

        if state.phase == "armed":
            self._try_fill(state, after, dry_run)
        if state.phase == "filled":
            self._manage(state, after, dry_run)

        self._store.save(state)
        return self._report(state, "")

    # -- execution -----------------------------------------------------

    def _try_fill(self, state: SessionState, bars: pd.DataFrame, dry_run: bool) -> None:
        """Fill when a bar's range contains the entry.

        The containment test, not `high >= entry`: a pullback entry sits
        BELOW the current price, and testing only one side fills instantly
        without any pullback having happened.
        """
        cutoff = self._trade.entry_cutoff
        for stamp, bar in bars.iterrows():
            if stamp.time() > cutoff:
                state.phase = "closed"
                state.exit_reason = "CUTOFF"
                return
            if bar["Low"] <= state.entry <= bar["High"]:
                state.fill_price = state.entry
                state.phase = "filled"
                logger.warning(
                    "%s FILLED %s %.2f", state.instrument, state.direction, state.entry
                )
                state.order_id = self._place(state, opening=True, dry_run=dry_run)
                return

    def _manage(self, state: SessionState, bars: pd.DataFrame, dry_run: bool) -> None:
        """Walk forward to the stop, the target, or the close."""
        long = state.direction == "LONG"
        flat_by = self._trade.exit_at
        for stamp, bar in bars.iterrows():
            hit_stop = bar["Low"] <= state.stop if long else bar["High"] >= state.stop
            hit_target = bar["High"] >= state.target if long else bar["Low"] <= state.target

            if hit_stop and hit_target:
                # Unknowable from OHLC; take the loss rather than the gain.
                hit_target = False
            if hit_stop:
                return self._close(state, "STOP", state.stop, dry_run)
            if hit_target:
                return self._close(state, "TARGET", state.target, dry_run)
            if stamp.time() >= flat_by:
                return self._close(state, "TIME", float(bar["Close"]), dry_run)

    def _close(self, state: SessionState, reason: str, price: float, dry_run: bool) -> None:
        state.exit_price, state.exit_reason, state.phase = price, reason, "closed"
        logger.warning("%s EXIT %s at %.2f", state.instrument, reason, price)
        self._place(state, opening=False, dry_run=dry_run)

    def _place(self, state: SessionState, *, opening: bool, dry_run: bool) -> str | None:
        if dry_run or self._service is None:
            return None
        from .broker.models import OrderType, Side, UnifiedOrder

        long = state.direction == "LONG"
        # Closing reverses the opening side.
        buy = long if opening else not long
        order = UnifiedOrder(
            symbol=state.instrument,
            side=Side.BUY if buy else Side.SELL,
            quantity=1,
            order_type=OrderType.MARKET,
            strategy="ORBIS-IB60",
            tags={"phase": "open" if opening else "close"},
        )
        try:
            placed = self._service.place_order(
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                order_type=order.order_type,
                strategy=order.strategy or "ORBIS-IB60",
            )
            return placed.broker_order_id or placed.order_id
        except Exception as exc:  # noqa: BLE001 - a rejected order must not lose state
            logger.error("ORBIS order failed for %s: %s", state.instrument, exc)
            return None

    def _report(self, state: SessionState, note: str) -> dict:
        payload = asdict(state)
        payload["note"] = note
        payload["evidence"] = (
            "Backtest found no edge: −0.001 R/trade, t = −0.01 over 32 fills. "
            "Run this to observe forward, not because the research supports it."
        )
        return payload
