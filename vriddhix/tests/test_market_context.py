"""Phase 3: relative strength, sector rotation, market regime."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from vriddhix.domain.types import MarketRegime, RsTrend, SectorQuadrant
from vriddhix.engines.regime import (
    RegimeConfig,
    apply_hysteresis,
    classify,
    compute_breadth,
    compute_regime,
)
from vriddhix.engines.rs import RsConfig, percentile_rank, rank_universe, rs_line, weighted_raw
from vriddhix.engines.sector import SectorConfig, aggregate_sectors, classify_quadrant

TODAY = date(2026, 9, 14)


# ===========================================================================
# Relative strength
# ===========================================================================


def returns_frame(data: dict[str, list[float | None]]) -> pd.DataFrame:
    return pd.DataFrame.from_dict(
        data, orient="index", columns=["ret_1m", "ret_3m", "ret_6m", "ret_12m"]
    )


def test_percentile_rank_is_a_percentile():
    ranks = percentile_rank(pd.Series({"a": 0.1, "b": 0.2, "c": 0.3, "d": 0.4}))
    assert ranks["d"] == 100.0
    assert ranks["a"] == 25.0


def test_ties_share_the_lower_rank():
    """Tied values take the LOWER percentile, not the average or the higher.

    Conservative on purpose: "RS 95" should mean the stock genuinely beat 95%
    of the universe, so a tie resolves against the stock rather than flattering
    it.
    """
    ranks = percentile_rank(pd.Series({"a": 0.5, "b": 0.5, "c": 0.9}))
    assert ranks["a"] == ranks["b"] == pytest.approx(100 / 3)
    assert ranks["c"] == 100.0


def test_strongest_stock_ranks_first():
    results = rank_universe(
        returns_frame({
            "STRONG": [0.30, 0.60, 0.90, 1.20],
            "MIDDLE": [0.05, 0.10, 0.15, 0.20],
            "WEAK": [-0.10, -0.20, -0.25, -0.30],
        }),
        TODAY,
    )
    assert results["STRONG"].rs_rank == 1
    assert results["STRONG"].rs_score == 100.0
    assert results["WEAK"].rs_rank == 3


def test_weights_are_front_loaded():
    """A 12-month return is mostly last year's news; 'currently strong' should
    not be dominated by it."""
    cfg = RsConfig()
    recent, _ = weighted_raw({"ret_1m": 0.20, "ret_3m": 0.0, "ret_6m": 0.0, "ret_12m": 0.0}, cfg)
    old, _ = weighted_raw({"ret_1m": 0.0, "ret_3m": 0.0, "ret_6m": 0.0, "ret_12m": 0.20}, cfg)
    assert recent > old


def test_partial_history_renormalises_and_records_coverage():
    """A two-month-old listing has no 6m or 12m return. Treating those as zero
    would rank it flat over a period it did not exist for."""
    raw, coverage = weighted_raw(
        {"ret_1m": 0.10, "ret_3m": 0.20, "ret_6m": None, "ret_12m": None}, RsConfig()
    )
    # 0.4*0.10 + 0.3*0.20 = 0.10, renormalised over 0.7
    assert raw == pytest.approx(0.10 / 0.7)
    assert coverage == ("ret_1m", "ret_3m")


def test_a_recent_listing_is_identifiable_as_such():
    results = rank_universe(
        returns_frame({
            "OLD": [0.10, 0.20, 0.30, 0.40],
            "NEW": [0.15, None, None, None],
        }),
        TODAY,
    )
    assert results["NEW"].coverage == ("ret_1m",)
    assert len(results["OLD"].coverage) == 4


def test_a_symbol_with_no_history_is_excluded_not_ranked_zero():
    results = rank_universe(
        returns_frame({"GOOD": [0.1, 0.2, 0.3, 0.4], "EMPTY": [None, None, None, None]}),
        TODAY,
    )
    assert "EMPTY" not in results


def test_rs_vs_sector_is_computed_within_the_sector():
    """Surfaces the strongest name in a weak sector, and the laggard in a hot
    one -- two populations worth separating."""
    results = rank_universe(
        returns_frame({
            "TECH_A": [0.30, 0.40, 0.50, 0.60],
            "TECH_B": [0.25, 0.35, 0.45, 0.55],
            "BANK_A": [0.05, 0.06, 0.07, 0.08],
            "BANK_B": [0.01, 0.02, 0.03, 0.04],
        }),
        TODAY,
        sectors={"TECH_A": "IT", "TECH_B": "IT", "BANK_A": "BANKS", "BANK_B": "BANKS"},
    )
    # Weak in absolute terms, strongest in its own sector.
    assert results["BANK_A"].rs_score < 60
    assert results["BANK_A"].rs_vs_sector == 100.0


def test_rs_trend_needs_a_prior_score():
    frame = returns_frame({"A": [0.1, 0.2, 0.3, 0.4], "B": [0.05, 0.1, 0.15, 0.2]})
    without = rank_universe(frame, TODAY)
    assert without["A"].rs_trend is RsTrend.STABLE  # not guessed

    improving = rank_universe(frame, TODAY, previous_scores={"A": 40.0})
    assert improving["A"].rs_trend is RsTrend.IMPROVING

    # B is the weaker of the two, so it scores 50 today; coming from 90 that
    # is a 40-point fall in percentile.
    deteriorating = rank_universe(frame, TODAY, previous_scores={"B": 90.0})
    assert deteriorating["B"].rs_score == 50.0
    assert deteriorating["B"].rs_trend is RsTrend.DETERIORATING


def test_rs_weights_must_sum_to_one():
    with pytest.raises(ValueError, match="sum to"):
        RsConfig(horizon_weights=(("ret_1m", 0.5), ("ret_3m", 0.2)))


def test_rs_line_is_indexed_to_100():
    index = pd.bdate_range("2024-01-01", periods=50)
    close = pd.Series(np.linspace(100, 150, 50), index=index)
    benchmark = pd.Series(np.linspace(200, 220, 50), index=index)
    line = rs_line(close, benchmark)
    assert line.iloc[0] == pytest.approx(100.0)
    assert line.iloc[-1] > 100.0  # outperformed


# ===========================================================================
# Sectors
# ===========================================================================


def sector_frame() -> pd.DataFrame:
    rows = []
    for i in range(8):
        rows.append({"symbol": f"IT{i}", "sector": "IT", "ret_1m": 0.10, "ret_3m": 0.25,
                     "ret_6m": 0.40, "ret_12m": 0.60, "above_ema_20": True,
                     "above_ema_50": True, "above_ema_200": True,
                     "is_new_high": i < 3, "is_new_low": False})
    for i in range(6):
        rows.append({"symbol": f"FMCG{i}", "sector": "FMCG", "ret_1m": -0.05, "ret_3m": -0.10,
                     "ret_6m": -0.12, "ret_12m": -0.15, "above_ema_20": False,
                     "above_ema_50": False, "above_ema_200": i < 1,
                     "is_new_high": False, "is_new_low": i < 2})
    for i in range(2):
        rows.append({"symbol": f"TINY{i}", "sector": "TINY", "ret_1m": 0.5, "ret_3m": 0.8,
                     "ret_6m": 1.0, "ret_12m": 1.5, "above_ema_20": True,
                     "above_ema_50": True, "above_ema_200": True,
                     "is_new_high": True, "is_new_low": False})
    return pd.DataFrame(rows).set_index("symbol")


def test_sector_aggregation_is_equal_weighted():
    results = aggregate_sectors(sector_frame(), TODAY)
    assert results["IT"].returns["ret_1m"] == pytest.approx(0.10)
    assert results["IT"].constituent_count == 8


def test_strong_sector_outranks_weak_sector():
    results = aggregate_sectors(sector_frame(), TODAY)
    assert results["IT"].rs_score > results["FMCG"].rs_score
    assert results["IT"].rs_rank < results["FMCG"].rs_rank


def test_small_sectors_are_flagged_and_excluded_from_ranking():
    """A rank derived from two stocks is a single-stock opinion wearing a
    sector's name."""
    results = aggregate_sectors(sector_frame(), TODAY, SectorConfig(min_constituents=5))
    assert results["TINY"].low_sample is True
    # Unranked, not ranked-last: None says "no standing", 0.0 would say
    # "weakest", and they are different claims.
    assert results["TINY"].rs_rank is None
    assert results["TINY"].rs_score is None
    assert results["IT"].low_sample is False


