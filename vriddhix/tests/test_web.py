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
    # Seven bars in, seven bars reported. The figure is carried in the
    # count-up attribute, which is also what the animation lands on.
    assert 'data-count="7"' in body


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
    assert 'class="ribbon"' in body, "the regime chart did not render"
    # One <rect> per bucket, each carrying the regime it covers.
    assert "<rect" in body
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


# ---------------------------------------------------------------------------
# API identity and spec health
# ---------------------------------------------------------------------------


def test_no_endpoint_is_documented_under_two_tags(client):
    """A second tag makes Swagger render the same endpoint twice.

    /api/v1/market/indices carried both its router's tag and its own, so it
    appeared under `market` and again under `reference`.
    """
    spec = client.get("/api/openapi.json").json()
    doubled = [
        f"{method.upper()} {path}"
        for path, ops in spec["paths"].items()
        for method, op in ops.items()
        if len(op.get("tags", [])) > 1
    ]
    assert doubled == []


def test_operation_ids_are_unique(client):
    """Duplicates silently break every generated client."""
    import collections

    spec = client.get("/api/openapi.json").json()
    ids = collections.Counter(
        op["operationId"]
        for ops in spec["paths"].values()
        for op in ops.values()
        if "operationId" in op
    )
    assert [k for k, v in ids.items() if v > 1] == []


def test_every_tag_in_use_is_described(client):
    """An undescribed tag is a bare heading in the reference."""
    spec = client.get("/api/openapi.json").json()
    used = {t for ops in spec["paths"].values() for op in ops.values()
            for t in op.get("tags", [])}
    described = {t["name"] for t in spec.get("tags", [])}
    assert used - described == set()


def test_the_spec_carries_identity_and_licence(client):
    spec = client.get("/api/openapi.json").json()["info"]
    assert spec["title"] == "Tathya"
    assert spec["version"]
    assert spec.get("summary")
    assert spec.get("license", {}).get("name") == "MIT"


def test_the_docs_page_carries_the_mark(client):
    body = client.get("/api/docs").text
    assert "/static/logo.svg" in body
    assert "Tathya" in body


def test_the_logo_is_served_and_self_coloured(client):
    """It is used in <img> and as a favicon, where currentColor never
    resolves -- a currentColor-only mark renders black and disappears on a
    dark tab strip."""
    import re

    r = client.get("/static/logo.svg")
    assert r.status_code == 200
    assert "svg" in r.headers["content-type"]

    # Comments explain WHY currentColor is avoided, so strip them before
    # asserting on the markup itself.
    markup = re.sub(r"<!--.*?-->", "", r.text, flags=re.S)
    assert "currentColor" not in markup
    assert re.search(r'(fill|stop-color|stroke)="#[0-9a-fA-F]{3,6}"', markup), \
        "no explicit colour in the mark"


# ---------------------------------------------------------------------------
# Search-engine surface
# ---------------------------------------------------------------------------


def test_an_unconfigured_instance_refuses_crawling(client, monkeypatch):
    """A dev box must never invite indexing.

    Without VRIDDHIX_PUBLIC_URL there is no way to emit correct canonicals,
    and a staging instance that gets indexed tells Google production is the
    duplicate.
    """
    monkeypatch.delenv("VRIDDHIX_PUBLIC_URL", raising=False)
    body = client.get("/robots.txt").text
    assert "Disallow: /" in body
    assert "Allow:" not in body

    # ...and every page says so in its own head, not only in robots.txt.
    assert 'name="robots" content="noindex' in client.get("/").text


def test_a_public_instance_invites_crawling_and_names_its_sitemap(client, monkeypatch):
    monkeypatch.setenv("VRIDDHIX_PUBLIC_URL", "https://vriddhix.example")
    body = client.get("/robots.txt").text

    assert "Sitemap: https://vriddhix.example/sitemap.xml" in body
    # The API is machine surface; indexing it burns crawl budget on JSON.
    assert "Disallow: /api/" in body
    assert 'name="robots" content="noindex' not in client.get("/").text


def test_the_sitemap_is_valid_and_lists_only_indexable_pages(client, monkeypatch):
    import xml.dom.minidom as minidom

    monkeypatch.setenv("VRIDDHIX_PUBLIC_URL", "https://vriddhix.example")
    body = client.get("/sitemap.xml").text
    doc = minidom.parseString(body)

    locs = [n.firstChild.data for n in doc.getElementsByTagName("loc")]
    assert locs == [
        "https://vriddhix.example/",
        "https://vriddhix.example/learn",
        "https://vriddhix.example/vcp-breakout-failure-rate",
        "https://vriddhix.example/signup",
    ]
    # Account surfaces stay out.
    assert not any("/login" in u or "/api" in u for u in locs)


