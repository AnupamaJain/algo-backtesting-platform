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