def test_breadth_percentages_reach_the_sector_result():
    results = aggregate_sectors(sector_frame(), TODAY)
    assert results["IT"].pct_above_ema_50 == pytest.approx(100.0)
    assert results["FMCG"].pct_above_ema_50 == pytest.approx(0.0)
    assert results["IT"].new_highs == 3
    assert results["FMCG"].new_lows == 2


@pytest.mark.parametrize(
    "rs,momentum,expected",
    [
        (80, 5, SectorQuadrant.LEADING),
        (80, -5, SectorQuadrant.WEAKENING),
        (20, 5, SectorQuadrant.IMPROVING),
        (20, -5, SectorQuadrant.LAGGING),
    ],
)
def test_quadrants(rs, momentum, expected):
    assert classify_quadrant(rs, momentum) is expected


def test_momentum_comes_from_the_change_in_rank():
    results = aggregate_sectors(sector_frame(), TODAY, previous_scores={"IT": 40.0, "FMCG": 90.0})
    assert results["IT"].momentum > 0
    assert results["IT"].quadrant is SectorQuadrant.LEADING
    assert results["FMCG"].momentum < 0


# ===========================================================================
# Breadth and regime
# ===========================================================================


def breadth_frame(n: int, *, above: float, advancing: float, new_highs: int = 0,
                  new_lows: int = 0) -> pd.DataFrame:
    rows = []
    for i in range(n):
        rows.append({
            "symbol": f"S{i}",
            "change": 1.0 if i < n * advancing else -1.0,
            "above_ema_20": i < n * above,
            "above_ema_50": i < n * above,
            "above_ema_100": i < n * above,
            "above_ema_200": i < n * above,
            "is_new_high": i < new_highs,
            "is_new_low": i >= n - new_lows if new_lows else False,
        })
    return pd.DataFrame(rows).set_index("symbol")


