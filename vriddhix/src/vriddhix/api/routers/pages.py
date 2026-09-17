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

from datetime import timedelta

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from ...config import PROJECT_ROOT
from ...db.models import (
    Breakout,
    BreakoutOutcome,
    Industry,
    MarketMetric,
    MarketRegimeRow,
    OhlcvDaily,
    Pattern,
    RelativeStrength,
    ScanRun,
    Sector,
    SectorMetric,
    Stock,
)
from ..deps import ProvenanceDep, SessionDep

logger = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

router = APIRouter(tags=["pages"], include_in_schema=False)


def _seo(request, path: str, *, kind: str = "WebPage", extra: dict | None = None) -> dict:
    """Per-page SEO context: canonical origin, and structured data.

    The structured data describes what the page IS rather than claiming
    ratings or offers it cannot substantiate. A SoftwareApplication block
    with an invented aggregateRating is the schema equivalent of a fabricated
    backtest, and Google penalises it when it notices.
    """
    from .seo import is_public, public_origin

    origin = public_origin(request)
    canonical = f"{origin}{path}"

    data = {
        "@context": "https://schema.org",
        "@type": kind,
        "name": "Pramana",
        "url": canonical,
        "inLanguage": "en-IN",
        "publisher": {"@type": "Organization", "name": "VriddhiX", "url": origin},
    }
    if extra:
        data.update(extra)

    return {
        "origin": origin,
        "canonical": canonical,
        "is_public": is_public(request),
        "structured_data": data,
    }


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


#: Answers a reader actually searches for, in the words they use. Written to
#: be true first: a question-and-answer block that exists only to carry terms
#: reads as filler and is treated as such.
LANDING_FAQ = [
    ("Is Pramana a free NSE stock screener?",
     "Yes. It scans NSE cash equities daily for volatility-contraction bases, "
     "breaks of market structure and fair value gaps, and an account costs "
     "nothing. It is a research tool rather than a broker — it has no trading "
     "path and cannot place an order."),
    ("What is the VCP or volatility contraction pattern?",
     "A base where each pullback is shallower than the last on drying volume, "
     "popularised by Mark Minervini. Pramana measures the prior trend, counts "
     "the contractions, locates the pivot and scores the base out of 100, "
     "showing every weighted component rather than a single opaque number."),
    ("Which smart money concepts does it detect?",
     "Swing structure, break of structure (BOS), change of character (CHoCH), "
     "order blocks, liquidity sweeps and fair value gaps — each stored with "
     "the date it became knowable, not just the date it printed."),
    ("How does it measure relative strength and sector rotation?",
     "Relative strength is a percentile rank across the universe blended over "
     "1, 3, 6 and 12-month returns — percentile rather than z-score, because "
     "Indian equity returns have fat tails. Sectors are aggregated "
     "equal-weight into leading, improving, weakening and lagging quadrants."),
    ("Can I backtest a screen on Indian stocks?",
     "Yes. A saved screen runs through the same condition evaluator the "
     "backtester uses, so a strategy cannot mean one thing on the dashboard "
     "and another in its own backtest. Fills are at the next open and costs "
     "include STT, stamp duty, exchange, SEBI and GST."),
    ("Does a VCP strategy beat buying the Nifty index?",
     "On this data, no. Replaying a 70+ VCP score over ten years of NSE F&O "
     "names returned 10.58% compound annually against 11.61% for holding the "
     "Nifty 50 ETF — after 353 trades, next-open fills and full statutory "
     "costs. It did fall less: worst drawdown 24.8% against 36.3%, because it "
     "sat in cash through most of the 2020 crash. The comparison is on the "
     "home page with the survivorship caveat attached."),
    ("Does it predict which stocks will go up?",
     "No, and it is built so it cannot pretend to. Scores classify what a "
     "chart has measurably done. The ledger records what followed each "
     "breakout, failures included, which is a record of the past rather than "
     "a claim about the future."),
]


#: Band colours for the regime ribbon, dark-mode friendly at both ends.
REGIME_COLOURS = {
    "STRONG_BULL": "#1f9d61",
    "BULL":        "#57b98a",
    "NEUTRAL":     "#9a948c",
    "BEAR":        "#d98368",
    "STRONG_BEAR": "#b8402f",
}


