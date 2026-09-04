"""Live signal generation, position sizing and risk management.

Turns the research pipeline's output into actual orders:

    latest bars → strategy signal → risk gates → position size → order

Three properties this must get right, because each is a way to lose money
quietly:

1. **No look-ahead.** A signal is computed from the last CLOSED bar and acted
   on afterwards. Using a forming bar's price would produce a backtest that
   cannot be reproduced live — the single most common way a paper system
   flatters itself.

2. **Sizing is risk-based, not capital-based.** Equal dollar amounts give a
   volatile asset several times the risk of a calm one. Positions are sized so
   each contributes a comparable share of risk, using ATR as the volatility
   estimate.

3. **Gates are checked before sizing, not after.** A blocked trade must never
   reach the broker at a reduced size — "smaller" is not the same as "no".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime

import pandas as pd

from ..broker.models import Side, UnifiedOrder
from ..broker.service import OrderService
from ..strategies import atr, get_strategy_class

logger = logging.getLogger(__name__)


# ==========================================================================
# Signals
# ==========================================================================


def net_signals_by_symbol(signals: list) -> tuple[list, dict]:
    """Collapse per-configuration signals into one target per instrument.

    Several deployed configurations of the same strategy can fire on the same
    symbol on the same day and disagree — two Bollinger settings, one saying
    long and one saying short. Sizing them independently opens a long and a
    short in the same instrument: net exposure zero, two spreads paid, and
    twice the risk budget consumed for a position that does not exist.

    Directions are summed and the sign taken, so agreement reinforces and
    disagreement cancels. A cancelled instrument is reported rather than
    dropped quietly — configurations that contradict each other are a
    research finding, not routine.
    """
    from collections import defaultdict

    grouped = defaultdict(list)
    for signal in signals:
        grouped[signal.symbol].append(signal)

    netted = []
    conflicts: dict[str, str] = {}

    for symbol, group in grouped.items():
        live = [s.target for s in group if s.target]
        total = sum(s.target for s in group)
        net = (total > 0) - (total < 0)          # sign of the sum

        if len(set(live)) > 1:
            detail = ", ".join(
                f"{s.strategy}{s.params or ''}="
                f"{'LONG' if s.target > 0 else 'SHORT'}"
                for s in group if s.target
            )
            conflicts[symbol] = f"{detail} -> net {'FLAT' if net == 0 else net}"

        if net == 0:
            continue

        # Keep an agreeing signal as the representative so the resulting order
        # carries a real strategy name and parameter set.
        winner = next(s for s in group if s.target == net)
        netted.append(replace(winner, target=net))

    return netted, conflicts


@dataclass(frozen=True)
class Signal:
    """A strategy's target position for one symbol.

    `target` is a direction in {-1, 0, +1}, not a quantity: strategies decide
    direction, the sizer decides magnitude. Keeping those separate is what
    lets one risk policy govern every strategy.
    """

    symbol: str
    strategy: str
    target: int
    price: float
    as_of: datetime
    params: dict = field(default_factory=dict)

    @property
    def direction(self) -> str:
        return {1: "LONG", -1: "SHORT", 0: "FLAT"}[self.target]


class SignalGenerator:
    """Runs deployed strategies against the latest bars."""

    def __init__(self, min_bars: int = 260) -> None:
        # Long-lookback strategies (252-day momentum, volatility quantiles)
        # produce nothing without enough history; emitting a signal from a
        # half-warmed indicator is worse than emitting none.
        self._min_bars = min_bars

    def generate(
        self, symbol: str, strategy_name: str, params: dict, data: pd.DataFrame
    ) -> Signal | None:
        if len(data) < self._min_bars:
            logger.debug(
                "%s/%s: %s bars is below the %s needed to warm up",
                symbol, strategy_name, len(data), self._min_bars,
            )
            return None

        try:
            strategy = get_strategy_class(strategy_name)(**params)
            signals = strategy.generate_signals(data)
        except Exception as exc:  # noqa: BLE001 - one strategy must not stop the rest
            logger.error("Signal failed for %s/%s: %s", symbol, strategy_name, exc)
            return None

        if signals.empty:
            return None

        # The last row is the most recently CLOSED bar. Acting on it now is
        # the live equivalent of the backtester's one-bar execution delay.
        return Signal(
            symbol=symbol,
            strategy=strategy_name,
            target=int(signals.iloc[-1]),
            price=float(data["Close"].iloc[-1]),
            as_of=data.index[-1].to_pydatetime(),
            params=dict(params),
        )

    def generate_all(
        self, deployed: pd.DataFrame, price_data: dict[str, pd.DataFrame]
    ) -> list[Signal]:
        """Signals for every deployed configuration that has data."""
        signals: list[Signal] = []
        for _, row in deployed.iterrows():
            data = price_data.get(row["symbol"])
            if data is None:
                continue
            params = _parse_params(row.get("params"))
            signal = self.generate(row["symbol"], row["strategy"], params, data)
            if signal is not None:
                signals.append(signal)
        return signals


def _parse_params(raw) -> dict:
    """Params survive a CSV round-trip as a string; accept either form."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        import ast

        try:
            parsed = ast.literal_eval(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, SyntaxError):
            return {}
    return {}