def bullish_benchmark() -> pd.Series:
    return pd.Series({
        "close": 24000.0, "ema_20": 23800.0, "ema_50": 23500.0, "ema_200": 22000.0,
        "ema_50_slope_20": 0.04, "ret_3m": 0.10,
    })


def bearish_benchmark() -> pd.Series:
    return pd.Series({
        "close": 20000.0, "ema_20": 20500.0, "ema_50": 21000.0, "ema_200": 23000.0,
        "ema_50_slope_20": -0.03, "ret_3m": -0.12,
    })


def test_breadth_counts_advancers_and_decliners():
    breadth = compute_breadth(breadth_frame(100, above=0.7, advancing=0.6), TODAY)
    assert breadth.advancers == 60
    assert breadth.decliners == 40
    assert breadth.ad_ratio == pytest.approx(1.5)
    assert breadth.pct_above_ema_50 == pytest.approx(70.0)


def test_missing_bars_leave_the_denominator_rather_than_counting_as_weak():
    """Counting an absent bar as 'not above its EMA' would manufacture
    bearishness on a data-outage day."""
    breadth = compute_breadth(breadth_frame(80, above=1.0, advancing=1.0), TODAY, universe_size=100)
    assert breadth.universe_size == 80
    assert breadth.excluded == 20
    assert breadth.pct_above_ema_50 == pytest.approx(100.0)  # of those present


def test_net_new_highs():
    breadth = compute_breadth(
        breadth_frame(100, above=0.6, advancing=0.6, new_highs=20, new_lows=5), TODAY
    )
    assert breadth.net_new_highs == 15


def test_a_broad_advance_is_a_bull_regime():
    breadth = compute_breadth(
        breadth_frame(200, above=0.80, advancing=0.70, new_highs=30, new_lows=2), TODAY
    )
    result = compute_regime(
        breadth, bullish_benchmark(), TODAY,
        sector_scores={"IT": 80.0, "BANKS": 70.0, "FMCG": 55.0},
        realised_vol=0.12,
    )
    assert result is not None
    assert result.regime in (MarketRegime.BULL, MarketRegime.STRONG_BULL)
    assert result.regime_score >= 60


