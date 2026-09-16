"""Password hashing and the signup / login endpoints.

Passwords are hashed with **scrypt** (stdlib, memory-hard), never stored and
never logged. Each password gets its own random salt, so two users who pick
the same password produce different digests and one cracked hash reveals
nothing about the other.

Three rules this module exists to keep:

* **A failed login never says which half was wrong.** "No such account" and
  "wrong password" are the same response, because the difference tells an
  attacker which addresses are worth attacking.
* **A NULL password hash never authenticates.** The dev identity and any
  externally-authenticated user have no local password, and treating a
  missing hash as "anything matches" is the classic way that becomes a
  bypass.
* **Verification is constant-time.** A comparison that returns early leaks
  the digest one byte at a time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Body
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from ..db.models import User
from . import errors
from .deps import SessionDep, UserDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

# scrypt cost. n=2^14 with r=8 is the classic interactive setting: hard
# enough to make offline cracking expensive, fast enough that a login is not
# a denial-of-service vector against our own server.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32

#: Deliberately permissive. Address syntax is nearly impossible to validate
#: correctly, and over-strict patterns reject real addresses; the real check
#: is a confirmation mail, not a regex.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

MIN_PASSWORD_LENGTH = 10

#: Failed logins allowed per address before the endpoint refuses, and the
#: window they are counted over. Without a limit, scrypt only raises the cost
#: of each guess -- it does not stop an attacker making millions of them.
MAX_FAILED_LOGINS = 6
LOCKOUT_WINDOW_SECONDS = 900

#: In-process, therefore per-worker. Deliberately simple: a real deployment
#: puts this in Redis so the limit is shared, and this module is where that
#: swap happens. A per-worker limit still raises the cost of a brute force by
#: orders of magnitude, which is the point.
_failures: dict[str, list[float]] = {}

#: Rejected outright regardless of length. A short list of the passwords that
#: actually appear in breach corpora catches most of what users would pick.
_OBVIOUS = {
    "password", "password1", "password123", "12345678", "123456789",
    "1234567890", "qwertyuiop", "letmein123", "iloveyou1", "admin12345",
    "welcome123", "trading123", "vriddhix123",
}


def hash_password(password: str) -> str:
    """``scrypt$n$r$p$salt$hash``, with a fresh random salt."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN,
    )
    return "$".join((
        "scrypt", str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P),
        base64.b64encode(salt).decode(), base64.b64encode(digest).decode(),
    ))


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check against a stored digest.

    Returns False for a missing or unparseable hash rather than raising: a
    user row with no local password simply cannot be authenticated this way.
    """
    if not stored:
        return False
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(hash_b64)
        computed = hashlib.scrypt(
            password.encode("utf-8"), salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p), dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(computed, expected)


def password_problem(password: str, email: str = "") -> str | None:
    """Why this password is unacceptable, or None.

    Length first, because it is the property that actually matters; the
    obvious-password list catches the rest. No composition rules: forcing a
    symbol produces "Password1!" and teaches nothing.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if password.lower() in _OBVIOUS:
        return "That password appears in breach lists. Choose another."
    local = email.split("@")[0].lower() if email else ""
    if local and len(local) > 3 and local in password.lower():
        return "Password must not contain your email address."
    return None


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def issue_token(user: User, *, hours: int = 12) -> str:
    """An HS256 JWT for this user.

    Signed with VRIDDHIX_API_JWT_SECRET. Without that configured the server
    refuses to issue one rather than falling back to an unsigned token that
    deps.py would then accept in development and someone would ship.
    """
    secret = os.getenv("VRIDDHIX_API_JWT_SECRET")
    if not secret:
        raise errors.ApiError(
            "INVALID_PARAMS", 500,
            "VRIDDHIX_API_JWT_SECRET is not configured; cannot issue a token.",
        )

    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    now = datetime.now(timezone.utc)
    payload = _b64(json.dumps({
        "sub": user.email,
        "email": user.email,
        "name": user.display_name,
        "tier": user.tier,
        "admin": user.is_admin,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=hours)).timestamp()),
    }).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(signature)}"


