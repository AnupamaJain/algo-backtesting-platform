"""Flattrade integration tests.

A live session token requires an interactive browser login and expires daily,
so these tests do not hit the network. What they DO verify is everything that
can silently be wrong without one: the SHA256 digest, the `jData/jKey` wire
format, symbol-to-token resolution, sign conventions, and the translation
between platform vocabulary and Noren's single-letter codes.

Those are exactly the details that produce a generic "Not_Ok" from the broker
with no clue which field was wrong.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from urllib.parse import parse_qs

import pytest

from quant_backtester.src.broker import (
    AuthError,
    InstrumentNotFound,
    OrderRejected,
    OrderStatus,
    ProductType,
    Side,
)
from quant_backtester.src.broker.flattrade import (
    API_HOST,
    NOREN_TO_PRODUCT,
    PRICE_TYPE_TO_NOREN,
    PRODUCT_TO_NOREN,
    FlattradeAdapter,
    FlattradeAuth,
    FlattradeClient,
    FlattradeQuotes,
    build_flattrade_downloader,
    build_login_url,
    _split_symbol,
)
from quant_backtester.src.broker.models import OrderType, UnifiedOrder


class FakeResponse:
    def __init__(self, payload, status_code: int = 200, text: str = ""):
        self._payload = payload
        self.status_code = status_code
        self.content = b"x" if payload is not None else b""
        # /auth/session answers with a bare sid string, not JSON.
        self.text = text

    def json(self):
        return self._payload


class RecordingSession:
    """Captures outgoing requests so the wire format can be asserted."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def post(self, url, data=None, timeout=None, **kwargs):
        self.calls.append((url, data))
        payload = self.responses.pop(0) if self.responses else {"stat": "Ok"}
        if isinstance(payload, Exception):
            raise payload
        return FakeResponse(payload)


def client_with(responses, client_id="FT1234") -> FlattradeClient:
    client = FlattradeClient(token="TOKEN123", client_id=client_id)
    client._session = RecordingSession(responses)
    return client


def sent(client: FlattradeClient, index: int = 0) -> tuple[str, dict, str]:
    """(url, decoded jData, jKey) for one recorded call."""
    url, body = client._session.calls[index]
    parsed = parse_qs(body, strict_parsing=True)
    return url, json.loads(parsed["jData"][0]), parsed["jKey"][0]



def adapter_with(responses, tmp_path) -> FlattradeAdapter:
    """A properly constructed adapter with a fresh token and a fake transport.

    Built through __init__ rather than __new__ so the auth strategy is real —
    bypassing construction would skip exactly the session handling that the
    adapter depends on.
    """
    token_file = tmp_path / "tok.json"
    token_file.write_text(
        json.dumps(
            {"token": "T", "client_id": "FT1234", "issued_at": datetime.now().isoformat()}
        )
    )
    adapter = FlattradeAdapter(
        FlattradeAuth("flattrade", {"token_file": str(token_file), "token_env": "UNSET_ADP"}),
        {"exchange": "NSE"},
    )
    adapter._client = client_with(responses)
    return adapter


# ==========================================================================
# Authentication
# ==========================================================================


def test_login_url_carries_the_app_key():
    assert build_login_url("ABC123") == "https://auth.flattrade.in/?app_key=ABC123"