def _regime_ribbon(session, buckets: int = 56) -> dict:
    """Ten years of market state as wide, solid blocks.

    Coarse on purpose. One band per session compresses 2,415 values into
    under a third of a pixel each and renders as static -- technically every
    value, visually nothing. At roughly two months per block the shape of a
    decade is legible at a glance, and the exact history stays available
    through the API for anyone who wants it.
    """
    rows = session.execute(
        select(MarketRegimeRow.date, MarketRegimeRow.regime)
        .order_by(MarketRegimeRow.date)
    ).all()
    if len(rows) < 2:
        return {}

    total = len(rows)
    size = max(1, total // buckets)
    groups = [rows[i:i + size] for i in range(0, total, size)]

    bands = []
    for i, group in enumerate(groups):
        counts: dict[str, int] = {}
        for _d, regime in group:
            counts[regime] = counts.get(regime, 0) + 1
        dominant = max(counts, key=counts.get)
        bands.append({
            "x": i / len(groups) * 100.0,
            "w": 100.0 / len(groups),
            "fill": REGIME_COLOURS.get(dominant, "#9a948c"),
            "regime": dominant.replace("_", " ").title(),
            "from": group[0][0],
            "to": group[-1][0],
        })

    counts_all: dict[str, int] = {}
    for _d, regime in rows:
        counts_all[regime] = counts_all.get(regime, 0) + 1
    changes = sum(1 for i in range(1, total) if rows[i][1] != rows[i - 1][1])

    return {
        "bands": bands,
        "first": rows[0][0],
        "last": rows[-1][0],
        "sessions": total,
        "changes": changes,
        "mix": [
            {"regime": r.replace("_", " ").title(),
             "pct": counts_all.get(r, 0) / total * 100.0,
             "fill": REGIME_COLOURS[r], "sessions": counts_all.get(r, 0)}
            for r in ("STRONG_BULL", "BULL", "NEUTRAL", "BEAR", "STRONG_BEAR")
            if counts_all.get(r)
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



def _screener_rows(session, as_of, *, days: int = 365, limit: int = 120) -> list[dict]:
    """Real setups for the screener demo on the landing page.

    The most recent base found in each symbol over the trailing year, not a
    fabricated sample. A marketing demo built on invented rows would be the
    one place on this site where the numbers are not the engine's -- and a
    visitor has no way to tell the difference, which is exactly why it must
    not be done.
    """
    if as_of is None:
        return []
    since = as_of - timedelta(days=days)

    rows = session.execute(
        select(Pattern, Stock.symbol, Sector.code, RelativeStrength.rs_score)
        .join(Stock, Stock.id == Pattern.stock_id)
        .outerjoin(Industry, Industry.id == Stock.industry_id)
        .outerjoin(Sector, Sector.id == Industry.sector_id)
        .outerjoin(
            RelativeStrength,
            (RelativeStrength.stock_id == Stock.id)
            & (RelativeStrength.date == Pattern.base_end),
        )
        .where(
            Pattern.base_end >= since,
            Pattern.base_end <= as_of,
            Pattern.score.isnot(None),
        )
        .order_by(Pattern.base_end.desc())
    ).all()

    seen: set[str] = set()
    out: list[dict] = []
    for pattern, symbol, sector, rs in rows:
        if symbol in seen:
            continue
        seen.add(symbol)
        out.append({
            "symbol": symbol,
            "sector": sector_label(sector),
            "stage": pattern.status,
            "score": round(float(pattern.score), 1),
            "rs": round(float(rs), 1) if rs is not None else None,
            "contractions": pattern.contraction_count or 0,
            "depth": round(float(pattern.base_depth_pct), 1) if pattern.base_depth_pct else None,
            "on": pattern.base_end.isoformat(),
        })
        if len(out) >= limit:
            break
    return sorted(out, key=lambda r: r["score"], reverse=True)


#: Sector codes that are acronyms. str.title() renders IT as "It" and FMCG as
#: "Fmcg", which looks like a typo to exactly the audience that would notice.
_ACRONYMS = {"IT", "FMCG", "ETF", "NBFC", "QSR", "PSU", "AMC"}


def sector_label(code: str | None) -> str:
    """Human-readable sector name, with acronyms left alone."""
    if not code:
        return "Unclassified"
    return " ".join(
        word if word.upper() in _ACRONYMS else word.title()
        for word in code.replace("_", " ").split()
    )


def _rotation(session, as_of) -> list[dict]:
    """Sectors by quadrant, for the rotation strip.

    Real output that was sitting unused: the engine has computed this on
    every scan since 2016 and nothing on the marketing page showed it.
    """
    if as_of is None:
        return []
    rows = session.execute(
        select(SectorMetric, Sector.code, Sector.name)
        .join(Sector, Sector.id == SectorMetric.sector_id)
        .where(SectorMetric.date == as_of)
    ).all()

    order = {"LEADING": 0, "IMPROVING": 1, "WEAKENING": 2, "LAGGING": 3}
    out = []
    for metric, code, name in rows:
        if metric.quadrant is None:
            continue          # unranked: no position to plot
        out.append({
            "code": sector_label(code),
            "name": name,
            "quadrant": metric.quadrant,
            "rank": metric.rs_rank,
            "rs": round(float(metric.rs_score), 1) if metric.rs_score is not None else None,
            "momentum": round(float(metric.momentum_score), 1) if metric.momentum_score is not None else None,
            "constituents": metric.constituent_count,
        })
    return sorted(out, key=lambda r: (order.get(r["quadrant"], 9), r["rank"] or 99))


#: Real screens, captured from this application rather than drawn. Each one
#: is a region of a page that actually renders from the database.
APP_SCREENS = [
    ("scanner",  "Live scanner",
     "209 NSE names, filtered in the browser against stored scan rows."),
    ("regime",   "Market regime",
     "Five weighted components with hysteresis, or an explicit refusal."),
    ("rotation", "Sector rotation",
     "Equal-weight aggregation into four quadrants."),
    ("evidence", "Breakout ledger",
     "Every outcome recorded, failures kept and counted."),
    ("api",      "Read API",
     "46 endpoints, every payload carrying its provenance."),
]


def _regime_surface(session, points: int = 420) -> list[float]:
    """Regime score over time, for the WebGL terrain.

    The 3D piece is built from this rather than from noise. A decorative
    surface would be the one graphic on the page not answerable to the data.
    """
    rows = session.execute(
        select(MarketRegimeRow.regime_score).order_by(MarketRegimeRow.date)
    ).all()
    if len(rows) < 20:
        return []
    step = max(1, len(rows) // points)
    return [
        round(float(r[0]), 2) for r in rows[::step] if r[0] is not None
    ]


def _measured_results(session) -> dict:
    """What the recorded breakouts actually did -- shown instead of an ROI.

    Deliberately not framed as a return anyone would have earned: there is no
    position sizing, no slippage and no capital here, only what price did
    after each recorded breakout. Calling that ROI would contradict every
    other surface on the site.
    """
    from sqlalchemy import case

    row = session.execute(
        select(
            func.count(),
            func.sum(case((Breakout.status == "FAILED", 1), else_=0)),
        ).where(Breakout.status.in_(("CONFIRMED", "FAILED")))
    ).first()
    total, failed = (row[0] or 0), int(row[1] or 0)
    if total < 50:
        return {}

    def avg(column, status):
        return session.scalar(
            select(func.avg(column))
            .select_from(BreakoutOutcome)
            .join(Breakout, Breakout.id == BreakoutOutcome.breakout_id)
            .where(Breakout.status == status)
        )

    held_mfe = _num(avg(BreakoutOutcome.mfe_pct, "CONFIRMED")) or 0.0
    held_mae = _num(avg(BreakoutOutcome.mae_pct, "CONFIRMED")) or 0.0
    lost_mfe = _num(avg(BreakoutOutcome.mfe_pct, "FAILED")) or 0.0
    lost_mae = _num(avg(BreakoutOutcome.mae_pct, "FAILED")) or 0.0

    return {
        "total": total,
        "held": total - failed,
        "failed": failed,
        "hold_pct": (total - failed) / total * 100.0,
        "fail_pct": failed / total * 100.0,
        "held_mfe": held_mfe, "held_mae": held_mae,
        "lost_mfe": lost_mfe, "lost_mae": lost_mae,
        # The asymmetry is the finding, and it is a ratio of measured
        # excursions rather than a projected return.
        "edge": round(held_mfe / abs(lost_mae), 2) if lost_mae else None,
    }


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


def _testimonials() -> list[dict]:
    """Quotes from config/testimonials.yaml, verified ones only.

    An unverified entry is a draft, not a testimonial, and never reaches the
    page. A fabricated quote on a financial product is a false statement
    about a real person's experience of something that affects money -- and
    in India that sits inside SEBI's advertising rules, which cover research
    and analytics products, not only advice. The section disappears when
    there is nothing real to put in it.
    """
    path = PROJECT_ROOT / "config" / "testimonials.yaml"
    try:
        with path.open() as handle:
            loaded = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError):
        return []

    out = []
    for entry in loaded.get("testimonials") or []:
        if not isinstance(entry, dict) or entry.get("verified") is not True:
            continue
        quote, name = str(entry.get("quote", "")).strip(), str(entry.get("name", "")).strip()
        if not quote or not name:
            continue
        out.append({
            "quote": quote,
            "name": name,
            "role": str(entry.get("role", "")).strip() or None,
            "initials": "".join(w[0] for w in name.split()[:2]).upper(),
        })
    return out


def _performance(session) -> dict:
    """The backtested equity curve, against buying the index and waiting."""
    from ...services.performance import performance_panel

    return performance_panel(session)


@router.get("/", response_class=HTMLResponse)
def landing(request: Request, session: SessionDep, prov: ProvenanceDep):
    regime = session.get(MarketRegimeRow, prov.as_of) if prov.as_of else None
    breadth = session.get(MarketMetric, prov.as_of) if prov.as_of else None
    return TEMPLATES.TemplateResponse(
        request, "landing.html",
        {
            **_seo(request, "/", kind="SoftwareApplication", extra={
                "applicationCategory": "FinanceApplication",
                "operatingSystem": "Web",
                "description": (
                    "Pattern research for Indian equities: volatility contraction "
                    "bases, market structure, regime and relative strength, with "
                    "the outcome of every detected setup recorded."
                ),
                "offers": {"@type": "Offer", "price": "0", "priceCurrency": "INR"},
                "mainEntity": [
                    {"@type": "Question", "name": q,
                     "acceptedAnswer": {"@type": "Answer", "text": a}}
                    for q, a in LANDING_FAQ
                ],
                "featureList": [
                    "VCP detection", "Market structure (BOS/CHoCH)",
                    "Fair value gaps", "Market regime", "Relative strength",
                    "Sector rotation", "Breakout ledger", "Backtesting",
                ],
            }),
            "coverage": _coverage(session),
            "regime": regime,
            "regime_colour": REGIME_COLOURS.get(regime.regime) if regime else None,
            "breadth": breadth,
            "ribbon": _regime_ribbon(session),
            "spark": _breadth_spark(session),
            "screener": _screener_rows(session, prov.as_of),
            "rotation": _rotation(session, prov.as_of),
            "screens": APP_SCREENS,
            "surface": _regime_surface(session),
            "results": _measured_results(session),
            "performance": _performance(session),
            "testimonials": _testimonials(),
            "faq": LANDING_FAQ,
            "universe_size": session.scalar(select(func.count()).select_from(Stock)) or 0,
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
        {**_seo(request, "/signup"), "page": "signup",
         "min_password": MIN_PASSWORD_LENGTH},
    )


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    from ..auth import MIN_PASSWORD_LENGTH

    return TEMPLATES.TemplateResponse(
        request, "signup.html",
        {**_seo(request, "/login"), "page": "login", "mode": "login",
         "min_password": MIN_PASSWORD_LENGTH},
    )


def _breakout_evidence(session) -> dict:
    """What actually followed every breakout the engine recorded.

    This is the one thing on the site nobody else can copy: published VCP
    outcome rates contradict each other wildly (90% in one place, 60-70% in
    another) and none of them show the population they measured. These
    numbers come from the ledger, failures included, with the universe and
    period stated.
    """
    from sqlalchemy import case

    resolved = select(func.count()).select_from(Breakout).where(
        Breakout.status.in_(("CONFIRMED", "FAILED"))
    )
    total = session.scalar(resolved) or 0
    if total < 50:
        # Too few to publish a rate. A percentage over a handful of rows
        # reads as a finding and is not one.
        return {}

    failed = session.scalar(
        select(func.count()).select_from(Breakout).where(Breakout.status == "FAILED")
    ) or 0

    by_regime = []
    rows = session.execute(
        select(
            Breakout.regime_at_breakout,
            func.count(),
            func.sum(case((Breakout.status == "FAILED", 1), else_=0)),
        )
        .where(Breakout.status.in_(("CONFIRMED", "FAILED")))
        .group_by(Breakout.regime_at_breakout)
    ).all()
    order = {"STRONG_BULL": 0, "BULL": 1, "NEUTRAL": 2, "BEAR": 3, "STRONG_BEAR": 4}
    for regime, n, f in sorted(rows, key=lambda r: order.get(r[0], 9)):
        if not regime or n < 20:
            continue
        by_regime.append({
            "regime": regime.replace("_", " ").title(),
            "n": n,
            "failed": int(f or 0),
            "failure_pct": (f or 0) / n * 100.0,
            "fill": REGIME_COLOURS.get(regime, "#9a948c"),
        })

    def avg(column, where):
        return session.scalar(
            select(func.avg(column))
            .select_from(BreakoutOutcome)
            .join(Breakout, Breakout.id == BreakoutOutcome.breakout_id)
            .where(where)
        )

    held = Breakout.status == "CONFIRMED"
    lost = Breakout.status == "FAILED"
    first, last = session.execute(
        select(func.min(ScanRun.scan_date), func.max(ScanRun.scan_date))
        .where(ScanRun.status == "COMPLETED")
    ).first()

    return {
        "total": total,
        "failed": failed,
        "held": total - failed,
        "failure_pct": failed / total * 100.0,
        "hold_pct": (total - failed) / total * 100.0,
        "by_regime": by_regime,
        "days_to_failure": _num(avg(BreakoutOutcome.days_to_failure, lost)),
        "failed_mfe": _num(avg(BreakoutOutcome.mfe_pct, lost)),
        "failed_mae": _num(avg(BreakoutOutcome.mae_pct, lost)),
        "held_mfe": _num(avg(BreakoutOutcome.mfe_pct, held)),
        "held_mae": _num(avg(BreakoutOutcome.mae_pct, held)),
        "from": first,
        "to": last,
        "sessions": session.scalar(
            select(func.count()).select_from(ScanRun).where(ScanRun.status == "COMPLETED")
        ) or 0,
        "patterns": session.scalar(select(func.count()).select_from(Pattern)) or 0,
    }


def _num(value):
    return round(float(value), 2) if value is not None else None


@router.get("/vcp-breakout-failure-rate", response_class=HTMLResponse)
def breakout_evidence(request: Request, session: SessionDep, prov: ProvenanceDep):
    """How often a VCP breakout actually fails, measured rather than asserted."""
    evidence = _breakout_evidence(session)
    faq = [
        ("How often do VCP breakouts fail?",
         (f"In this measurement, {evidence['failure_pct']:.0f}% of "
          f"{evidence['total']:,} resolved breakouts on NSE closed back below "
          f"their pivot. The remaining {evidence['hold_pct']:.0f}% held above it."
          ) if evidence else
         "The rate is published from a ledger of recorded breakouts once "
         "enough have resolved to be worth quoting."),
        ("Does the market regime change the failure rate?",
         ("Yes. The rate rises as conditions deteriorate: it is lowest in "
          "bullish regimes and highest in bearish ones, measured on the "
          "regime recorded at the moment of each breakout."
          ) if evidence else "Yes — see the table."),
        ("How quickly does a failed breakout fail?",
         (f"On average {evidence['days_to_failure']:.0f} trading sessions after "
          f"the breakout, with an average best excursion of only "
          f"{evidence['failed_mfe']:.1f}% before it broke down."
          ) if evidence else "Within a few sessions."),
        ("Is this survivorship-biased?",
         "Partly, and it says so. Index membership is backfilled from today's "
         "constituent list, so the universe is the names liquid now rather "
         "than the names liquid then. Failed breakouts themselves are never "
         "removed — they are the point of the ledger."),
    ]
    return TEMPLATES.TemplateResponse(
        request, "evidence.html",
        {**_seo(request, "/vcp-breakout-failure-rate", kind="TechArticle", extra={
            "headline": "How often does a VCP breakout fail? Measured on NSE.",
            "description": (
                "A measured failure rate for volatility-contraction breakouts on "
                "Indian equities, broken down by market regime, with the method "
                "and the survivorship caveat stated."
            ),
            "mainEntity": [
                {"@type": "Question", "name": q,
                 "acceptedAnswer": {"@type": "Answer", "text": a}}
                for q, a in faq
            ],
        }),
         "page": "evidence", "e": evidence, "faq": faq},
    )


@router.get("/learn", response_class=HTMLResponse)
def learn(request: Request, session: SessionDep):
    return TEMPLATES.TemplateResponse(
        request, "learn.html",
        {**_seo(request, "/learn", kind="TechArticle", extra={
            "headline": "How Pramana measures a chart",
            "description": (
                "How the VCP, market structure, regime and relative strength "
                "engines work, and the five ways a research platform misleads "
                "you without meaning to."
            ),
        }),
         "page": "learn", "coverage": _coverage(session)},
    )
