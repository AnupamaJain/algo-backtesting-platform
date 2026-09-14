"""AI explanation and research. docs/09 section 9.

The server builds the payload from stored rows. **The client cannot inject
market facts**: the request names a subject by id, and every number the model
sees is read from the database here. A client that could supply the facts
could make the model say anything and have it look authoritative.

``inputs`` is echoed in the response so any claim in the prose can be checked
against the data that produced it. Every explanation is persisted for the same
reason -- an explanation nobody can audit is not evidence of anything.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Body
from sqlalchemy import select

from ...ai.explain import (
    DISCLAIMER,
    ExplanationRejected,
    explain_failure,
    explain_setup,
)
from ...domain.types import ScoreComponent
from ...services.scanner import SetupRow
from ...db.models import (
    AiExplanation,
    Breakout,
    MarketRegimeRow,
    Pattern,
    RelativeStrength,
    SectorMetric,
    Stock,
)
from .. import errors
from ..deps import ProvenanceDep, SessionDep, UserDep
from ..serialise import iso, loads, num

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])

SUBJECTS = {"PATTERN", "BREAKOUT"}


class _Fields:
    """A loose attribute bag for shapes the explainer reads duck-typed."""

    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


def _setup_row_from(session, pattern: Pattern) -> SetupRow:
    """Rebuild the scanner's own row shape from stored rows.

    Returns an actual ``SetupRow`` rather than a look-alike: the explainer
    reads a documented set of fields, and a hand-rolled stand-in silently
    drifts the moment a field is added to the real one.
    """
    stock = session.get(Stock, pattern.stock_id)
    rs = session.get(RelativeStrength, (pattern.stock_id, pattern.base_end))
    regime = session.get(MarketRegimeRow, pattern.base_end)

    sector_code = quadrant = None
    sector_rs = None
    if stock is not None and stock.industry_id is not None:
        from ...db.models import Industry, Sector

        row = session.execute(
            select(Sector.id, Sector.code)
            .join(Industry, Industry.sector_id == Sector.id)
            .where(Industry.id == stock.industry_id)
        ).first()
        if row is not None:
            sector_code = row[1]
            metric = session.get(SectorMetric, (row[0], pattern.base_end))
            if metric is not None:
                sector_rs = num(metric.rs_score)
                quadrant = metric.quadrant

    return SetupRow(
        symbol=stock.symbol if stock else "",
        as_of=pattern.base_end,
        # The last stored close, not the pivot: an explanation that reported
        # the pivot as the price would be describing a level, not the market.
        close=_last_close(session, pattern.stock_id, pattern.base_end) or 0.0,
        sector=sector_code,
        vcp_score=num(pattern.score),
        vcp_stage=pattern.status,
        pivot_price=num(pattern.pivot_price),
        base_depth_pct=num(pattern.base_depth_pct),
        base_start=pattern.base_start,
        base_end=pattern.base_end,
        contractions=pattern.contraction_count,
        rs_score=num(rs.rs_score) if rs else None,
        rs_rank=rs.rs_rank if rs else None,
        rs_trend=rs.rs_trend if rs else None,
        sector_rs=sector_rs,
        sector_quadrant=quadrant,
        market_regime=regime.regime if regime else None,
        engine_version=pattern.engine_version,
    )


def _last_close(session, stock_id: int, on) -> float | None:
    from ...db.models import OhlcvDaily

    value = session.scalar(
        select(OhlcvDaily.close)
        .where(OhlcvDaily.stock_id == stock_id, OhlcvDaily.date <= on)
        .order_by(OhlcvDaily.date.desc())
        .limit(1)
    )
    return num(value)


@router.post("/explain")
def explain(
    session: SessionDep,
    user: UserDep,
    prov: ProvenanceDep,
    payload: Annotated[dict, Body()],
) -> dict:
    subject_type = (payload.get("subject_type") or "").strip().upper()
    subject_id = payload.get("subject_id")

    if subject_type not in SUBJECTS:
        raise errors.invalid_params(
            f"subject_type must be one of {sorted(SUBJECTS)}.", given=subject_type or None
        )
    try:
        subject_id = int(subject_id)
    except (TypeError, ValueError) as exc:
        raise errors.invalid_params("subject_id must be an integer.") from exc

    if subject_type == "PATTERN":
        pattern = session.get(Pattern, subject_id)
        if pattern is None:
            raise errors.not_found(f"Pattern {subject_id}")
        row = _setup_row_from(session, pattern)
        components = tuple(loads(pattern.score_breakdown, []) or ())
        try:
            explanation = explain_setup(
                row,
                # The real domain type, not a stand-in: it carries the
                # `contribution` property the renderer reads.
                tuple(
                    ScoreComponent(
                        name=c.get("name", ""),
                        raw=float(c.get("raw") or 0.0),
                        weight=float(c.get("weight") or 0.0),
                    )
                    for c in components
                ),
                engine_version=pattern.engine_version,
            )
        except ExplanationRejected as exc:
            # Discarded, not shown with a caveat. Text that failed a guardrail
            # is not made safe by a warning printed next to it.
            raise errors.ApiError(
                "INVALID_PARAMS", 422, f"Explanation rejected by guardrail: {exc}"
            ) from exc
    else:
        breakout = session.get(Breakout, subject_id)
        if breakout is None:
            raise errors.not_found(f"Breakout {subject_id}")
        stock = session.get(Stock, breakout.stock_id)
        subject = _Fields(
            symbol=stock.symbol if stock else None,
            breakout_date=breakout.breakout_date,
            breakout_price=num(breakout.breakout_price),
            pivot_price=num(breakout.pivot_price),
            rel_volume=num(breakout.rel_volume),
            rs_at_breakout=num(breakout.rs_at_breakout),
            regime_at_breakout=breakout.regime_at_breakout,
            setup_score=num(breakout.setup_score),
            outcome=breakout.outcome,
        )
        try:
            explanation = explain_failure(
                subject, engine_version=breakout.engine_version
            )
        except ExplanationRejected as exc:
            raise errors.ApiError(
                "INVALID_PARAMS", 422, f"Explanation rejected by guardrail: {exc}"
            ) from exc

    stored = AiExplanation(
        subject_type=subject_type,
        subject_id=str(subject_id),
        model=explanation.model,
        input_payload=json.dumps(explanation.inputs, default=str),
        output_text=explanation.text,
        engine_version_of_inputs=explanation.engine_version_of_inputs,
    )
    session.add(stored)
    session.flush()

    return {
        **explanation.to_payload(),
        "id": stored.id,
        **prov.envelope(engine_version=explanation.engine_version_of_inputs),
    }


@router.get("/explanations/{subject_type}/{subject_id}")
def explanation_history(
    subject_type: str, subject_id: str, session: SessionDep, user: UserDep
) -> dict:
    """Past explanations for one subject, newest first.

    Kept rather than overwritten: an explanation generated under an older
    engine version described the numbers as they were then.
    """
    rows = session.scalars(
        select(AiExplanation)
        .where(
            AiExplanation.subject_type == subject_type.upper(),
            AiExplanation.subject_id == str(subject_id),
        )
        .order_by(AiExplanation.id.desc())
    ).all()
    return {
        "explanations": [
            {
                "id": row.id,
                "explanation": row.output_text,
                "inputs": loads(row.input_payload, {}),
                "model": row.model,
                "generated_at": iso(row.generated_at),
                "engine_version_of_inputs": row.engine_version_of_inputs,
                "disclaimer": DISCLAIMER,
            }
            for row in rows
        ]
    }


#: Questions the research assistant can answer, each mapped to a stored query.
#: It has no free-text path to market data: a question outside this set is
#: refused rather than answered from the model's own recollection.
RESEARCH_INTENTS = {
    "top_setups": "Highest-scoring patterns on the latest scan",
    "failed_breakouts": "Breakouts that failed, with reasons",
    "sector_leaders": "Sectors ranked by relative strength",
    "regime": "The current market regime and its components",
}


@router.post("/research")
def research(
    session: SessionDep,
    user: UserDep,
    prov: ProvenanceDep,
    payload: Annotated[dict, Body()],
) -> dict:
    """Translate a question into a query against stored tables and narrate it.

    If the question does not map to a query the database can answer, the
    assistant says so. It never answers from the model's own knowledge -- a
    fabricated market fact delivered in the same voice as a computed one is
    the worst failure mode this whole system is built to avoid.
    """
    question = (payload.get("question") or "").strip()
    if not question:
        raise errors.invalid_params("'question' is required.")

    lowered = question.lower()
    intent = None
    if any(w in lowered for w in ("regime", "market condition", "bull", "bear")):
        intent = "regime"
    elif any(w in lowered for w in ("fail", "failed", "failure")):
        intent = "failed_breakouts"
    elif any(w in lowered for w in ("sector", "rotation", "leading")):
        intent = "sector_leaders"
    elif any(w in lowered for w in ("setup", "pattern", "vcp", "score", "top")):
        intent = "top_setups"

    if intent is None:
        return {
            "question": question,
            "answered": False,
            "reason": (
                "This question does not map to a query the research database "
                "can answer. Rephrase it in terms of setups, sectors, "
                "breakout failures, or market regime."
            ),
            "supported_intents": RESEARCH_INTENTS,
        }

    target = prov.as_of
    if target is None:
        raise errors.scan_not_run()

    rows: list[dict] = []
    if intent == "regime":
        from ..serialise import regime_payload

        regime = session.get(MarketRegimeRow, target)
        rows = [regime_payload(regime)] if regime else []
    elif intent == "sector_leaders":
        from ...db.models import Sector

        for metric, code, name in session.execute(
            select(SectorMetric, Sector.code, Sector.name)
            .join(Sector, Sector.id == SectorMetric.sector_id)
            .where(SectorMetric.date == target, SectorMetric.rs_rank.isnot(None))
            .order_by(SectorMetric.rs_rank)
            .limit(10)
        ).all():
            rows.append({"sector": code, "name": name,
                         "rs_score": num(metric.rs_score), "rs_rank": metric.rs_rank,
                         "quadrant": metric.quadrant})
    elif intent == "failed_breakouts":
        for breakout, symbol in session.execute(
            select(Breakout, Stock.symbol)
            .join(Stock, Stock.id == Breakout.stock_id)
            .where(Breakout.status == "FAILED")
            .order_by(Breakout.breakout_date.desc())
            .limit(20)
        ).all():
            rows.append({
                "symbol": symbol,
                "breakout_date": iso(breakout.breakout_date),
                "setup_score": num(breakout.setup_score),
                "failure_reason": (
                    breakout.outcome.failure_reason if breakout.outcome else None
                ),
            })
    else:
        for pattern, symbol in session.execute(
            select(Pattern, Stock.symbol)
            .join(Stock, Stock.id == Pattern.stock_id)
            .where(Pattern.base_end == target, Pattern.score.isnot(None))
            .order_by(Pattern.score.desc())
            .limit(20)
        ).all():
            rows.append({"symbol": symbol, "score": num(pattern.score),
                         "stage": pattern.status,
                         "pivot_price": num(pattern.pivot_price)})

    return {
        "question": question,
        "answered": True,
        "intent": intent,
        "intent_description": RESEARCH_INTENTS[intent],
        "rows": rows,
        "row_count": len(rows),
        # Echoed so the narration can be checked against what was queried.
        "query": {"intent": intent, "as_of": target.isoformat()},
        "disclaimer": DISCLAIMER,
        **prov.envelope(as_of=target.isoformat()),
    }