def test_every_page_declares_one_canonical_url(client, monkeypatch):
    monkeypatch.setenv("VRIDDHIX_PUBLIC_URL", "https://vriddhix.example")
    for path, expected in (
        ("/", "https://vriddhix.example/"),
        ("/learn", "https://vriddhix.example/learn"),
        ("/signup", "https://vriddhix.example/signup"),
    ):
        body = client.get(path).text
        assert f'<link rel="canonical" href="{expected}">' in body


def test_the_canonical_origin_never_leaks_a_dev_host(client, monkeypatch):
    """Publishing localhost canonicals from production would deindex the site."""
    monkeypatch.setenv("VRIDDHIX_PUBLIC_URL", "https://vriddhix.example")
    body = client.get("/").text
    assert "127.0.0.1" not in body
    assert "localhost" not in body


def test_pages_carry_link_preview_metadata(client):
    body = client.get("/").text
    for tag in ("og:title", "og:description", "og:url", "og:image",
                "twitter:card", "og:locale"):
        assert tag in body, f"missing {tag}"


def test_structured_data_claims_nothing_it_cannot_substantiate(client):
    """A schema block with an invented rating is the markup equivalent of a
    fabricated backtest, and it is penalised when noticed."""
    import json
    import re

    body = client.get("/").text
    block = re.search(
        r'<script type="application/ld\+json">(.*?)</script>', body, re.S
    ).group(1)
    data = json.loads(block)

    assert data["@type"] == "SoftwareApplication"
    assert data["offers"]["price"] == "0"
    for invented in ("aggregateRating", "review", "ratingValue"):
        assert invented not in json.dumps(data)


def test_the_learn_page_is_marked_up_as_an_article(client):
    import json
    import re

    body = client.get("/learn").text
    data = json.loads(re.search(
        r'<script type="application/ld\+json">(.*?)</script>', body, re.S).group(1))
    assert data["@type"] == "TechArticle"
    assert data["headline"]


