"""Layer 2 - execution simulation, walk-forward analysis and the validation funnel."""

from __future__ import annotations

import itertools
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Mapping

import numpy as np
import pandas as pd

from .config import BacktestConfig, OutputConfig
from .risk import RiskOverlay
from .strategies import get_strategy_class

logger = logging.getLogger(__name__)


# ==========================================================================
# TRANSACTION COST AND SLIPPAGE MODELS
# ==========================================================================
# Transaction-cost and slippage models.
#
# Single Responsibility: convert a stream of position changes into a stream of
# per-bar cost drags, expressed as a *fraction of deployed capital* so the
# backtester can subtract them directly from returns.
#
# Open/Closed: the engine depends on the `CostModel` interface, so adding a
# tiered/spread-aware model later requires no engine change.


class CostModel(ABC):
    """Interface every execution-drag model implements."""

    @abstractmethod
    def cost_fraction(self, turnover: pd.Series, prices: pd.Series) -> pd.Series:
        """Return per-bar cost as a fraction of capital.

        Args:
            turnover: |change in target position| per bar, in exposure units
                (a flip from +1 to -1 is a turnover of 2.0).
            prices: the execution price series, aligned to `turnover`.
        """

    @abstractmethod
    def describe(self) -> dict:
        """Machine-readable parameters, recorded into result artifacts."""


class PercentageCostModel(CostModel):
    """Commission and slippage both charged as a percentage of traded notional.

    This is the right model for ETFs/crypto quoted per-unit, and it is
    capital-invariant: doubling account size doubles both the cost and the
    notional, leaving the return drag unchanged.
    """

    def __init__(self, commission_pct: float = 0.0005, slippage_pct: float = 0.0002) -> None:
        if commission_pct < 0 or slippage_pct < 0:
            raise ValueError("cost rates must be non-negative")
        self.commission_pct = commission_pct
        self.slippage_pct = slippage_pct

    def cost_fraction(self, turnover: pd.Series, prices: pd.Series) -> pd.Series:
        return turnover.abs() * (self.commission_pct + self.slippage_pct)

    def describe(self) -> dict:
        return {
            "model": "percentage",
            "commission_pct": self.commission_pct,
            "slippage_pct": self.slippage_pct,
        }


class PerShareCostModel(CostModel):
    """Commission charged per share traded (e.g. $0.005/share), slippage as a
    percentage of notional.

    Share count is derived as `capital_base / price` per unit of exposure, so
    the resulting drag is price-dependent: a $5 stock incurs far more
    per-share commission drag than a $500 one for the same notional. That
    asymmetry is real and is exactly why this model exists alongside the
    percentage one.
    """

    def __init__(
        self,
        commission_per_share: float = 0.005,
        slippage_pct: float = 0.0002,
        capital_base: float = 100_000.0,
    ) -> None:
        if commission_per_share < 0 or slippage_pct < 0:
            raise ValueError("cost rates must be non-negative")
        if capital_base <= 0:
            raise ValueError("capital_base must be positive")
        self.commission_per_share = commission_per_share
        self.slippage_pct = slippage_pct
        self.capital_base = capital_base

    def cost_fraction(self, turnover: pd.Series, prices: pd.Series) -> pd.Series:
        safe_prices = prices.replace(0.0, np.nan)
        # shares traded per unit exposure = capital / price; commission as a
        # fraction of capital therefore reduces to rate * turnover / price.
        commission = self.commission_per_share * turnover.abs() / safe_prices
        slippage = turnover.abs() * self.slippage_pct
        return (commission + slippage).fillna(0.0)

    def describe(self) -> dict:
        return {
            "model": "per_share",
            "commission_per_share": self.commission_per_share,
            "slippage_pct": self.slippage_pct,
            "capital_base": self.capital_base,
        }


def build_cost_model(config: dict) -> CostModel:
    """Factory: construct the configured cost model from the `costs` block."""
    model_name = config.get("model", "percentage")
    if model_name == "percentage":
        params = config["percentage"]
        return PercentageCostModel(
            commission_pct=float(params["commission_pct"]),
            slippage_pct=float(params["slippage_pct"]),
        )
    if model_name == "per_share":
        params = config["per_share"]
        return PerShareCostModel(
            commission_per_share=float(params["commission_per_share"]),
            slippage_pct=float(params["slippage_pct"]),
            capital_base=float(params["capital_base"]),
        )
    raise ValueError(f"unknown cost model: {model_name!r}")


# ==========================================================================
# PERFORMANCE METRICS
# ==========================================================================
# Vectorized performance metrics.
#
# Single Responsibility: pure functions over a return series (and the position
# series that produced it). Nothing here downloads data, decides trades, or
# knows what a strategy is.
#
# Every metric is computed with numpy/pandas array operations — no per-row
# Python loops — so scoring thousands of configurations stays fast.


TRADING_DAYS_PER_YEAR = 252
CRYPTO_DAYS_PER_YEAR = 365


def infer_periods_per_year(index: pd.DatetimeIndex) -> int:
    """Infer how many bars a year this series actually contains.

    Annualization is not a global constant. Equities trade ~252 days a year;
    crypto trades every day. Applying 252 to a 365-bar-a-year series
    understates its Sharpe by sqrt(365/252) — about 20% — and overstates the
    elapsed years used for CAGR by 1.45x. Both make crypto look materially
    worse than it was.

    The rate is measured from the index itself rather than configured per
    symbol, so a new asset gets the right calendar without anyone remembering
    to declare it. The result snaps to the nearest conventional value to avoid
    a ragged number from a short or gappy history.
    """
    if len(index) < 2:
        return TRADING_DAYS_PER_YEAR
    span_days = (index[-1] - index[0]).days
    if span_days <= 0:
        return TRADING_DAYS_PER_YEAR
    observed = len(index) / (span_days / 365.25)
    # Snap to the closest of the two real-world calendars.
    return min(
        (TRADING_DAYS_PER_YEAR, CRYPTO_DAYS_PER_YEAR),
        key=lambda candidate: abs(candidate - observed),
    )


@dataclass(frozen=True)
class PerformanceMetrics:
    """Standardized metric bundle passed between Layer 2 components."""

    total_return: float
    cagr: float
    annual_volatility: float
    sharpe: float
    max_drawdown: float          # positive fraction, e.g. 0.35 == 35% drawdown
    profit_factor: float
    num_trades: int
    win_rate: float
    num_periods: int

    def to_dict(self) -> dict:
        return asdict(self)


def compute_asset_returns(prices: pd.Series) -> pd.Series:
    """Simple close-to-close returns."""
    return prices.pct_change().fillna(0.0)


def compute_equity_curve(returns: pd.Series) -> pd.Series:
    """Compounded equity curve, starting at 1.0."""
    return (1.0 + returns.fillna(0.0)).cumprod()


def compute_drawdown_series(returns: pd.Series) -> pd.Series:
    """Running drawdown as a negative fraction from the running peak."""
    equity = compute_equity_curve(returns)
    running_peak = equity.cummax()
    return equity / running_peak - 1.0


def compute_max_drawdown(returns: pd.Series) -> float:
    """Peak-to-trough max drawdown as a positive fraction."""
    if len(returns) == 0:
        return 0.0
    drawdown = compute_drawdown_series(returns)
    return float(-drawdown.min())


