"""Layer 3 - parameter sensitivity and bootstrap stress testing."""

from __future__ import annotations

import ast
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import numpy as np
import pandas as pd

from .backtest import WalkForwardEngine, extract_trade_returns
from .config import BootstrapConfig, RobustnessConfig, SensitivityConfig
from .strategies import get_strategy_class

logger = logging.getLogger(__name__)


# ==========================================================================
# PARAMETER SENSITIVITY ANALYSIS
# ==========================================================================
# Parameter sensitivity analysis.
#
# The question this module answers: is a surviving configuration sitting on a
# broad performance *plateau*, or on a fragile *island* that only exists
# because those exact parameter values happened to fit historical noise?
#
# Method: one-at-a-time (OAT) perturbation. Each numeric parameter is nudged to
# adjacent values while the others are held fixed, every resulting neighbour is
# re-backtested through the same walk-forward machinery as the centre, and the
# spread of out-of-sample performance across that neighbourhood is measured.
#
# A robust configuration is surrounded by configurations that also work. An
# overfit one is a spike: its neighbours collapse.


@dataclass(frozen=True)
class ParameterVariant:
    """One perturbed parameter set and how it differs from the centre."""

    params: dict
    changed_param: str
    original_value: float
    perturbed_value: float

    @property
    def label(self) -> str:
        return f"{self.changed_param}: {self.original_value} -> {self.perturbed_value}"


class PerturbationPolicy(ABC):
    """Decides which neighbouring parameter values to test."""

    @abstractmethod
    def neighbours(self, params: dict) -> list[ParameterVariant]:
        """Return the OAT neighbourhood around `params`."""


class StepPerturbationPolicy(PerturbationPolicy):
    """Integers step by whole units, floats step multiplicatively.

    Stepping floats by a percentage rather than a fixed amount keeps the
    perturbation meaningful across parameters on different scales: a Bollinger
    `num_std` of 2.0 and a hypothetical threshold of 0.001 both get nudged by
    a comparable *relative* amount.
    """

    def __init__(
        self,
        int_offsets: list[int] | None = None,
        float_relative_offsets: list[float] | None = None,
    ) -> None:
        self.int_offsets = int_offsets if int_offsets is not None else [-2, -1, 1, 2]
        self.float_relative_offsets = (
            float_relative_offsets
            if float_relative_offsets is not None
            else [-0.2, -0.1, 0.1, 0.2]
        )

    def neighbours(self, params: dict) -> list[ParameterVariant]:
        variants: list[ParameterVariant] = []
        for name, value in params.items():
            # bool is a subclass of int in Python; perturbing a flag is
            # meaningless, so skip it explicitly.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue

            for candidate in self._candidates(value):
                if candidate == value:
                    continue
                variants.append(
                    ParameterVariant(
                        params={**params, name: candidate},
                        changed_param=name,
                        original_value=value,
                        perturbed_value=candidate,
                    )
                )
        return variants

    def _candidates(self, value: float) -> list:
        if isinstance(value, int):
            return [value + offset for offset in self.int_offsets if value + offset >= 1]
        candidates = []
        for offset in self.float_relative_offsets:
            perturbed = round(value * (1.0 + offset), 6)
            if perturbed > 0:
                candidates.append(perturbed)
        return candidates


