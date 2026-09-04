"""Layer 4 - market-regime detection, risk management and dynamic allocation.

Three components, each independently testable:

  * `MarketRegimeDetector` — an unsupervised Gaussian HMM over volatility,
    trend and normalized-ATR features, segmenting history into bear /
    trending / ranging states.
  * `RiskManager` — ATR volatility-targeted position sizing plus two hard
    gates: a rolling-drawdown equity-curve stop and a portfolio heat cap.
  * `DynamicRegimePortfolio` — routes capital to the strategy families
    appropriate to the active regime, blending overlapping signals by
    inverse correlation.

Look-ahead discipline carries through from the lower layers, and matters more
here because an unsupervised classifier is an easy place to leak the future:

  1. The HMM is FIT on a training prefix only. Regimes for later dates are
     inferred with the frozen model, never re-fit (`train_fraction`).
  2. State labels are assigned from TRAINING-period statistics only, so the
     mapping "state 2 == the bear one" cannot be informed by out-of-sample
     behaviour.
  3. The regime driving day t's allocation is the one CONFIRMED as of day
     t-1, shifted forward. Nobody knows today's regime at today's open.
  4. Persistence filtering and every risk gate likewise read only past data.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .backtest import (
    TRADING_DAYS_PER_YEAR,
    infer_periods_per_year,
    PerformanceMetrics,
    VectorizedBacktester,
    compute_drawdown_series,
    compute_equity_curve,
    compute_max_drawdown,
    compute_metrics,
)
from .config import AllocationConfig, RegimeConfig, RegimeDetectionConfig, RiskConfig
from .strategies import atr, get_strategy_class, sma

logger = logging.getLogger(__name__)


# ==========================================================================
# REGIME LABELS
# ==========================================================================


class Regime:
    """Canonical regime codes. These are semantic labels, not raw HMM states.

    The HMM assigns arbitrary integer states; `MarketRegimeDetector` maps
    them onto these meanings using training-period feature statistics.
    """

    BEAR = 0        # high volatility, downtrend
    TRENDING = 1    # low-to-mid volatility, uptrend
    RANGING = 2     # low volatility, sideways

    NAMES = {BEAR: "Bear/Crash", TRENDING: "Bull/Trending", RANGING: "Choppy/Ranging"}

    @classmethod
    def name(cls, code: int) -> str:
        return cls.NAMES.get(int(code), f"Unknown({code})")


# ==========================================================================
# MARKET REGIME DETECTOR (HIDDEN MARKOV MODEL)
# ==========================================================================


@dataclass
class RegimeFeatures:
    """The feature matrix fed to the HMM, plus the index it belongs to."""

    frame: pd.DataFrame

    @property
    def matrix(self) -> np.ndarray:
        return self.frame.to_numpy()

    @property
    def index(self) -> pd.DatetimeIndex:
        return self.frame.index


class MarketRegimeDetector:
    """Segments market history into hidden regimes with a Gaussian HMM.

    Feature set (all strictly causal — each day's value uses only that day
    and earlier):
      * `volatility`   — rolling std of daily returns, annualized
      * `trend`        — fast SMA / slow SMA ratio, centred on 0
      * `atr_norm`     — ATR as a fraction of price
      * `momentum`     — trailing return over the volatility window

    The model is unsupervised: it finds structure without being told what a
    bear market looks like. `_label_states` then assigns semantic meaning to
    the discovered states using training-period statistics only.
    """

    def __init__(self, config: RegimeDetectionConfig) -> None:
        self._config = config
        self._model = None
        self._state_to_regime: dict[int, int] = {}
        self._scaler_mean: np.ndarray | None = None
        self._scaler_std: np.ndarray | None = None
        self._training_end: pd.Timestamp | None = None

    # -- feature engineering -------------------------------------------

    def build_features(self, data: pd.DataFrame) -> RegimeFeatures:
        """Compute the regime feature matrix from OHLCV benchmark data."""
        close = data["Close"]
        returns = close.pct_change()
        cfg = self._config

        volatility = returns.rolling(cfg.volatility_window).std() * np.sqrt(
            TRADING_DAYS_PER_YEAR
        )
        fast = sma(close, cfg.fast_ma)
        slow = sma(close, cfg.slow_ma)
        trend = (fast / slow) - 1.0
        atr_norm = atr(data["High"], data["Low"], close, cfg.atr_period) / close
        momentum = close.pct_change(cfg.volatility_window)

        frame = pd.DataFrame(
            {
                "volatility": volatility,
                "trend": trend,
                "atr_norm": atr_norm,
                "momentum": momentum,
            }
        ).dropna()
        return RegimeFeatures(frame)

    # -- fitting and inference -----------------------------------------

    def fit(self, data: pd.DataFrame) -> "MarketRegimeDetector":
        """Fit the HMM on the training prefix of `data`.

        Only the first `train_fraction` of observations is used. Fitting on
        the full history would let the classifier's own parameters be shaped
        by data the strategies are later evaluated on — a subtle but real
        look-ahead leak.
        """
        from hmmlearn.hmm import GaussianHMM

        features = self.build_features(data)
        if len(features.frame) < self._config.n_states * 10:
            raise ValueError(
                f"insufficient history to fit a {self._config.n_states}-state HMM: "
                f"{len(features.frame)} usable rows"
            )

        split = max(int(len(features.frame) * self._config.train_fraction), 1)
        train = features.frame.iloc[:split]
        self._training_end = train.index[-1]

        # Standardize using TRAINING statistics only, then reuse them at
        # inference — recomputing on the full sample would leak future scale.
        self._scaler_mean = train.to_numpy().mean(axis=0)
        self._scaler_std = train.to_numpy().std(axis=0)
        self._scaler_std[self._scaler_std == 0] = 1.0

        self._model = GaussianHMM(
            n_components=self._config.n_states,
            covariance_type=self._config.covariance_type,
            n_iter=self._config.n_iter,
            random_state=self._config.random_seed,
        )
        self._model.fit(self._standardize(train.to_numpy()))

        train_states = self._model.predict(self._standardize(train.to_numpy()))
        self._state_to_regime = self._label_states(train, train_states)

        logger.info(
            "HMM fit on %s training rows (through %s); state->regime map: %s",
            len(train),
            self._training_end.date(),
            {k: Regime.name(v) for k, v in self._state_to_regime.items()},
        )
        return self

    def predict_regime(self, data: pd.DataFrame) -> pd.Series:
        """Append a regime label (0/1/2) to each trading day.

        Returns a Series indexed by date. Days before enough history exists
        to compute features are absent from the result; callers should
        reindex and treat missing days as "no regime opinion yet".
        """
        if self._model is None:
            self.fit(data)

        features = self.build_features(data)
        raw_states = self._model.predict(self._standardize(features.matrix))
        regimes = pd.Series(
            [self._state_to_regime.get(int(s), Regime.RANGING) for s in raw_states],
            index=features.index,
            name="regime",
        )
        return self._apply_persistence(regimes)

    def annotate(self, data: pd.DataFrame) -> pd.DataFrame:
        """Return `data` with `regime` and `regime_name` columns attached."""
        regimes = self.predict_regime(data)
        out = data.copy()
        out["regime"] = regimes.reindex(out.index)
        out["regime_name"] = out["regime"].map(
            lambda r: Regime.name(r) if pd.notna(r) else "Unclassified"
        )
        return out

    # -- internals ------------------------------------------------------

    def _standardize(self, matrix: np.ndarray) -> np.ndarray:
        return (matrix - self._scaler_mean) / self._scaler_std

    def _label_states(self, train: pd.DataFrame, states: np.ndarray) -> dict[int, int]:
        """Map arbitrary HMM state ids onto semantic regime codes.

        The HMM has no idea which of its states is "the bear one" — the ids
        are arbitrary and vary with the seed. Assignment is by training-period
        feature means:
          * highest volatility combined with weakest trend -> BEAR
          * positive trend at non-elevated volatility      -> TRENDING
          * whatever remains                               -> RANGING

        The mapping is deliberately many-to-one. An HMM given three states
        will use all three even when the data contains only two distinct
        regimes, splitting one of them into near-identical twins. Forcing a
        1:1 state->regime assignment then labels one twin BEAR and the other
        TRENDING purely by tie-breaking noise, producing a regime series that
        flips between them every few days. Letting both twins carry the same
        label keeps the output honest: the detector reports the structure it
        actually found, not the structure it was asked to find.
        """
        stats = train.copy()
        stats["_state"] = states
        grouped = stats.groupby("_state")[["volatility", "trend", "momentum"]].mean()
        self._warn_if_degenerate(grouped)

        # Standardize across states so volatility and trend are comparable
        # despite living on different scales.
        def _z(series: pd.Series) -> pd.Series:
            spread = series.std(ddof=0)
            return (series - series.mean()) / (spread if spread > 0 else 1.0)

        vol_z = _z(grouped["volatility"])
        trend_z = _z(grouped["trend"])

        # Bear affinity is high when volatility is high AND trend is weak.
        # Requiring both avoids labelling a fast melt-up (also high-vol) as a
        # crash. Note the sign: high vol_z and NEGATIVE trend_z maximise this.
        bear_affinity = vol_z - trend_z
        candidate = int(bear_affinity.idxmax())
        candidate_row = grouped.loc[candidate]

        # A bear label must be earned, not merely ranked into.
        #
        # Some state always has the highest bear affinity, but "highest of
        # three" is not the same as "bearish". Over a 15-year bull market the
        # top-ranked state can still have a POSITIVE trend — labelling it a
        # crash then puts most of history into the defensive bucket and
        # de-risks the portfolio permanently. Require the candidate to
        # actually be falling (negative trend or negative momentum) at
        # above-average volatility before calling it a bear regime; if no
        # state qualifies, this fit simply contains no crash state, and
        # saying so is more useful than inventing one.
        is_genuine_bear = (
            (candidate_row["trend"] < 0 or candidate_row["momentum"] < 0)
            and vol_z[candidate] > 0
        )

        median_vol = grouped["volatility"].median()
        mapping: dict[int, int] = {}
        if is_genuine_bear:
            mapping[candidate] = Regime.BEAR
        else:
            logger.info(
                "No state met the bear criteria (best candidate: trend=%.4f, "
                "momentum=%.4f); this fit has no crash regime.",
                candidate_row["trend"],
                candidate_row["momentum"],
            )

        for state, row in grouped.iterrows():
            state = int(state)
            if state in mapping:
                continue
            if row["trend"] > 0 and row["volatility"] <= median_vol:
                mapping[state] = Regime.TRENDING
            else:
                mapping[state] = Regime.RANGING
        return mapping

    @staticmethod
    def _warn_if_degenerate(grouped: pd.DataFrame, tolerance: float = 0.02) -> None:
        """Warn when two fitted states are effectively the same regime.

        An over-parameterized HMM will happily spend two states describing one
        market condition, which silently costs a whole regime: with three
        states and two of them duplicated, there is nothing left to represent
        crashes. This is quiet and easy to miss, so say it out loud.
        """
        states = list(grouped.index)
        for i, a in enumerate(states):
            for b in states[i + 1 :]:
                spread = (grouped.loc[a] - grouped.loc[b]).abs()
                scale = grouped.loc[[a, b]].abs().max().replace(0.0, 1.0)
                if (spread / scale).max() < tolerance:
                    logger.warning(
                        "HMM states %s and %s are near-identical (features within "
                        "%.0f%%); the model is wasting a state and may not isolate "
                        "a distinct regime. Consider a more constrained "
                        "covariance_type.",
                        a,
                        b,
                        tolerance * 100,
                    )

    def _apply_persistence(self, regimes: pd.Series) -> pd.Series:
        """Suppress regime flips that do not persist.

        A single-day classification blip would otherwise reroute the entire
        portfolio and immediately reroute it back, paying the turnover cost
        twice for no information. A new regime is adopted only once it has
        held for `min_regime_persistence_days` consecutive days; until then
        the previous confirmed regime stands.
        """
        min_days = self._config.min_regime_persistence_days
        if min_days <= 1 or regimes.empty:
            return regimes

        values = regimes.to_numpy()
        confirmed = np.empty_like(values)
        current = values[0]
        run_value, run_length = None, 0

        for i, value in enumerate(values):
            if value == run_value:
                run_length += 1
            else:
                run_value, run_length = value, 1
            if run_length >= min_days:
                current = run_value
            confirmed[i] = current

        return pd.Series(confirmed, index=regimes.index, name="regime")


# ==========================================================================
# RISK MANAGEMENT AND POSITION SIZING
# ==========================================================================


@dataclass
class RiskState:
    """Diagnostics from a risk-gate pass, for auditing why exposure moved."""

    position_scale: pd.Series
    equity_stop_active: pd.Series
    heat_scale: pd.Series


class RiskManager:
    """Volatility-targeted sizing plus hard risk gates.

    Three independent controls, applied in order:
      1. ATR position sizing — equalize risk per trade, so a calm asset gets
         a larger notional than a wild one for the same risk budget.
      2. Equity-curve stop — cut a strategy's exposure when its own rolling
         drawdown breaches a threshold.
      3. Portfolio heat cap — bound total simultaneous gross exposure.
    """

    def __init__(self, config: RiskConfig) -> None:
        self._config = config

    # -- 1. ATR-based position sizing ----------------------------------

    def atr_position_scale(self, data: pd.DataFrame) -> pd.Series:
        """Exposure multiplier that targets constant volatility.

        `scale = target_vol / realized_vol`, where realized volatility is
        derived from ATR as a fraction of price and annualized. High-ATR
        periods scale exposure DOWN, quiet periods scale it UP, equalizing
        risk contribution across assets and across time.

        Shifted by one day: today's size uses volatility known as of
        yesterday's close.
        """
        close = data["Close"]
        atr_values = atr(data["High"], data["Low"], close, self._config.atr_period)
        daily_vol = (atr_values / close).replace(0.0, np.nan)
        annual_vol = daily_vol * np.sqrt(TRADING_DAYS_PER_YEAR)

        scale = self._config.target_annual_volatility / annual_vol
        scale = scale.clip(upper=self._config.max_position_leverage)
        # No volatility estimate yet => no position, rather than a guess.
        return scale.shift(1).fillna(0.0)

    # -- 2. Equity-curve stop -------------------------------------------

    def equity_curve_stop(self, returns: pd.Series) -> pd.Series:
        """Exposure multiplier from the rolling-drawdown stop.

        When the strategy's drawdown measured over the trailing
        `drawdown_window_days` breaches `drawdown_threshold`, exposure is
        scaled by `drawdown_scale_factor` (0.0 halts it). Trading resumes
        only after `recovery_days` consecutive days back below the
        threshold — without that hysteresis the stop would chatter on and
        off around the boundary.

        The output is shifted one day: a stop can only act on a drawdown
        that was already observable.
        """
        cfg = self._config
        if returns.empty:
            return pd.Series(dtype=float)

        equity = compute_equity_curve(returns)
        rolling_peak = equity.rolling(cfg.drawdown_window_days, min_periods=1).max()
        rolling_drawdown = equity / rolling_peak - 1.0
        breached = (-rolling_drawdown) > cfg.drawdown_threshold

        scale = np.ones(len(returns))
        days_clear = cfg.recovery_days
        tripped = False

        for i, is_breached in enumerate(breached.to_numpy()):
            if is_breached:
                tripped, days_clear = True, 0
            elif tripped:
                days_clear += 1
                if days_clear >= cfg.recovery_days:
                    tripped = False
            scale[i] = cfg.drawdown_scale_factor if tripped else 1.0

        return pd.Series(scale, index=returns.index).shift(1).fillna(1.0)

    # -- 3. Portfolio heat cap ------------------------------------------

    def apply_heat_cap(self, exposures: pd.DataFrame) -> pd.DataFrame:
        """Scale all positions down proportionally when total gross exposure
        exceeds `max_portfolio_heat`.

        Scaling proportionally (rather than dropping positions) preserves the
        intended relative weighting — the portfolio's shape is a decision the
        allocator already made; the cap only controls its size.
        """
        if exposures.empty:
            return exposures
        gross = exposures.abs().sum(axis=1)
        scale = (self._config.max_portfolio_heat / gross).clip(upper=1.0)
        scale = scale.replace([np.inf, -np.inf], 1.0).fillna(1.0)
        return exposures.mul(scale, axis=0)


# ==========================================================================
# REGIME-SWITCHING PORTFOLIO ALLOCATOR
# ==========================================================================


@dataclass
class StrandSet:
    """Per-strand series needed to cost a portfolio correctly.

    `gross_returns` deliberately excludes execution cost: the cost is charged
    once, later, on the strand's EFFECTIVE position (allocation x direction).
    Using the strand's own net returns and then scaling them would charge for
    the signal flips but nothing for the portfolio's own rebalancing.
    """

    gross_returns: pd.DataFrame
    positions: pd.DataFrame
    sizes: pd.DataFrame
    prices: pd.DataFrame

    @property
    def empty(self) -> bool:
        return self.gross_returns.empty

    @property
    def index(self) -> pd.Index:
        return self.gross_returns.index


def apply_portfolio_costs(
    allocation: pd.DataFrame,
    strands: StrandSet,
    cost_model,
) -> tuple[pd.Series, pd.Series]:
    """Return (net portfolio returns, per-bar portfolio turnover).

    The effective position in an asset is `allocation x direction`. Charging
    cost on the change in THAT quantity captures every source of trading:
    the strategy flipping direction, ATR sizing drifting day to day, a regime
    switch rerouting capital, and the correlation blend rebalancing weights.

    Scaling a strand's already-net returns by its allocation — the obvious
    shortcut — silently charges only the first of those four. On a portfolio
    whose volatility-targeted sizing moves every single day, that omits most
    of the real trading cost.
    """
    columns = list(allocation.columns)
    gross = strands.gross_returns.reindex(columns=columns).fillna(0.0)
    directions = strands.positions.reindex(columns=columns).fillna(0.0)
    prices = strands.prices.reindex(columns=columns)

    effective = allocation * directions
    turnover = effective.diff()
    # The opening bar establishes the whole position from flat.
    turnover.iloc[0] = effective.iloc[0]
    turnover = turnover.abs().fillna(0.0)

    costs = pd.DataFrame(0.0, index=allocation.index, columns=columns)
    for column in columns:
        costs[column] = cost_model.cost_fraction(
            turnover[column], prices[column]
        ).fillna(0.0)

    portfolio_returns = (allocation * gross).sum(axis=1) - costs.sum(axis=1)
    return portfolio_returns, turnover.sum(axis=1)


@dataclass
class PortfolioResult:
    """Output of one portfolio backtest."""

    name: str
    returns: pd.Series = field(repr=False)
    exposures: pd.DataFrame = field(repr=False)
    metrics: PerformanceMetrics = None
    regimes: pd.Series = field(repr=False, default_factory=lambda: pd.Series(dtype=float))
    # Human-readable notes explaining anomalies in the result (e.g. a regime
    # that held no position). Surfaced in the UI so a legitimate zero is not
    # mistaken for a bug.
    diagnostics: list[str] = field(default_factory=list)
    turnover: pd.Series = field(repr=False, default_factory=lambda: pd.Series(dtype=float))

    @property
    def equity_curve(self) -> pd.Series:
        return compute_equity_curve(self.returns)

    def calmar_ratio(self) -> float:
        """CAGR divided by max drawdown — return per unit of worst pain."""
        max_dd = self.metrics.max_drawdown
        if max_dd <= 0:
            return float("inf") if self.metrics.cagr > 0 else 0.0
        return float(self.metrics.cagr / max_dd)

    def summary_row(self) -> dict:
        return {
            "portfolio": self.name,
            "annualized_return": self.metrics.cagr,
            "annualized_volatility": self.metrics.annual_volatility,
            "sharpe": self.metrics.sharpe,
            "max_drawdown": self.metrics.max_drawdown,
            "calmar": self.calmar_ratio(),
            "total_return": self.metrics.total_return,
            "num_periods": self.metrics.num_periods,
            "avg_daily_turnover": (
                float(self.turnover.mean()) if len(self.turnover) else 0.0
            ),
        }


class SignalBlender:
    """Combines overlapping strategy signals by inverse correlation.

    Two strategies that always agree carry one bet between them, not two.
    Weighting each strand inversely to its average correlation with the
    others pushes capital toward genuinely independent sources of return
    instead of double-counting a single view.

    Weights are computed on a trailing window and shifted, so a day's
    blending never uses that day's own correlations.
    """

    def __init__(self, correlation_window: int = 120, min_weight: float = 0.05) -> None:
        self._window = correlation_window
        self._min_weight = min_weight

    def inverse_correlation_weights(self, strategy_returns: pd.DataFrame) -> pd.Series:
        """Weights inversely proportional to mean pairwise correlation."""
        columns = list(strategy_returns.columns)
        if len(columns) <= 1:
            return pd.Series(1.0, index=columns)

        window = strategy_returns.tail(self._window)
        if len(window) < 2:
            return pd.Series(1.0 / len(columns), index=columns)

        correlation = window.corr().fillna(0.0)
        np.fill_diagonal(correlation.values, np.nan)
        mean_correlation = correlation.mean(axis=1, skipna=True).fillna(0.0)

        # Map correlation in [-1, 1] to a positive weight: perfectly
        # uncorrelated (or negatively correlated) strands score highest.
        raw = (1.0 - mean_correlation).clip(lower=self._min_weight)
        total = raw.sum()
        if total <= 0:
            return pd.Series(1.0 / len(columns), index=columns)
        return raw / total


class DynamicRegimePortfolio:
    """Routes capital across strategies according to the prevailing regime.

    Each day:
      1. Read the regime CONFIRMED as of yesterday.
      2. Select the strategy families that regime routes to.
      3. Blend their signals by inverse correlation.
      4. Size by ATR volatility targeting.
      5. Apply the equity-curve stop and the portfolio heat cap.
      6. In a bear regime, scale down and rotate toward defensive assets.
    """

    def __init__(
        self,
        detector: MarketRegimeDetector,
        risk_manager: RiskManager,
        backtester: VectorizedBacktester,
        allocation: AllocationConfig,
        blender: SignalBlender | None = None,
    ) -> None:
        self._detector = detector
        self._risk = risk_manager
        self._backtester = backtester
        self._allocation = allocation
        self._blender = blender or SignalBlender(
            allocation.correlation_window, allocation.min_weight
        )

    def run(
        self,
        price_data: dict[str, pd.DataFrame],
        selected: pd.DataFrame,
        benchmark_data: pd.DataFrame,
    ) -> PortfolioResult:
        """Backtest the regime-switched portfolio.

        Args:
            price_data: OHLCV per symbol.
            selected: the strategies to deploy — Layer 3's ultra-robust set,
                with `symbol`, `strategy` and `params` columns.
            benchmark_data: OHLCV of the index defining the regime.
        """
        regimes = self._detector.predict_regime(benchmark_data)
        # Today trades on yesterday's confirmed regime. Without this shift the
        # portfolio would rotate on information it could not have had.
        regimes = regimes.shift(1)

        strands = self._build_strands(price_data, selected)
        strand_returns, strand_exposures = strands.gross_returns, strands.sizes
        if strands.empty:
            logger.warning("No deployable strategy strands; portfolio is empty")
            empty = pd.Series(dtype=float)
            return PortfolioResult("Dynamic Regime", empty, pd.DataFrame(), _zero_metrics())

        index = strand_returns.index
        regimes = regimes.reindex(index).ffill()

        diagnostics: list[str] = []
        exposures = self._allocate_by_regime(
            strand_returns, strand_exposures, regimes, diagnostics
        )
        exposures = self._risk.apply_heat_cap(exposures)

        portfolio_returns, turnover = apply_portfolio_costs(
            exposures, strands, self._backtester.cost_model
        )
        metrics = compute_metrics(
            portfolio_returns,
            exposures.abs().sum(axis=1).clip(upper=1.0),
            periods_per_year=infer_periods_per_year(exposures.index),
        )
        return PortfolioResult(
            name="Dynamic Regime",
            returns=portfolio_returns,
            exposures=exposures,
            metrics=metrics,
            regimes=regimes,
            diagnostics=diagnostics,
            turnover=turnover,
        )

    # -- internals ------------------------------------------------------

    def _build_strands(
        self, price_data: dict[str, pd.DataFrame], selected: pd.DataFrame
    ) -> StrandSet:
        """Backtest each selected asset-strategy pair into a return strand.

        Gross (pre-cost) returns are kept, together with each strand's
        direction and price, so execution cost can be charged once on the
        portfolio's effective position rather than at strand level.
        """
        returns: dict[str, pd.Series] = {}
        exposures: dict[str, pd.Series] = {}
        positions: dict[str, pd.Series] = {}
        prices: dict[str, pd.Series] = {}

        for _, row in selected.iterrows():
            symbol, strategy_name = row["symbol"], row["strategy"]
            data = price_data.get(symbol)
            if data is None:
                continue
            params = row.get("params") or {}
            if isinstance(params, str):
                import ast

                try:
                    params = ast.literal_eval(params)
                except (ValueError, SyntaxError):
                    params = {}

            try:
                strategy = get_strategy_class(strategy_name)(**params)
                signals = strategy.generate_signals(data).astype(float)
                result = self._backtester.run(data["Close"], signals)
            except Exception as exc:  # noqa: BLE001
                logger.error("Strand %s/%s failed: %s", symbol, strategy_name, exc)
                continue

            # ATR volatility targeting scales this strand's exposure.
            size = self._risk.atr_position_scale(data).reindex(result.net_returns.index)
            # The equity-curve stop reacts to this strand's own drawdown.
            stop = self._risk.equity_curve_stop(result.net_returns)

            key = f"{symbol}|{strategy_name}"
            returns[key] = result.gross_returns
            positions[key] = result.position
            prices[key] = data["Close"].reindex(result.gross_returns.index)
            exposures[key] = (size * stop).fillna(0.0)

        if not returns:
            return StrandSet(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
        return StrandSet(
            gross_returns=pd.DataFrame(returns).fillna(0.0),
            positions=pd.DataFrame(positions).fillna(0.0),
            sizes=pd.DataFrame(exposures).fillna(0.0),
            prices=pd.DataFrame(prices).ffill(),
        )

    def _allocate_by_regime(
        self,
        strand_returns: pd.DataFrame,
        strand_exposures: pd.DataFrame,
        regimes: pd.Series,
        diagnostics: list[str] | None = None,
    ) -> pd.DataFrame:
        """Build the daily exposure matrix from regime routing rules."""
        allocation = pd.DataFrame(
            0.0, index=strand_returns.index, columns=strand_returns.columns
        )
        strategy_of = {c: c.split("|")[1] for c in strand_returns.columns}
        symbol_of = {c: c.split("|")[0] for c in strand_returns.columns}

        for regime_code in sorted(self._allocation.regime_strategies):
            allowed = set(self._allocation.regime_strategies[regime_code])
            exposure_multiplier = self._allocation.regime_exposure.get(regime_code, 1.0)
            active_days = regimes == regime_code
            if not active_days.any():
                continue

            eligible = [c for c in strand_returns.columns if strategy_of[c] in allowed]
            if regime_code == Regime.BEAR and self._allocation.defensive_assets:
                # In a crash, prefer defensive assets if any are available;
                # otherwise stay with the (already scaled-down) mean-reversion set.
                defensive = [
                    c
                    for c in strand_returns.columns
                    if symbol_of[c] in self._allocation.defensive_assets
                ]
                if defensive:
                    eligible = defensive
            if not eligible:
                # The deployed set contains nothing this regime routes to, so
                # the portfolio sits in cash for these days. That is a valid
                # outcome, but it looks identical to a bug in the results, so
                # make the reason explicit.
                message = (
                    f"{Regime.name(regime_code)} routes capital to "
                    f"{', '.join(sorted(allowed))}, but none of those strategies are "
                    f"in the deployed set — {int(active_days.sum())} days held no "
                    f"position. Either deploy one of those strategies, or point this "
                    f"regime at a family that is available."
                )
                logger.warning(message)
                if diagnostics is not None:
                    diagnostics.append(message)
                continue

            weights = self._blender.inverse_correlation_weights(
                strand_returns.loc[active_days, eligible]
            )
            for column in eligible:
                allocation.loc[active_days, column] = (
                    strand_exposures.loc[active_days, column]
                    * float(weights.get(column, 0.0))
                    * exposure_multiplier
                )

        return allocation


# ==========================================================================
# STATIC BASELINE PORTFOLIOS (FOR COMPARISON)
# ==========================================================================


class StaticPortfolio:
    """A regime-blind baseline: run the selected strategies all the time.

    This is the control the dynamic portfolio must beat. Without it, a good
    Sharpe from the regime overlay proves nothing — it could simply be the
    underlying strategies doing the work.
    """

    def __init__(
        self,
        risk_manager: RiskManager,
        backtester: VectorizedBacktester,
        name: str = "Static Baseline",
    ) -> None:
        self._risk = risk_manager
        self._backtester = backtester
        self._name = name

    def run(
        self, price_data: dict[str, pd.DataFrame], selected: pd.DataFrame
    ) -> PortfolioResult:
        returns: dict[str, pd.Series] = {}
        exposures: dict[str, pd.Series] = {}
        positions: dict[str, pd.Series] = {}
        prices: dict[str, pd.Series] = {}

        for _, row in selected.iterrows():
            symbol, strategy_name = row["symbol"], row["strategy"]
            data = price_data.get(symbol)
            if data is None:
                continue
            params = row.get("params") or {}
            if isinstance(params, str):
                import ast

                try:
                    params = ast.literal_eval(params)
                except (ValueError, SyntaxError):
                    params = {}
            try:
                strategy = get_strategy_class(strategy_name)(**params)
                signals = strategy.generate_signals(data).astype(float)
                result = self._backtester.run(data["Close"], signals)
            except Exception as exc:  # noqa: BLE001
                logger.error("Baseline strand %s/%s failed: %s", symbol, strategy_name, exc)
                continue
            key = f"{symbol}|{strategy_name}"
            returns[key] = result.gross_returns
            positions[key] = result.position
            prices[key] = data["Close"].reindex(result.gross_returns.index)
            exposures[key] = self._risk.atr_position_scale(data).reindex(
                result.gross_returns.index
            )

        if not returns:
            return PortfolioResult(self._name, pd.Series(dtype=float), pd.DataFrame(), _zero_metrics())

        strands = StrandSet(
            gross_returns=pd.DataFrame(returns).fillna(0.0),
            positions=pd.DataFrame(positions).fillna(0.0),
            sizes=pd.DataFrame(exposures).fillna(0.0),
            prices=pd.DataFrame(prices).ffill(),
        )
        # Equal weight across strands, always on.
        weight = 1.0 / strands.gross_returns.shape[1]
        exposure_frame = strands.sizes * weight
        exposure_frame = self._risk.apply_heat_cap(exposure_frame)

        # Costed identically to the dynamic portfolio — this baseline also
        # rebalances daily through ATR sizing, and comparing a costed
        # portfolio against an uncosted one would rig the comparison.
        portfolio_returns, turnover = apply_portfolio_costs(
            exposure_frame, strands, self._backtester.cost_model
        )
        metrics = compute_metrics(
            portfolio_returns,
            exposure_frame.abs().sum(axis=1).clip(upper=1.0),
            periods_per_year=infer_periods_per_year(exposure_frame.index),
        )
        return PortfolioResult(
            self._name, portfolio_returns, exposure_frame, metrics, turnover=turnover
        )


class BuyAndHoldPortfolio:
    """Equal-weight buy-and-hold — the honest hurdle every system must clear."""

    def __init__(self, backtester: VectorizedBacktester, name: str = "Buy & Hold") -> None:
        self._backtester = backtester
        self._name = name

    def run(self, price_data: dict[str, pd.DataFrame]) -> PortfolioResult:
        returns: dict[str, pd.Series] = {}
        for symbol, data in price_data.items():
            signals = pd.Series(1.0, index=data.index)
            returns[symbol] = self._backtester.run(data["Close"], signals).net_returns

        if not returns:
            return PortfolioResult(self._name, pd.Series(dtype=float), pd.DataFrame(), _zero_metrics())

        frame = pd.DataFrame(returns).fillna(0.0)
        portfolio_returns = frame.mean(axis=1)
        position = pd.Series(1.0, index=frame.index)
        return PortfolioResult(
            self._name, portfolio_returns, frame, compute_metrics(portfolio_returns, position)
        )


# ==========================================================================
# PERFORMANCE COMPARATOR
# ==========================================================================


class PerformanceComparator:
    """Compares portfolios head-to-head and produces the reporting artifacts."""

    def __init__(self, config: RegimeConfig) -> None:
        self._config = config

    def compare(self, results: list[PortfolioResult]) -> pd.DataFrame:
        """Annualized return, Sharpe, max drawdown and Calmar for each."""
        rows = [r.summary_row() for r in results if r.metrics is not None]
        return pd.DataFrame(rows)

    def rolling_correlation(
        self, results: list[PortfolioResult], window: int = 60
    ) -> pd.DataFrame:
        """Rolling pairwise correlation between portfolio return streams.

        Shows whether the portfolios actually decoupled during regime
        transitions — if the dynamic and static curves stay perfectly
        correlated throughout, the overlay is not doing anything.
        """
        frame = pd.DataFrame(
            {r.name: r.returns for r in results if not r.returns.empty}
        ).dropna(how="all")
        if frame.shape[1] < 2:
            return pd.DataFrame()

        out = {}
        names = list(frame.columns)
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                out[f"{a} vs {b}"] = frame[a].rolling(window).corr(frame[b])
        return pd.DataFrame(out)

    def regime_attribution(self, result: PortfolioResult) -> pd.DataFrame:
        """Performance broken down by the regime it was earned in."""
        if result.regimes.empty or result.returns.empty:
            return pd.DataFrame()

        frame = pd.DataFrame(
            {"returns": result.returns, "regime": result.regimes.reindex(result.returns.index)}
        ).dropna()
        if frame.empty:
            return pd.DataFrame()

        rows = []
        for regime_code, group in frame.groupby("regime"):
            rows.append(
                {
                    "regime": int(regime_code),
                    "regime_name": Regime.name(int(regime_code)),
                    "days": len(group),
                    "total_return": float(compute_equity_curve(group["returns"]).iloc[-1] - 1),
                    "sharpe": float(
                        compute_metrics(
                            group["returns"], pd.Series(1.0, index=group.index)
                        ).sharpe
                    ),
                    "max_drawdown": float(compute_max_drawdown(group["returns"])),
                }
            )
        return pd.DataFrame(rows)

    def execution_log(self, result: PortfolioResult) -> pd.DataFrame:
        """Per-day, per-strand exposure changes tagged with the active regime.

        This is the "trade executions marked by regime" artifact: every row
        is a day on which a strand's exposure changed, alongside the regime
        that was in force when it changed.
        """
        if result.exposures.empty:
            return pd.DataFrame()

        changes = result.exposures.diff().fillna(result.exposures)
        records = []
        regimes = (
            result.regimes.reindex(result.exposures.index)
            if not result.regimes.empty
            else pd.Series(np.nan, index=result.exposures.index)
        )

        for column in changes.columns:
            moved = changes[column][changes[column].abs() > 1e-9]
            if moved.empty:
                continue
            symbol, _, strategy = column.partition("|")
            for timestamp, delta in moved.items():
                regime_code = regimes.get(timestamp, np.nan)
                records.append(
                    {
                        "date": timestamp,
                        "symbol": symbol,
                        "strategy": strategy,
                        "exposure_change": float(delta),
                        "exposure_after": float(result.exposures.loc[timestamp, column]),
                        "regime": regime_code,
                        "regime_name": (
                            Regime.name(int(regime_code))
                            if pd.notna(regime_code)
                            else "Unclassified"
                        ),
                    }
                )

        if not records:
            return pd.DataFrame()
        return pd.DataFrame(records).sort_values(["date", "symbol"]).reset_index(drop=True)

    def write_artifacts(
        self, results: list[PortfolioResult], dynamic: PortfolioResult | None = None
    ) -> None:
        out_dir = self._config.regime_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        self.compare(results).to_csv(out_dir / "portfolio_comparison.csv", index=False)
        self.rolling_correlation(results).to_csv(out_dir / "rolling_correlation.csv")

        equity = pd.DataFrame(
            {r.name: r.equity_curve for r in results if not r.returns.empty}
        )
        equity.to_csv(out_dir / "equity_curves.csv")

        if dynamic is not None:
            self.regime_attribution(dynamic).to_csv(
                out_dir / "regime_attribution.csv", index=False
            )
            self.execution_log(dynamic).to_csv(
                out_dir / "executions_by_regime.csv", index=False
            )
            if not dynamic.regimes.empty:
                dynamic.regimes.rename("regime").to_frame().to_csv(
                    out_dir / "regime_timeline.csv"
                )
            with (out_dir / "manifest.json").open("w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "diagnostics": dynamic.diagnostics,
                        "portfolios": [r.name for r in results],
                    },
                    fh,
                    indent=2,
                )
        logger.info("Layer 4 artifacts written to %s", out_dir)


def _zero_metrics() -> PerformanceMetrics:
    return PerformanceMetrics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0)