# ==========================================================================
# Position sizing
# ==========================================================================


@dataclass(frozen=True)
class SizingPolicy:
    """How much capital a single position may command."""

    risk_per_trade: float = 0.01        # fraction of equity risked per position
    atr_period: int = 14
    atr_stop_multiple: float = 2.0      # distance to the notional stop, in ATRs
    max_position_pct: float = 0.20      # cap on any one position's notional
    min_quantity: float = 1.0
    allow_fractional: bool = False

    def describe(self) -> dict:
        return {
            "risk_per_trade": self.risk_per_trade,
            "atr_stop_multiple": self.atr_stop_multiple,
            "max_position_pct": self.max_position_pct,
        }


@dataclass(frozen=True)
class SizedOrder:
    """A signal converted into an executable quantity."""

    symbol: str
    strategy: str
    side: Side
    quantity: float
    price: float
    target_position: float
    current_position: float
    risk_amount: float
    stop_distance: float
    reason: str = ""


class PositionSizer:
    """Volatility-targeted sizing: equal risk, not equal capital.

    Quantity is derived from what a loss would cost, not from what a position
    would cost:

        quantity = (equity x risk_per_trade) / (ATR x stop_multiple)

    A stock whose ATR is twice another's gets half the shares, so both put a
    similar amount at risk. Sizing by equal dollar amounts instead would hand
    the most volatile name several times the risk of the calmest one.
    """

    def __init__(self, policy: SizingPolicy | None = None) -> None:
        self._policy = policy or SizingPolicy()

    @property
    def policy(self) -> SizingPolicy:
        return self._policy

    def stop_distance(self, data: pd.DataFrame) -> float | None:
        """Per-unit risk: the ATR-based distance to a notional stop."""
        atr_values = atr(data["High"], data["Low"], data["Close"], self._policy.atr_period)
        if atr_values.empty:
            return None
        latest = atr_values.iloc[-1]
        if pd.isna(latest) or latest <= 0:
            return None
        return float(latest) * self._policy.atr_stop_multiple

    def size(
        self,
        signal: Signal,
        equity: float,
        data: pd.DataFrame,
        current_position: float = 0.0,
    ) -> SizedOrder | None:
        """Convert a signal into an order, or None when nothing should trade."""
        if equity <= 0:
            return None

        stop = self.stop_distance(data)
        if stop is None:
            logger.debug("%s: no usable ATR, cannot size", signal.symbol)
            return None

        risk_amount = equity * self._policy.risk_per_trade
        raw_quantity = risk_amount / stop

        # Independently of risk, no single position may dominate the account.
        notional_cap = equity * self._policy.max_position_pct
        if signal.price > 0:
            raw_quantity = min(raw_quantity, notional_cap / signal.price)

        if not self._policy.allow_fractional:
            raw_quantity = float(int(raw_quantity))

        target = raw_quantity * signal.target
        delta = target - current_position

        # Ignore trivial adjustments: churning a position by a share or two
        # pays commission and slippage for no change in risk.
        if abs(delta) < self._policy.min_quantity:
            return None

        return SizedOrder(
            symbol=signal.symbol,
            strategy=signal.strategy,
            side=Side.BUY if delta > 0 else Side.SELL,
            quantity=abs(delta),
            price=signal.price,
            target_position=target,
            current_position=current_position,
            risk_amount=risk_amount,
            stop_distance=stop,
        )