@dataclass
class SensitivityResult:
    """Outcome of testing one configuration's parameter neighbourhood."""

    symbol: str
    strategy_name: str
    params: dict
    centre_metric: float
    neighbour_metrics: list[float] = field(default_factory=list)
    neighbour_labels: list[str] = field(default_factory=list)
    num_neighbours_tested: int = 0
    num_neighbours_failed: int = 0

    @property
    def mean_neighbour_metric(self) -> float:
        return float(np.mean(self.neighbour_metrics)) if self.neighbour_metrics else 0.0

    @property
    def std_neighbour_metric(self) -> float:
        if len(self.neighbour_metrics) < 2:
            return 0.0
        return float(np.std(self.neighbour_metrics, ddof=1))

    @property
    def min_neighbour_metric(self) -> float:
        return float(np.min(self.neighbour_metrics)) if self.neighbour_metrics else 0.0

    @property
    def retention_ratio(self) -> float:
        """Mean neighbour performance as a fraction of the centre's.

        Near 1.0 means the neighbourhood performs like the centre (a
        plateau). Near 0 or negative means performance collapses the moment
        the parameters move (an island).
        """
        if self.centre_metric <= 0:
            return 0.0
        return float(self.mean_neighbour_metric / self.centre_metric)

    @property
    def coefficient_of_variation(self) -> float:
        """Dispersion of the neighbourhood, scale-free."""
        mean = self.mean_neighbour_metric
        if mean == 0:
            return float("inf")
        return float(self.std_neighbour_metric / abs(mean))

    def stability_fraction(self, min_retention_ratio: float) -> float:
        """Fraction of neighbours retaining enough of the centre's edge."""
        if not self.neighbour_metrics or self.centre_metric <= 0:
            return 0.0
        threshold = min_retention_ratio * self.centre_metric
        return float(np.mean(np.array(self.neighbour_metrics) >= threshold))

    def sensitivity_score(self, min_retention_ratio: float) -> float:
        """Overall stability in [0, 1]; higher is more robust.

        Defined as `stability_fraction * min(retention_ratio, 1)`: a
        configuration scores well only if MOST of its neighbours survive AND
        they do so at close to full strength. Beating the centre is capped at
        1.0 rather than rewarded — this measures stability, not performance.

        Scores 0 when the centre itself has no positive edge, since
        "stability" around a losing configuration is not a virtue.
        """
        if self.centre_metric <= 0:
            return 0.0
        return float(
            self.stability_fraction(min_retention_ratio)
            * min(self.retention_ratio, 1.0)
        )

    def to_row(self, config: SensitivityConfig) -> dict:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy_name,
            "centre_metric": self.centre_metric,
            "mean_neighbour_metric": self.mean_neighbour_metric,
            "std_neighbour_metric": self.std_neighbour_metric,
            "min_neighbour_metric": self.min_neighbour_metric,
            "retention_ratio": self.retention_ratio,
            "coefficient_of_variation": self.coefficient_of_variation,
            "stability_fraction": self.stability_fraction(config.min_retention_ratio),
            "sensitivity_score": self.sensitivity_score(config.min_retention_ratio),
            "num_neighbours_tested": self.num_neighbours_tested,
            "num_neighbours_failed": self.num_neighbours_failed,
            "is_stable": self.is_stable(config),
        }

    def is_stable(self, config: SensitivityConfig) -> bool:
        """Verdict: a plateau, not an island."""
        return bool(
            self.sensitivity_score(config.min_retention_ratio) > 0
            and self.stability_fraction(config.min_retention_ratio)
            >= config.min_stability_fraction
            and self.retention_ratio >= config.min_retention_ratio
        )


class ParameterSensitivityChecker:
    """Re-backtests a configuration's parameter neighbourhood.

    Neighbours are scored through the SAME walk-forward engine as the centre,
    on the same out-of-sample metric — otherwise the comparison would be
    between two different things.
    """

    def __init__(
        self,
        engine: WalkForwardEngine,
        config: SensitivityConfig,
        policy: PerturbationPolicy | None = None,
    ) -> None:
        self._engine = engine
        self._config = config
        self._policy = policy or StepPerturbationPolicy(
            config.int_offsets, config.float_relative_offsets
        )

    def check(
        self,
        symbol: str,
        strategy_name: str,
        params: dict,
        data: pd.DataFrame,
    ) -> SensitivityResult | None:
        """Evaluate the neighbourhood around `params` for one symbol."""
        if not params:
            logger.debug("%s/%s has no tunable parameters", symbol, strategy_name)
            return None

        centre_metric = self._score(symbol, strategy_name, params, data)
        if centre_metric is None:
            logger.warning("Could not score centre config for %s/%s", symbol, strategy_name)
            return None

        result = SensitivityResult(
            symbol=symbol,
            strategy_name=strategy_name,
            params=params,
            centre_metric=centre_metric,
        )

        for variant in self._policy.neighbours(params):
            result.num_neighbours_tested += 1
            metric = self._score(symbol, strategy_name, variant.params, data)
            if metric is None:
                # An invalid combination (e.g. fast_period >= slow_period) is
                # not a performance failure — it is a parameter set that
                # cannot exist, so it is counted separately rather than
                # scored as a zero that would drag the mean down.
                result.num_neighbours_failed += 1
                continue
            result.neighbour_metrics.append(metric)
            result.neighbour_labels.append(variant.label)

        return result

    def _score(
        self, symbol: str, strategy_name: str, params: dict, data: pd.DataFrame
    ) -> float | None:
        """Backtest one parameter set and return its OOS metric, or None if
        the parameters are invalid or the run produced nothing."""
        try:
            strategy = get_strategy_class(strategy_name)(**params)
            signals = strategy.generate_signals(data).astype(float)
            result = self._engine.evaluate_configuration(
                symbol=symbol,
                strategy_name=strategy_name,
                param_key=strategy.param_key(),
                params=params,
                prices=data["Close"],
                signals=signals,
                data=data,
            )
            if result is None:
                return None
            return float(getattr(result.oos_metrics, self._config.metric))
        except Exception as exc:  # noqa: BLE001 - invalid neighbours are expected
            logger.debug("Neighbour %s/%s %s rejected: %s", symbol, strategy_name, params, exc)
            return None