def test_the_open_graph_image_exists_and_is_the_right_shape(client):
    r = client.get("/static/og.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    # 1200x630 is what every platform crops to; PNG header carries the size.
    width = int.from_bytes(r.content[16:20], "big")
    height = int.from_bytes(r.content[20:24], "big")
    assert (width, height) == (1200, 630)


def test_login_survives_a_failure_to_record_the_timestamp(client, session, monkeypatch):
    """A verified password must not be undone by bookkeeping.

    last_login_at is a nicety. When a backfill held the SQLite write lock,
    that UPDATE raised and took the whole login down with it -- a 500 on
    correct credentials.
    """
    from sqlalchemy.exc import OperationalError

    client.post(
        "/api/v1/auth/signup",
        json={"email": "locked@example.com", "password": "a long enough passphrase"},
    )

    real_flush = session.flush

    def flaky_flush(*args, **kwargs):
        # Fail only a flush that is actually writing last_login_at, not the
        # autoflush SQLAlchemy runs before the lookup query.
        stamping = any(
            getattr(obj, "last_login_at", None) is not None
            for obj in session.dirty
        )
        if stamping:
            raise OperationalError("UPDATE users", {}, Exception("database is locked"))
        return real_flush(*args, **kwargs)

    monkeypatch.setattr(session, "flush", flaky_flush)

    response = client.post(
        "/api/v1/auth/login",
        json={"email": "locked@example.com", "password": "a long enough passphrase"},
    )
    assert response.status_code == 200
    assert response.json()["user"]["email"] == "locked@example.com"


def test_sqlite_waits_for_a_busy_lock_rather_than_failing(migrated_db):
    """SQLite's default is to fail a contended write immediately."""
    from sqlalchemy import create_engine, event, text

    from vriddhix.db.base import _configure_sqlite

    engine = create_engine(migrated_db, future=True)
    event.listen(engine, "connect", _configure_sqlite)
    with engine.connect() as conn:
        timeout = conn.execute(text("PRAGMA busy_timeout")).scalar()
        journal = conn.execute(text("PRAGMA journal_mode")).scalar()
    engine.dispose()

    assert timeout >= 5000, "a contended write would fail instantly"
    assert journal.lower() == "wal"


# ---------------------------------------------------------------------------
# The evidence page
# ---------------------------------------------------------------------------


def test_the_evidence_page_renders_with_no_data(client, seeded):
    """A brand-new instance must still serve the page, without a rate."""
    r = client.get("/vcp-breakout-failure-rate")
    assert r.status_code == 200
    assert "Internal Server Error" not in r.text


def test_no_failure_rate_is_published_over_a_tiny_sample(client, session, seeded):
    """A percentage over a handful of rows reads as a finding and is not one."""
    from datetime import date

    from vriddhix.db.models import Breakout, Stock

    stock = session.query(Stock).first()
    for i in range(10):
        session.add(Breakout(
            stock_id=stock.id, breakout_date=date(2025, 1, 1),
            breakout_price=100, pivot_price=99,
            status="CONFIRMED" if i else "FAILED",
            engine_version="VCP_ENGINE_V1.0",
        ))
    session.flush()

    body = client.get("/vcp-breakout-failure-rate").text
    # Ten rows is below the floor, so no headline percentage is shown.
    assert "of breakouts closed back below" not in body


def test_the_evidence_page_states_its_survivorship_bias(client):
    """A failure rate without its caveat is a slogan."""
    body = client.get("/vcp-breakout-failure-rate").text.lower()
    assert "backfilled from today" in body
    assert "flatters" in body


def test_the_evidence_page_carries_faq_structured_data(client):
    import json
    import re

    body = client.get("/vcp-breakout-failure-rate").text
    data = json.loads(re.search(
        r'<script type="application/ld\+json">(.*?)</script>', body, re.S).group(1))
    questions = data.get("mainEntity", [])
    assert len(questions) >= 3
    for q in questions:
        assert q["@type"] == "Question"
        assert q["acceptedAnswer"]["text"]


def test_the_evidence_page_is_crawlable_from_the_homepage(client, monkeypatch):
    """A page reachable only from the sitemap is a page Google deprioritises."""
    monkeypatch.setenv("VRIDDHIX_PUBLIC_URL", "https://tathya.example")
    assert "/vcp-breakout-failure-rate" in client.get("/").text
    assert "/vcp-breakout-failure-rate" in client.get("/sitemap.xml").text
    assert "/vcp-breakout-failure-rate" in client.get("/robots.txt").text


# ---------------------------------------------------------------------------
# A page that moves, and the terms it carries
# ---------------------------------------------------------------------------


def test_acronym_sectors_are_not_title_cased(client, scanned=None):
    """str.title() turns IT into "It" and FMCG into "Fmcg", which reads as a
    typo to precisely the audience that would notice."""
    from vriddhix.api.routers.pages import sector_label

    assert sector_label("IT") == "IT"
    assert sector_label("FMCG") == "FMCG"
    assert sector_label("CONSUMER_DURABLES") == "Consumer Durables"
    assert sector_label("CAPGOODS") == "Capgoods"
    assert sector_label(None) == "Unclassified"


def test_the_landing_page_polls_for_freshness(client):
    """A tab left open overnight must not quietly show yesterday's regime."""
    body = client.get("/").text
    assert "/api/v1/market" in body, "nothing re-reads the regime"
    assert "setInterval" in body
    # Staleness is surfaced, never hidden -- the same rule the API follows.
    assert "is_stale" in body


def test_motion_is_skipped_when_the_visitor_asks_for_less(client):
    body = client.get("/")
    assert "prefers-reduced-motion" in body.text
    css = client.get("/static/app.css").text
    assert "prefers-reduced-motion" in css


def test_the_landing_faq_is_marked_up_for_search(client):
    import json
    import re

    body = client.get("/").text
    data = json.loads(re.search(
        r'<script type="application/ld\+json">(.*?)</script>', body, re.S).group(1))
    questions = data.get("mainEntity", [])
    assert len(questions) >= 5
    assert all(q["acceptedAnswer"]["text"] for q in questions)


def test_the_page_names_what_it_does_in_the_words_people_search(client):
    """Terminology carried by real answers, not stuffed into a keyword list.

    If these disappear the page has stopped describing its own features, not
    merely lost some SEO.
    """
    body = client.get("/").text.lower()
    for term in (
        "nse", "stock screener", "volatility contraction", "vcp",
        "market structure", "fair value gap", "relative strength",
        "sector rotation", "backtest", "breakout",
    ):
        assert term in body, f"the page never mentions {term!r}"


def test_the_page_still_refuses_to_oversell(client):
    """Adding search terms must not smuggle in promises."""
    body = client.get("/").text.lower()
    for phrase in ("guaranteed", "risk-free", "multibagger", "assured return",
                   "best stock to buy", "sure shot"):
        assert phrase not in body
    assert "not investment advice" in body
