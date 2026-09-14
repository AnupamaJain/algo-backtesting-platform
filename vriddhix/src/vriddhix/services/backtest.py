"""Backtesting. Phase 8.

The backtester replays the SAME engines and the SAME condition evaluator the
live scanner uses. It is not a reimplementation of the strategy -- it is the
strategy, run over dates that have already happened.

Three properties it is built to guarantee:

**No look-ahead.** At every evaluation date the price frame is truncated
there. Entries execute on the NEXT bar's open, never on the close that
triggered them, because that close was not knowable until the session ended.

**Costs are real.** Brokerage, slippage and Indian statutory charges (STT,
stamp duty, exchange and SEBI fees, GST) are applied per trade. A backtest
that ignores them overstates a 60-trade-a-year strategy by several percent
annually, which is the difference between viable and not.

**Survivorship is declared.** The run records how its universe was resolved,
and a CURRENT_UNIVERSE run carries a warning into its own metrics payload.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from ..domain.types import SurvivorshipMode

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CostModel:
    """Indian cash-equity delivery costs. Defaults are representative
    discount-broker rates; every figure is configurable because they change."""

    brokerage_pct: float = 0.03
    brokerage_cap: float = 20.0
    slippage_pct: float = 0.05
    stt_pct_sell: float = 0.1
    exchange_pct: float = 0.00325
    sebi_pct: float = 0.0001
    stamp_duty_pct_buy: float = 0.015
    gst_pct: float = 18.0

    def charges(self, value: float, *, side: str) -> float:
        brokerage = min(value * self.brokerage_pct / 100.0, self.brokerage_cap)
        exchange = value * self.exchange_pct / 100.0
        sebi = value * self.sebi_pct / 100.0
        gst = (brokerage + exchange) * self.gst_pct / 100.0

        statutory = 0.0
        if side == "SELL":
            statutory += value * self.stt_pct_sell / 100.0
        else:
            statutory += value * self.stamp_duty_pct_buy / 100.0

        return brokerage + exchange + sebi + gst + statutory

    def fill_price(self, price: float, *, side: str) -> float:
        """Slippage always works against the trade."""
        drift = price * self.slippage_pct / 100.0
        return price + drift if side == "BUY" else price - drift


@dataclass(frozen=True, slots=True)
class SizingRule:
    method: str = "risk_based"   # fixed_qty | fixed_capital | fixed_pct | risk_based | atr_based
    fixed_qty: int = 1
    fixed_capital: float = 100_000.0
    fixed_pct: float = 10.0
    risk_pct: float = 1.0
    stop_pct: float = 8.0
    atr_multiple: float = 2.0
    max_position_pct: float = 25.0


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    start: date
    end: date
    initial_capital: float = 1_000_000.0
    entry: dict = field(default_factory=dict)
    exit: dict | None = None
    stop_pct: float | None = 8.0
    target_pct: float | None = None
    trailing_stop_pct: float | None = None
    max_holding_days: int | None = 60
    max_positions: int = 10
    #: Evaluating every fifth session rather than every one. A base persists
    #: for weeks, so this changes entry timing by days at most while cutting
    #: the work by 80%.
    rebalance_days: int = 5
    sizing: SizingRule = field(default_factory=SizingRule)
    costs: CostModel = field(default_factory=CostModel)
    survivorship_mode: SurvivorshipMode = SurvivorshipMode.CURRENT_UNIVERSE
    survivorship_warning: str | None = None


# ---------------------------------------------------------------------------
# Trades and results
# ---------------------------------------------------------------------------


@dataclass
class Trade:
    symbol: str
    entry_date: date
    entry_price: float
    quantity: int
    entry_reason: str
    stop_price: float | None = None
    target_price: float | None = None

    exit_date: date | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    gross_pnl: float = 0.0
    costs: float = 0.0
    net_pnl: float = 0.0
    return_pct: float = 0.0
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    days_held: int = 0
    context: dict = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.exit_date is None


@dataclass
class BacktestMetrics:
    total_trades: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    total_return: float = 0.0
    cagr: float = 0.0
    max_drawdown: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    avg_holding_days: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    total_costs: float = 0.0
    final_equity: float = 0.0


@dataclass
class BacktestResult:
    config: BacktestConfig
    trades: list[Trade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    metrics: BacktestMetrics = field(default_factory=BacktestMetrics)

    @property
    def survivorship_warning(self) -> str | None:
        if self.config.survivorship_mode is SurvivorshipMode.POINT_IN_TIME:
            return None
        return self.config.survivorship_warning or (
            "Universe membership history was unavailable; the current constituent "
            "list was used. Results may overstate performance."
        )

    def to_payload(self) -> dict:
        """What the API returns. The warning travels with the numbers."""
        return {
            "metrics": self.metrics.__dict__,
            "survivorship_mode": self.config.survivorship_mode.value,
            "survivorship_warning": self.survivorship_warning,
            "trades": len(self.trades),
        }


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------


def position_size(
    rule: SizingRule, *, equity: float, price: float, atr: float | None = None
) -> int:
    """Shares to buy. Deterministic -- the same inputs always give the same size."""
    if price <= 0:
        return 0

    if rule.method == "fixed_qty":
        quantity = rule.fixed_qty
    elif rule.method == "fixed_capital":
        quantity = int(rule.fixed_capital // price)
    elif rule.method == "fixed_pct":
        quantity = int((equity * rule.fixed_pct / 100.0) // price)
    elif rule.method == "atr_based":
        if not atr or atr <= 0:
            return 0
        risk_capital = equity * rule.risk_pct / 100.0
        quantity = int(risk_capital // (atr * rule.atr_multiple))
    else:  # risk_based
        risk_capital = equity * rule.risk_pct / 100.0
        risk_per_share = price * rule.stop_pct / 100.0
        quantity = int(risk_capital // risk_per_share) if risk_per_share > 0 else 0

    # Concentration cap applies regardless of method: a risk-based size on a
    # tight stop can otherwise become most of the book.
    cap = int((equity * rule.max_position_pct / 100.0) // price)
    return max(0, min(quantity, cap))


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def compute_metrics(
    trades: list[Trade], equity_curve: pd.Series, initial_capital: float,
    *, periods_per_year: int = 252,
) -> BacktestMetrics:
    closed = [t for t in trades if not t.is_open]
    metrics = BacktestMetrics(total_trades=len(closed))
    if not closed:
        metrics.final_equity = initial_capital
        return metrics

    wins = [t for t in closed if t.net_pnl > 0]
    losses = [t for t in closed if t.net_pnl <= 0]

    metrics.win_rate = len(wins) / len(closed)
    metrics.avg_win = float(np.mean([t.net_pnl for t in wins])) if wins else 0.0
    metrics.avg_loss = float(np.mean([t.net_pnl for t in losses])) if losses else 0.0

    gross_profit = sum(t.net_pnl for t in wins)
    gross_loss = abs(sum(t.net_pnl for t in losses))
    metrics.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    metrics.expectancy = float(np.mean([t.net_pnl for t in closed]))
    metrics.avg_holding_days = float(np.mean([t.days_held for t in closed]))
    metrics.largest_win = max((t.net_pnl for t in closed), default=0.0)
    metrics.largest_loss = min((t.net_pnl for t in closed), default=0.0)
    metrics.total_costs = sum(t.costs for t in closed)

    if len(equity_curve) > 1:
        final = float(equity_curve.iloc[-1])
        metrics.final_equity = final
        metrics.total_return = final / initial_capital - 1.0

        years = max((equity_curve.index[-1] - equity_curve.index[0]).days / 365.25, 1e-9)
        if final > 0:
            metrics.cagr = (final / initial_capital) ** (1 / years) - 1.0

        running_max = equity_curve.cummax()
        drawdown = equity_curve / running_max - 1.0
        metrics.max_drawdown = float(drawdown.min())

        returns = equity_curve.pct_change().dropna()
        if len(returns) > 1 and returns.std() > 0:
            metrics.sharpe = float(returns.mean() / returns.std() * math.sqrt(periods_per_year))
        downside = returns[returns < 0]
        if len(downside) > 1 and downside.std() > 0:
            metrics.sortino = float(returns.mean() / downside.std() * math.sqrt(periods_per_year))
        if metrics.max_drawdown < 0:
            metrics.calmar = metrics.cagr / abs(metrics.max_drawdown)
    else:
        metrics.final_equity = initial_capital

    return metrics


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


def run(
    price_data: dict[str, pd.DataFrame],
    signals: dict[date, list[dict]],
    cfg: BacktestConfig,
) -> BacktestResult:
    """Replay signals into trades.

    ``signals`` maps an evaluation date to the scanner rows produced ON that
    date (already truncated, already scored). Keeping signal generation
    outside this function is what lets the backtester consume exactly what the
    live scanner emits, rather than a parallel version of it.
    """
    result = BacktestResult(config=cfg)

    calendar = sorted({d for frame in price_data.values() for d in frame.index})
    calendar = [d for d in calendar if cfg.start <= d.date() <= cfg.end]
    if not calendar:
        return result

    cash = cfg.initial_capital
    open_trades: dict[str, Trade] = {}
    pending: list[dict] = []
    equity_points: list[tuple[pd.Timestamp, float]] = []

    for today in calendar:
        today_date = today.date()

        # --- fills happen on THIS bar's open, from signals raised yesterday.
        # Filling on the triggering close would be trading on information the
        # session had not finished producing.
        for signal in pending:
            symbol = signal["symbol"]
            frame = price_data.get(symbol)
            if frame is None or today not in frame.index or symbol in open_trades:
                continue
            if len(open_trades) >= cfg.max_positions:
                break

            raw_price = float(frame.loc[today, "open"])
            price = cfg.costs.fill_price(raw_price, side="BUY")
            equity = cash + sum(_value(t, price_data, today) for t in open_trades.values())
            quantity = position_size(
                cfg.sizing, equity=equity, price=price, atr=signal.get("atr")
            )
            if quantity <= 0:
                continue

            value = price * quantity
            costs = cfg.costs.charges(value, side="BUY")
            if value + costs > cash:
                continue

            cash -= value + costs
            open_trades[symbol] = Trade(
                symbol=symbol, entry_date=today_date, entry_price=price,
                quantity=quantity, entry_reason=signal.get("reason", "entry rule met"),
                stop_price=price * (1 - cfg.stop_pct / 100.0) if cfg.stop_pct else None,
                target_price=price * (1 + cfg.target_pct / 100.0) if cfg.target_pct else None,
                costs=costs,
                context=signal.get("context", {}),
            )
        pending = []

        # --- exits ------------------------------------------------------
        for symbol, trade in list(open_trades.items()):
            frame = price_data.get(symbol)
            if frame is None or today not in frame.index:
                continue

            bar = frame.loc[today]
            high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])

            trade.mfe_pct = max(trade.mfe_pct, (high - trade.entry_price) / trade.entry_price * 100)
            trade.mae_pct = min(trade.mae_pct, (low - trade.entry_price) / trade.entry_price * 100)
            trade.days_held = (today_date - trade.entry_date).days

            reason = None
            exit_price = close
            if trade.stop_price is not None and low <= trade.stop_price:
                reason, exit_price = "STOP", trade.stop_price
            elif trade.target_price is not None and high >= trade.target_price:
                reason, exit_price = "TARGET", trade.target_price
            elif cfg.max_holding_days and trade.days_held >= cfg.max_holding_days:
                reason = "TIME_EXIT"
            elif cfg.trailing_stop_pct:
                peak = trade.entry_price * (1 + trade.mfe_pct / 100.0)
                trail = peak * (1 - cfg.trailing_stop_pct / 100.0)
                if close <= trail and trade.mfe_pct > cfg.trailing_stop_pct:
                    reason, exit_price = "TRAILING_STOP", close

            if reason is None:
                continue

            fill = cfg.costs.fill_price(exit_price, side="SELL")
            proceeds = fill * trade.quantity
            exit_costs = cfg.costs.charges(proceeds, side="SELL")

            cash += proceeds - exit_costs
            trade.exit_date = today_date
            trade.exit_price = fill
            trade.exit_reason = reason
            trade.costs += exit_costs
            trade.gross_pnl = (fill - trade.entry_price) * trade.quantity
            trade.net_pnl = trade.gross_pnl - trade.costs
            trade.return_pct = trade.net_pnl / (trade.entry_price * trade.quantity) * 100.0

            result.trades.append(trade)
            del open_trades[symbol]

        # --- new signals, queued for tomorrow's open --------------------
        if today_date in signals:
            for row in signals[today_date]:
                if row["symbol"] not in open_trades:
                    pending.append(row)

        equity = cash + sum(_value(t, price_data, today) for t in open_trades.values())
        equity_points.append((today, equity))

    # Close whatever is still open at the end, marked as such.
    final_bar = calendar[-1]
    for symbol, trade in open_trades.items():
        frame = price_data.get(symbol)
        if frame is None or final_bar not in frame.index:
            continue
        fill = cfg.costs.fill_price(float(frame.loc[final_bar, "close"]), side="SELL")
        proceeds = fill * trade.quantity
        exit_costs = cfg.costs.charges(proceeds, side="SELL")
        trade.exit_date = final_bar.date()
        trade.exit_price = fill
        trade.exit_reason = "END_OF_BACKTEST"
        trade.costs += exit_costs
        trade.gross_pnl = (fill - trade.entry_price) * trade.quantity
        trade.net_pnl = trade.gross_pnl - trade.costs
        trade.return_pct = trade.net_pnl / (trade.entry_price * trade.quantity) * 100.0
        trade.days_held = (trade.exit_date - trade.entry_date).days
        result.trades.append(trade)

    result.equity_curve = pd.Series(
        [v for _, v in equity_points], index=[d for d, _ in equity_points]
    )
    result.metrics = compute_metrics(result.trades, result.equity_curve, cfg.initial_capital)
    return result


def _value(trade: Trade, price_data: dict[str, pd.DataFrame], on: pd.Timestamp) -> float:
    frame = price_data.get(trade.symbol)
    if frame is None or on not in frame.index:
        return trade.entry_price * trade.quantity
    return float(frame.loc[on, "close"]) * trade.quantity


# ---------------------------------------------------------------------------
# Walk-forward and Monte Carlo
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WalkForwardWindow:
    train_start: date
    train_end: date
    test_start: date
    test_end: date


def walk_forward_windows(
    start: date, end: date, *, train_months: int = 36, test_months: int = 12, step_months: int = 12
) -> list[WalkForwardWindow]:
    """Rolling train/test splits.

    Parameters must be chosen on the TRAIN window and evaluated on the TEST
    window. Tuning against the test period and reporting the result is the
    most common way a backtest becomes a curve-fit with a nice chart.
    """
    def add_months(d: date, months: int) -> date:
        month = d.month - 1 + months
        return date(d.year + month // 12, month % 12 + 1, min(d.day, 28))

    windows: list[WalkForwardWindow] = []
    cursor = start
    while True:
        train_end = add_months(cursor, train_months)
        test_end = add_months(train_end, test_months)
        if train_end >= end:
            break
        windows.append(
            WalkForwardWindow(cursor, train_end, train_end + timedelta(days=1), min(test_end, end))
        )
        if test_end >= end:
            break
        cursor = add_months(cursor, step_months)
    return windows


def monte_carlo(
    trades: list[Trade], initial_capital: float, *, runs: int = 1000, seed: int = 42
) -> dict:
    """Resample the trade sequence to estimate a drawdown distribution.

    This is a statistical simulation of ORDER RISK -- what the same set of
    trades could have felt like in a different sequence. It is not a forecast
    of future returns, and the UI must label it that way.
    """
    closed = [t for t in trades if not t.is_open]
    if len(closed) < 5:
        return {"runs": 0, "note": "too few trades to simulate"}

    returns = np.array([t.net_pnl / initial_capital for t in closed])
    rng = np.random.default_rng(seed)

    drawdowns: list[float] = []
    finals: list[float] = []
    for _ in range(runs):
        path = 1.0 + np.cumsum(rng.permutation(returns))
        peak = np.maximum.accumulate(np.concatenate([[1.0], path]))
        drawdowns.append(float((np.concatenate([[1.0], path]) / peak - 1.0).min()))
        finals.append(float(path[-1] - 1.0))

    return {
        "runs": runs,
        "median_drawdown": float(np.median(drawdowns)),
        "p95_drawdown": float(np.percentile(drawdowns, 5)),
        "worst_drawdown": float(np.min(drawdowns)),
        "median_return": float(np.median(finals)),
        "p05_return": float(np.percentile(finals, 5)),
        "p95_return": float(np.percentile(finals, 95)),
        "note": "Simulation of trade-order risk, not a forecast of future returns.",
    }


def compare(results: dict[str, BacktestResult]) -> pd.DataFrame:
    """Side-by-side comparison, so an added filter can be shown to earn its keep."""
    return pd.DataFrame(
        {
            name: {
                "trades": r.metrics.total_trades,
                "win_rate": round(r.metrics.win_rate, 4),
                "profit_factor": round(r.metrics.profit_factor, 3),
                "expectancy": round(r.metrics.expectancy, 2),
                "cagr": round(r.metrics.cagr, 4),
                "max_drawdown": round(r.metrics.max_drawdown, 4),
                "sharpe": round(r.metrics.sharpe, 3),
            }
            for name, r in results.items()
        }
    ).T