# ==========================================================================
# BOOTSTRAP STRESS TESTER
# ==========================================================================
# Bootstrap (Monte Carlo) stress testing of trade sequences.
#
# The question this module answers: did the strategy have a real edge, or did
# it just get a lucky *ordering* of trades?
#
# Method: take the exact list of realized trade returns and resample it many
# times, building an "alternate universe" equity curve from each resample. The
# set of trades is held constant — only their sequence (and, with replacement,
# their multiplicity) changes. A strategy whose drawdown explodes under
# reshuffling was depending on a specific historical path, not on edge.
#
# Every simulation is built with vectorized NumPy operations on a single
# (num_simulations x num_trades) matrix — no Python loop over simulations.
#
# Interpretive caveats, stated plainly because they bound what the verdict
# means:
#   * Resampling with replacement treats trades as independent and identically
#     distributed. Real trade sequences can be autocorrelated (a trending
#     regime produces runs of winners), and this destroys that structure by
#     construction. It stress-tests sequence risk; it does not model regime
#     persistence.
#   * Each trade is compounded as though it were a full-capital bet, matching
#     the Layer 2 engine's single-position convention.
#   * The distribution describes rearrangements of trades that already
#     happened. It is not a forecast.


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass(frozen=True)
class BootstrapResult:
    """Distribution statistics across all simulated alternate universes."""

    verdict: Verdict
    num_simulations: int
    num_trades: int

    # Final equity / return distribution
    mean_final_return: float
    std_final_equity: float
    p5_final_return: float
    p50_final_return: float
    p95_final_return: float
    probability_of_loss: float

    # Drawdown distribution
    original_max_drawdown: float
    mean_max_drawdown: float
    p50_max_drawdown: float
    p95_max_drawdown: float
    worst_max_drawdown: float
    drawdown_amplification: float

    # Losing-streak distribution
    mean_max_loss_streak: float
    p95_max_loss_streak: float
    worst_loss_streak: int

    reason: str = ""

    def to_row(self) -> dict:
        row = {
            "bootstrap_verdict": self.verdict.value,
            "bootstrap_reason": self.reason,
            "bootstrap_num_trades": self.num_trades,
            "bootstrap_mean_final_return": self.mean_final_return,
            "bootstrap_std_final_equity": self.std_final_equity,
            "bootstrap_p5_final_return": self.p5_final_return,
            "bootstrap_p50_final_return": self.p50_final_return,
            "bootstrap_p95_final_return": self.p95_final_return,
            "bootstrap_probability_of_loss": self.probability_of_loss,
            "bootstrap_original_max_drawdown": self.original_max_drawdown,
            "bootstrap_mean_max_drawdown": self.mean_max_drawdown,
            "bootstrap_p95_max_drawdown": self.p95_max_drawdown,
            "bootstrap_worst_max_drawdown": self.worst_max_drawdown,
            "bootstrap_drawdown_amplification": self.drawdown_amplification,
            "bootstrap_p95_loss_streak": self.p95_max_loss_streak,
            "bootstrap_worst_loss_streak": self.worst_loss_streak,
        }
        return row


