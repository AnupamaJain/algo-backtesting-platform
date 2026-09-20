"""Rebuild the regime chain when a session is inserted out of order.

Regime is not a pure function of its own day. Hysteresis makes each label
depend on the label before it, which means the stored sequence is a chain:
insert a session in the middle and every link after it was computed against a
predecessor that is no longer its predecessor.

That is not hypothetical. The nightly catch-up scans sessions it has never
seen, oldest first, and NSE holds occasional weekend sessions -- Budget Day,
Muhurat trading, disaster-recovery drills. When one of those is discovered
long after the fact, it lands in the middle of a chain that is already built.
2024-01-20 arrived that way and was labelled STRONG_BULL on a score of 78.3
while the session before it, scoring 78.4, was BULL.

The repair replays the chain in date order through the engine's own
``classify`` and ``apply_hysteresis``. Calling those rather than restating
the rules is the point: a second copy of the hysteresis logic here would be
free to drift from the one the scanner uses, and the two would disagree about
the same day.

Only the label is rebuilt. Scores are left exactly as measured -- they are
per-day facts that no neighbour can change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.types import MarketRegime
from ..engines.regime import RegimeConfig, apply_hysteresis, classify


@dataclass(frozen=True)
class ChainRepair:
    """What a replay found, whether or not anything was written."""

    examined: int = 0
    changed: int = 0
    first_change: date | None = None
    changes: tuple[tuple[date, str, str], ...] = ()

    @property
    def is_clean(self) -> bool:
        return self.changed == 0

    def summary(self) -> str:
        if self.is_clean:
            return f"{self.examined} sessions examined · chain consistent"
        return (
            f"{self.examined} sessions examined · {self.changed} relabelled "
            f"from {self.first_change}"
        )


def replay(
    session: Session,
    cfg: RegimeConfig,
    *,
    since: date | None = None,
    commit: bool = False,
) -> ChainRepair:
    """Recompute every stored regime label from its stored score, in order.

    ``commit=False`` is a dry run: it reports what a repair would change
    without touching a row, so the damage can be inspected before anything is
    rewritten.

    ``since`` starts the replay at a date, but the chain state is still seeded
    from the sessions before it -- starting cold would invent a state change
    on the first day rather than finding one.
    """
    from ..db.models import MarketRegimeRow

    rows = list(session.scalars(
        select(MarketRegimeRow).order_by(MarketRegimeRow.date)
    ).all())
    if not rows:
        return ChainRepair()

    current: MarketRegime | None = None
    candidates: list[MarketRegime] = []
    examined = 0
    changes: list[tuple[date, str, str]] = []

    for row in rows:
        # regime_score is NOT NULL in the schema, so every stored session can
        # be reclassified from what it measured.
        score = float(row.regime_score)
        raw = classify(score, cfg)
        effective, _pending = apply_hysteresis(raw, score, current, candidates, cfg)

        replaying = since is None or row.date >= since
        if replaying:
            examined += 1
            if row.regime != effective.value:
                changes.append((row.date, row.regime, effective.value))
                if commit:
                    row.regime = effective.value

        current = effective
        candidates.append(raw)
        if len(candidates) > 10:
            candidates.pop(0)

    if commit and changes:
        session.flush()

    return ChainRepair(
        examined=examined,
        changed=len(changes),
        first_change=changes[0][0] if changes else None,
        changes=tuple(changes),
    )


def flickers(session: Session) -> list[date]:
    """Dates whose label differs from both neighbours', which are identical.

    A single-session state change surrounded by agreement is what hysteresis
    exists to prevent, so finding one means the chain is broken rather than
    the market having genuinely changed its mind for a day.
    """
    from ..db.models import MarketRegimeRow

    rows = session.execute(
        select(MarketRegimeRow.date, MarketRegimeRow.regime)
        .order_by(MarketRegimeRow.date)
    ).all()
    return [
        rows[i][0]
        for i in range(1, len(rows) - 1)
        if rows[i][1] != rows[i - 1][1] and rows[i - 1][1] == rows[i + 1][1]
    ]
