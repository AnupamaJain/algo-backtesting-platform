"""Public pages: landing, signup, learn.

Served from the same process as the API so there is one deployable and no
second source of truth about what the platform claims. Every number shown on
these pages is read from the database at request time -- the landing page
reports the actual coverage, not a marketing figure that drifts.

Nothing here promises a return, ranks a stock as a buy, or implies advice.
That is a product rule before it is a regulatory one: the whole system is
built to measure and record, and a landing page that oversells it would be
the first thing to make the measurements untrustworthy.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from ...db.models import (
    Breakout,
    Industry,
    MarketMetric,
    MarketRegimeRow,
    OhlcvDaily,
    Pattern,
    RelativeStrength,
    ScanRun,
    Sector,
    Stock,
)
from ..deps import ProvenanceDep, SessionDep

logger = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

router = APIRouter(tags=["pages"], include_in_schema=False)


def _coverage(session) -> dict:
    """What the database actually holds right now.

    Read live rather than hard-coded: a landing page quoting a number that
    has drifted from the database is the smallest possible version of the
    dishonesty this platform is built to avoid.
    """
    try:
        first = session.scalar(select(func.min(OhlcvDaily.date)))
        last = session.scalar(select(func.max(OhlcvDaily.date)))
        return {
            "symbols": session.scalar(select(func.count()).select_from(Stock)) or 0,
            "bars": session.scalar(select(func.count()).select_from(OhlcvDaily)) or 0,
            "scans": session.scalar(
                select(func.count()).select_from(ScanRun).where(ScanRun.status == "COMPLETED")
            ) or 0,
            "patterns": session.scalar(select(func.count()).select_from(Pattern)) or 0,
            "first_bar": first,
            "last_bar": last,
            "years": round((last - first).days / 365.25, 1) if first and last else 0,
        }
    except Exception:  # noqa: BLE001
        # A page that cannot count must still render. The figures are
        # context, not the product.
        logger.exception("coverage query failed")
        return {}


#: Band colours for the regime ribbon, dark-mode friendly at both ends.
REGIME_COLOURS = {
    "STRONG_BULL": "#1f9d61",
    "BULL":        "#57b98a",
    "NEUTRAL":     "#9a948c",
    "BEAR":        "#d98368",
    "STRONG_BEAR": "#b8402f",
}


def _regime_ribbon(session, points: int = 320) -> dict:
    """Ten years of regime score, drawn as a chart over its own thresholds.

    Bands-per-session were tried and abandoned. The regime genuinely changes
    every ten sessions or so, so any vertical-stripe encoding renders as
    static: technically every value, visually nothing. A score line over
    shaded threshold zones says the same thing and can actually be read --
    where the line sits IS the regime, because the zones are the thresholds
    that define it.
    """
    rows = session.execute(
        select(MarketRegimeRow.date, MarketRegimeRow.regime_score)
        .order_by(MarketRegimeRow.date)
    ).all()
    if len(rows) < 2:
        return {}

    total = len(rows)
    step = max(1, total // points)
    sampled = [
        (rows[i][0], float(rows[i][1]))
        for i in range(0, total, step)
        if rows[i][1] is not None
    ]
    if len(sampled) < 2:
        return {}

    width, height = 100.0, 40.0
    coords = [
        (i / (len(sampled) - 1) * width, height - score / 100.0 * height)
        for i, (_, score) in enumerate(sampled)
    ]
    line = " ".join(f"{x:.2f},{y:.2f}" for x, y in coords)

    # Zones are the classifier's own thresholds, so the chart and the engine
    # cannot drift apart visually.
    cfg_zones = [
        ("STRONG_BULL", 75.0, 100.0),
        ("BULL",        60.0,  75.0),
        ("NEUTRAL",     40.0,  60.0),
        ("BEAR",        25.0,  40.0),
        ("STRONG_BEAR",  0.0,  25.0),
    ]
    zones = [
        {
            "y": height - hi / 100.0 * height,
            "h": (hi - lo) / 100.0 * height,
            "fill": REGIME_COLOURS[name],
            "label": name.replace("_", " ").title(),
        }
        for name, lo, hi in cfg_zones
    ]

    counts: dict[str, int] = {}
    regimes = session.execute(
        select(MarketRegimeRow.regime).order_by(MarketRegimeRow.date)
    ).all()
    for (regime,) in regimes:
        counts[regime] = counts.get(regime, 0) + 1
    changes = sum(
        1 for i in range(1, len(regimes)) if regimes[i][0] != regimes[i - 1][0]
    )

    return {
        "line": line,
        "area": f"0,{height:.2f} {line} {width:.2f},{height:.2f}",
        "zones": zones,
        "height": height,
        "width": width,
        "first": rows[0][0],
        "last": rows[-1][0],
        "sessions": total,
        "changes": changes,
        "mix": [
            {"regime": r, "pct": counts.get(r, 0) / total * 100.0,
             "fill": REGIME_COLOURS[r], "sessions": counts.get(r, 0)}
            for r in ("STRONG_BULL", "BULL", "NEUTRAL", "BEAR", "STRONG_BEAR")
            if counts.get(r)
        ],
    }


def _sparkline(values: list[float], width: float = 100.0, height: float = 30.0) -> dict:
    """An SVG path plus its fill area, scaled to the values' own range."""
    clean = [v for v in values if v is not None]
    if len(clean) < 2:
        return {}
    low, high = min(clean), max(clean)
    span = (high - low) or 1.0
    step = width / (len(clean) - 1)
    points = [
        (i * step, height - (v - low) / span * height) for i, v in enumerate(clean)
    ]
    line = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    return {
        "line": line,
        "area": f"0,{height:.2f} {line} {width:.2f},{height:.2f}",
        "last": clean[-1],
        "first": clean[0],
        "change": clean[-1] - clean[0],
    }


