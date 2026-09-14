"""Row → JSON. One place, so every endpoint renders the same row identically.

Two conventions from docs/04 section 10 are enforced here:

* ``Numeric`` columns come back from SQLAlchemy as ``Decimal``. JSON has no
  decimal type, so they are converted once, here, rather than in each router.
* **None survives.** A missing measurement is serialised as ``null``, never
  coerced to 0. Zero and "not measured" are different claims about the market
  and a chart cannot tell them apart once they are the same number.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

from ..db.models import Breakout, MarketRegimeRow, Pattern, RelativeStrength, SectorMetric


def num(value) -> float | None:
    """A JSON number, or None. NaN is not a number a payload should carry."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float) and value != value:  # NaN
        return None
    return float(value)


def iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def loads(raw: str | None, default=None):
    """Parse a stored JSON column, tolerating a row written before a schema
    change rather than failing the whole request for one bad row."""
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


# ---------------------------------------------------------------------------
# Derived rows
# ---------------------------------------------------------------------------


def regime_payload(row: MarketRegimeRow | None) -> dict | None:
    if row is None:
        return None
    components = [
        {"name": name, "raw": num(value)}
        for name, value in (
            ("trend", row.trend_score),
            ("breadth", row.breadth_score),
            ("momentum", row.momentum_score),
            ("participation", row.participation_score),
            ("volatility", row.volatility_score),
        )
        if value is not None
    ]
    return {
        "date": iso(row.date),
        "regime": row.regime,
        "score": num(row.regime_score),
        "confidence": num(row.confidence),
        "components": components,
        # Not cosmetic: it distinguishes "stable" from "a threshold was
        # crossed and hysteresis is still holding the old state".
        "pending_regime": row.pending_regime,
        "engine_version": row.engine_version,
    }


def sector_payload(row: SectorMetric, code: str, name: str) -> dict:
    return {
        "sector": {"id": row.sector_id, "code": code, "name": name},
        "rs_score": num(row.rs_score),
        "rs_rank": row.rs_rank,
        "momentum": num(row.momentum_score),
        "quadrant": row.quadrant,
        "ret_1m": num(row.ret_1m),
        "ret_3m": num(row.ret_3m),
        "ret_6m": num(row.ret_6m),
        "pct_above_ema_20": num(row.pct_above_ema_20),
        "pct_above_ema_50": num(row.pct_above_ema_50),
        "pct_above_ema_200": num(row.pct_above_ema_200),
        "new_highs": row.new_highs,
        "new_lows": row.new_lows,
        "breakouts": row.breakouts,
        "failed_breakouts": row.failed_breakouts,
        "avg_pattern_score": num(row.avg_pattern_score),
        "constituent_count": row.constituent_count,
        # The UI greys the row on this rather than re-deriving the threshold.
        "low_sample": bool(row.low_sample),
    }


def rs_payload(row: RelativeStrength | None) -> dict | None:
    if row is None:
        return None
    return {
        "rs_score": num(row.rs_score),
        "rs_rank": row.rs_rank,
        "rs_trend": row.rs_trend,
        "rs_vs_sector": num(row.rs_vs_sector),
        "rs_raw": num(row.rs_raw),
        "ret_1m": num(row.ret_1m),
        "ret_3m": num(row.ret_3m),
        "ret_6m": num(row.ret_6m),
        "ret_12m": num(row.ret_12m),
        # A rank from one horizon is a weaker claim than one from four, and
        # the payload has to say which it is.
        "coverage": row.coverage.split(",") if row.coverage else [],
        "universe_size": row.universe_size,
        "engine_version": row.engine_version,
    }


def pattern_payload(row: Pattern | None, symbol: str | None = None) -> dict | None:
    if row is None:
        return None
    return {
        "id": row.id,
        "symbol": symbol,
        "type": row.pattern_type,
        "status": row.status,
        "score": num(row.score),
        # Non-negotiable per docs/09 section 5: this is what makes the
        # "why this setup?" panel a projection of stored data rather than a
        # second computation in the frontend.
        "score_components": loads(row.score_breakdown, []),
        "pivot_price": num(row.pivot_price),
        "base_start": iso(row.base_start),
        "base_end": iso(row.base_end),
        "base_depth_pct": num(row.base_depth_pct),
        "base_duration_days": row.base_duration_days,
        "contractions": row.contraction_count,
        "detected_on": iso(row.detected_on),
        "engine_version": row.engine_version,
        "rule_version": row.rule_version,
    }


def breakout_payload(row: Breakout, symbol: str | None = None) -> dict:
    outcome = row.outcome
    return {
        "id": row.id,
        "symbol": symbol,
        "pattern_id": row.pattern_id,
        "breakout_date": iso(row.breakout_date),
        "breakout_price": num(row.breakout_price),
        "pivot_price": num(row.pivot_price),
        "volume": num(row.volume),
        "rel_volume": num(row.rel_volume),
        "rs_at_breakout": num(row.rs_at_breakout),
        "sector_rank_at_breakout": row.sector_rank_at_breakout,
        "regime_at_breakout": row.regime_at_breakout,
        "setup_score": num(row.setup_score),
        "status": row.status,
        "engine_version": row.engine_version,
        # Failures are first-class rows with the same shape as successes.
        "outcome": None if outcome is None else {
            "mfe_pct": num(outcome.mfe_pct),
            "mae_pct": num(outcome.mae_pct),
            "days_held": outcome.days_held,
            "exit_date": iso(outcome.exit_date),
            "exit_price": num(outcome.exit_price),
            "return_pct": num(outcome.return_pct),
            "failed": bool(outcome.failed),
            "failure_date": iso(outcome.failure_date),
            "failure_reason": outcome.failure_reason,
            "days_to_failure": outcome.days_to_failure,
            "drawdown_pct": num(outcome.drawdown_pct),
        },
    }
