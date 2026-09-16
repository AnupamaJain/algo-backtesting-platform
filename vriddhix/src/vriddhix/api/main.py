"""FastAPI application. docs/09.

Run locally:

    uvicorn vriddhix.api.main:app --reload

Nothing in this layer computes. Every endpoint projects rows the daily scan
already wrote, which is what keeps a read cheap, repeatable, and identical for
two users asking the same question on the same date.
"""

from __future__ import annotations

import logging

from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .. import versioning
from . import auth
from .errors import ApiError, api_error_handler, validation_error_handler
from fastapi.responses import HTMLResponse

from .routers import (
    ai, breakouts, market, ops, pages, product, research, scanners, sectors,
    seo, stocks,
)

logger = logging.getLogger(__name__)

#: Swagger UI ships an unbranded header. This paints the topbar in the
#: product's palette and puts the mark where the default logo would be, so
#: the reference does not look like a different product from the site.
_DOCS_STYLE = """
<style>
  :root { --sk-accent:#a9662c; --sk-ink:#1b1a18; --sk-line:#e3ded6; }
  body { background:#fbfaf8; }
  .swagger-ui .topbar { display:none; }
  .sk-head {
    display:flex; align-items:center; gap:14px;
    max-width:1460px; margin:0 auto; padding:22px 20px 0;
  }
  .sk-head img { width:38px; height:38px; }
  .sk-head h1 {
    margin:0; font:650 1.35rem/1.1 ui-sans-serif,-apple-system,system-ui;
    letter-spacing:-.02em; color:var(--sk-ink);
  }
  .sk-head span {
    font:400 .85rem/1.3 ui-sans-serif,system-ui; color:#6c6760; display:block;
    margin-top:3px;
  }
  .swagger-ui .info { margin:22px 0 30px; }
  .swagger-ui .info .title small.version-stamp { background:var(--sk-accent); }
  .swagger-ui .opblock.opblock-get .opblock-summary-method { background:#5b7fa6; }
  .swagger-ui .opblock.opblock-post .opblock-summary-method { background:#1f7a4d; }
  .swagger-ui .opblock.opblock-delete .opblock-summary-method { background:#a3392f; }
  .swagger-ui .btn.execute { background:var(--sk-accent); border-color:var(--sk-accent); }
  .swagger-ui .scheme-container { background:transparent; box-shadow:none;
    border-bottom:1px solid var(--sk-line); }
</style>
<script>
  window.addEventListener("DOMContentLoaded", function () {
    var head = document.createElement("div");
    head.className = "sk-head";
    head.innerHTML =
      '<img src="/static/logo.svg" alt="">' +
      '<h1>Sakshi<span>the witness \u00b7 VriddhiX read API</span></h1>';
    document.body.insertBefore(head, document.body.firstChild);
  });
</script>
""" 

#: The API is named separately from the platform. VriddhiX is the product;
#: Sākṣī -- "the witness" -- is the read surface, and the name is the design
#: brief: a witness testifies to what it observed and does not speculate about
#: what comes next. Every endpoint here reports a measurement already taken.
API_NAME = "Sakshi"
API_TAGLINE = "the witness \u00b7 VriddhiX read API"

TAGS_METADATA = [
    {"name": "market", "description":
     "Regime and breadth. What the market **is doing now**, measured from the "
     "universe -- never a forecast."},
    {"name": "sectors", "description":
     "Sector standing and rotation. Equal-weight, so a handful of index "
     "heavyweights cannot stand in for the group."},
    {"name": "stocks", "description":
     "One symbol: quote, causal features, relative strength, active pattern, "
     "chart geometry and market structure."},
    {"name": "scanners", "description":
     "What the last completed scan found. These read stored rows; no endpoint "
     "runs a detector while you wait."},
    {"name": "breakouts", "description":
     "The ledger. Failed breakouts are first-class rows with the same shape as "
     "successes, and no view drops them by default."},
    {"name": "research", "description":
     "Saved screens and backtests. Screens use the same condition evaluator as "
     "the backtester, so the two cannot disagree about what a rule means."},
    {"name": "product", "description": "Watchlists and alerts, scoped to the token."},
    {"name": "auth", "description":
     "Accounts. Passwords are scrypt-hashed with a per-account salt; a failed "
     "login never reveals whether the address exists."},
    {"name": "ai", "description":
     "Explanations built server-side from stored rows. The client cannot inject "
     "market facts, and every input is echoed for audit."},
    {"name": "ops", "description": "Health, scan history and data-quality findings."},
    {"name": "reference", "description": "Engine versions and static reference data."},
]

DESCRIPTION = """
Indian market intelligence and quantitative research.

**Every derived number carries provenance** -- `as_of`, `engine_version` and
`is_stale`. A client that cannot tell how old a number is will eventually
present a three-day-old score as live.

**Reads are precomputed.** No endpoint runs a scan or an engine. If the data
is not there, the endpoint says `SCAN_NOT_RUN` rather than computing it while
the user waits.

**Nothing is a recommendation.** Scores and grades are research
classifications of measured behaviour, not advice.
""".strip()


def create_app() -> FastAPI:
    app = FastAPI(
        title=API_NAME,
        version="1.0.0",
        summary="Read-only access to measured market structure on NSE equities.",
        description=DESCRIPTION,
        openapi_tags=TAGS_METADATA,
        license_info={"name": "MIT",
                      "url": "https://github.com/AnupamaJain/algo-backtesting-platform/blob/main/LICENSE"},
        contact={"name": "VriddhiX",
                 "url": "https://github.com/AnupamaJain/algo-backtesting-platform"},
        # Default docs route is disabled; a custom one below carries the
        # mark and the brand colours. Swagger's own header cannot be styled
        # through swagger_ui_parameters.
        docs_url=None,
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        swagger_ui_parameters={
            "defaultModelsExpandDepth": -1,   # schemas are noise on a read API
            "docExpansion": "none",
            "displayRequestDuration": True,
            "tryItOutEnabled": True,
        },
    )

    # Permissive by default for local development only. A deployment sets
    # VRIDDHIX_API_CORS_ORIGINS; credentials are never allowed with "*",
    # which is both a browser rule and the right default.
    import os

    origins = [
        o.strip()
        for o in os.getenv("VRIDDHIX_API_CORS_ORIGINS", "http://localhost:3000").split(",")
        if o.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials="*" not in origins,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)

    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    for router in (
        market.router, sectors.router, stocks.router, scanners.router,
        breakouts.router, research.router, product.router, ai.router,
        ops.router, auth.router, seo.router, pages.router,
    ):
        app.include_router(router)

    @app.get("/api/docs", include_in_schema=False)
    def docs() -> HTMLResponse:
        """Swagger UI, wearing the product's own mark and palette."""
        from fastapi.openapi.docs import get_swagger_ui_html

        page = get_swagger_ui_html(
            openapi_url=app.openapi_url,
            title=f"{API_NAME} — API reference",
            swagger_favicon_url="/static/logo.svg",
            swagger_ui_parameters=app.swagger_ui_parameters,
        )
        body = page.body.decode()
        body = body.replace("</head>", _DOCS_STYLE + "</head>")
        return HTMLResponse(body)

    @app.get("/api/v1/versions", tags=["reference"])
    def versions() -> dict:
        """Every engine version the API can attribute a row to.

        Exposed so a client can detect that stored rows and running code
        disagree, rather than discovering it through a number that changed for
        no visible reason.
        """
        return {
            name: value
            for name, value in vars(versioning).items()
            if name.isupper() and isinstance(value, str)
        }

    return app


app = create_app()
