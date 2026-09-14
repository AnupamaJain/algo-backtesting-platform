"""Request dependencies: session, config, provenance, auth, pagination.

Two rules from docs/09 live here rather than in each router, because a rule
enforced per-router is a rule that will eventually be forgotten in one of
them:

* **Provenance travels with the numbers.** ``Provenance.envelope()`` supplies
  ``as_of`` / ``engine_version`` / ``computed_at`` / ``is_stale`` for every
  derived payload. A client that cannot tell how old a number is will
  eventually present a three-day-old score as live.
* **User-scoped rows are filtered by the token**, never by a query parameter.
  A ``user_id`` in the querystring is an authorisation bug with a convenient
  interface.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Iterator

from fastapi import Depends, Header, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Config, get_config
from ..db.base import get_engine
from ..db.models import ScanRun, User
from . import errors

#: Dev-mode identity. Real deployments set VRIDDHIX_API_REQUIRE_AUTH=1 and
#: put a verified JWT subject here instead; see resolve_user.
DEV_USER_EMAIL = "local@vriddhix.invalid"


def get_session() -> Iterator[Session]:
    """One session per request, rolled back on failure.

    Read endpoints do not commit. A GET that writes is a GET that cannot be
    retried safely.
    """
    session = Session(bind=get_engine(), expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def config() -> Config:
    return get_config()


SessionDep = Annotated[Session, Depends(get_session)]
ConfigDep = Annotated[Config, Depends(config)]


# ---------------------------------------------------------------------------
# Provenance and staleness
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Provenance:
    """How old the data is, and what produced it."""

    as_of: date | None
    computed_at: datetime | None
    is_stale: bool
    stale_after_hours: float
    age_hours: float | None
    scan_run_id: int | None

    def envelope(self, **overrides: Any) -> dict:
        """Provenance fields for a derived payload.

        ``as_of`` defaults to the latest completed scan; an endpoint serving a
        historical date passes its own. ``latest_scan_date`` is always the
        newest scan, so a client can tell a historical query apart from a
        stale one -- they are different problems and only one needs fixing.
        """
        payload: dict[str, Any] = {
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "latest_scan_date": self.as_of.isoformat() if self.as_of else None,
            "computed_at": self.computed_at.isoformat() if self.computed_at else None,
            "is_stale": self.is_stale,
            "age_hours": round(self.age_hours, 2) if self.age_hours is not None else None,
            "scan_run_id": self.scan_run_id,
        }
        payload.update(overrides)
        return payload

    def require_scan(self, as_of: date | None = None) -> None:
        """Refuse rather than return an empty body that looks like 'no matches'."""
        if self.as_of is None:
            raise errors.scan_not_run(as_of)


def _last_scan(session: Session) -> ScanRun | None:
    return session.scalar(
        select(ScanRun)
        .where(ScanRun.status == "COMPLETED")
        .order_by(ScanRun.scan_date.desc(), ScanRun.id.desc())
        .limit(1)
    )


def provenance(session: SessionDep, cfg: ConfigDep) -> Provenance:
    """Provenance of the most recent completed scan.

    Staleness is reported, never raised: stale data plus its age is more
    useful than an error, as long as the age is impossible to miss.
    """
    stale_after = float(cfg.get("api.stale_after_hours", 20))
    run = _last_scan(session)
    if run is None:
        return Provenance(None, None, True, stale_after, None, None)

    computed = run.finished_at or run.started_at
    age_hours = None
    if computed is not None:
        if computed.tzinfo is None:
            computed = computed.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - computed).total_seconds() / 3600.0

    return Provenance(
        as_of=run.scan_date,
        computed_at=computed,
        is_stale=age_hours is None or age_hours > stale_after,
        stale_after_hours=stale_after,
        age_hours=age_hours,
        scan_run_id=run.id,
    )


ProvenanceDep = Annotated[Provenance, Depends(provenance)]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _decode_unverified(token: str) -> dict:
    """Read a JWT payload WITHOUT verifying the signature.

    This exists so local development works without a key server. It is gated
    behind VRIDDHIX_API_REQUIRE_AUTH being unset, and it is deliberately not
    called when auth is required -- an unverified token is not an identity,
    and treating it as one in production would be an authentication bypass.
    """
    try:
        _, payload, _ = token.split(".")
        padded = payload + "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, binascii.Error, json.JSONDecodeError):
        return {}


def _verify(token: str, secret: str) -> dict:
    """Verify an HS256 JWT and return its claims."""
    import hashlib
    import hmac

    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
    except ValueError as exc:
        raise errors.ApiError("NOT_FOUND", 401, "Malformed token.") from exc

    signing_input = f"{header_b64}.{payload_b64}".encode()
    expected = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    given = base64.urlsafe_b64decode(signature_b64 + "=" * (-len(signature_b64) % 4))
    # Constant-time: a timing-variable comparison leaks the signature one
    # byte at a time.
    if not hmac.compare_digest(expected, given):
        raise errors.ApiError("NOT_FOUND", 401, "Invalid token signature.")

    claims = _decode_unverified(token)
    exp = claims.get("exp")
    if exp is not None and datetime.now(timezone.utc).timestamp() > float(exp):
        raise errors.ApiError("NOT_FOUND", 401, "Token expired.")
    return claims


def resolve_user(
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    """The authenticated user, or the dev user when auth is not required.

    Every user-scoped query filters on the id this returns. No endpoint
    accepts a user id as a parameter.
    """
    require_auth = os.getenv("VRIDDHIX_API_REQUIRE_AUTH", "").lower() in ("1", "true", "yes")
    secret = os.getenv("VRIDDHIX_API_JWT_SECRET")

    if require_auth and not secret:
        # Fail closed. Starting with auth required and no key configured
        # must not silently degrade to open access.
        raise errors.ApiError(
            "NOT_FOUND", 500,
            "Auth is required but VRIDDHIX_API_JWT_SECRET is not configured.",
        )

    email = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        claims = _verify(token, secret) if secret else _decode_unverified(token)
        email = claims.get("email") or claims.get("sub")
    elif require_auth:
        raise errors.ApiError("NOT_FOUND", 401, "Authorization header required.")

    email = email or DEV_USER_EMAIL
    user = session.scalar(select(User).where(User.email == email))
    if user is None:
        if require_auth:
            raise errors.ApiError("NOT_FOUND", 401, "Unknown subject.")
        user = User(email=email, display_name="Local Development")
        session.add(user)
        session.flush()
    return user


UserDep = Annotated[User, Depends(resolve_user)]


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Page:
    limit: int
    cursor: dict | None

    def encode(self, **values) -> str:
        raw = json.dumps(values, sort_keys=True, default=str).encode()
        return base64.urlsafe_b64encode(raw).decode()


def paging(
    cfg: ConfigDep,
    limit: Annotated[int | None, Query(ge=1)] = None,
    cursor: Annotated[str | None, Query()] = None,
) -> Page:
    """Keyset pagination. Offsets are not used.

    Scanner results reorder as scores update, so page 2 of an offset-paged
    scan can repeat or skip rows that page 1 already showed.
    """
    default = int(cfg.get("api.default_page_size", 50))
    maximum = int(cfg.get("api.max_page_size", 500))
    size = min(limit or default, maximum)

    decoded = None
    if cursor:
        try:
            decoded = json.loads(
                base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            )
        except (ValueError, binascii.Error, json.JSONDecodeError) as exc:
            raise errors.invalid_params("Cursor is not decodable.") from exc
        if not isinstance(decoded, dict):
            raise errors.invalid_params("Cursor does not describe a position.")

    return Page(limit=size, cursor=decoded)


PageDep = Annotated[Page, Depends(paging)]


def parse_date(value: str | None, field: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise errors.invalid_params(f"{field} must be an ISO date (YYYY-MM-DD).") from exc


def window(
    from_: str | None, to: str | None, *, default_days: int = 90
) -> tuple[date, date]:
    """A closed date range, defaulted rather than unbounded.

    An unbounded default would make the first call from a new client the most
    expensive query the database ever serves.
    """
    end = parse_date(to, "to") or date.today()
    start = parse_date(from_, "from") or (end - timedelta(days=default_days))
    if start > end:
        raise errors.invalid_params("'from' is after 'to'.")
    return start, end
