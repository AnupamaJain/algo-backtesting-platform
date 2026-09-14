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

from ...db.models import MarketRegimeRow, OhlcvDaily, Pattern, ScanRun, Stock
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


@router.get("/", response_class=HTMLResponse)
def landing(request: Request, session: SessionDep, prov: ProvenanceDep):
    regime = session.get(MarketRegimeRow, prov.as_of) if prov.as_of else None
    return TEMPLATES.TemplateResponse(
        request, "landing.html",
        {
            "coverage": _coverage(session),
            "regime": regime,
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