def _breadth_spark(session, sessions: int = 180) -> dict:
    rows = session.execute(
        select(MarketMetric.pct_above_ema_50)
        .order_by(MarketMetric.date.desc())
        .limit(sessions)
    ).all()
    values = [float(v) for (v,) in reversed(rows) if v is not None]
    return _sparkline(values)


def _top_setups(session, as_of, limit: int = 5) -> list[dict]:
    """What the scanner is looking at right now."""
    if as_of is None:
        return []
    rows = session.execute(
        select(Pattern, Stock.symbol, Sector.code, RelativeStrength.rs_score)
        .join(Stock, Stock.id == Pattern.stock_id)
        .outerjoin(Industry, Industry.id == Stock.industry_id)
        .outerjoin(Sector, Sector.id == Industry.sector_id)
        .outerjoin(
            RelativeStrength,
            (RelativeStrength.stock_id == Stock.id)
            & (RelativeStrength.date == as_of),
        )
        .where(Pattern.base_end == as_of, Pattern.score.isnot(None))
        .order_by(Pattern.score.desc())
        .limit(limit)
    ).all()
    return [
        {
            "symbol": symbol,
            "sector": sector,
            "stage": pattern.status,
            "score": float(pattern.score),
            "rs": float(rs) if rs is not None else None,
            "contractions": pattern.contraction_count,
        }
        for pattern, symbol, sector, rs in rows
    ]


def _ledger(session) -> dict:
    """Confirmed against failed. Shown because hiding it would be the lie."""
    rows = session.execute(
        select(Breakout.status, func.count()).group_by(Breakout.status)
    ).all()
    counts = {status: n for status, n in rows}
    confirmed = counts.get("CONFIRMED", 0)
    failed = counts.get("FAILED", 0)
    resolved = confirmed + failed
    if not resolved:
        return {}
    return {
        "confirmed": confirmed,
        "failed": failed,
        "resolved": resolved,
        "pending": counts.get("PENDING", 0),
        "confirmed_pct": confirmed / resolved * 100.0,
        "failed_pct": failed / resolved * 100.0,
    }


@router.get("/", response_class=HTMLResponse)
def landing(request: Request, session: SessionDep, prov: ProvenanceDep):
    regime = session.get(MarketRegimeRow, prov.as_of) if prov.as_of else None
    breadth = session.get(MarketMetric, prov.as_of) if prov.as_of else None
    return TEMPLATES.TemplateResponse(
        request, "landing.html",
        {
            "coverage": _coverage(session),
            "regime": regime,
            "regime_colour": REGIME_COLOURS.get(regime.regime) if regime else None,
            "breadth": breadth,
            "ribbon": _regime_ribbon(session),
            "spark": _breadth_spark(session),
            "setups": _top_setups(session, prov.as_of),
            "ledger": _ledger(session),
            "as_of": prov.as_of,
            "is_stale": prov.is_stale,
            "page": "home",
        },
    )


@router.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    from ..auth import MIN_PASSWORD_LENGTH

    return TEMPLATES.TemplateResponse(
        request, "signup.html",
        {"page": "signup", "min_password": MIN_PASSWORD_LENGTH},
    )


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    from ..auth import MIN_PASSWORD_LENGTH

    return TEMPLATES.TemplateResponse(
        request, "signup.html",
        {"page": "login", "mode": "login", "min_password": MIN_PASSWORD_LENGTH},
    )


@router.get("/learn", response_class=HTMLResponse)
def learn(request: Request, session: SessionDep):
    return TEMPLATES.TemplateResponse(
        request, "learn.html", {"page": "learn", "coverage": _coverage(session)},
    )
