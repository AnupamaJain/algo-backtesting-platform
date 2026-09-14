"""FastAPI application. docs/09.

Run locally:

    uvicorn vriddhix.api.main:app --reload

Nothing in this layer computes. Every endpoint projects rows the daily scan
already wrote, which is what keeps a read cheap, repeatable, and identical for
two users asking the same question on the same date.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from .. import versioning
from .errors import ApiError, api_error_handler, validation_error_handler
from .routers import ai, breakouts, market, ops, product, research, scanners, sectors, stocks

logger = logging.getLogger(__name__)

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
        title="VriddhiX API",
        version="1.0.0",
        description=DESCRIPTION,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
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

    for router in (
        market.router, sectors.router, stocks.router, scanners.router,
        breakouts.router, research.router, product.router, ai.router, ops.router,
    ):
        app.include_router(router)

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