def test_a_broad_decline_is_a_bear_regime():
    breadth = compute_breadth(
        breadth_frame(200, above=0.15, advancing=0.25, new_highs=1, new_lows=40), TODAY
    )
    result = compute_regime(
        breadth, bearish_benchmark(), TODAY,
        sector_scores={"IT": 20.0, "BANKS": 25.0, "FMCG": 30.0},
        realised_vol=0.35,
    )
    assert result.regime in (MarketRegime.BEAR, MarketRegime.STRONG_BEAR)
    assert result.regime_score < 40


def test_regime_reports_every_component():
    breadth = compute_breadth(breadth_frame(200, above=0.6, advancing=0.55), TODAY)
    result = compute_regime(breadth, bullish_benchmark(), TODAY)
    assert {c.name for c in result.components} == {
        "trend", "breadth", "momentum", "participation", "volatility"
    }
    assert result.regime_score == pytest.approx(
        sum(c.contribution for c in result.components), abs=1e-9
    )


def test_thin_coverage_returns_nothing_rather_than_a_biased_number():
    """A regime computed from 40% of the universe describes a biased sample.
    The dashboard must say 'unavailable', not show a confident number."""
    breadth = compute_breadth(breadth_frame(40, above=0.9, advancing=0.9), TODAY, universe_size=100)
    assert compute_regime(breadth, bullish_benchmark(), TODAY) is None


def test_adequate_coverage_is_accepted():
    breadth = compute_breadth(breadth_frame(85, above=0.7, advancing=0.6), TODAY, universe_size=100)
    assert compute_regime(breadth, bullish_benchmark(), TODAY) is not None


def test_confidence_falls_when_components_disagree():
    """Five components averaging 60 by agreeing is a different statement from
    five averaging 60 by cancelling out."""
    breadth = compute_breadth(breadth_frame(200, above=0.6, advancing=0.55), TODAY)
    agreeing = compute_regime(breadth, bullish_benchmark(), TODAY,
                              sector_scores={"A": 60.0, "B": 60.0}, realised_vol=0.18)
    conflicted = compute_regime(breadth, bearish_benchmark(), TODAY,
                                sector_scores={"A": 95.0, "B": 5.0}, realised_vol=0.45)
    assert agreeing.confidence > conflicted.confidence


@pytest.mark.parametrize(
    "score,expected",
    [(90, MarketRegime.STRONG_BULL), (65, MarketRegime.BULL), (50, MarketRegime.NEUTRAL),
     (30, MarketRegime.BEAR), (10, MarketRegime.STRONG_BEAR)],
)
def test_classification_thresholds(score, expected):
    assert classify(score, RegimeConfig()) is expected


def test_hysteresis_ignores_a_marginal_one_day_crossing():
    """Without this, a regime filter flickers daily and produces entry churn
    that has nothing to do with the market."""
    cfg = RegimeConfig()
    effective, pending = apply_hysteresis(
        MarketRegime.BULL, 60.5, MarketRegime.NEUTRAL, [], cfg
    )
    assert effective is MarketRegime.NEUTRAL  # unchanged
    assert pending is MarketRegime.BULL       # but recorded


def test_hysteresis_allows_a_decisive_sustained_change():
    cfg = RegimeConfig()
    effective, pending = apply_hysteresis(
        MarketRegime.BULL, 70.0, MarketRegime.NEUTRAL, [MarketRegime.BULL], cfg
    )
    assert effective is MarketRegime.BULL
    assert pending is None


def test_regime_weights_must_sum_to_one():
    with pytest.raises(ValueError, match="sum to"):
        RegimeConfig(weights=(("trend", 0.5), ("breadth", 0.2)))


def test_regime_config_loads_from_yaml(cfg):
    loaded = RegimeConfig.from_config(cfg)
    assert loaded.min_universe_coverage_pct == cfg.get("regime.min_universe_coverage_pct")
    assert loaded.hysteresis_days == cfg.get("regime.hysteresis_days")


def test_concentrated_strength_scores_lower_participation():
    """A tape carried by five names should not read as a healthy bull."""
    from vriddhix.engines.regime import _participation_score

    broad = pd.Series(np.full(100, 0.05))
    narrow = pd.Series(np.concatenate([np.full(10, 0.50), np.full(90, 0.001)]))

    assert _participation_score(None, broad) > _participation_score(None, narrow)
