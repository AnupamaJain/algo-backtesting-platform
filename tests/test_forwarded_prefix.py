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
