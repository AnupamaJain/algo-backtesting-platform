"""Typed API errors. docs/09 section 1.

Every failure the client can act on differently gets its own code. The
distinction that matters most is `SCAN_NOT_RUN` (503) versus an empty result
list (200): "we have no data for that date" and "no stock matched" look
identical to a UI that only sees `[]`, and the first is an operational
problem while the second is a normal answer.
"""

from __future__ import annotations

from fastapi import HTTPException
from fastapi.responses import JSONResponse


class ApiError(HTTPException):
    """An error carrying a stable machine-readable code."""

    def __init__(self, code: str, http_status: int, message: str, **detail) -> None:
        # Named http_status, not status: **detail carries arbitrary keys from
        # callers, and a detail field called "status" would collide with the
        # signature and fail at the moment the error is being reported.
        super().__init__(status_code=http_status, detail=message)
        self.code = code
        self.message = message
        self.extra = detail

    def payload(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "detail": self.extra or {},
            }
        }


def not_found(what: str, **detail) -> ApiError:
    return ApiError("NOT_FOUND", 404, f"{what} not found", **detail)


def scan_not_run(as_of=None, **detail) -> ApiError:
    return ApiError(
        "SCAN_NOT_RUN",
        503,
        "No completed scan is available for the requested date."
        if as_of else "No completed scan has been run.",
        as_of=str(as_of) if as_of else None,
        **detail,
    )


def insufficient_coverage(message: str, **detail) -> ApiError:
    return ApiError("INSUFFICIENT_COVERAGE", 503, message, **detail)


def invalid_params(message: str, **detail) -> ApiError:
    return ApiError("INVALID_PARAMS", 422, message, **detail)


def rate_limited(message: str = "Quota exceeded.", **detail) -> ApiError:
    return ApiError("RATE_LIMITED", 429, message, **detail)


def backtest_running(run_id: int, **detail) -> ApiError:
    return ApiError(
        "BACKTEST_RUNNING", 202, "Backtest still running.", run_id=run_id, **detail
    )


async def api_error_handler(_request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.payload())


async def validation_error_handler(_request, exc) -> JSONResponse:
    """FastAPI's own 422 reshaped into the documented envelope.

    Without this, parameter validation failures would be the one error class
    with a different shape, and a client would need two parsers.
    """
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "INVALID_PARAMS",
                "message": "Request parameters failed validation.",
                "detail": {"errors": _jsonable_errors(exc)},
            }
        },
    )


def _jsonable_errors(exc) -> list[dict]:
    """Strip the non-serialisable ``ctx`` FastAPI attaches to some errors."""
    out = []
    for error in getattr(exc, "errors", lambda: [])():
        out.append(
            {
                "loc": [str(part) for part in error.get("loc", ())],
                "msg": error.get("msg"),
                "type": error.get("type"),
            }
        )
    return out