def compute_sharpe(
    returns: pd.Series,
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """Annualized Sharpe ratio.

    Returns 0.0 (not NaN/inf) for a flat or empty series, so a strategy that
    never traded scores as "no edge" rather than poisoning downstream
    comparisons.
    """
    if len(returns) < 2:
        return 0.0
    excess = returns - (risk_free_rate / periods_per_year)
    std = excess.std(ddof=1)
    if std == 0 or np.isnan(std):
        return 0.0
    return float(excess.mean() / std * np.sqrt(periods_per_year))


def compute_cagr(returns: pd.Series, periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float:
    """Compound annual growth rate implied by the return series."""
    if len(returns) == 0:
        return 0.0
    equity = compute_equity_curve(returns)
    final = float(equity.iloc[-1])
    years = len(returns) / periods_per_year
    if years <= 0 or final <= 0:
        return 0.0
    return float(final ** (1.0 / years) - 1.0)


def extract_trade_returns(returns: pd.Series, position: pd.Series) -> pd.Series:
    """Compound each in-market holding stretch into a single trade P&L.

    A "trade" is one continuous stretch at the same non-zero position. This
    is done with a vectorized groupby over a run-length id rather than a
    Python loop over rows.
    """
    if len(returns) == 0:
        return pd.Series(dtype=float)

    position = position.reindex(returns.index).fillna(0.0)
    in_market = position != 0
    if not in_market.any():
        return pd.Series(dtype=float)

    trade_id = (position != position.shift()).cumsum()
    # log-space sum then expm1 == compounding, but vectorized in one pass.
    safe = returns.clip(lower=-0.999999)
    log_returns = np.log1p(safe)
    grouped = log_returns[in_market].groupby(trade_id[in_market]).sum()
    return np.expm1(grouped)


def compute_num_trades(position: pd.Series) -> int:
    """Number of entries into a non-zero position.

    A direct flip (+1 -> -1) counts as one entry into the new direction, not
    two; the exit is implicit in the entry.
    """
    if len(position) == 0:
        return 0
    changed = position != position.shift()
    return int((changed & (position != 0)).sum())


def compute_profit_factor(trade_returns: pd.Series) -> float:
    """Gross profits / gross losses across closed trades.

    Returns inf when there are winners and no losers, and 0.0 when there are
    no winners — both are meaningful extremes the funnel can filter on.
    """
    if len(trade_returns) == 0:
        return 0.0
    gains = trade_returns[trade_returns > 0].sum()
    losses = -trade_returns[trade_returns < 0].sum()
    if losses == 0:
        return float("inf") if gains > 0 else 0.0
    return float(gains / losses)


def compute_win_rate(trade_returns: pd.Series) -> float:
    if len(trade_returns) == 0:
        return 0.0
    return float((trade_returns > 0).mean())


def compute_metrics(
    returns: pd.Series,
    position: pd.Series,
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> PerformanceMetrics:
    """Compute the full metric bundle for one net-return stream."""
    returns = returns.fillna(0.0)
    trade_returns = extract_trade_returns(returns, position)
    equity = compute_equity_curve(returns)
    total_return = float(equity.iloc[-1] - 1.0) if len(equity) else 0.0

    return PerformanceMetrics(
        total_return=total_return,
        cagr=compute_cagr(returns, periods_per_year),
        annual_volatility=float(returns.std(ddof=1) * np.sqrt(periods_per_year))
        if len(returns) > 1
        else 0.0,
        sharpe=compute_sharpe(returns, risk_free_rate, periods_per_year),
        max_drawdown=compute_max_drawdown(returns),
        profit_factor=compute_profit_factor(trade_returns),
        num_trades=compute_num_trades(position),
        win_rate=compute_win_rate(trade_returns),
        num_periods=int(len(returns)),
    )


def sharpe_p_value(
    sharpe: float, num_periods: int, periods_per_year: int = TRADING_DAYS_PER_YEAR
) -> float:
    """One-sided p-value for H0: true Sharpe <= 0.

    The annualized Sharpe is de-annualized back to per-period, giving the
    standard t-statistic `SR_period * sqrt(n)`. Used only by the multiple-
    comparison filter, which needs a p-value per configuration.
    """
    from scipy import stats

    if num_periods < 2:
        return 1.0
    sharpe_per_period = sharpe / np.sqrt(periods_per_year)
    t_stat = sharpe_per_period * np.sqrt(num_periods)
    return float(stats.t.sf(t_stat, df=num_periods - 1))


# ==========================================================================
# VECTORIZED BACKTESTER
# ==========================================================================
# Vectorized single-pass backtester.
#
# This module owns the *execution semantics* of the whole system, and in
# particular the one guarantee everything else depends on:
#
#     A signal computed from the close of bar t is acted on for bar t+1.
#
# That is enforced here, once, by `signals.shift(1)` — no strategy and no
# caller can opt out of it. Concretely:
#
#     position[t] = signal[t-1]        # decided at yesterday's close
#     gross[t]    = position[t] * asset_return[t]
#     net[t]      = gross[t] - cost(|position[t] - position[t-1]|)
#
# so the return earned on bar t is only ever multiplied by a position that was
# knowable strictly before bar t began.


@dataclass(frozen=True)
class BacktestResult:
    """Output of one vectorized backtest over one contiguous date range."""

    net_returns: pd.Series
    gross_returns: pd.Series
    costs: pd.Series
    position: pd.Series
    metrics: PerformanceMetrics


class VectorizedBacktester:
    """Applies a signal series to a price series with realistic execution drag.

    Stateless and reusable: one instance can score thousands of
    configurations. All configuration is injected at construction (DIP) —
    the cost model is an interface, not a concrete class.
    """

    def __init__(
        self,
        cost_model: CostModel,
        risk_free_rate: float = 0.0,
        periods_per_year: int | None = None,
        risk_overlay: "RiskOverlay | None" = None,
    ) -> None:
        self._cost_model = cost_model
        self._risk_free_rate = risk_free_rate
        # None => infer each series' own calendar (see infer_periods_per_year).
        self._periods_per_year = periods_per_year
        # Optional stops/targets. Off by default: a naked backtest is the
        # honest test of whether a signal has an edge at all.
        self._risk_overlay = risk_overlay

    @property
    def cost_model(self) -> CostModel:
        """The execution-drag model, for callers (e.g. the Layer 4 portfolios)
        that must charge cost on their own rebalancing turnover."""
        return self._cost_model

    def periods_for(self, index: pd.DatetimeIndex) -> int:
        """The annualization factor to use for a given series."""
        if self._periods_per_year is not None:
            return self._periods_per_year
        return infer_periods_per_year(index)

    def run(
        self,
        prices: pd.Series,
        signals: pd.Series,
        data: pd.DataFrame | None = None,
    ) -> BacktestResult:
        """Backtest `signals` against `prices` over their shared index.

        `data` is the full OHLC frame, required only when a risk overlay is
        configured — stops need the intrabar high/low, which a close-only
        series cannot provide.
        """
        prices, signals = self._align(prices, signals)
        periods_per_year = self.periods_for(prices.index)

        # --- the look-ahead guard: today's return is earned by yesterday's
        # decision. Never remove this shift. -----------------------------
        position = signals.shift(1).fillna(0.0)

        asset_returns = compute_asset_returns(prices)
        gross_returns = position * asset_returns

        if self._risk_overlay is not None:
            if data is None:
                raise ValueError(
                    "a risk overlay needs the OHLC frame; pass data= to run()"
                )
            # The overlay both cuts positions short and re-prices the bars it
            # exits on, so the returns come from it rather than from
            # position * asset_returns.
            overlay = self._risk_overlay.apply(data.loc[prices.index], position)
            position = overlay.position
            gross_returns = overlay.gross_returns

        turnover = position.diff().abs().fillna(position.abs())
        costs = self._cost_model.cost_fraction(turnover, prices).fillna(0.0)
        net_returns = gross_returns - costs

        metrics = compute_metrics(
            net_returns,
            position,
            risk_free_rate=self._risk_free_rate,
            periods_per_year=periods_per_year,
        )
        return BacktestResult(
            net_returns=net_returns,
            gross_returns=gross_returns,
            costs=costs,
            position=position,
            metrics=metrics,
        )

    @staticmethod
    def _align(prices: pd.Series, signals: pd.Series) -> tuple[pd.Series, pd.Series]:
        if not isinstance(prices.index, pd.DatetimeIndex):
            raise ValueError("prices must be indexed by a DatetimeIndex")
        if not isinstance(signals.index, pd.DatetimeIndex):
            raise ValueError("signals must be indexed by a DatetimeIndex")

        common = prices.index.intersection(signals.index)
        if len(common) == 0:
            raise ValueError("prices and signals share no overlapping dates")
        return prices.loc[common].sort_index(), signals.loc[common].sort_index()


# ==========================================================================
# WALK-FORWARD ENGINE
# ==========================================================================
# Walk-forward analysis engine.
#
# Splits the history into rolling (or anchored) in-sample / out-of-sample
# window pairs and scores configurations separately on each.
#
# Two evaluation modes, because the system needs both:
#
#   * `evaluate_configuration()` — scores ONE fixed parameter set, reporting IS
#     and OOS performance separately. This is what feeds the validation
#     funnel: the funnel's overfitting filter (IS vs OOS) and its multiple-
#     comparison filter both need one row per configuration tested, across all
#     thousands of them.
#
#   * `select_and_run()` — the classic walk-forward optimization: on each
#     window, pick the best parameter set using ONLY that window's in-sample
#     data, then apply that choice to the untouched out-of-sample window and
#     stitch those OOS segments into one continuous track record. Parameter
#     selection never sees the data it is later scored on.
#
# Aggregation rule (important, and asymmetric on purpose): OOS windows are
# disjoint, so their returns are unioned into one genuinely continuous
# out-of-sample equity curve. IS windows overlap heavily (a 3-year window
# stepping 1 year shares 2 years with its neighbour), so stitching them would
# double-count; IS metrics are therefore averaged across windows instead.


@dataclass(frozen=True)
class WalkForwardWindow:
    """One in-sample / out-of-sample window pair."""

    index: int
    is_start: pd.Timestamp
    is_end: pd.Timestamp
    oos_start: pd.Timestamp
    oos_end: pd.Timestamp

    def describe(self) -> dict:
        return {
            "window": self.index,
            "is_start": self.is_start.date().isoformat(),
            "is_end": self.is_end.date().isoformat(),
            "oos_start": self.oos_start.date().isoformat(),
            "oos_end": self.oos_end.date().isoformat(),
        }


class WindowGenerator:
    """Builds the walk-forward window schedule from a date index.

    `rolling` mode slides a fixed-length IS window forward; `anchored` mode
    keeps the IS start pinned to the beginning of history and grows the
    window. Both advance the OOS window by `step_years` each iteration.
    """

    def __init__(
        self,
        is_years: int = 3,
        oos_years: int = 1,
        step_years: int = 1,
        mode: str = "rolling",
        min_oos_days: int = 60,
    ) -> None:
        if mode not in ("rolling", "anchored"):
            raise ValueError(f"mode must be 'rolling' or 'anchored', got {mode!r}")
        if is_years <= 0 or oos_years <= 0 or step_years <= 0:
            raise ValueError("is_years, oos_years and step_years must be positive")
        self.is_years = is_years
        self.oos_years = oos_years
        self.step_years = step_years
        self.mode = mode
        self.min_oos_days = min_oos_days

    def generate(self, index: pd.DatetimeIndex) -> list[WalkForwardWindow]:
        if len(index) == 0:
            return []
        index = index.sort_values()
        history_start, history_end = index[0], index[-1]

        windows: list[WalkForwardWindow] = []
        cursor = history_start
        i = 0
        while True:
            is_start = history_start if self.mode == "anchored" else cursor
            is_end = cursor + pd.DateOffset(years=self.is_years)
            oos_start = is_end
            oos_end = oos_start + pd.DateOffset(years=self.oos_years)

            if oos_start >= history_end:
                break

            # Only count windows with enough real observations to score.
            oos_days = int(((index >= oos_start) & (index < oos_end)).sum())
            is_days = int(((index >= is_start) & (index < is_end)).sum())
            if oos_days >= self.min_oos_days and is_days > 0:
                windows.append(
                    WalkForwardWindow(
                        index=i,
                        is_start=is_start,
                        is_end=is_end,
                        oos_start=oos_start,
                        oos_end=min(oos_end, history_end + pd.Timedelta(days=1)),
                    )
                )
                i += 1

            cursor = cursor + pd.DateOffset(years=self.step_years)
            if cursor >= history_end:
                break
        return windows


@dataclass
class WalkForwardResult:
    """Per-configuration walk-forward outcome."""

    symbol: str
    strategy_name: str
    param_key: str
    params: dict
    is_metrics: PerformanceMetrics
    oos_metrics: PerformanceMetrics
    num_windows: int
    # The annualization factor this configuration was scored on. Carried into
    # the funnel so the multiple-comparison p-value uses the same calendar the
    # Sharpe was computed with, rather than a single global assumption.
    periods_per_year: int = TRADING_DAYS_PER_YEAR
    oos_returns: pd.Series = field(repr=False, default_factory=lambda: pd.Series(dtype=float))
    # Retained so Layer 3 can reconstruct individual trades from the OOS
    # track record without re-running the backtest.
    oos_position: pd.Series = field(repr=False, default_factory=lambda: pd.Series(dtype=float))
    per_window: list[dict] = field(repr=False, default_factory=list)

    def to_row(self) -> dict:
        """Flatten into one DataFrame row for the validation funnel."""
        return {
            "symbol": self.symbol,
            "strategy": self.strategy_name,
            "param_key": self.param_key,
            "params": self.params,
            "num_windows": self.num_windows,
            "is_sharpe": self.is_metrics.sharpe,
            "is_total_return": self.is_metrics.total_return,
            "is_max_drawdown": self.is_metrics.max_drawdown,
            "is_num_trades": self.is_metrics.num_trades,
            "oos_sharpe": self.oos_metrics.sharpe,
            "oos_total_return": self.oos_metrics.total_return,
            "oos_cagr": self.oos_metrics.cagr,
            "oos_max_drawdown": self.oos_metrics.max_drawdown,
            "oos_profit_factor": self.oos_metrics.profit_factor,
            "oos_win_rate": self.oos_metrics.win_rate,
            "oos_num_trades": self.oos_metrics.num_trades,
            "oos_num_periods": self.oos_metrics.num_periods,
            "periods_per_year": self.periods_per_year,
        }


class WalkForwardEngine:
    """Runs configurations through the walk-forward window schedule."""

    def __init__(
        self,
        backtester: VectorizedBacktester,
        window_generator: WindowGenerator,
        risk_free_rate: float = 0.0,
        periods_per_year: int | None = None,
        selection_metric: str = "sharpe",
    ) -> None:
        self._backtester = backtester
        self._windows = window_generator
        self._risk_free_rate = risk_free_rate
        self._periods_per_year = periods_per_year
        self._selection_metric = selection_metric

    @property
    def backtester(self) -> VectorizedBacktester:
        """The underlying single-pass backtester, for callers (e.g. Layer 3)
        that need a full-history run outside the window schedule."""
        return self._backtester

    # -- mode 1: score one fixed configuration -------------------------

    def evaluate_configuration(
        self,
        symbol: str,
        strategy_name: str,
        param_key: str,
        params: dict,
        prices: pd.Series,
        signals: pd.Series,
        data: pd.DataFrame | None = None,
    ) -> WalkForwardResult | None:
        """Score one parameter set, reporting IS and OOS performance apart.

        The backtest runs ONCE over full history and the resulting return and
        position series are then sliced into windows. Running it per-window
        instead would zero out each window's first-bar return (pct_change has
        no predecessor inside a slice) and discard the carried-in position —
        both artifacts of slicing, not of the strategy.
        """
        result = self._backtester.run(prices, signals, data=data)
        # Score this asset on its own calendar: a crypto pair trades 365 bars
        # a year, an ETF ~252, and annualizing both at 252 is simply wrong.
        periods_per_year = self._resolve_periods(result.net_returns.index)
        windows = self._windows.generate(result.net_returns.index)
        if not windows:
            logger.debug("No valid walk-forward windows for %s/%s", symbol, strategy_name)
            return None

        net_returns, position = result.net_returns, result.position
        index = net_returns.index

        per_window: list[dict] = []
        is_metric_bundles: list[PerformanceMetrics] = []
        oos_mask = pd.Series(False, index=index)

        for window in windows:
            is_slice = (index >= window.is_start) & (index < window.is_end)
            oos_slice = (index >= window.oos_start) & (index < window.oos_end)
            oos_mask |= pd.Series(oos_slice, index=index)

            is_metrics = compute_metrics(
                net_returns[is_slice],
                position[is_slice],
                self._risk_free_rate,
                periods_per_year,
            )
            oos_metrics = compute_metrics(
                net_returns[oos_slice],
                position[oos_slice],
                self._risk_free_rate,
                periods_per_year,
            )
            is_metric_bundles.append(is_metrics)
            per_window.append(
                {
                    **window.describe(),
                    "is_sharpe": is_metrics.sharpe,
                    "oos_sharpe": oos_metrics.sharpe,
                    "oos_return": oos_metrics.total_return,
                }
            )

        # OOS windows are disjoint -> a boolean union is a continuous,
        # non-double-counted out-of-sample track record.
        stitched_oos_returns = net_returns[oos_mask]
        stitched_oos_position = position[oos_mask]
        oos_metrics = compute_metrics(
            stitched_oos_returns,
            stitched_oos_position,
            self._risk_free_rate,
            periods_per_year,
        )
        is_metrics = self._average_metrics(is_metric_bundles)

        return WalkForwardResult(
            symbol=symbol,
            strategy_name=strategy_name,
            param_key=param_key,
            params=params,
            is_metrics=is_metrics,
            oos_metrics=oos_metrics,
            num_windows=len(windows),
            periods_per_year=periods_per_year,
            oos_returns=stitched_oos_returns,
            oos_position=stitched_oos_position,
            per_window=per_window,
        )

    # -- mode 2: walk-forward parameter optimization -------------------

    def select_and_run(
        self,
        symbol: str,
        strategy_name: str,
        prices: pd.Series,
        signals_by_param: Mapping[str, pd.Series],
        params_by_key: Mapping[str, dict] | None = None,
        data: pd.DataFrame | None = None,
    ) -> WalkForwardResult | None:
        """Classic WFA: choose parameters on each IS window, apply to the
        following OOS window, and stitch the OOS segments together.

        The selection only ever reads in-sample data, so the resulting OOS
        curve is an honest out-of-sample track record of the *process*
        (including its parameter instability), not of a single lucky setting.
        """
        if not signals_by_param:
            return None
        params_by_key = params_by_key or {}

        # One full-history pass per candidate, reused across every window.
        results = {
            key: self._backtester.run(prices, sig, data=data)
            for key, sig in signals_by_param.items()
        }
        any_result = next(iter(results.values()))
        windows = self._windows.generate(any_result.net_returns.index)
        if not windows:
            return None

        index = any_result.net_returns.index
        stitched_returns = pd.Series(0.0, index=index)
        stitched_position = pd.Series(0.0, index=index)
        oos_mask = pd.Series(False, index=index)
        is_metric_bundles: list[PerformanceMetrics] = []
        per_window: list[dict] = []
        chosen_counts: dict[str, int] = {}

        for window in windows:
            is_slice = (index >= window.is_start) & (index < window.is_end)
            oos_slice = (index >= window.oos_start) & (index < window.oos_end)

            best_key, best_is_metrics = self._select_best(results, is_slice)
            if best_key is None:
                continue
            chosen_counts[best_key] = chosen_counts.get(best_key, 0) + 1

            chosen = results[best_key]
            stitched_returns[oos_slice] = chosen.net_returns[oos_slice]
            stitched_position[oos_slice] = chosen.position[oos_slice]
            oos_mask |= pd.Series(oos_slice, index=index)

            oos_metrics = compute_metrics(
                chosen.net_returns[oos_slice],
                chosen.position[oos_slice],
                self._risk_free_rate,
                self._resolve_periods(chosen.net_returns.index),
            )
            is_metric_bundles.append(best_is_metrics)
            per_window.append(
                {
                    **window.describe(),
                    "chosen_param_key": best_key,
                    "is_sharpe": best_is_metrics.sharpe,
                    "oos_sharpe": oos_metrics.sharpe,
                    "oos_return": oos_metrics.total_return,
                }
            )

        if not per_window:
            return None

        oos_metrics = compute_metrics(
            stitched_returns[oos_mask],
            stitched_position[oos_mask],
            self._risk_free_rate,
            self._resolve_periods(index),
        )
        most_common_key = max(chosen_counts, key=chosen_counts.get)
        return WalkForwardResult(
            symbol=symbol,
            strategy_name=strategy_name,
            param_key=f"WFA_optimized(most_common={most_common_key})",
            params=params_by_key.get(most_common_key, {}),
            is_metrics=self._average_metrics(is_metric_bundles),
            oos_metrics=oos_metrics,
            num_windows=len(per_window),
            periods_per_year=self._resolve_periods(index),
            oos_returns=stitched_returns[oos_mask],
            oos_position=stitched_position[oos_mask],
            per_window=per_window,
        )

    # -- internals ------------------------------------------------------

    def _resolve_periods(self, index: pd.DatetimeIndex) -> int:
        """Configured annualization factor, or the series' own if unset."""
        if self._periods_per_year is not None:
            return self._periods_per_year
        return infer_periods_per_year(index)

    def _select_best(self, results: dict, is_slice) -> tuple[str | None, PerformanceMetrics | None]:
        """Pick the configuration with the best in-sample selection metric."""
        best_key, best_metrics, best_score = None, None, -np.inf
        for key, result in results.items():
            metrics = compute_metrics(
                result.net_returns[is_slice],
                result.position[is_slice],
                self._risk_free_rate,
                self._resolve_periods(result.net_returns.index),
            )
            score = getattr(metrics, self._selection_metric)
            if np.isfinite(score) and score > best_score:
                best_key, best_metrics, best_score = key, metrics, score
        return best_key, best_metrics

    @staticmethod
    def _average_metrics(bundles: list[PerformanceMetrics]) -> PerformanceMetrics:
        """Average per-window in-sample metrics.

        Used only for IS, where overlapping windows make concatenation
        double-count. Ratio metrics are averaged; count metrics are summed.
        """
        if not bundles:
            return PerformanceMetrics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0)

        def mean_of(attr: str) -> float:
            values = [getattr(b, attr) for b in bundles]
            finite = [v for v in values if np.isfinite(v)]
            return float(np.mean(finite)) if finite else 0.0

        return PerformanceMetrics(
            total_return=mean_of("total_return"),
            cagr=mean_of("cagr"),
            annual_volatility=mean_of("annual_volatility"),
            sharpe=mean_of("sharpe"),
            max_drawdown=mean_of("max_drawdown"),
            profit_factor=mean_of("profit_factor"),
            num_trades=int(sum(b.num_trades for b in bundles)),
            win_rate=mean_of("win_rate"),
            num_periods=int(sum(b.num_periods for b in bundles)),
        )


# ==========================================================================
# THE 6-STAGE VALIDATION FUNNEL
# ==========================================================================
# The 6-stage validation funnel.
#
# Each filter is a small, independently testable class implementing the
# `Filter` interface (Open/Closed: adding a 7th gate means adding a class and a
# config entry, never editing the funnel). `ValidationFunnel` runs them in
# order and records how many configurations survive each stage.
#
# The funnel operates on a DataFrame with one row per *configuration tested*
# (symbol x strategy x parameter set), as produced by
# `WalkForwardEngine.evaluate_configuration()`. That granularity is required:
# gate 4 compares in-sample against out-of-sample for the same configuration,
# and gate 6 corrects for how many configurations were tried in total.


@dataclass(frozen=True)
class StageReport:
    """How one gate performed: what came in, what survived, and why."""

    stage: int
    name: str
    description: str
    entered: int
    survived: int

    @property
    def rejected(self) -> int:
        return self.entered - self.survived

    @property
    def survival_rate(self) -> float:
        return self.survived / self.entered if self.entered else 0.0

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "name": self.name,
            "description": self.description,
            "entered": self.entered,
            "survived": self.survived,
            "rejected": self.rejected,
            "survival_rate": self.survival_rate,
        }


class Filter(ABC):
    """One gate in the funnel."""

    stage: int
    name: str

    @abstractmethod
    def describe(self) -> str:
        """Human-readable statement of the rule being applied."""

    @abstractmethod
    def apply(self, df: pd.DataFrame, context: dict) -> pd.Series:
        """Return a boolean mask: True == this configuration survives."""


class MinTradesFilter(Filter):
    """Stage 1 — statistical relevance: too few trades means the metrics are
    noise, however good they look."""

    stage = 1
    name = "Trade Viability"

    def __init__(self, min_trades: int = 30) -> None:
        self.min_trades = min_trades

    def describe(self) -> str:
        return f"OOS trades >= {self.min_trades}"

    def apply(self, df: pd.DataFrame, context: dict) -> pd.Series:
        return df["oos_num_trades"] >= self.min_trades


class OOSSharpeFilter(Filter):
    """Stage 2 — risk-adjusted return on data the parameters never saw."""

    stage = 2
    name = "OOS Sharpe Hurdle"

    def __init__(self, min_sharpe: float = 0.5) -> None:
        self.min_sharpe = min_sharpe

    def describe(self) -> str:
        return f"OOS Sharpe > {self.min_sharpe}"

    def apply(self, df: pd.DataFrame, context: dict) -> pd.Series:
        return df["oos_sharpe"] > self.min_sharpe


class MaxDrawdownFilter(Filter):
    """Stage 3 — survivability: a return stream nobody could hold through is
    not tradeable regardless of its Sharpe."""

    stage = 3
    name = "Drawdown Cap"

    def __init__(self, max_drawdown: float = 0.35) -> None:
        self.max_drawdown = max_drawdown

    def describe(self) -> str:
        return f"OOS max drawdown <= {self.max_drawdown:.0%}"

    def apply(self, df: pd.DataFrame, context: dict) -> pd.Series:
        return df["oos_max_drawdown"] <= self.max_drawdown


class OverfittingFilter(Filter):
    """Stage 4 — IS/OOS consistency.

    A configuration whose in-sample Sharpe towers over its out-of-sample
    Sharpe was fitted to historical noise. Configurations that did *better*
    out-of-sample pass trivially (ratio <= 1), which is intended — that is
    not evidence of overfitting.
    """

    stage = 4
    name = "Overfitting Check"

    def __init__(self, max_is_oos_ratio: float = 2.0) -> None:
        self.max_is_oos_ratio = max_is_oos_ratio

    def describe(self) -> str:
        return f"IS Sharpe <= {self.max_is_oos_ratio}x OOS Sharpe"

    def apply(self, df: pd.DataFrame, context: dict) -> pd.Series:
        is_sharpe = df["is_sharpe"]
        oos_sharpe = df["oos_sharpe"]
        # A non-positive OOS Sharpe alongside a positive IS Sharpe is the
        # most extreme overfit case; the ratio is undefined, so reject
        # explicitly rather than letting a divide-by-zero decide.
        degenerate = (oos_sharpe <= 0) & (is_sharpe > 0)
        ratio = is_sharpe / oos_sharpe.where(oos_sharpe > 0, np.nan)
        return (~degenerate) & (ratio.isna() | (ratio <= self.max_is_oos_ratio))


class ProfitFactorFilter(Filter):
    """Stage 5 — right-tail quality: gross wins must meaningfully exceed
    gross losses on unseen data."""

    stage = 5
    name = "Profit Factor"

    def __init__(self, min_profit_factor: float = 1.1) -> None:
        self.min_profit_factor = min_profit_factor

    def describe(self) -> str:
        return f"OOS profit factor >= {self.min_profit_factor}"

    def apply(self, df: pd.DataFrame, context: dict) -> pd.Series:
        return df["oos_profit_factor"] >= self.min_profit_factor


class MultipleComparisonFilter(Filter):
    """Stage 6 — the luck filter.

    Testing thousands of configurations guarantees some will clear every
    prior gate by chance alone. Each survivor's OOS Sharpe is converted to a
    one-sided p-value and corrected for multiplicity.

    The correction uses the number of configurations *originally tested*, not
    the number reaching this stage. Correcting by the survivor count would
    understate the search that produced them — the earlier gates are part of
    the same selection process, and the hypotheses they discarded were still
    tested.
    """

    stage = 6
    name = "Multiple Comparison Correction"

    #: How to count the number of hypotheses actually tested.
    #:
    #: "all" treats every configuration as an independent test. That is the
    #: textbook default and it is wrong here: RSI(14) and RSI(15) on SPY are
    #: overwhelmingly the same bet, not two discoveries. Dividing alpha by a
    #: count inflated with near-duplicates makes the gate impossible to pass
    #: for reasons that have nothing to do with the evidence.
    #:
    #: "clustered" counts distinct symbol x strategy ideas, treating a
    #: parameter sweep within one idea as the single hypothesis it is. Still
    #: conservative — assets in one universe are themselves correlated — but
    #: far closer to the real multiplicity.
    MULTIPLICITY_MODES = ("all", "clustered", "strategies")

    def __init__(
        self, method: str = "fdr", alpha: float = 0.05, multiplicity: str = "clustered"
    ) -> None:
        if method not in ("bonferroni", "fdr"):
            raise ValueError("method must be 'bonferroni' or 'fdr'")
        if multiplicity not in self.MULTIPLICITY_MODES:
            raise ValueError(f"multiplicity must be one of {self.MULTIPLICITY_MODES}")
        self.method = method
        self.alpha = alpha
        self.multiplicity = multiplicity

    def describe(self) -> str:
        return f"{self.method} correction at alpha={self.alpha} ({self.multiplicity} multiplicity)"

    def effective_tests(self, context: dict) -> int:
        """How many genuinely distinct hypotheses were tested."""
        counts = context.get("multiplicity_counts", {})
        total = int(counts.get("all", context.get("num_configurations_tested", 1)))
        if self.multiplicity == "all":
            return max(total, 1)
        return max(int(counts.get(self.multiplicity, total)), 1)

    def apply(self, df: pd.DataFrame, context: dict) -> pd.Series:
        if df.empty:
            return pd.Series(dtype=bool, index=df.index)

        default_periods = int(context.get("periods_per_year") or TRADING_DAYS_PER_YEAR)
        num_tests = self.effective_tests(context)

        # Correct over EVERY hypothesis the search tested, not just the few
        # that reached this gate — the earlier gates already selected on
        # performance, so this frame is the survivors of a search, not the
        # search itself.
        population = context.get("all_configurations")
        if population is None:
            population = df

        keys = self._cluster_keys(population)
        # Both the population and the frame under test are scored the same
        # way, so the two correction methods stay comparable.
        p_population, sizes = self._adjusted_p_values(population, default_periods, keys)
        p_here, _ = self._adjusted_p_values(df, default_periods, keys, sizes=sizes)

        if keys:
            # One p-value per idea: its best variant, already penalised for
            # how many variants were tried.
            candidates = p_population.groupby(
                [population[k].values for k in keys]
            ).min()
        else:
            candidates = p_population
        cutoff = self._cutoff(candidates.to_numpy(), max(num_tests, len(candidates)))

        annotations = context.setdefault("annotations", {})
        annotations["p_value"] = p_here
        annotations["p_threshold"] = pd.Series(cutoff, index=df.index)

        return p_here <= cutoff

    def _cluster_keys(self, frame: pd.DataFrame) -> list[str]:
        """Which columns define one distinct hypothesis."""
        if self.multiplicity == "all":
            return []
        if self.multiplicity == "strategies":
            keys = ["strategy"]
        else:
            keys = ["symbol", "strategy"]
        return keys if set(keys).issubset(frame.columns) else []

    def _adjusted_p_values(
        self,
        frame: pd.DataFrame,
        default_periods: int,
        keys: list[str],
        sizes: pd.Series | None = None,
    ) -> tuple[pd.Series, pd.Series | None]:
        """P-values with the within-idea parameter search priced in.

        Sweeping twelve RSI settings and reporting the best is a search. Left
        unpriced it manufactures significance, so the winner's p-value pays a
        Bonferroni penalty for the variants it beat.
        """
        raw = _sharpe_p_values(frame, default_periods)
        if not keys:
            return raw, sizes

        index = pd.MultiIndex.from_arrays([frame[k] for k in keys])
        if sizes is None:
            sizes = raw.groupby([frame[k].values for k in keys]).size()
        variants = pd.Series(
            index.map(lambda key: sizes.get(key, 1)), index=frame.index
        ).astype(float)
        return (raw * variants).clip(upper=1.0), sizes

    def _cutoff(self, p_values, num_tests: int) -> float:
        """The largest p-value that still counts as a discovery."""
        import numpy as _np

        num_tests = max(int(num_tests), 1)
        finite = _np.sort(_np.asarray([p for p in p_values if _np.isfinite(p)]))
        if finite.size == 0:
            return 0.0

        if self.method == "bonferroni":
            return self.alpha / num_tests

        # Benjamini-Hochberg: find the largest k with p_(k) <= (k/m) * alpha,
        # then reject everything at or below THAT RANK'S THRESHOLD. Returning
        # the observed p_(k) instead would make BH stricter than Bonferroni
        # whenever the winning p-value sits well under its own threshold.
        ranks = _np.arange(1, finite.size + 1)
        thresholds = ranks / num_tests * self.alpha
        passing = _np.flatnonzero(finite <= thresholds)
        if passing.size == 0:
            return 0.0
        return float(thresholds[passing[-1]])


def _multiplicity_counts(configurations: pd.DataFrame) -> dict:
    """Count hypotheses at each level of aggregation.

    A parameter sweep is one idea explored several ways, not several
    independent discoveries — counting it as the latter is what makes the
    correction reject everything regardless of the evidence.
    """
    counts = {"all": len(configurations)}
    if {"symbol", "strategy"}.issubset(configurations.columns):
        counts["clustered"] = configurations.groupby(["symbol", "strategy"]).ngroups
        counts["strategies"] = configurations["strategy"].nunique()
    return counts


def _sharpe_p_values(frame: pd.DataFrame, default_periods: int) -> pd.Series:
    """One-sided p-value per configuration, on its own calendar."""
    return frame.apply(
        lambda row: sharpe_p_value(
            row["oos_sharpe"],
            int(row["oos_num_periods"]),
            int(row.get("periods_per_year") or default_periods),
        ),
        axis=1,
    )


@dataclass
class FunnelResult:
    """Everything Layer 3 needs to visualize and audit the funnel."""

    survivors: pd.DataFrame
    stage_reports: list[StageReport]
    all_configurations: pd.DataFrame

    def summary_table(self) -> pd.DataFrame:
        """Survivor summary: strategy, ticker, IS/OOS Sharpe, drawdown, trades."""
        columns = [
            "symbol",
            "strategy",
            "param_key",
            # `params` is carried through so Layer 3 can rebuild the exact
            # strategy from a round-tripped CSV, not just from memory.
            "params",
            "is_sharpe",
            "oos_sharpe",
            "oos_max_drawdown",
            "oos_profit_factor",
            "oos_total_return",
            "oos_num_trades",
        ]
        available = [c for c in columns if c in self.survivors.columns]
        if self.survivors.empty:
            # Emit the full expected header even with zero rows: a file with no
            # columns is not parseable, and "nothing survived" is a normal
            # outcome that downstream layers must be able to read.
            return pd.DataFrame(columns=available or columns)
        return self.survivors[available].sort_values("oos_sharpe", ascending=False)

    def funnel_report(self) -> pd.DataFrame:
        """Stage-by-stage survival counts."""
        return pd.DataFrame([r.to_dict() for r in self.stage_reports])

    def scatter_coordinates(self) -> pd.DataFrame:
        """IS vs OOS Sharpe coordinates for every configuration tested.

        `survived` marks the final survivors and `rejected_at_stage` records
        where each casualty died — so the Layer 3 plot can show overfitting
        as the cloud of points sitting far below the y=x diagonal.
        """
        df = self.all_configurations.copy()
        columns = [
            "symbol",
            "strategy",
            "param_key",
            "is_sharpe",
            "oos_sharpe",
            "survived",
            "rejected_at_stage",
        ]
        return df[[c for c in columns if c in df.columns]]


class ValidationFunnel:
    """Runs configurations through the ordered gauntlet of filters."""

    def __init__(self, filters: list[Filter], periods_per_year: int | None = None) -> None:
        self._filters = sorted(filters, key=lambda f: f.stage)
        self._periods_per_year = periods_per_year

    @classmethod
    def from_config(
        cls, config: dict, periods_per_year: int | None = None
    ) -> "ValidationFunnel":
        """Build the standard 6-stage funnel from the `funnel` config block."""
        f6 = config.get("filter_6", {})
        return cls(
            filters=[
                MinTradesFilter(int(config["filter_1_min_trades"])),
                OOSSharpeFilter(float(config["filter_2_min_oos_sharpe"])),
                MaxDrawdownFilter(float(config["filter_3_max_drawdown"])),
                OverfittingFilter(float(config["filter_4_max_is_oos_sharpe_ratio"])),
                ProfitFactorFilter(float(config["filter_5_min_profit_factor"])),
                MultipleComparisonFilter(
                    method=f6.get("method", "fdr"),
                    alpha=float(f6.get("alpha", 0.05)),
                    multiplicity=f6.get("multiplicity", "clustered"),
                ),
            ],
            periods_per_year=periods_per_year,
        )

    def run(self, configurations: pd.DataFrame) -> FunnelResult:
        """Apply every gate in order, logging survivors at each step."""
        if configurations.empty:
            logger.warning("Validation funnel received zero configurations")
            return FunnelResult(
                survivors=configurations.copy(),
                stage_reports=[],
                all_configurations=configurations.copy(),
            )

        all_configs = configurations.copy().reset_index(drop=True)
        all_configs["survived"] = True
        all_configs["rejected_at_stage"] = pd.NA

        context = {
            "num_configurations_tested": len(all_configs),
            "multiplicity_counts": _multiplicity_counts(all_configs),
            "all_configurations": all_configs,
            "periods_per_year": self._periods_per_year,
            "annotations": {},
        }

        current = all_configs.copy()
        reports: list[StageReport] = []
        logger.info("Validation funnel: %s configurations entering", len(current))

        for filt in self._filters:
            entered = len(current)
            if entered == 0:
                reports.append(
                    StageReport(filt.stage, filt.name, filt.describe(), 0, 0)
                )
                logger.info(
                    "Stage %s (%s): no configurations left to test", filt.stage, filt.name
                )
                continue

            mask = filt.apply(current, context).fillna(False)
            rejected_idx = current.index[~mask]
            all_configs.loc[rejected_idx, "survived"] = False
            all_configs.loc[rejected_idx, "rejected_at_stage"] = filt.stage

            current = current[mask]
            report = StageReport(
                stage=filt.stage,
                name=filt.name,
                description=filt.describe(),
                entered=entered,
                survived=len(current),
            )
            reports.append(report)
            logger.info(
                "Stage %s (%s | %s): %s -> %s survived (%.1f%%)",
                filt.stage,
                filt.name,
                filt.describe(),
                entered,
                len(current),
                report.survival_rate * 100,
            )

        for name, series in context.get("annotations", {}).items():
            all_configs.loc[series.index, name] = series

        logger.info(
            "Funnel complete: %s/%s configurations survived all %s stages",
            len(current),
            len(all_configs),
            len(self._filters),
        )
        return FunnelResult(
            survivors=current.reset_index(drop=True),
            stage_reports=reports,
            all_configurations=all_configs,
        )


# ==========================================================================
# SIGNAL REPOSITORIES
# ==========================================================================
# Access to Layer 1's generated signal matrices.
#
# Dependency inversion: Layer 2 depends on the `SignalRepository` interface, so
# it can run against signals loaded from Layer 1's parquet output (the normal
# path), regenerated on the fly, or handed in by a test — without knowing which.


@dataclass(frozen=True)
class SignalSet:
    """One configuration's signal series plus the parameters that made it."""

    param_key: str
    params: dict
    signals: pd.Series


def build_param_key_map(grid: dict[str, list]) -> dict[str, dict]:
    """Map every param_key in a grid back to its parameter dict.

    `BaseStrategy.param_key()` is deterministic (sorted key=value pairs), so
    regenerating the grid reproduces exactly the keys Layer 1 wrote to disk.
    This avoids parsing filenames, which is ambiguous when parameter names
    themselves contain underscores.
    """
    if not grid:
        return {"": {}}
    keys = list(grid.keys())
    combos = itertools.product(*(grid[k] for k in keys))
    result: dict[str, dict] = {}
    for combo in combos:
        params = dict(zip(keys, combo))
        key = "_".join(f"{k}={v}" for k, v in sorted(params.items()))
        result[key] = params
    return result


class SignalRepository(ABC):
    """Interface for retrieving a strategy's signal sets for one symbol."""

    @abstractmethod
    def load(self, symbol: str, strategy_name: str) -> list[SignalSet]:
        """Return every parameter configuration's signals for this pairing."""


class FileSignalRepository(SignalRepository):
    """Loads the signal matrices Layer 1 persisted under `results/`."""

    def __init__(
        self,
        signals_dir: Path,
        strategy_grid: dict[str, dict[str, list]],
        file_format: str = "parquet",
    ) -> None:
        self._signals_dir = Path(signals_dir)
        self._strategy_grid = strategy_grid
        self._format = file_format

    def load(self, symbol: str, strategy_name: str) -> list[SignalSet]:
        directory = self._signals_dir / strategy_name / symbol
        if not directory.is_dir():
            logger.debug("No Layer 1 signals at %s", directory)
            return []

        key_to_params = build_param_key_map(self._strategy_grid.get(strategy_name, {}))
        signal_sets: list[SignalSet] = []

        for path in sorted(directory.glob(f"*.{self._format}")):
            param_key = path.stem
            try:
                frame = (
                    pd.read_parquet(path)
                    if self._format == "parquet"
                    else pd.read_csv(path, index_col=0, parse_dates=True)
                )
                series = frame.iloc[:, 0]
                series.index = pd.to_datetime(series.index)
                signal_sets.append(
                    SignalSet(
                        param_key=param_key,
                        params=key_to_params.get(param_key, {}),
                        signals=series.astype(float),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one bad file must not kill the sweep
                logger.error("Could not read signals from %s: %s", path, exc)

        return signal_sets


class GeneratedSignalRepository(SignalRepository):
    """Generates signals on demand from the strategy registry.

    Useful for running Layer 2 standalone (no Layer 1 artifacts on disk) and
    for tests, which should never depend on a prior batch run.
    """

    def __init__(
        self,
        price_data: dict[str, pd.DataFrame],
        strategy_grid: dict[str, dict[str, list]],
    ) -> None:
        self._price_data = price_data
        self._strategy_grid = strategy_grid

    def load(self, symbol: str, strategy_name: str) -> list[SignalSet]:
        data = self._price_data.get(symbol)
        if data is None:
            return []

        strategy_class = get_strategy_class(strategy_name)
        key_to_params = build_param_key_map(self._strategy_grid.get(strategy_name, {}))
        signal_sets: list[SignalSet] = []

        for param_key, params in key_to_params.items():
            try:
                strategy = strategy_class(**params)
                signal_sets.append(
                    SignalSet(
                        param_key=strategy.param_key(),
                        params=params,
                        signals=strategy.generate_signals(data).astype(float),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Signal generation failed for %s/%s/%s: %s",
                    symbol,
                    strategy_name,
                    param_key,
                    exc,
                )
        return signal_sets


# ==========================================================================
# BATCH SIGNAL GENERATOR (LAYER 1 SWEEP)
# ==========================================================================
# Batch signal generator: asset x strategy x parameter-grid fan-out.
#
# Single Responsibility: this module only orchestrates — it builds parameter
# combinations from config, calls HistoricalDataManager and BaseStrategy
# subclasses (both injected), and writes results. It contains no indicator
# math and no data-download logic itself.


@dataclass(frozen=True)
class RunResult:
    symbol: str
    strategy_name: str
    param_key: str
    status: str  # "ok" | "error"
    detail: str = ""


class ParameterGridBuilder:
    """Expands a {param_name: [values...]} grid into a list of concrete
    parameter dicts (cartesian product)."""

    @staticmethod
    def build(grid: dict[str, list]) -> list[dict]:
        if not grid:
            return [{}]
        keys = list(grid.keys())
        value_lists = [grid[k] for k in keys]
        return [dict(zip(keys, combo)) for combo in itertools.product(*value_lists)]


class BatchSignalGenerator:
    """Loops over every (asset, strategy, parameter-combo) triple, generates
    signals, and persists them — one failing combination is logged and
    skipped rather than aborting the whole batch, since a 9,000-combo sweep
    should not die because one edge case (e.g. insufficient history for a
    long lookback) raises."""

    def __init__(
        self,
        data_manager: HistoricalDataManager,
        strategy_grid: dict[str, dict[str, list]],
        output_config: OutputConfig,
    ) -> None:
        self._data_manager = data_manager
        self._strategy_grid = strategy_grid
        self._output_config = output_config
        self._output_config.results_dir.mkdir(parents=True, exist_ok=True)

    def run(self, symbols: list[str]) -> list[RunResult]:
        history_by_symbol = self._data_manager.get_universe_history(symbols)
        results: list[RunResult] = []

        for symbol, data in history_by_symbol.items():
            for strategy_name, grid in self._strategy_grid.items():
                strategy_class = get_strategy_class(strategy_name)
                for params in ParameterGridBuilder.build(grid):
                    results.append(self._run_one(symbol, data, strategy_class, params))

        ok = sum(1 for r in results if r.status == "ok")
        logger.info("Batch complete: %s/%s configurations succeeded", ok, len(results))
        return results

    def _run_one(self, symbol: str, data: pd.DataFrame, strategy_class, params: dict) -> RunResult:
        strategy = None
        try:
            strategy = strategy_class(**params)
            signals = strategy.generate_signals(data)
            self._write_signals(symbol, strategy, signals)
            return RunResult(symbol, strategy_class.__name__, strategy.param_key(), "ok")
        except Exception as exc:  # noqa: BLE001 - batch must survive per-combo failures
            param_key = strategy.param_key() if strategy is not None else str(params)
            logger.error(
                "Failed %s/%s/%s: %s", symbol, strategy_class.__name__, param_key, exc
            )
            return RunResult(symbol, strategy_class.__name__, param_key, "error", str(exc))

    def _write_signals(self, symbol: str, strategy, signals: pd.Series) -> None:
        out_dir = self._output_config.results_dir / strategy.name / symbol
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{strategy.param_key() or 'default'}.{self._output_config.format}"

        if self._output_config.format == "parquet":
            signals.to_frame().to_parquet(out_path)
        elif self._output_config.format == "csv":
            signals.to_frame().to_csv(out_path)
        else:
            raise ValueError(f"unsupported output format: {self._output_config.format}")


# ==========================================================================
# LAYER 2 PIPELINE
# ==========================================================================
# Layer 2 orchestration: walk-forward evaluation -> validation funnel -> artifacts.
#
# Single Responsibility: wiring only. Every piece of math lives in metrics.py,
# backtest.py, walk_forward.py or validation.py; this module just connects them
# and writes the outputs Layer 3 consumes.


@dataclass
class Layer2Output:
    """Everything Layer 2 hands downstream."""

    configurations: pd.DataFrame
    funnel_result: FunnelResult
    optimized_results: pd.DataFrame


class Layer2Pipeline:
    """Runs every (symbol, strategy, parameter set) through walk-forward
    evaluation, then through the validation funnel."""

    def __init__(
        self,
        engine: WalkForwardEngine,
        repository: SignalRepository,
        funnel: ValidationFunnel,
        config: BacktestConfig,
    ) -> None:
        self._engine = engine
        self._repository = repository
        self._funnel = funnel
        self._config = config

    def run(
        self,
        price_data: dict[str, pd.DataFrame],
        strategy_names: list[str],
        run_optimized: bool = True,
    ) -> Layer2Output:
        rows: list[dict] = []
        optimized_rows: list[dict] = []

        for symbol, data in price_data.items():
            prices = data["Close"]
            for strategy_name in strategy_names:
                signal_sets = self._repository.load(symbol, strategy_name)
                if not signal_sets:
                    logger.debug("No signals for %s/%s", symbol, strategy_name)
                    continue

                for signal_set in signal_sets:
                    result = self._safe_evaluate(
                        symbol, strategy_name, signal_set, prices, data=data
                    )
                    if result is not None:
                        rows.append(result.to_row())

                if run_optimized:
                    optimized = self._safe_optimize(
                        symbol, strategy_name, signal_sets, prices
                    )
                    if optimized is not None:
                        optimized_rows.append(optimized.to_row())

        configurations = pd.DataFrame(rows)
        logger.info(
            "Walk-forward evaluation complete: %s configurations scored", len(configurations)
        )

        funnel_result = self._funnel.run(configurations)
        return Layer2Output(
            configurations=configurations,
            funnel_result=funnel_result,
            optimized_results=pd.DataFrame(optimized_rows),
        )

    def write_artifacts(self, output: Layer2Output) -> None:
        """Persist funnel report, survivors, scatter coordinates, and the
        optimized-WFA table for Layer 3."""
        out_dir = self._config.layer2_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        funnel = output.funnel_result
        self._write_frame(funnel.summary_table(), out_dir / "survivors.csv")
        self._write_frame(funnel.funnel_report(), out_dir / "funnel_report.csv")
        self._write_frame(funnel.scatter_coordinates(), out_dir / "is_oos_scatter.csv")
        self._write_frame(output.configurations, out_dir / "all_configurations.csv")
        self._write_frame(output.optimized_results, out_dir / "wfa_optimized.csv")

        manifest = {
            "num_configurations_tested": int(len(output.configurations)),
            "num_survivors": int(len(funnel.survivors)),
            "stages": [r.to_dict() for r in funnel.stage_reports],
            "costs": self._config.costs,
            "walk_forward": {
                "mode": self._config.walk_forward.mode,
                "is_years": self._config.walk_forward.is_years,
                "oos_years": self._config.walk_forward.oos_years,
                "step_years": self._config.walk_forward.step_years,
            },
            "funnel_thresholds": self._config.funnel,
        }
        with (out_dir / "manifest.json").open("w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, default=str)
        logger.info("Layer 2 artifacts written to %s", out_dir)

    # -- internals ------------------------------------------------------

    def _safe_evaluate(
        self,
        symbol: str,
        strategy_name: str,
        signal_set,
        prices: pd.Series,
        data: pd.DataFrame | None = None,
    ) -> WalkForwardResult | None:
        try:
            return self._engine.evaluate_configuration(
                symbol=symbol,
                strategy_name=strategy_name,
                param_key=signal_set.param_key,
                params=signal_set.params,
                prices=prices,
                signals=signal_set.signals,
                # Needed only when a risk overlay is configured; stops read
                # the intrabar high/low that a close series cannot supply.
                data=data,
            )
        except Exception as exc:  # noqa: BLE001 - one config must not kill the sweep
            logger.error(
                "Walk-forward failed for %s/%s/%s: %s",
                symbol,
                strategy_name,
                signal_set.param_key,
                exc,
            )
            return None

    def _safe_optimize(
        self, symbol: str, strategy_name: str, signal_sets: list, prices: pd.Series
    ) -> WalkForwardResult | None:
        try:
            return self._engine.select_and_run(
                symbol=symbol,
                strategy_name=strategy_name,
                prices=prices,
                signals_by_param={s.param_key: s.signals for s in signal_sets},
                params_by_key={s.param_key: s.params for s in signal_sets},
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("WFA optimization failed for %s/%s: %s", symbol, strategy_name, exc)
            return None

    @staticmethod
    def _write_frame(frame: pd.DataFrame, path: Path) -> None:
        frame.to_csv(path, index=False)
