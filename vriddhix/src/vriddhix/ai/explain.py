"""AI explanation. Phase 9.

The AI layer explains numbers that the engines already computed. It has no
path to the computation and cannot produce a market fact of its own -- the
only facts in its prompt are the ones passed to it, and its output is stored
alongside, never in place of, the computed values.

Three guardrails, all enforced here rather than trusted to the prompt:

1. **The payload is built server-side** from stored rows. A client cannot
   inject a claim for the model to repeat back as analysis.
2. **The output is screened.** Recommendation language ("buy", "target",
   "guaranteed") and unsupported numbers are rejected before storage.
3. **The inputs are echoed** with every explanation, so any sentence can be
   checked against the data that produced it.

A deterministic renderer is included and used when no model client is
configured. It is not a fallback in the apologetic sense -- for a panel whose
job is to restate computed components in English, a template is arguably the
more honest implementation, and it is the one the tests pin.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Protocol

DISCLAIMER = (
    "Explanation of computed measurements. Research output, not investment advice."
)

#: Language that would turn a measurement into a recommendation.
BANNED_PATTERNS = (
    r"\bbuy\b", r"\bsell\b", r"\bshould (?:buy|sell|invest)\b",
    r"\btarget price\b", r"\bprice target\b",
    r"\bguarantee(?:d|s)?\b", r"\bwill (?:rise|fall|go up|go down|reach)\b",
    r"\bsure(?:-| )?shot\b", r"\bmultibagger\b", r"\brecommend\b",
    r"\bprofit is\b", r"\bcertain(?:ly)? (?:to|will)\b",
)

_BANNED = re.compile("|".join(BANNED_PATTERNS), re.IGNORECASE)


class ExplanationRejected(ValueError):
    """Raised when generated text violates a guardrail. The explanation is
    discarded rather than shown with a warning attached."""


class ModelClient(Protocol):
    def complete(self, prompt: str) -> str: ...


@dataclass(frozen=True, slots=True)
class Explanation:
    subject_type: str
    subject_id: str
    text: str
    inputs: dict
    model: str
    generated_at: date
    engine_version_of_inputs: str
    disclaimer: str = DISCLAIMER

    def to_payload(self) -> dict:
        return {
            "explanation": self.text,
            "inputs": self.inputs,          # echoed so claims can be audited
            "model": self.model,
            "generated_at": self.generated_at.isoformat(),
            "engine_version_of_inputs": self.engine_version_of_inputs,
            "disclaimer": self.disclaimer,
        }


# ---------------------------------------------------------------------------
# Payload construction -- server-side only
# ---------------------------------------------------------------------------


def build_setup_payload(row, components: tuple = ()) -> dict:
    """Assemble the facts an explanation is allowed to use.

    Everything here came from an engine. Nothing is derived, inferred or
    rounded into a different claim.
    """
    payload = {
        "symbol": row.symbol,
        "as_of": row.as_of.isoformat() if isinstance(row.as_of, date) else str(row.as_of),
        "close": row.close,
        "sector": row.sector,
        "vcp_score": row.vcp_score,
        "vcp_stage": row.vcp_stage,
        "grade": row.grade,
        "pivot_price": row.pivot_price,
        "distance_from_pivot_pct": row.distance_from_pivot_pct,
        "base_depth_pct": row.base_depth_pct,
        "contractions": row.contractions,
        "rs_score": row.rs_score,
        "rs_rank": row.rs_rank,
        "rs_trend": row.rs_trend,
        "sector_rs": row.sector_rs,
        "sector_quadrant": row.sector_quadrant,
        "market_regime": row.market_regime,
        "structure": row.structure,
        "confluence_score": row.confluence_score,
        "rel_volume": row.rel_volume,
    }
    if components:
        payload["score_components"] = [
            {"name": c.name, "raw": round(c.raw, 2), "weight": c.weight,
             "contribution": round(c.contribution, 2)}
            for c in components
        ]
    return {k: v for k, v in payload.items() if v is not None}


def build_failure_payload(breakout) -> dict:
    """Facts available about a failed breakout.

    Deliberately limited to what was recorded. The model is asked for possible
    contributing factors, not a cause -- the data cannot establish causation
    and the prose must not imply that it can.
    """
    return {
        k: v
        for k, v in {
            "symbol": getattr(breakout, "symbol", None),
            "breakout_date": str(getattr(breakout, "breakout_date", "")),
            "breakout_price": getattr(breakout, "breakout_price", None),
            "pivot_price": getattr(breakout, "pivot_price", None),
            "rel_volume_at_breakout": getattr(breakout, "rel_volume", None),
            "rs_at_breakout": getattr(breakout, "rs_at_breakout", None),
            "regime_at_breakout": getattr(breakout, "regime_at_breakout", None),
            "days_to_failure": getattr(breakout, "days_to_failure", None),
            "failure_reason": getattr(breakout, "failure_reason", None),
            "mfe_pct": getattr(breakout, "mfe_pct", None),
            "mae_pct": getattr(breakout, "mae_pct", None),
        }.items()
        if v not in (None, "")
    }


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------


def screen(text: str, payload: dict) -> str:
    """Reject text that recommends, predicts, or cites unsupported numbers."""
    match = _BANNED.search(text)
    if match:
        raise ExplanationRejected(
            f"generated text contains recommendation language: {match.group(0)!r}"
        )

    supported = _supported_numbers(payload)
    for token in re.findall(r"\d+(?:\.\d+)?", text):
        value = float(token)
        if not any(abs(value - s) < 0.6 for s in supported):
            raise ExplanationRejected(
                f"generated text cites {token}, which is not in the supplied inputs"
            )
    return text


def _supported_numbers(payload: dict) -> set[float]:
    found: set[float] = set()

    def walk(value):
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            found.add(float(value))
            found.add(float(round(value)))
            found.add(float(round(value, 1)))
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v)
        elif isinstance(value, str):
            for token in re.findall(r"\d+(?:\.\d+)?", value):
                found.add(float(token))

    walk(payload)
    return found


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def render_setup(payload: dict) -> str:
    """Deterministic prose from the payload. Every clause is a stored fact."""
    parts: list[str] = []
    symbol = payload.get("symbol", "This symbol")

    score = payload.get("vcp_score")
    grade = payload.get("grade")
    if score is not None:
        opening = f"{symbol} scores {score:.0f} on the VCP measure"
        if grade:
            opening += f" ({grade.replace('_', '+')})"
        parts.append(opening + ".")

    components = payload.get("score_components") or []
    if components:
        ranked = sorted(components, key=lambda c: c["contribution"], reverse=True)[:3]
        described = ", ".join(
            f"{c['name'].replace('_', ' ')} at {c['raw']:.0f}" for c in ranked
        )
        parts.append(f"The largest contributions come from {described}.")

    rs = payload.get("rs_score")
    if rs is not None:
        sentence = f"Relative strength is {rs:.0f}"
        trend = payload.get("rs_trend")
        if trend:
            sentence += f" and {trend.lower()}"
        parts.append(sentence + ".")

    sector = payload.get("sector")
    quadrant = payload.get("sector_quadrant")
    if sector and quadrant:
        parts.append(f"Its sector ({sector}) currently sits in the {quadrant} quadrant.")

    distance = payload.get("distance_from_pivot_pct")
    pivot = payload.get("pivot_price")
    if distance is not None and pivot is not None:
        if distance <= 0:
            parts.append(f"Price is above the identified pivot of {pivot:.2f}.")
        else:
            parts.append(
                f"Price sits {distance:.1f}% below the identified pivot of {pivot:.2f}."
            )

    regime = payload.get("market_regime")
    if regime:
        parts.append(f"The measured market regime is {regime}.")

    depth = payload.get("base_depth_pct")
    contractions = payload.get("contractions")
    if depth is not None and contractions is not None:
        parts.append(
            f"The base is {depth:.1f}% deep and contains {contractions} measured contractions."
        )

    return " ".join(parts)


def render_failure(payload: dict) -> str:
    """Possible contributing factors -- never a stated cause.

    The recorded data cannot establish why a breakout failed, and prose that
    implies otherwise would be inventing a mechanism.
    """
    factors: list[str] = []

    rel_volume = payload.get("rel_volume_at_breakout")
    if rel_volume is not None and rel_volume < 1.2:
        factors.append(f"breakout volume was thin at {rel_volume:.2f}x average")

    rs = payload.get("rs_at_breakout")
    if rs is not None and rs < 70:
        factors.append(f"relative strength was moderate at {rs:.0f}")

    regime = payload.get("regime_at_breakout")
    if regime in ("BEAR", "STRONG_BEAR", "NEUTRAL"):
        factors.append(f"the market regime was {regime}")

    mfe = payload.get("mfe_pct")
    if mfe is not None and mfe < 3:
        factors.append(f"the move never extended beyond {mfe:.1f}% above entry")

    days = payload.get("days_to_failure")
    header = f"{payload.get('symbol', 'The setup')} failed"
    if days is not None:
        header += f" after {days} days"

    if not factors:
        return (
            f"{header}. The recorded data does not distinguish a contributing factor; "
            "the measured inputs were within their usual ranges."
        )
    return f"{header}. Possible contributing factors: " + "; ".join(factors) + "."


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def explain_setup(
    row, components: tuple = (), *, client: ModelClient | None = None,
    model_name: str = "deterministic", engine_version: str = "VCP_ENGINE_V1.0",
    today: date | None = None,
) -> Explanation:
    payload = build_setup_payload(row, components)
    text = client.complete(_prompt_for_setup(payload)) if client else render_setup(payload)

    return Explanation(
        subject_type="PATTERN",
        subject_id=str(row.symbol),
        text=screen(text, payload),
        inputs=payload,
        model=model_name if client else "deterministic",
        generated_at=today or (row.as_of if isinstance(row.as_of, date) else date.today()),
        engine_version_of_inputs=engine_version,
    )


def explain_failure(
    breakout, *, client: ModelClient | None = None, model_name: str = "deterministic",
    engine_version: str = "VCP_ENGINE_V1.0", today: date | None = None,
) -> Explanation:
    payload = build_failure_payload(breakout)
    text = client.complete(_prompt_for_failure(payload)) if client else render_failure(payload)

    return Explanation(
        subject_type="FAILURE",
        subject_id=str(payload.get("symbol", "")),
        text=screen(text, payload),
        inputs=payload,
        model=model_name if client else "deterministic",
        generated_at=today or date.today(),
        engine_version_of_inputs=engine_version,
    )


def _prompt_for_setup(payload: dict) -> str:
    return (
        "Explain, in three to five sentences, why the following measurements "
        "produced this score. Use ONLY the values given. Do not recommend an "
        "action, do not state a target, do not predict a direction, and do not "
        "introduce any number that is not present below.\n\n"
        f"{payload}"
    )


def _prompt_for_failure(payload: dict) -> str:
    return (
        "List possible contributing factors to this failed breakout, using ONLY "
        "the values given. Say 'possible contributing factors'; do not assert a "
        "cause, and do not introduce any number not present below.\n\n"
        f"{payload}"
    )