def _profile(user: User) -> dict:
    """What a client may know about its own account. No hash, ever."""
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "tier": user.tier,
        "is_admin": user.is_admin,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/signup", status_code=201)
def signup(session: SessionDep, payload: Annotated[dict, Body()]) -> dict:
    """Create an account."""
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    display_name = (payload.get("display_name") or "").strip()

    if not _EMAIL.match(email):
        raise errors.invalid_params("Enter a valid email address.", field="email")
    problem = password_problem(password, email)
    if problem:
        raise errors.invalid_params(problem, field="password")
    if not display_name:
        display_name = email.split("@")[0]

    existing = session.scalar(
        select(User).where(func.lower(User.email) == email)
    )
    if existing is not None:
        # 409 rather than a generic error: the address is already visible to
        # whoever typed it, so there is nothing to protect here, and a vague
        # message would just make people retry.
        raise errors.ApiError(
            "ALREADY_EXISTS", 409, "An account with that email already exists.",
            field="email",
        )

    user = User(
        email=email,
        display_name=display_name[:128],
        password_hash=hash_password(password),
        tier="FREE",
    )
    session.add(user)
    session.flush()
    logger.info("account created for user id %s", user.id)

    return {"user": _profile(user), "token": _maybe_token(user)}


def _recent_failures(email: str) -> int:
    """Failed attempts for this address inside the window, pruned as we go."""
    import time

    cutoff = time.monotonic() - LOCKOUT_WINDOW_SECONDS
    kept = [t for t in _failures.get(email, []) if t > cutoff]
    if kept:
        _failures[email] = kept
    else:
        _failures.pop(email, None)
    return len(kept)


def _record_failure(email: str) -> None:
    import time

    _failures.setdefault(email, []).append(time.monotonic())


def reset_failures(email: str | None = None) -> None:
    """Clear the throttle. Called on success, and by tests."""
    if email is None:
        _failures.clear()
    else:
        _failures.pop(email, None)


@router.post("/login")
def login(session: SessionDep, payload: Annotated[dict, Body()]) -> dict:
    """Exchange email and password for a token."""
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""

    if _recent_failures(email) >= MAX_FAILED_LOGINS:
        # Throttled on the address, not the password: scrypt makes each guess
        # expensive, but only a limit makes making millions of them
        # impractical.
        raise errors.rate_limited(
            "Too many failed attempts. Try again in a few minutes.",
            retry_after_seconds=LOCKOUT_WINDOW_SECONDS,
        )

    user = session.scalar(select(User).where(func.lower(User.email) == email))
    ok = verify_password(password, user.password_hash if user else None)
    if not ok:
        _record_failure(email)
        # One message for both failures. Saying "no such account" tells an
        # attacker which addresses are worth attacking.
        raise errors.ApiError(
            "NOT_FOUND", 401, "Email or password is incorrect."
        )

    reset_failures(email)

    # Built BEFORE the stamp. A failed flush leaves the session unusable until
    # it is rolled back, and a rollback expires `user` -- so reading the
    # profile afterwards would raise on a detached instance and turn a
    # recovered error back into a 500.
    profile = _profile(user)
    token = _maybe_token(user)
    user_id = user.id

    # The password is already verified; this stamp is bookkeeping. If the
    # write cannot land -- a backfill holding the write lock, a read-only
    # replica -- the user is still authenticated, and failing their login over
    # it would be absurd.
    try:
        user.last_login_at = datetime.now(timezone.utc)
        session.flush()
    except SQLAlchemyError:
        session.rollback()
        logger.warning("could not record last_login_at for user %s", user_id)

    return {"user": profile, "token": token}


@router.get("/me")
def me(user: UserDep) -> dict:
    return {"user": _profile(user)}


def _maybe_token(user: User) -> str | None:
    """A token when signing is configured, None when it is not.

    Local development runs without a secret; the account is still created and
    the API still works, because deps.py falls back to the dev identity
    there. Returning None says plainly that no token was issued rather than
    handing back something unsigned.
    """
    try:
        return issue_token(user)
    except errors.ApiError:
        logger.warning(
            "account created but no token issued: VRIDDHIX_API_JWT_SECRET unset"
        )
        return None
