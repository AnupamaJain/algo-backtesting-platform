"""Auth and the public pages.

The security properties here are the ones that are cheap to get wrong and
expensive to discover later: a password that is stored recoverably, a login
that reveals which accounts exist, a NULL credential that authenticates, or a
page that promises a return.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def client(session):
    from fastapi.testclient import TestClient

    from vriddhix.api.deps import get_session
    from vriddhix.api.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Password handling
# ---------------------------------------------------------------------------


def test_a_password_is_never_recoverable_from_what_is_stored():
    from vriddhix.api.auth import hash_password

    secret = "correct horse battery staple"
    stored = hash_password(secret)

    assert secret not in stored
    assert stored.startswith("scrypt$")
    # A bare digest of the password must not appear either: that would be
    # crackable from a rainbow table the moment the database leaked.
    import hashlib

    assert hashlib.sha256(secret.encode()).hexdigest() not in stored


def test_the_same_password_hashes_differently_every_time():
    """Per-password salt: one cracked hash must reveal nothing about another."""
    from vriddhix.api.auth import hash_password, verify_password

    a = hash_password("the same passphrase")
    b = hash_password("the same passphrase")
    assert a != b
    assert verify_password("the same passphrase", a)
    assert verify_password("the same passphrase", b)


def test_verification_rejects_the_wrong_password():
    from vriddhix.api.auth import hash_password, verify_password

    stored = hash_password("a perfectly fine passphrase")
    assert not verify_password("a perfectly fine passphras", stored)
    assert not verify_password("", stored)


@pytest.mark.parametrize("stored", [None, "", "garbage", "scrypt$bad", "md5$x$y$z$w"])
def test_a_missing_or_unparseable_hash_never_authenticates(stored):
    """The classic nullable-credential bypass, closed explicitly."""
    from vriddhix.api.auth import verify_password

    assert not verify_password("anything at all", stored)
    assert not verify_password("", stored)


def test_weak_passwords_are_refused():
    from vriddhix.api.auth import MIN_PASSWORD_LENGTH, password_problem

    assert password_problem("short") is not None
    assert password_problem("password123") is not None
    assert password_problem("x" * MIN_PASSWORD_LENGTH) is None
    # A password containing the account's own address is refused.
    assert password_problem("hemanshu-is-great", "hemanshu@example.com") is not None


# ---------------------------------------------------------------------------
# Signup and login
# ---------------------------------------------------------------------------


def test_signup_creates_an_account_and_never_returns_the_hash(client):
    response = client.post(
        "/api/v1/auth/signup",
        json={"email": "Aarti@Example.COM", "password": "a long enough passphrase",
              "display_name": "Aarti"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["user"]["email"] == "aarti@example.com"   # normalised
    assert body["user"]["tier"] == "FREE"
    assert "password" not in str(body)
    assert "password_hash" not in str(body)


def test_signup_rejects_a_bad_address_and_a_weak_password(client):
    bad = client.post(
        "/api/v1/auth/signup", json={"email": "not-an-email", "password": "x" * 12}
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["detail"]["field"] == "email"

    weak = client.post(
        "/api/v1/auth/signup", json={"email": "a@b.co", "password": "short"}
    )
    assert weak.status_code == 422
    assert weak.json()["error"]["detail"]["field"] == "password"


def test_a_duplicate_signup_is_refused(client):
    payload = {"email": "dup@example.com", "password": "a long enough passphrase"}
    assert client.post("/api/v1/auth/signup", json=payload).status_code == 201
    second = client.post("/api/v1/auth/signup", json=payload)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "ALREADY_EXISTS"


def test_login_succeeds_with_the_right_password(client):
    client.post(
        "/api/v1/auth/signup",
        json={"email": "ok@example.com", "password": "a long enough passphrase"},
    )
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "ok@example.com", "password": "a long enough passphrase"},
    )
    assert response.status_code == 200
    assert response.json()["user"]["email"] == "ok@example.com"


def test_login_gives_one_message_whether_or_not_the_account_exists(client):
    """Distinguishing them tells an attacker which addresses to attack."""
    client.post(
        "/api/v1/auth/signup",
        json={"email": "real@example.com", "password": "a long enough passphrase"},
    )
    wrong_password = client.post(
        "/api/v1/auth/login",
        json={"email": "real@example.com", "password": "not the passphrase"},
    )
    no_account = client.post(
        "/api/v1/auth/login",
        json={"email": "ghost@example.com", "password": "not the passphrase"},
    )

    assert wrong_password.status_code == no_account.status_code == 401
    assert wrong_password.json() == no_account.json()


def test_a_user_row_without_a_password_cannot_log_in(client, session):
    """Covers the externally-authenticated and dev identities."""
    from vriddhix.db.models import User

    session.add(User(email="nopass@example.com", display_name="No Password"))
    session.flush()

    response = client.post(
        "/api/v1/auth/login",
        json={"email": "nopass@example.com", "password": ""},
    )
    assert response.status_code == 401


def test_a_token_is_signed_and_verifiable(monkeypatch, session):
    from vriddhix.api.auth import issue_token
    from vriddhix.api.deps import _verify
    from vriddhix.db.models import User

    monkeypatch.setenv("VRIDDHIX_API_JWT_SECRET", "test-signing-secret")
    user = User(email="signed@example.com", display_name="Signed")
    session.add(user)
    session.flush()

    token = issue_token(user)
    claims = _verify(token, "test-signing-secret")
    assert claims["email"] == "signed@example.com"
    assert claims["exp"] > claims["iat"]


def test_a_token_signed_with_another_key_is_rejected(monkeypatch, session):
    from vriddhix.api import errors
    from vriddhix.api.auth import issue_token
    from vriddhix.api.deps import _verify
    from vriddhix.db.models import User

    monkeypatch.setenv("VRIDDHIX_API_JWT_SECRET", "the-real-secret")
    user = User(email="x@example.com", display_name="X")
    session.add(user)
    session.flush()
    token = issue_token(user)

    with pytest.raises(errors.ApiError):
        _verify(token, "an-attackers-secret")


def test_signing_without_a_configured_secret_refuses_rather_than_downgrading(
    monkeypatch, session
):
    """An unsigned token that deps.py would accept in dev is how a bypass ships."""
    from vriddhix.api import errors
    from vriddhix.api.auth import issue_token
    from vriddhix.db.models import User

    monkeypatch.delenv("VRIDDHIX_API_JWT_SECRET", raising=False)
    user = User(email="y@example.com", display_name="Y")
    session.add(user)
    session.flush()

    with pytest.raises(errors.ApiError):
        issue_token(user)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/signup", "/login", "/learn"])
def test_every_page_renders(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "VriddhiX" in response.text


def test_the_landing_page_renders_without_any_scan_data(client):
    """A brand-new deployment must still have a homepage."""
    body = client.get("/").text
    assert "VriddhiX" in body
    assert "Internal Server Error" not in body


@pytest.mark.parametrize("path", ["/", "/signup", "/learn"])
def test_no_page_promises_a_return(client, path):
    """The product rule, asserted rather than trusted to review.

    A landing page that oversells is the first thing that makes the
    measurements underneath it untrustworthy.
    """
    text = client.get(path).text.lower()
    for phrase in (
        "guaranteed", "risk-free", "sure shot", "multibagger",
        "double your money", "assured return", "profit guarantee",
    ):
        assert phrase not in text, f"{path} contains {phrase!r}"


@pytest.mark.parametrize("path", ["/", "/signup", "/learn"])
def test_every_page_carries_the_not_advice_disclaimer(client, path):
    text = client.get(path).text
    assert "not investment advice" in text.lower()


def test_the_landing_page_reports_real_coverage(client, session):
    """Figures are read from the database, never written into the copy.

    A marketing page quoting a number that has drifted from the data beneath
    it is the smallest possible version of the dishonesty this whole system
    is built to avoid.
    """
    from datetime import date

    from vriddhix.db.models import OhlcvDaily, Stock

    stock = Stock(symbol="RELI", name="Reliance", exchange="NSE")
    session.add(stock)
    session.flush()
    for day in range(1, 8):
        session.add(
            OhlcvDaily(stock_id=stock.id, date=date(2024, 1, day), open=1, high=2,
                       low=1, close=2, volume=10, provider="test")
        )
    session.flush()

    body = client.get("/").text
    # Seven bars in, seven bars reported.
    assert ">7</b> bars" in body or ">7</b>\n" in body or "7</b> bars" in body


def test_the_landing_chart_is_drawn_from_stored_regimes(client, session):
    """The hero chart is engine output, not artwork."""
    from datetime import date

    from vriddhix.db.models import MarketRegimeRow

    for day, score in enumerate([20.0, 45.0, 80.0], start=1):
        session.add(
            MarketRegimeRow(
                date=date(2025, 6, day),
                regime="NEUTRAL", regime_score=score,
                engine_version="REGIME_ENGINE_V1.0",
            )
        )
    session.flush()

    body = client.get("/").text
    assert "ribbon-line" in body, "the regime chart did not render"
    assert "2025-06-01" in body and "2025-06-03" in body


def test_the_header_marks_links_that_may_collapse_on_a_phone(client):
    """The nav used to run past a 390px viewport and take the page into a
    horizontal scroll."""
    body = client.get("/").text
    assert 'class="secondary' in body
    # The account CTA is the one link that must survive the collapse.
    assert 'href="/signup"' in body


def test_the_static_stylesheet_is_served(client):
    response = client.get("/static/app.css")
    assert response.status_code == 200
    assert "--accent" in response.text


def test_repeated_failed_logins_are_throttled(client):
    """scrypt raises the cost of each guess; only a limit stops millions."""
    from vriddhix.api.auth import MAX_FAILED_LOGINS, reset_failures

    reset_failures()
    client.post(
        "/api/v1/auth/signup",
        json={"email": "target@example.com", "password": "a long enough passphrase"},
    )

    wrong = {"email": "target@example.com", "password": "wrong guess here"}
    for _ in range(MAX_FAILED_LOGINS):
        assert client.post("/api/v1/auth/login", json=wrong).status_code == 401

    blocked = client.post("/api/v1/auth/login", json=wrong)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "RATE_LIMITED"

    # Even the correct password is refused while the throttle holds: a limit
    # that the right password bypasses is not a limit.
    right = {"email": "target@example.com", "password": "a long enough passphrase"}
    assert client.post("/api/v1/auth/login", json=right).status_code == 429
    reset_failures()


def test_the_throttle_counts_unknown_addresses_too(client):
    """Otherwise an attacker enumerates by watching which addresses throttle."""
    from vriddhix.api.auth import MAX_FAILED_LOGINS, reset_failures

    reset_failures()
    guess = {"email": "ghost@example.com", "password": "some guess entirely"}
    for _ in range(MAX_FAILED_LOGINS):
        assert client.post("/api/v1/auth/login", json=guess).status_code == 401
    assert client.post("/api/v1/auth/login", json=guess).status_code == 429
    reset_failures()


def test_a_successful_login_clears_the_throttle(client):
    from vriddhix.api.auth import reset_failures

    reset_failures()
    client.post(
        "/api/v1/auth/signup",
        json={"email": "clears@example.com", "password": "a long enough passphrase"},
    )
    client.post(
        "/api/v1/auth/login",
        json={"email": "clears@example.com", "password": "wrong one"},
    )
    ok = client.post(
        "/api/v1/auth/login",
        json={"email": "clears@example.com", "password": "a long enough passphrase"},
    )
    assert ok.status_code == 200

    from vriddhix.api.auth import _recent_failures

    assert _recent_failures("clears@example.com") == 0
    reset_failures()
