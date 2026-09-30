"""The two URL wrappers, and the order they must install in.

Behind the gateway the terminal is served at /terminal, and ~100 template
call sites use root-relative URLs. A shim in _app_shim.html prefixes them.
A second shim attaches the CSRF token. The order between them is not a
detail: get it wrong and every mutating request in the product is rejected.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


def test_the_prefix_wrapper_installs_before_the_csrf_shim():
    """The CSRF shim captures window.fetch to fetch its own token from
    /api/csrf-token. Capture the raw fetch and that request escapes the
    prefix, 404s on the gateway, and the token is null -- so every POST goes
    out with no X-CSRFToken header and the server rejects it. That was one
    bug on every execution path in the product."""
    shim = (TEMPLATES / "_app_shim.html").read_text()
    prefix_at = shim.index("window.fetch = function")
    csrf_at = shim.index('include "_csrf_shim.html"')
    assert prefix_at < csrf_at, "the prefix wrapper must install first"


def test_the_csrf_shim_fetches_its_token_through_whatever_wraps_fetch():
    """It must capture window.fetch, not a saved reference to the native one."""
    csrf = (TEMPLATES / "_csrf_shim.html").read_text()
    assert "window.__origFetchForCsrf = window.fetch" in csrf


@pytest.mark.parametrize(
    "template",
    sorted(
        p.name
        for p in TEMPLATES.glob("*.html")
        if not p.name.startswith("_")
        and re.search(r"""(fetch\(['"]/|XMLHttpRequest)""", p.read_text())
    ),
)
def test_every_page_that_calls_home_has_the_shim(template):
    """A page without the shim makes unprefixed calls that 404 on the gateway.
    nifty_contributors and the cas_tracker dashboards each lost a whole page
    of data to this."""
    body = (TEMPLATES / template).read_text()
    assert "_app_shim.html" in body or "_header_partial.html" in body, (
        f"{template} makes root-relative calls but installs no URL shim"
    )


def test_urls_that_never_touch_fetch_are_prefixed_by_hand():
    """Audio(), EventSource and friends bypass the fetch and XHR wrappers, so
    they need window.appUrl. The notification sound 404'd on every page."""
    partial = (TEMPLATES / "_notifications_partial.html").read_text()
    assert "new Audio(window.appUrl(" in partial


def test_no_alert_can_render_the_word_undefined():
    """alert(result.message + ...) shows "undefined" whenever the server
    answered with an {"error": ...} shape instead -- which is exactly what a
    rejected request looks like, so the one moment the user most needs the
    reason is the one moment it was withheld."""
    offenders = [
        p.name
        for p in TEMPLATES.glob("*.html")
        if re.search(r"alert\(\s*\w+\.message\s*\+", p.read_text())
    ]
    assert not offenders, f"these alert on .message without a fallback: {offenders}"