class BootstrapStressTester:
    """Reshuffles a strategy's realized trades to expose sequence risk."""

    def __init__(self, config: BootstrapConfig) -> None:
        self._config = config

    def run(
        self, trade_returns: pd.Series | np.ndarray, original_max_drawdown: float = 0.0
    ) -> BootstrapResult:
        """Simulate `num_simulations` reorderings of `trade_returns`."""
        trades = np.asarray(trade_returns, dtype=float)
        trades = trades[np.isfinite(trades)]
        num_trades = len(trades)

        if num_trades < self._config.min_trades_required:
            return self._insufficient(num_trades, original_max_drawdown)

        # Sample ONCE and derive every statistic from the same matrix, so the
        # equity curves, drawdowns and loss streaks all describe the same set
        # of alternate universes.
        sampled = self._resample(trades)
        equity_curves = self._equity_curves(sampled)
        final_equity = equity_curves[:, -1]
        max_drawdowns = self._max_drawdowns(equity_curves)
        loss_streaks = self._max_loss_streaks(sampled)

        p5_final_return = float(np.percentile(final_equity, 5) - 1.0)
        p95_max_drawdown = float(np.percentile(max_drawdowns, 95))
        verdict, reason = self._decide(p5_final_return, p95_max_drawdown)

        return BootstrapResult(
            verdict=verdict,
            reason=reason,
            num_simulations=self._config.num_simulations,
            num_trades=num_trades,
            mean_final_return=float(final_equity.mean() - 1.0),
            std_final_equity=float(final_equity.std(ddof=1)),
            p5_final_return=p5_final_return,
            p50_final_return=float(np.percentile(final_equity, 50) - 1.0),
            p95_final_return=float(np.percentile(final_equity, 95) - 1.0),
            probability_of_loss=float((final_equity < 1.0).mean()),
            original_max_drawdown=float(original_max_drawdown),
            mean_max_drawdown=float(max_drawdowns.mean()),
            p50_max_drawdown=float(np.percentile(max_drawdowns, 50)),
            p95_max_drawdown=p95_max_drawdown,
            worst_max_drawdown=float(max_drawdowns.max()),
            drawdown_amplification=(
                float(p95_max_drawdown / original_max_drawdown)
                if original_max_drawdown > 0
                else float("nan")
            ),
            mean_max_loss_streak=float(loss_streaks.mean()),
            p95_max_loss_streak=float(np.percentile(loss_streaks, 95)),
            worst_loss_streak=int(loss_streaks.max()),
        )

    # -- vectorized internals -------------------------------------------

    def _resample(self, trades: np.ndarray) -> np.ndarray:
        """Draw the (num_simulations x num_trades) matrix of reordered trades.

        Seeded from config, so the same inputs always produce the same
        verdict — a bootstrap that changed its answer between runs would be
        useless as a gate.
        """
        rng = np.random.default_rng(self._config.random_seed)
        num_sims, num_trades = self._config.num_simulations, len(trades)

        if self._config.with_replacement:
            return rng.choice(trades, size=(num_sims, num_trades), replace=True)
        # Pure permutation: same trades, different order, each exactly once.
        return np.array([rng.permutation(trades) for _ in range(num_sims)])

    @staticmethod
    def _equity_curves(sampled: np.ndarray) -> np.ndarray:
        """Compound each row into an equity curve.

        Returns shape (num_simulations, num_trades + 1); the leading column of
        1.0 is starting capital, without which a drawdown beginning on the
        very first trade would be invisible.
        """
        growth = np.cumprod(1.0 + sampled, axis=1)
        return np.hstack([np.ones((sampled.shape[0], 1)), growth])

    @staticmethod
    def _max_drawdowns(equity_curves: np.ndarray) -> np.ndarray:
        """Peak-to-trough drawdown of every simulation, as positive fractions."""
        running_peak = np.maximum.accumulate(equity_curves, axis=1)
        drawdowns = equity_curves / running_peak - 1.0
        return -drawdowns.min(axis=1)

    @staticmethod
    def _max_loss_streaks(sampled: np.ndarray) -> np.ndarray:
        """Longest run of consecutive losing trades per simulation.

        The scan walks trades (typically tens to hundreds) while operating on
        all simulations at once, so the cost is one vector op per trade rather
        than one Python iteration per simulation.
        """
        num_sims, num_trades = sampled.shape
        is_loss = sampled < 0
        current = np.zeros(num_sims, dtype=int)
        longest = np.zeros(num_sims, dtype=int)
        for column in range(num_trades):
            current = np.where(is_loss[:, column], current + 1, 0)
            longest = np.maximum(longest, current)
        return longest

    def _decide(self, p5_final_return: float, p95_max_drawdown: float) -> tuple[Verdict, str]:
        """Apply the verdict rule: the unlucky-but-plausible universe must
        still be profitable and survivable."""
        failures = []
        if p5_final_return <= self._config.min_p5_final_return:
            failures.append(
                f"5th-percentile return {p5_final_return:.2%} <= "
                f"{self._config.min_p5_final_return:.2%}"
            )
        if p95_max_drawdown > self._config.max_p95_drawdown:
            failures.append(
                f"95th-percentile drawdown {p95_max_drawdown:.2%} > "
                f"{self._config.max_p95_drawdown:.2%}"
            )
        if failures:
            return Verdict.FAIL, "; ".join(failures)
        return Verdict.PASS, "survives sequence reshuffling"

    def _insufficient(self, num_trades: int, original_max_drawdown: float) -> BootstrapResult:
        reason = (
            f"only {num_trades} trades (need {self._config.min_trades_required}); "
            "bootstrap distribution would be meaningless"
        )
        logger.debug("Bootstrap skipped: %s", reason)
        return BootstrapResult(
            verdict=Verdict.INSUFFICIENT_DATA,
            reason=reason,
            num_simulations=0,
            num_trades=num_trades,
            mean_final_return=float("nan"),
            std_final_equity=float("nan"),
            p5_final_return=float("nan"),
            p50_final_return=float("nan"),
            p95_final_return=float("nan"),
            probability_of_loss=float("nan"),
            original_max_drawdown=float(original_max_drawdown),
            mean_max_drawdown=float("nan"),
            p50_max_drawdown=float("nan"),
            p95_max_drawdown=float("nan"),
            worst_max_drawdown=float("nan"),
            drawdown_amplification=float("nan"),
            mean_max_loss_streak=float("nan"),
            p95_max_loss_streak=float("nan"),
            worst_loss_streak=0,
        )