# ==========================================================================
# Risk management
# ==========================================================================


@dataclass(frozen=True)
class RiskLimits:
    """Account-level limits applied before any order is sent."""

    max_gross_exposure: float = 2.0     # multiple of equity, long + short
    max_positions: int = 10
    max_position_pct: float = 0.20
    max_daily_loss_pct: float = 0.05    # halt for the day past this drawdown
    min_cash_buffer_pct: float = 0.05   # never deploy the last of the cash
    allow_short: bool = True

    def describe(self) -> dict:
        return {
            "max_gross_exposure": self.max_gross_exposure,
            "max_positions": self.max_positions,
            "max_position_pct": self.max_position_pct,
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "allow_short": self.allow_short,
        }


@dataclass
class RiskDecision:
    """Whether an order may proceed, and why not when it may not."""

    approved: bool
    order: SizedOrder
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "symbol": self.order.symbol,
            "strategy": self.order.strategy,
            "side": self.order.side.value,
            "quantity": self.order.quantity,
            "approved": self.approved,
            "reason": self.reason,
        }


class RiskManager:
    """Account-level gates.

    Deliberately separate from the sizer: sizing answers "how much would this
    position be?", risk answers "may it exist at all?". Blending them tends to
    produce trades that are quietly shrunk instead of refused, which hides the
    fact that a limit was hit.
    """

    def __init__(self, limits: RiskLimits | None = None) -> None:
        self._limits = limits or RiskLimits()

    @property
    def limits(self) -> RiskLimits:
        return self._limits

    def evaluate(
        self,
        order: SizedOrder,
        *,
        equity: float,
        cash: float,
        positions: dict[str, float],
        prices: dict[str, float],
        day_pnl_pct: float = 0.0,
    ) -> RiskDecision:
        limits = self._limits

        # 1. Daily loss halt — checked first, because past this nothing else
        #    matters. Closing trades stay allowed so the book can be reduced.
        closing = _is_closing(order, positions.get(order.symbol, 0.0))
        if day_pnl_pct <= -abs(limits.max_daily_loss_pct) and not closing:
            return RiskDecision(
                False, order,
                f"daily loss {day_pnl_pct:.2%} breached the "
                f"{-abs(limits.max_daily_loss_pct):.2%} halt; only closing trades allowed",
            )

        # 2. Shorting permission.
        if not limits.allow_short and order.target_position < 0:
            return RiskDecision(False, order, "short selling is disabled")

        # 3. Position count — only for genuinely NEW symbols.
        held = {s for s, q in positions.items() if abs(q) > 1e-9}
        if order.symbol not in held and len(held) >= limits.max_positions and not closing:
            return RiskDecision(
                False, order, f"already holding the maximum of {limits.max_positions} positions"
            )

        # 4. Single-position concentration.
        position_notional = abs(order.target_position) * order.price
        if equity > 0 and position_notional > equity * limits.max_position_pct * 1.001:
            return RiskDecision(
                False, order,
                f"position would be {position_notional / equity:.1%} of equity, above the "
                f"{limits.max_position_pct:.0%} cap",
            )

        # 5. Portfolio heat, computed on the book as it WOULD be — checking
        #    the current book would let each order pass while the total
        #    quietly breached the limit.
        projected = dict(positions)
        projected[order.symbol] = order.target_position
        gross = sum(abs(q) * prices.get(s, order.price) for s, q in projected.items())
        if equity > 0 and gross > equity * limits.max_gross_exposure * 1.001 and not closing:
            return RiskDecision(
                False, order,
                f"gross exposure would reach {gross / equity:.2f}x equity, above the "
                f"{limits.max_gross_exposure:.2f}x limit",
            )

        # 6. Cash buffer for opening buys.
        if order.side is Side.BUY and not closing:
            required = order.quantity * order.price
            usable = cash - equity * limits.min_cash_buffer_pct
            if required > usable:
                return RiskDecision(
                    False, order,
                    f"needs {required:,.0f} but only {max(usable, 0):,.0f} is available "
                    f"after the {limits.min_cash_buffer_pct:.0%} cash buffer",
                )

        return RiskDecision(True, order, "within all limits")


