"""The terminal behind the gateway, under /terminal.

Direct requests carry no prefix and behave as before; forwarded ones must
generate every link under the prefix, or each one points at the gateway's
root where nothing of ours exists.
"""

import pytest


@pytest.fixture
def client():
    import flask_app

    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as c:
        yield c


def test_direct_requests_are_unprefixed(client):
    import flask_app

    with flask_app.app.test_request_context("/"):
        from flask import url_for

        assert url_for("home_page") == "/home"


def test_the_forwarded_prefix_reaches_url_for(client):
    import flask_app

    with flask_app.app.test_request_context("/", headers={"X-Forwarded-Prefix": "/terminal"}):
        pass  # test_request_context bypasses WSGI middleware; exercise it directly

    environ = {"REQUEST_METHOD": "GET", "PATH_INFO": "/terminal/home", "SCRIPT_NAME": "",
               "HTTP_X_FORWARDED_PREFIX": "/terminal", "wsgi.url_scheme": "http",
               "SERVER_NAME": "localhost", "SERVER_PORT": "80"}
    captured = {}

    def inner(env, start_response):
        captured["script"] = env["SCRIPT_NAME"]; captured["path"] = env["PATH_INFO"]
        start_response("200 OK", []); return [b""]

    flask_app._ForwardedPrefix(inner)(environ, lambda *a: None)
    assert captured == {"script": "/terminal", "path": "/home"}


def test_templates_render_links_under_the_prefix(client):
    """The header partial is on every signed-in page; its links must carry
    the root. (The gatekeeper login page has no header, only the footer.)"""
    with client.session_transaction() as sess:
        sess["app_authenticated"] = True
    r = client.get("/home", headers={"X-Forwarded-Prefix": "/terminal"})
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'window.APP_ROOT = "/terminal"' in body
    assert 'href="/terminal/static/theme.css"' in body
    # and without the header, nothing is prefixed
    plain = client.get("/home").get_data(as_text=True)
    assert 'window.APP_ROOT = ""' in plain
    assert 'href="/static/theme.css"' in plain


def test_an_absent_optional_module_is_not_a_server_error(client):
    """delta_live_tracker is not in this checkout. Answering 500 with an HTML
    error page made the caller die on JSON.parse ("Unexpected token '<'") and
    took the whole delta table down with it."""
    with client.session_transaction() as sess:
        sess["app_authenticated"] = True
    r = client.get("/api/delta_live_snapshot")
    assert r.status_code == 200
    body = r.get_json()
    assert body["unavailable"]
    assert body["NIFTY"] == {} and body["updated_at"] is None


def test_a_job_whose_optional_module_is_absent_disables_itself():
    """It logged a traceback every 60 seconds instead -- 300 of them in the
    log by the time anyone looked."""
    from notifications import scheduler

    scheduler._MISSING_OPTIONAL.clear()
    try:
        scheduler._job_delta_live_snapshot()
        assert "DELTA_LIVE_SNAPSHOT" in scheduler._MISSING_OPTIONAL
        # second call returns before importing again
        scheduler._job_delta_live_snapshot()
    finally:
        scheduler._MISSING_OPTIONAL.clear()


def test_tradable_symbols_come_from_the_public_scrip_master(client):
    """Contracts must be listable without a broker session.

    The symbol boxes were free text with a worked example for a
    placeholder, so a run began by typing a contract from memory. The scrip
    master is a plain cached CSV, which matters: it is the one contract
    source that still answers when a token has expired -- exactly when
    someone is trying to work out what is wrong.
    """
    with client.session_transaction() as sess:
        sess["app_authenticated"] = True

    r = client.get("/api/tradable_symbols?name=NIFTY&limit=5")
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    body = r.get_json()

    assert body["symbols"], "no contracts listed"
    assert len(body["symbols"]) <= 5
    assert body["expiry"].startswith("NIFTY")
    assert body["days_to_expiry"] >= 0, "an expired contract was offered"

    for row in body["symbols"]:
        assert row["symbol"].startswith(body["expiry"])
        assert row["option_type"] in ("CE", "PE")
        assert row["symbol"].endswith(row["option_type"])
        assert str(row["strike"]) in row["symbol"]

    # nearest expiry first
    days = [e["days_to_expiry"] for e in body["expiries"]]
    assert days == sorted(days)


def test_tradable_symbols_rejects_a_malformed_underlying(client):
    """The name reaches a file lookup; it is validated, not trusted."""
    with client.session_transaction() as sess:
        sess["app_authenticated"] = True
    assert client.get("/api/tradable_symbols?name=../../etc/passwd").status_code == 400


def test_expiries_survive_an_empty_instrument_cache(client):
    """The Delta Limits table and the testbed must still list expiries.

    The instrument cache is filled by a broker sync that needs a live
    session; without one it holds no F&O rows and every underlying returned
    [], leaving the table with nothing but its three Default rows.
    """
    import flask_app

    with client.session_transaction() as sess:
        sess["app_authenticated"] = True

    for underlying in ("NIFTY", "BANKNIFTY", "SENSEX"):
        dates = flask_app.upcoming_expiries(underlying, 2)
        assert dates, f"no expiries for {underlying} from either source"
        assert dates == sorted(dates), "nearest expiry must come first"

    body = client.get("/api/delta_config").get_json()
    for underlying in ("NIFTY", "BANKNIFTY", "SENSEX"):
        assert body["upcoming_expiries"][underlying], underlying


def test_an_equity_is_never_mistaken_for_an_option():
    """RELIANCE ends in "CE".

    The position guard is options-only by design, so a stock landing in it
    would be netted against option positions on the same underlying.
    """
    from position_guard.detector import _is_option_symbol

    assert not _is_option_symbol("RELIANCE")
    assert not _is_option_symbol("GOLDBEES-EQ")
    assert _is_option_symbol("NIFTY26O0623800CE")
    assert _is_option_symbol("RELIANCE24DEC2900CE")
    assert _is_option_symbol("SENSEX2620583200PE")