# ==========================================================================
# ROBUSTNESS SUITE ORCHESTRATION
# ==========================================================================
# Layer 3 orchestration: survivors -> sensitivity + bootstrap -> ultra-robust set.
#
# Single Responsibility: wiring. The plateau-vs-island math lives in
# sensitivity.py, the sequence-risk math in bootstrap.py; this module runs both
# over Layer 2's survivors and assembles the final table.


@dataclass
class RobustnessOutput:
    """Everything Layer 3 produces."""

    annotated: pd.DataFrame        # every survivor, with robustness columns
    ultra_robust: pd.DataFrame     # the subset passing both checks


class RobustnessSuite:
    """Runs parameter-sensitivity and bootstrap stress tests over survivors."""

    def __init__(
        self,
        sensitivity_checker: ParameterSensitivityChecker,
        bootstrap_tester: BootstrapStressTester,
        engine: WalkForwardEngine,
        config: RobustnessConfig,
    ) -> None:
        self._sensitivity = sensitivity_checker
        self._bootstrap = bootstrap_tester
        self._engine = engine
        self._config = config

    def run(
        self, survivors: pd.DataFrame, price_data: dict[str, pd.DataFrame]
    ) -> RobustnessOutput:
        """Annotate each surviving configuration with robustness metrics."""
        if survivors.empty:
            logger.warning("Layer 3 received zero survivors — nothing to stress test")
            return RobustnessOutput(survivors.copy(), survivors.copy())

        rows: list[dict] = []
        for _, survivor in survivors.iterrows():
            row = self._evaluate_one(survivor, price_data)
            if row is not None:
                rows.append(row)

        annotated = pd.DataFrame(rows)
        if annotated.empty:
            return RobustnessOutput(annotated, annotated)

        ultra_robust = annotated[
            annotated["is_stable"] & (annotated["bootstrap_verdict"] == Verdict.PASS.value)
        ].copy()

        logger.info(
            "Layer 3 complete: %s/%s configurations are ultra-robust "
            "(stable parameters AND survive trade reshuffling)",
            len(ultra_robust),
            len(annotated),
        )
        return RobustnessOutput(annotated, ultra_robust)

    def write_artifacts(self, output: RobustnessOutput) -> None:
        out_dir = self._config.robustness_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        output.annotated.to_csv(out_dir / "robustness_full.csv", index=False)
        output.ultra_robust.to_csv(out_dir / "ultra_robust_strategies.csv", index=False)

        manifest = {
            "num_evaluated": int(len(output.annotated)),
            "num_ultra_robust": int(len(output.ultra_robust)),
            "sensitivity": {
                "metric": self._config.sensitivity.metric,
                "int_offsets": self._config.sensitivity.int_offsets,
                "float_relative_offsets": self._config.sensitivity.float_relative_offsets,
                "min_retention_ratio": self._config.sensitivity.min_retention_ratio,
                "min_stability_fraction": self._config.sensitivity.min_stability_fraction,
            },
            "bootstrap": {
                "num_simulations": self._config.bootstrap.num_simulations,
                "random_seed": self._config.bootstrap.random_seed,
                "with_replacement": self._config.bootstrap.with_replacement,
                "min_p5_final_return": self._config.bootstrap.min_p5_final_return,
                "max_p95_drawdown": self._config.bootstrap.max_p95_drawdown,
            },
        }
        with (out_dir / "manifest.json").open("w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, default=str)
        logger.info("Layer 3 artifacts written to %s", out_dir)

    # -- internals ------------------------------------------------------

    def _evaluate_one(
        self, survivor: pd.Series, price_data: dict[str, pd.DataFrame]
    ) -> dict | None:
        symbol = survivor["symbol"]
        strategy_name = survivor["strategy"]
        data = price_data.get(symbol)
        if data is None:
            logger.warning("No price data for %s; skipping", symbol)
            return None

        params = self._parse_params(survivor)
        if params is None:
            logger.warning(
                "Could not recover parameters for %s/%s; skipping",
                symbol,
                strategy_name,
            )
            return None

        row = {
            "symbol": symbol,
            "strategy": strategy_name,
            "param_key": survivor.get("param_key", ""),
            "params": params,
            "oos_sharpe": survivor.get("oos_sharpe"),
            "oos_max_drawdown": survivor.get("oos_max_drawdown"),
            "oos_num_trades": survivor.get("oos_num_trades"),
        }

        row.update(self._run_sensitivity(symbol, strategy_name, params, data))
        row.update(self._run_bootstrap(symbol, strategy_name, params, data))
        return row

    def _run_sensitivity(
        self, symbol: str, strategy_name: str, params: dict, data: pd.DataFrame
    ) -> dict:
        try:
            result = self._sensitivity.check(symbol, strategy_name, params, data)
        except Exception as exc:  # noqa: BLE001 - one bad config must not abort the suite
            logger.error("Sensitivity failed for %s/%s: %s", symbol, strategy_name, exc)
            result = None

        if result is None:
            return {
                "sensitivity_score": float("nan"),
                "retention_ratio": float("nan"),
                "stability_fraction": float("nan"),
                "num_neighbours_tested": 0,
                "is_stable": False,
            }
        row = result.to_row(self._config.sensitivity)
        row.pop("symbol", None)
        row.pop("strategy", None)
        return row

    def _run_bootstrap(
        self, symbol: str, strategy_name: str, params: dict, data: pd.DataFrame
    ) -> dict:
        try:
            trade_returns, original_dd = self._trade_returns(
                symbol, strategy_name, params, data
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Trade extraction failed for %s/%s: %s", symbol, strategy_name, exc)
            trade_returns, original_dd = pd.Series(dtype=float), 0.0

        return self._bootstrap.run(trade_returns, original_dd).to_row()

    def _trade_returns(
        self, symbol: str, strategy_name: str, params: dict, data: pd.DataFrame
    ) -> tuple[pd.Series, float]:
        """Recover the configuration's realized trade-by-trade returns."""
        strategy = get_strategy_class(strategy_name)(**params)
        signals = strategy.generate_signals(data).astype(float)
        result = self._engine.evaluate_configuration(
            symbol=symbol,
            strategy_name=strategy_name,
            param_key=strategy.param_key(),
            params=params,
            prices=data["Close"],
            signals=signals,
            data=data,
        )
        if result is None:
            return pd.Series(dtype=float), 0.0

        if self._config.bootstrap.trade_source == "oos":
            returns, position = result.oos_returns, result.oos_position
            original_dd = result.oos_metrics.max_drawdown
        else:
            full = self._engine.backtester.run(data["Close"], signals)
            returns, position = full.net_returns, full.position
            original_dd = full.metrics.max_drawdown

        return extract_trade_returns(returns, position), original_dd

    @staticmethod
    def _parse_params(survivor: pd.Series) -> dict | None:
        """Recover the parameter dict from a survivors row.

        Layer 2 writes survivors to CSV, which stringifies the dict, so a
        round-tripped row needs literal_eval. An in-memory DataFrame still
        holds the real dict — both paths must work.
        """
        raw = survivor.get("params")
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = ast.literal_eval(raw)
                return parsed if isinstance(parsed, dict) else None
            except (ValueError, SyntaxError):
                return None
        return None