def test_token_digest_is_sha256_of_key_code_secret(monkeypatch):
    """Field order in the digest is not guessable — an inverted order returns a
    generic failure with no indication of what was wrong."""
    from quant_backtester.src.broker import flattrade

    captured = {}

    def fake_post(url, json=None, timeout=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        return FakeResponse({"token": "TOK"})

    monkeypatch.setattr(flattrade.requests, "post", fake_post)
    token = flattrade.exchange_request_code("KEY", "SECRET", "CODE")

    assert token == "TOK"
    assert captured["url"] == "https://authapi.flattrade.in/trade/apitoken"
    assert captured["json"]["api_key"] == "KEY"
    assert captured["json"]["request_code"] == "CODE"
    assert captured["json"]["api_secret"] == hashlib.sha256(b"KEYCODESECRET").hexdigest()


def test_token_exchange_failure_raises_auth_error(monkeypatch):
    from quant_backtester.src.broker import flattrade

    monkeypatch.setattr(
        flattrade.requests, "post", lambda *a, **k: FakeResponse({"emsg": "bad secret"})
    )
    with pytest.raises(AuthError, match="bad secret"):
        flattrade.exchange_request_code("KEY", "SECRET", "CODE")


def test_missing_token_explains_how_to_get_one(monkeypatch):
    """With no token and auto-login switched off, the error must name the
    tool that fixes it.

    auto_login is disabled explicitly: leaving it on makes this test perform
    a REAL login against the broker using whatever credentials happen to be
    on the machine.
    """
    auth = FlattradeAuth(
        "flattrade", {"token_env": "NOT_SET_ANYWHERE", "auto_login": False}
    )
    with pytest.raises(AuthError, match="flattrade_token.py"):
        auth.authenticate()


def test_stale_token_is_never_reused(tmp_path, monkeypatch):
    """Tokens expire daily. A stale one must never be handed out — it would
    fail on every subsequent call with a confusing 'invalid session'.

    Since automated login was added, the stale path attempts a refresh rather
    than erroring outright; with no credentials configured it reports how to
    fix that instead of silently using the dead token.
    """
    for var in (
        "FLATTRADE_API_KEY", "FLATTRADE_API_SECRET", "FLATTRADE_CLIENT_ID",
        "FLATTRADE_PASSWORD", "FLATTRADE_TOTP_SECRET", "FLATTRADE_SECOND_FACTOR",
    ):
        monkeypatch.delenv(var, raising=False)

    token_file = tmp_path / "token.json"
    token_file.write_text(
        json.dumps({"token": "OLD", "client_id": "FT1", "issued_at": "2020-01-01T09:00:00"})
    )
    auth = FlattradeAuth(
        "flattrade",
        {"token_file": str(token_file), "token_env": "UNSET_X", "credentials_file": str(tmp_path / "none.ini")},
    )
    with pytest.raises(AuthError, match="automated login is not configured"):
        auth.authenticate()

    # The dead token was not handed out as a fallback.
    assert auth.session is None


def test_fresh_token_file_is_accepted(tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text(
        json.dumps(
            {"token": "NEW", "client_id": "FT1", "issued_at": datetime.now().isoformat()}
        )
    )
    auth = FlattradeAuth("flattrade", {"token_file": str(token_file), "token_env": "UNSET_Y"})
    session = auth.authenticate()
    assert session.is_valid()
    assert session.user_id == "FT1"


def test_session_dict_does_not_leak_the_token(tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text(
        json.dumps({"token": "SECRET", "client_id": "F", "issued_at": datetime.now().isoformat()})
    )
    auth = FlattradeAuth("flattrade", {"token_file": str(token_file), "token_env": "UNSET_Z"})
    payload = auth.authenticate().to_dict()
    assert "SECRET" not in json.dumps(payload)


# ==========================================================================
# Wire format
# ==========================================================================


def test_requests_use_jdata_jkey_form_encoding():
    """Noren expects a form body, not JSON. Sending JSON returns a generic
    error that looks like an auth problem."""
    client = client_with([{"stat": "Ok", "lp": "100"}])
    client._post("getquotes", {"exch": "NSE", "token": "2885"})

    url, jdata, jkey = sent(client)
    assert url == f"{API_HOST}/GetQuotes"
    assert jkey == "TOKEN123"
    assert jdata["exch"] == "NSE"
    assert jdata["token"] == "2885"


def test_uid_is_injected_into_every_request():
    client = client_with([{"stat": "Ok"}])
    client._post("limits", {})
    _, jdata, _ = sent(client)
    assert jdata["uid"] == "FT1234"


def test_not_ok_response_raises_even_on_http_200():
    """Noren reports failures with HTTP 200 and stat=Not_Ok, so the status
    code alone is not a success signal."""
    client = client_with([{"stat": "Not_Ok", "emsg": "insufficient funds"}])
    with pytest.raises(OrderRejected, match="insufficient funds"):
        client._post("placeorder", {})


def test_session_errors_are_auth_errors_not_rejections():
    """An expired session must be distinguishable from a rejected order — one
    is retryable after re-auth, the other never is."""
    client = client_with([{"stat": "Not_Ok", "emsg": "Invalid Session Key"}])
    with pytest.raises(AuthError):
        client._post("orderbook", {})


def test_http_429_becomes_rate_limited():
    from quant_backtester.src.broker import RateLimited

    client = FlattradeClient("T", "U")
    client._session = type(
        "S", (), {"post": lambda self, *a, **k: FakeResponse({}, status_code=429)}
    )()
    with pytest.raises(RateLimited):
        client._post("getquotes", {})


# ==========================================================================
# Instrument resolution
# ==========================================================================


def test_symbol_resolves_to_a_numeric_token():
    client = client_with(
        [{"stat": "Ok", "values": [{"exch": "NSE", "token": "2885", "tsym": "RELIANCE-EQ"}]}]
    )
    assert client.resolve_token("NSE", "RELIANCE-EQ") == "2885"


def test_exact_symbol_match_wins_over_substring_hits():
    """Searching "INFY" also returns INFY futures. Taking the first hit would
    quietly trade the wrong instrument."""
    client = client_with(
        [
            {
                "stat": "Ok",
                "values": [
                    {"token": "999", "tsym": "INFYNIFTY-EQ"},
                    {"token": "1594", "tsym": "INFY-EQ"},
                ],
            }
        ]
    )
    assert client.resolve_token("NSE", "INFY-EQ") == "1594"


def test_token_lookup_is_cached():
    client = client_with([{"stat": "Ok", "values": [{"token": "2885", "tsym": "RELIANCE-EQ"}]}])
    client.resolve_token("NSE", "RELIANCE-EQ")
    client.resolve_token("NSE", "RELIANCE-EQ")
    assert len(client._session.calls) == 1


def test_unknown_symbol_raises_instrument_not_found():
    client = client_with([{"stat": "Ok", "values": []}])
    with pytest.raises(InstrumentNotFound):
        client.resolve_token("NSE", "NOSUCHSCRIP")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("RELIANCE-EQ", ("NSE", "RELIANCE-EQ")),
        ("BSE:RELIANCE", ("BSE", "RELIANCE")),
        ("NFO:NIFTY24DEC", ("NFO", "NIFTY24DEC")),
    ],
)
def test_symbol_may_carry_an_exchange_prefix(raw, expected):
    assert _split_symbol(raw, "NSE") == expected


# ==========================================================================
# Vocabulary translation
# ==========================================================================


def test_product_types_map_to_noren_codes_and_back():
    assert PRODUCT_TO_NOREN[ProductType.INTRADAY] == "I"
    assert PRODUCT_TO_NOREN[ProductType.CARRYFORWARD] == "M"
    assert PRODUCT_TO_NOREN[ProductType.DELIVERY] == "C"
    for product, code in PRODUCT_TO_NOREN.items():
        assert NOREN_TO_PRODUCT[code] is product


def test_order_types_map_to_noren_price_types():
    assert PRICE_TYPE_TO_NOREN[OrderType.MARKET] == "MKT"
    assert PRICE_TYPE_TO_NOREN[OrderType.LIMIT] == "LMT"
    assert PRICE_TYPE_TO_NOREN[OrderType.STOP] == "SL-MKT"
    assert PRICE_TYPE_TO_NOREN[OrderType.STOP_LIMIT] == "SL-LMT"


# ==========================================================================
# Quotes and positions
# ==========================================================================


def test_quote_maps_noren_fields():
    client = client_with(
        [
            {"stat": "Ok", "values": [{"token": "2885", "tsym": "RELIANCE-EQ"}]},
            {"stat": "Ok", "lp": "1420.50", "bp1": "1420.00", "sp1": "1421.00", "c": "1410.00"},
        ]
    )
    quote = FlattradeQuotes(client).get_quote("RELIANCE-EQ")

    assert quote.last_price == 1420.50
    assert quote.bid == 1420.00 and quote.ask == 1421.00
    assert quote.previous_close == 1410.00
    assert quote.change_pct == pytest.approx(1420.50 / 1410.00 - 1)


def test_short_position_keeps_its_negative_sign(tmp_path):
    """Noren already signs netqty; the platform convention is the same, so it
    must pass through untouched rather than being made positive."""
    adapter = adapter_with(
        [[{"tsym": "SBIN-EQ", "netqty": "-50", "netavgprc": "800", "lp": "790", "prd": "I", "rpnl": "0"}]],
        tmp_path,
    )
    positions = adapter.get_positions()

    assert len(positions) == 1
    assert positions[0].quantity == -50
    assert positions[0].direction == "SHORT"
    # Short + falling price = profit.
    assert positions[0].unrealized_pnl > 0


def test_flat_positions_are_dropped(tmp_path):
    adapter = adapter_with([[{"tsym": "SBIN-EQ", "netqty": "0", "netavgprc": "800"}]], tmp_path)
    assert adapter.get_positions() == []


def test_place_order_translates_to_noren_fields(tmp_path):
    """A wrong field name here returns a generic Not_Ok with no indication of
    which one was rejected."""
    adapter = adapter_with([{"stat": "Ok", "norenordno": "24120400012345"}], tmp_path)
    placed = adapter.place_order(
        UnifiedOrder(
            symbol="RELIANCE-EQ",
            side=Side.BUY,
            quantity=10,
            order_type=OrderType.LIMIT,
            limit_price=1400.0,
            product=ProductType.INTRADAY,
            strategy="rsi",
        )
    )

    _, jdata, _ = sent(adapter._client)
    assert jdata["exch"] == "NSE"
    assert jdata["tsym"] == "RELIANCE-EQ"
    assert jdata["trantype"] == "B"
    assert jdata["prctyp"] == "LMT"
    assert jdata["prd"] == "I"
    assert jdata["qty"] == "10"
    assert jdata["prc"] == "1400.0"
    assert placed.broker_order_id == "24120400012345"
    assert placed.status is OrderStatus.OPEN


def test_sell_order_uses_noren_s_transaction_code(tmp_path):
    adapter = adapter_with([{"stat": "Ok", "norenordno": "1"}], tmp_path)
    adapter.place_order(UnifiedOrder(symbol="SBIN-EQ", side=Side.SELL, quantity=5))
    _, jdata, _ = sent(adapter._client)
    assert jdata["trantype"] == "S"


def test_order_without_order_number_is_rejected(tmp_path):
    adapter = adapter_with([{"stat": "Ok", "emsg": "market closed"}], tmp_path)
    with pytest.raises(OrderRejected, match="market closed"):
        adapter.place_order(UnifiedOrder(symbol="SBIN-EQ", side=Side.BUY, quantity=1))


def test_adapter_is_not_simulated():
    """Flattrade moves real money, so it must sit behind the live-trading gate."""
    assert FlattradeAdapter.is_simulated is False


# ==========================================================================
# Historical data for the research pipeline
# ==========================================================================


def test_downloader_produces_the_pipeline_frame_shape():
    client = client_with(
        [
            [
                {"ssboe": "1704067200", "into": "100", "inth": "105", "intl": "99", "intc": "104", "intv": "1000"},
                {"ssboe": "1704153600", "into": "104", "inth": "108", "intl": "103", "intc": "107", "intv": "1200"},
            ]
        ]
    )
    frame = build_flattrade_downloader(client)("RELIANCE-EQ", date(2024, 1, 1), date(2024, 1, 3))

    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    assert len(frame) == 2
    assert frame["Close"].iloc[-1] == 107.0
    assert frame.index.is_monotonic_increasing


def test_downloader_marks_data_already_adjusted():
    """Flattrade EOD is corporate-action adjusted. Setting Adj Close equal to
    Close keeps data_loader's back-adjustment a no-op instead of adjusting
    twice."""
    client = client_with([[{"ssboe": "1704067200", "into": "1", "inth": "1", "intl": "1", "intc": "50", "intv": "1"}]])
    frame = build_flattrade_downloader(client)("TCS-EQ", date(2024, 1, 1), date(2024, 1, 2))
    assert (frame["Adj Close"] == frame["Close"]).all()


def test_downloader_raises_when_history_is_empty():
    from quant_backtester.src.broker import BrokerUnavailable

    client = client_with([[]])
    with pytest.raises(BrokerUnavailable):
        build_flattrade_downloader(client)("X-EQ", date(2024, 1, 1), date(2024, 1, 2))


def test_eod_request_uses_exchange_qualified_symbol_and_epochs():
    """EODChartData is the one endpoint keyed by EXCH:TSYM rather than token."""
    client = client_with([[{"ssboe": "1704067200", "intc": "100"}]])
    build_flattrade_downloader(client)("RELIANCE-EQ", date(2024, 1, 1), date(2024, 1, 5))

    _, jdata, _ = sent(client)
    assert jdata["sym"] == "NSE:RELIANCE-EQ"
    assert jdata["from"].isdigit() and jdata["to"].isdigit()
    assert int(jdata["to"]) > int(jdata["from"])


# ==========================================================================
# Real response quirks — captured from live calls
# ==========================================================================
#
# These formats were observed against the live API. The original tests mocked
# EODChartData as a list of dicts, matching the documented shape; the service
# actually returns a list of JSON *strings*. Mocks agreed with the docs and
# both were wrong, which is exactly the gap only a live call could close.


REAL_EOD_ROWS = [
    '{"time":"01-SEP-2026", "into":"1285.00", "inth":"1311.80", "intl":"1280.00", '
    '"intc":"1309.00", "ssboe":"1788220800", "intv":"12617528.00"}',
    '{"time":"31-AUG-2026", "into":"1278.70", "inth":"1297.60", "intl":"1271.00", '
    '"intc":"1277.00", "ssboe":"1788134400", "intv":"34871137.00"}',
]


def test_list_endpoints_decode_json_string_elements():
    """EODChartData and TPSeries return a list of JSON strings, not objects."""
    client = client_with([REAL_EOD_ROWS])
    rows = client.get_daily_bars("NSE", "RELIANCE-EQ", date(2026, 8, 1), date(2026, 9, 2))

    assert all(isinstance(r, dict) for r in rows), "elements must be decoded to dicts"
    assert rows[0]["intc"] == "1309.00"


def test_downloader_handles_the_real_eod_payload():
    client = client_with([REAL_EOD_ROWS])
    frame = build_flattrade_downloader(client)(
        "RELIANCE-EQ", date(2026, 8, 1), date(2026, 9, 2)
    )

    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    assert len(frame) == 2
    # Flattrade returns newest-first; the pipeline needs oldest-first.
    assert frame.index.is_monotonic_increasing
    assert frame["Close"].iloc[-1] == 1309.00
    assert frame["High"].iloc[-1] == 1311.80
    assert frame["Volume"].iloc[-1] == 12617528.0


def test_dict_responses_are_left_alone():
    """Positions and the orderbook already return objects; decoding must not
    corrupt them."""
    client = client_with([[{"tsym": "SBIN-EQ", "netqty": "10"}]])
    rows = client.get_positions()
    assert rows == [{"tsym": "SBIN-EQ", "netqty": "10"}]


def test_flattrade_date_format_is_day_month_year():
    """Dates are spelled "01-SEP-2026" — uppercase, month abbreviated. A
    "%d-%m-%Y" parse (the original guess) raises on this."""
    from datetime import datetime as dt

    assert dt.strptime("01-SEP-2026".upper(), "%d-%b-%Y").date() == date(2026, 9, 1)


def test_downloader_falls_back_to_the_time_field_without_epoch():
    """ssboe is preferred, but the row must still parse if only `time` is present."""
    rows = ['{"time":"15-JUL-2026", "into":"100", "inth":"101", "intl":"99", '
            '"intc":"100.5", "intv":"5000"}']
    client = client_with([rows])
    frame = build_flattrade_downloader(client)("TCS-EQ", date(2026, 7, 1), date(2026, 8, 1))
    assert frame.index[0].date() == date(2026, 7, 15)


# ==========================================================================
# Automated (non-interactive) login
# ==========================================================================


def _login_response(code="RC-123"):
    return FakeResponse(
        {"emsg": "", "RedirectURL": f"http://localhost:8080/?code={code}&client=FZ1"}
    )


class FakeSession:
    """Stands in for requests.Session across the whole login sequence.

    The real flow opens a session, authenticates against it, then exchanges
    the request code — three calls sharing cookies. Patching only the module
    level `requests.post` leaves the Session calls unmocked, which sends real
    traffic to the broker from the test suite.
    """

    #: Set per test to control what /ftauth answers.
    ftauth_response = None

    def __init__(self, *args, **kwargs):
        self.calls = FakeSession.calls

    def post(self, url, json=None, timeout=None, headers=None, **kwargs):
        FakeSession.calls.append((url, json))
        if url.endswith("/auth/session"):
            return FakeResponse("SID-XYZ", text="SID-XYZ")
        if url.endswith("/ftauth"):
            return FakeSession.ftauth_response or _login_response()
        return FakeResponse({"token": "TK"})


def _patch_session(monkeypatch, ftauth=None):
    from quant_backtester.src.broker import flattrade

    FakeSession.calls = []
    FakeSession.ftauth_response = ftauth
    monkeypatch.setattr(flattrade.requests, "Session", FakeSession)
    monkeypatch.setattr(
        flattrade.requests, "post",
        lambda url, **kw: FakeSession().post(url, **kw),
    )
    return FakeSession.calls


def test_automated_login_posts_the_login_form_shape(monkeypatch):
    """Mirrors what the login page itself sends: SHA256 password, TOTP in
    PAN_DOB, API key alongside. A wrong field name yields a bare failure."""
    from quant_backtester.src.broker import flattrade

    calls = _patch_session(monkeypatch)
    token = flattrade.automated_login(
        api_key="KEY", api_secret="SEC", client_id="FZ1",
        password="hunter2", totp_secret="JBSWY3DPEHPK3PXP",
    )

    # calls[0] is now /auth/session; the login post follows it.
    assert calls[0][0].endswith("/auth/session")
    login_url, payload = calls[1]
    assert login_url == "https://authapi.flattrade.in/ftauth"
    assert payload["UserName"] == "FZ1"
    assert payload["APIKey"] == "KEY"
    # Source is required; omitting it is rejected as "Session invalid", which
    # reads like an expired session rather than a malformed request.
    assert payload["Source"] == "AUTHPAGE"
    # Every field the login page sends must be present, even the empty ones.
    assert set(payload) == {
        "UserName", "Rd", "Password", "PAN_DOB", "App", "ClientID",
        "Key", "APIKey", "Sid", "Override", "Source",
    }
    # Password is hashed before it leaves the process.
    assert payload["Password"] == hashlib.sha256(b"hunter2").hexdigest()
    assert "hunter2" not in json.dumps(payload)
    # TOTP lands in the second-factor field as six digits.
    assert payload["PAN_DOB"].isdigit() and len(payload["PAN_DOB"]) == 6
    assert token == "TK"


def test_totp_changes_between_time_steps(monkeypatch):
    """A static code would be replayable; each login must mint a fresh one."""
    import pyotp

    totp = pyotp.TOTP("JBSWY3DPEHPK3PXP")
    assert totp.at(0) != totp.at(60)


def test_automated_login_surfaces_the_broker_message(monkeypatch):
    from quant_backtester.src.broker import flattrade

    _patch_session(
        monkeypatch,
        ftauth=FakeResponse({"emsg": "Invalid Password", "RedirectURL": ""}),
    )
    with pytest.raises(AuthError, match="Invalid Password"):
        flattrade.automated_login(
            api_key="K", api_secret="S", client_id="U",
            password="wrong", totp_secret="JBSWY3DPEHPK3PXP",
        )


def test_automated_login_requires_a_second_factor():
    from quant_backtester.src.broker import flattrade

    with pytest.raises(AuthError, match="no second factor"):
        flattrade.automated_login(
            api_key="K", api_secret="S", client_id="U", password="p"
        )


def test_second_factor_escape_hatch_is_uppercased(monkeypatch):
    """Accounts still on PAN/DOB must remain usable."""
    from quant_backtester.src.broker import flattrade

    calls = _patch_session(monkeypatch)
    flattrade.automated_login(
        api_key="K", api_secret="S", client_id="U", password="p", second_factor="abcde1234f"
    )
    # calls[0] is /auth/session (no body); the login payload follows.
    assert calls[1][1]["PAN_DOB"] == "ABCDE1234F"


def test_missing_redirect_url_is_an_auth_error(monkeypatch):
    from quant_backtester.src.broker import flattrade

    _patch_session(
        monkeypatch, ftauth=FakeResponse({"emsg": "", "RedirectURL": ""})
    )
    with pytest.raises(AuthError, match="no RedirectURL"):
        flattrade.automated_login(
            api_key="K", api_secret="S", client_id="U", password="p", second_factor="X"
        )


def test_stale_token_triggers_auto_login_not_an_error(tmp_path, monkeypatch):
    """The point of automating this: an unattended 9am run must refresh
    itself rather than stopping for a human."""
    from quant_backtester.src.broker import flattrade

    token_file = tmp_path / "tok.json"
    token_file.write_text(
        json.dumps({"token": "OLD", "client_id": "FZ1", "issued_at": "2020-01-01T09:00:00"})
    )

    monkeypatch.setenv("FLATTRADE_API_KEY", "K")
    monkeypatch.setenv("FLATTRADE_API_SECRET", "S")
    monkeypatch.setenv("FLATTRADE_CLIENT_ID", "FZ1")
    monkeypatch.setenv("FLATTRADE_PASSWORD", "p")
    monkeypatch.setenv("FLATTRADE_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    monkeypatch.setattr(flattrade, "automated_login", lambda **kw: "FRESH")

    auth = flattrade.FlattradeAuth(
        "flattrade", {"token_file": str(token_file), "token_env": "UNSET_AUTO"}
    )
    session = auth.authenticate()

    assert session.access_token == "FRESH"
    # The refreshed token is cached for the rest of the day.
    assert json.loads(token_file.read_text())["token"] == "FRESH"
    assert json.loads(token_file.read_text())["source"] == "automated"


def test_fresh_token_is_reused_without_logging_in(tmp_path, monkeypatch):
    """Re-authenticating on every call would burn rate limit for nothing."""
    from quant_backtester.src.broker import flattrade

    token_file = tmp_path / "tok.json"
    token_file.write_text(
        json.dumps(
            {"token": "GOOD", "client_id": "FZ1", "issued_at": datetime.now().isoformat()}
        )
    )

    def explode(**kw):
        raise AssertionError("must not re-login while the token is still valid")

    monkeypatch.setattr(flattrade, "automated_login", explode)
    auth = flattrade.FlattradeAuth(
        "flattrade", {"token_file": str(token_file), "token_env": "UNSET_FRESH"}
    )
    assert auth.authenticate().access_token == "GOOD"


def test_auto_login_can_be_disabled(tmp_path, monkeypatch):
    """Some operators want an explicit manual step; that must stay possible."""
    from quant_backtester.src.broker import flattrade

    token_file = tmp_path / "tok.json"
    token_file.write_text(
        json.dumps({"token": "OLD", "client_id": "F", "issued_at": "2020-01-01T09:00:00"})
    )
    auth = flattrade.FlattradeAuth(
        "flattrade",
        {"token_file": str(token_file), "token_env": "UNSET_OFF", "auto_login": False},
    )
    with pytest.raises(AuthError, match="auto_login is disabled"):
        auth.authenticate()


def test_token_file_is_written_private(tmp_path, monkeypatch):
    """A session token is a credential; it must not be world-readable."""
    from quant_backtester.src.broker import flattrade

    token_file = tmp_path / "tok.json"
    monkeypatch.setenv("FLATTRADE_API_KEY", "K")
    monkeypatch.setenv("FLATTRADE_API_SECRET", "S")
    monkeypatch.setenv("FLATTRADE_CLIENT_ID", "FZ1")
    monkeypatch.setenv("FLATTRADE_PASSWORD", "p")
    monkeypatch.setenv("FLATTRADE_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    monkeypatch.setattr(flattrade, "automated_login", lambda **kw: "FRESH")

    auth = flattrade.FlattradeAuth(
        "flattrade", {"token_file": str(token_file), "token_env": "UNSET_PERM"}
    )
    auth.authenticate()
    assert oct(token_file.stat().st_mode)[-3:] == "600"


# ==========================================================================
# Credential resolution
# ==========================================================================


def test_bare_pwd_env_var_is_never_read_as_a_password(monkeypatch, tmp_path):
    """Regression: `PWD` is the shell's working directory.

    An early alias list accepted a bare `pwd`, so the resolver silently picked
    up the current directory as the login password — wrong, and it would have
    sent a filesystem path to the broker in a login request.
    """
    from quant_backtester.src.broker.flattrade import resolve_credentials

    monkeypatch.setenv("PWD", "/Users/someone/some/very/long/path")
    monkeypatch.delenv("FLATTRADE_PASSWORD", raising=False)
    empty = tmp_path / "empty.ini"
    empty.write_text("[flattrade]\n")

    assert resolve_credentials(empty)["password"] == ""


def test_environment_names_must_be_namespaced(monkeypatch, tmp_path):
    """Only FLATTRADE_-prefixed variables are read from the environment."""
    from quant_backtester.src.broker.flattrade import resolve_credentials

    for var in ("UID", "TOTP", "PAN", "DOB"):
        monkeypatch.setenv(var, "should-be-ignored")
    for var in ("FLATTRADE_CLIENT_ID", "FLATTRADE_TOTP", "FLATTRADE_TOTP_SECRET"):
        monkeypatch.delenv(var, raising=False)
    empty = tmp_path / "empty.ini"
    empty.write_text("[flattrade]\n")

    creds = resolve_credentials(empty)
    assert creds["client_id"] == ""
    assert creds["totp_secret"] == ""


def test_flattrade_totp_alias_is_accepted(monkeypatch, tmp_path):
    """Operators write the seed as FLATTRADE_TOTP as often as totp_secret."""
    from quant_backtester.src.broker.flattrade import resolve_credentials

    for var in ("FLATTRADE_TOTP", "FLATTRADE_TOTP_SECRET"):
        monkeypatch.delenv(var, raising=False)
    config = tmp_path / "c.ini"
    config.write_text("[flattrade]\nFLATTRADE_TOTP = JBSWY3DPEHPK3PXP\n")

    assert resolve_credentials(config)["totp_secret"] == "JBSWY3DPEHPK3PXP"


def test_environment_overrides_the_config_file(monkeypatch, tmp_path):
    from quant_backtester.src.broker.flattrade import resolve_credentials

    config = tmp_path / "c.ini"
    config.write_text("[flattrade]\napi_key = from_file\n")
    monkeypatch.setenv("FLATTRADE_API_KEY", "from_env")

    assert resolve_credentials(config)["api_key"] == "from_env"


def test_section_keys_are_case_insensitive(tmp_path):
    from quant_backtester.src.broker.flattrade import resolve_credentials

    config = tmp_path / "c.ini"
    config.write_text("[flattrade]\nAPI_KEY = k\nClient_Id = FZ1\n")
    creds = resolve_credentials(config)
    assert creds["api_key"] == "k"
    assert creds["client_id"] == "FZ1"


def test_closed_market_bid_ask_is_absent_not_zero():
    """Outside trading hours Noren reports bid/ask as "0.00". Presenting that
    as a real quote is misleading, and a zero reaching the fill path would
    price a trade at nothing."""
    client = client_with(
        [
            {"stat": "Ok", "values": [{"token": "2885", "tsym": "RELIANCE-EQ"}]},
            {"stat": "Ok", "lp": "1313.10", "bp1": "0.00", "sp1": "0.00", "c": "1309.00"},
        ]
    )
    quote = FlattradeQuotes(client).get_quote("RELIANCE-EQ")

    assert quote.last_price == 1313.10
    assert quote.bid is None and quote.ask is None
    # With no book, mid falls back to the last traded price — never zero.
    assert quote.mid == 1313.10


def test_paper_fill_never_uses_a_zero_price(tmp_path):
    """A closed-market quote must not fill a paper order at 0."""
    from quant_backtester.src.broker import PaperBroker, Side, UnifiedOrder
    from quant_backtester.src.broker.models import UnifiedQuote
    from quant_backtester.src.broker.quotes import QuoteProvider
    from quant_backtester.src.broker.store import BrokerStore

    class ClosedMarket(QuoteProvider):
        def get_quote(self, symbol):
            return UnifiedQuote(
                symbol=symbol, last_price=1313.10, timestamp=datetime.now(),
                bid=None, ask=None, previous_close=1309.0,
            )

    broker = PaperBroker(
        BrokerStore(tmp_path / "closed.db"), ClosedMarket(),
        config={"starting_cash": 1_000_000.0, "universe": ["RELIANCE-EQ"]},
    )
    filled = broker.place_order(
        UnifiedOrder(symbol="RELIANCE-EQ", side=Side.BUY, quantity=1)
    )
    assert filled.average_price is not None and filled.average_price > 1000


def test_login_opens_a_session_before_authenticating(monkeypatch):
    """The step whose absence caused "Session invalid" on every attempt.

    /ftauth validates the Sid it is handed. Sending an empty one is rejected
    with a message that reads like an EXPIRED session rather than a missing
    one, which is why it was mistaken for a credential problem for so long.
    """
    from quant_backtester.src.broker import flattrade

    calls = _patch_session(monkeypatch)
    flattrade.automated_login(
        api_key="K", api_secret="S", client_id="U",
        password="p", totp_secret="JBSWY3DPEHPK3PXP",
    )

    assert calls[0][0].endswith("/auth/session"), "must open a session first"
    _, payload = calls[1]
    assert payload["Sid"] == "SID-XYZ", "the sid from step 1 must be used"
    assert payload["Override"] == "Y", "without Override a same-day re-login is refused"


def test_an_empty_book_is_not_an_error():
    """Flattrade answers "no data" when there is nothing to return.

    Raising made a flat account indistinguishable from a broken one — the
    caller could not tell "you hold nothing" from "the broker is down".
    """
    client = client_with([{"stat": "Not_Ok", "emsg": 'Error Occurred : 5 "no data"'}])
    assert client.get_positions() == []


def test_a_real_error_still_raises():
    client = client_with([{"stat": "Not_Ok", "emsg": "Invalid actid"}])
    with pytest.raises(OrderRejected, match="Invalid actid"):
        client.get_positions()


def test_relative_token_file_resolves_under_quant_backtester_once(tmp_path, monkeypatch):
    """`FlattradeAuth` already anchors a relative `token_file` to the
    quant_backtester/ package root -- a caller-supplied path that ALSO
    starts with "quant_backtester/" doubles it up
    ("quant_backtester/quant_backtester/state/...") and silently misses the
    real, shared token file every other caller reads and writes.

    This is not hypothetical: two call sites (initialise_ticker() in
    common_lib.py, and ticker_daemon.py) shipped with exactly this bug,
    each one minting a brand-new Flattrade login on every call because it
    never found its own previous token "fresh" at the wrong path -- and
    each fresh login invalidated whatever session (including an actively
    streaming ticker) was using the old one. That was the real cause of a
    day of "the websocket keeps getting evicted" symptoms that looked like
    a concurrency bug.
    """
    from quant_backtester.src.broker.flattrade import FlattradeAuth

    auth = FlattradeAuth("flattrade", {"token_file": "state/flattrade_token.json"})
    resolved = auth._token_file
    assert str(resolved).count("quant_backtester") == 1
    assert resolved.name == "flattrade_token.json"


def test_common_lib_and_ticker_daemon_use_the_canonical_token_path():
    """Guards the exact regression above: neither call site's literal
    config should carry the package-root prefix a second time."""
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    for relative_path in ("common_lib.py", "ticker_daemon.py"):
        text = (repo_root / relative_path).read_text()
        assert '"quant_backtester/state/flattrade_token.json"' not in text, (
            f"{relative_path} doubles the quant_backtester/ prefix on the "
            "Flattrade token_file path"
        )