def _is_closing(order: SizedOrder, current: float) -> bool:
    """True when the order reduces or flattens an existing position.

    Closing trades are exempt from most gates: a limit that prevents you from
    getting OUT of a position makes risk worse, not better.
    """
    return abs(order.target_position) < abs(current) - 1e-9


# ==========================================================================
# The loop
# ==========================================================================


@dataclass
class TradingRun:
    """What one pass of the engine did."""

    signals: list[Signal] = field(default_factory=list)
    decisions: list[RiskDecision] = field(default_factory=list)
    placed: list[UnifiedOrder] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    equity: float = 0.0
    #: symbol -> why its deployed configurations disagreed, when they did
    conflicts: dict = field(default_factory=dict)

    def summary(self) -> dict:
        approved = [d for d in self.decisions if d.approved]
        return {
            "signals": len(self.signals),
            "actionable": len(self.decisions),
            "approved": len(approved),
            "blocked": len(self.decisions) - len(approved),
            "placed": len(self.placed),
            "errors": len(self.errors),
            "conflicts": len(self.conflicts),
            "equity": self.equity,
        }


class TradingEngine:
    """Signals → sizing → risk → orders, in that order."""

    def __init__(
        self,
        service: OrderService,
        sizer: PositionSizer | None = None,
        risk: RiskManager | None = None,
        generator: SignalGenerator | None = None,
    ) -> None:
        self._service = service
        self._sizer = sizer or PositionSizer()
        self._risk = risk or RiskManager()
        self._generator = generator or SignalGenerator()

    def run(
        self,
        deployed: pd.DataFrame,
        price_data: dict[str, pd.DataFrame],
        *,
        dry_run: bool = False,
        day_pnl_pct: float = 0.0,
    ) -> TradingRun:
        """One pass. `dry_run` evaluates everything but places nothing."""
        run = TradingRun()

        account = self._service.adapter.get_account()
        run.equity = account.equity
        positions = {p.symbol: p.quantity for p in account.positions}
        prices = {p.symbol: (p.last_price or p.average_price) for p in account.positions}

        raw_signals = self._generator.generate_all(deployed, price_data)
        run.signals, conflicts = net_signals_by_symbol(raw_signals)
        logger.info(
            "Generated %s signals from %s configurations", len(run.signals), len(raw_signals)
        )
        for symbol, detail in conflicts.items():
            logger.warning("Conflicting signals on %s: %s", symbol, detail)
        run.conflicts = conflicts

        for signal in run.signals:
            data = price_data.get(signal.symbol)
            if data is None:
                continue
            prices.setdefault(signal.symbol, signal.price)

            sized = self._sizer.size(
                signal, account.equity, data, positions.get(signal.symbol, 0.0)
            )
            if sized is None:
                continue  # already at target, or too small to be worth trading

            decision = self._risk.evaluate(
                sized,
                equity=account.equity,
                cash=account.cash,
                positions=positions,
                prices=prices,
                day_pnl_pct=day_pnl_pct,
            )
            run.decisions.append(decision)

            if not decision.approved:
                logger.info("Blocked %s %s: %s", sized.side.value, sized.symbol, decision.reason)
                continue
            if dry_run:
                continue

            try:
                placed = self._service.place_order(
                    symbol=sized.symbol,
                    side=sized.side,
                    quantity=sized.quantity,
                    strategy=sized.strategy,
                )
                run.placed.append(placed)
                # Reflect the fill immediately so later orders in this same
                # pass see the book they are actually adding to.
                positions[sized.symbol] = sized.target_position
            except Exception as exc:  # noqa: BLE001 - one rejection must not stop the pass
                run.errors.append(f"{sized.symbol}: {exc}")
                logger.error("Order failed for %s: %s", sized.symbol, exc)

        return run

    def describe(self) -> dict:
        return {
            "sizing": self._sizer.policy.describe(),
            "risk": self._risk.limits.describe(),
            "broker": self._service.describe(),
        }
