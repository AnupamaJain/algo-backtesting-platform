"""The HTTP layer. docs/09.

The properties defended here are the ones a frontend silently depends on:

* every derived payload carries provenance, so no client can present a
  three-day-old score as live
* "no scan" is a typed 503, never an empty list that reads as "no matches"
* nulls survive serialisation -- an unranked sector must not arrive as 0
* user-owned rows are filtered by the token, never by a query parameter
* no endpoint computes; a read after the database is frozen returns the same
  numbers the scan wrote
"""

from __future__ import annotations

from datetime import date

import pytest
from conftest import make_ohlcv, make_vcp


@pytest.fixture
def client(session, monkeypatch):
    """A TestClient bound to the test session.

    The session dependency is overridden rather than letting the app open its
    own connection: the test's transaction is rolled back afterwards, so the
    API sees the fixture's rows and leaves nothing behind.
    """
    from fastapi.testclient import TestClient

    from vriddhix.api.deps import get_session
    from vriddhix.api.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app, raise_server_exceptions=True) as test_client:
        yield test_client


@pytest.fixture
def scanned(session, seeded, cfg):
    """A full pipeline run, so the API has real rows to project."""
    from vriddhix.data.ingest import ingest_symbol
    from vriddhix.data.providers import ChainedProvider
    from vriddhix.jobs.daily_scan import run_daily_scan
    from test_providers import FakeProvider

    start = date(2021, 1, 4)
    frames = {
        "RELIANCE": make_vcp(start=start),
        "TCS": make_ohlcv(319, pattern="uptrend", seed=5, start=start),
        "INFY": make_ohlcv(319, pattern="downtrend", seed=6, start=start),
    }
    for symbol, frame in frames.items():
        ingest_symbol(session, symbol, ChainedProvider([FakeProvider("fake", frame)]), cfg)
    session.flush()

    as_of = frames["RELIANCE"].index[-1].date()
    report = run_daily_scan(session, cfg, as_of, symbols=list(frames))
    assert report.ok, report.errors
    return {"frames": frames, "as_of": as_of, "report": report}


# ---------------------------------------------------------------------------
# Provenance and refusal
# ---------------------------------------------------------------------------


PROVENANCE_ENDPOINTS = [
    "/api/v1/market",
    "/api/v1/sectors",
    "/api/v1/scanners/vcp",
    "/api/v1/breakouts",
    "/api/v1/stocks/RELIANCE",
]


@pytest.mark.parametrize("url", PROVENANCE_ENDPOINTS)
def test_every_derived_payload_carries_provenance(client, scanned, url):
    body = client.get(url).json()
    for field in ("as_of", "is_stale", "computed_at", "scan_run_id"):
        assert field in body, f"{url} is missing {field}"


def test_staleness_is_reported_not_raised(client, scanned, session):
    """Stale data plus its age is more useful than an error."""
    from datetime import datetime, timedelta, timezone

    from vriddhix.db.models import ScanRun

    run = session.get(ScanRun, scanned["report"].scan_run_id)
    run.finished_at = datetime.now(timezone.utc) - timedelta(days=3)
    session.flush()

    response = client.get("/api/v1/market")
    assert response.status_code == 200
    body = response.json()
    assert body["is_stale"] is True
    assert body["age_hours"] > 20
    assert body["regime"] is not None, "stale data is still returned"


def test_no_scan_is_a_typed_503_not_an_empty_list(client, seeded):
    """An empty list reads as 'nothing set up today'. That is a different
    statement from 'we never looked'."""
    response = client.get("/api/v1/scanners/vcp")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SCAN_NOT_RUN"


def test_an_unknown_symbol_is_a_typed_404(client, scanned):
    body = client.get("/api/v1/stocks/NOSUCH").json()
    assert body["error"]["code"] == "NOT_FOUND"


def test_bad_parameters_use_the_documented_error_envelope(client, scanned):
    response = client.get("/api/v1/sectors?sort=not_a_column")
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "INVALID_PARAMS"
    # The allowed set is returned so a client can correct itself.
    assert "rs_score" in error["detail"]["allowed"]


def test_fastapi_validation_errors_use_the_same_envelope(client, scanned):
    """Otherwise a client needs two parsers for one error class."""
    response = client.get("/api/v1/ops/scans?limit=0")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_PARAMS"


# ---------------------------------------------------------------------------
# Nulls
# ---------------------------------------------------------------------------


def test_an_unranked_sector_serialises_as_null(client, scanned):
    body = client.get("/api/v1/sectors").json()
    unranked = [s for s in body["sectors"] if s["rs_score"] is None]
    if not unranked:
        pytest.skip("every sector was ranked in this fixture")
    for sector in unranked:
        assert sector["rs_rank"] is None
        assert sector["quadrant"] is None


def test_unranked_sectors_sort_last_in_both_directions(client, scanned):
    for order in ("asc", "desc"):
        sectors = client.get(f"/api/v1/sectors?order={order}").json()["sectors"]
        scores = [s["rs_score"] for s in sectors]
        seen_none = False
        for score in scores:
            if score is None:
                seen_none = True
            else:
                assert not seen_none, f"a ranked sector followed a null ({order})"


def test_rotation_lists_unranked_sectors_separately(client, scanned):
    """Dropping them would understate how much of the market is unmeasured."""
    quadrants = client.get("/api/v1/sectors/rotation").json()["quadrants"]
    assert "UNRANKED" in quadrants


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def test_the_scanner_payload_uses_the_condition_vocabulary(client, scanned):
    """One vocabulary end to end.

    A screen saved against `vcp_score` has to mean the same thing when it is
    applied to a scanner row; two names for one value is how drift starts.
    """
    from vriddhix.services.conditions import known_fields

    results = client.get("/api/v1/scanners/vcp").json()["results"]
    if not results:
        pytest.skip("no patterns detected in this fixture")

    row = results[0]
    assert "vcp_score" in row and "vcp_stage" in row
    assert "score" not in row and "stage" not in row
    assert {"vcp_score", "vcp_stage", "rs_score", "symbol"} <= known_fields()


def test_every_scanner_row_carries_its_score_components(client, scanned):
    """The 'why this setup?' panel is a projection, not a recomputation."""
    results = client.get("/api/v1/scanners/vcp").json()["results"]
    if not results:
        pytest.skip("no patterns detected in this fixture")
    for row in results:
        assert isinstance(row["score_components"], list)
        assert row["score_components"], f"{row['symbol']} has no stored components"
        assert {"name", "raw", "weight"} <= set(row["score_components"][0])


def test_scanner_filters_narrow_the_result_set(client, scanned):
    everything = client.get("/api/v1/scanners/vcp").json()
    if everything["total"] == 0:
        pytest.skip("no patterns detected in this fixture")

    filtered = client.get("/api/v1/scanners/vcp?min_score=99.9").json()
    assert filtered["total"] <= everything["total"]
    # Filtered-to-nothing is a 200 with an empty list, NOT a 503: the scan ran.
    assert all(r["vcp_score"] >= 99.9 for r in filtered["results"])


def test_an_unknown_stage_is_rejected_rather_than_silently_ignored(client, scanned):
    response = client.get("/api/v1/scanners/vcp?stage=MOONSHOT")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_PARAMS"


# ---------------------------------------------------------------------------
# Reads do not compute
# ---------------------------------------------------------------------------


def test_a_read_is_repeatable(client, scanned):
    """Two identical requests return identical numbers.

    If an endpoint were computing, a second call could disagree with the
    first -- and two users would see different answers for the same date.
    """
    first = client.get("/api/v1/scanners/vcp").json()
    second = client.get("/api/v1/scanners/vcp").json()
    assert first["results"] == second["results"]
    assert first["total"] == second["total"]


def test_a_read_writes_no_rows(client, scanned, session):
    from vriddhix.db.models import Pattern, RelativeStrength

    before = (
        session.query(Pattern).count(),
        session.query(RelativeStrength).count(),
    )
    for url in ("/api/v1/market", "/api/v1/scanners/vcp", "/api/v1/sectors",
                "/api/v1/stocks/RELIANCE", "/api/v1/breakouts"):
        client.get(url)
    after = (
        session.query(Pattern).count(),
        session.query(RelativeStrength).count(),
    )
    assert before == after


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_structure_swings_expose_their_confirmation_lag(client, scanned):
    body = client.get("/api/v1/stocks/RELIANCE/structure").json()
    for swing in body["swings"]:
        if swing["confirmed_on"] is None:
            continue
        assert swing["confirmed_on"] > swing["date"]
        assert swing["right_bars"] >= 1


def test_fvg_open_includes_partially_filled(client, scanned):
    """A 30%-filled gap is still an unfilled zone."""
    body = client.get("/api/v1/scanners/fvg?status=OPEN").json()
    assert set(body["status"]) == {"OPEN", "PARTIALLY_FILLED"}


# ---------------------------------------------------------------------------
# User-scoped resources
# ---------------------------------------------------------------------------


def test_a_watchlist_is_scoped_to_the_token_not_a_parameter(client, scanned, session):
    from vriddhix.db.models import User, Watchlist

    created = client.post("/api/v1/watchlists", json={"name": "Mine"})
    assert created.status_code == 201

    # Another user's list must be invisible, even by id.
    other = User(email="someone@else.invalid", display_name="Other")
    session.add(other)
    session.flush()
    theirs = Watchlist(user_id=other.id, name="Theirs")
    session.add(theirs)
    session.flush()

    listed = client.get("/api/v1/watchlists").json()["watchlists"]
    assert [w["name"] for w in listed] == ["Mine"]

    # 404, not 403: confirming it exists is itself a disclosure.
    assert client.get(f"/api/v1/watchlists/{theirs.id}/items").status_code == 404


def test_adding_a_watchlist_item_is_idempotent(client, scanned):
    watchlist = client.post("/api/v1/watchlists", json={"name": "Setups"}).json()
    first = client.post(
        f"/api/v1/watchlists/{watchlist['id']}/items", json={"symbol": "RELIANCE"}
    ).json()
    second = client.post(
        f"/api/v1/watchlists/{watchlist['id']}/items", json={"symbol": "RELIANCE"}
    ).json()
    assert second["already_present"] is True
    assert first["id"] == second["id"]


def test_a_screen_is_validated_when_it_is_saved(client, scanned):
    """A screen that cannot be evaluated should fail where the user is
    looking at it, not on its first scheduled run."""
    bad = client.post(
        "/api/v1/screens",
        json={"name": "Broken", "conditions": {"field": "nope", "cmp": "gt", "value": 1}},
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "INVALID_PARAMS"

    good = client.post(
        "/api/v1/screens",
        json={"name": "Strong", "conditions": {"field": "vcp_score", "cmp": "gte", "value": 70}},
    )
    assert good.status_code == 201
    assert "vcp_score" in good.json()["fields_used"]


def test_a_saved_screen_and_the_scanner_agree(client, scanned):
    """One evaluator: a screen cannot mean one thing here and another in a
    backtest."""
    screen = client.post(
        "/api/v1/screens",
        json={"name": "All",
              "conditions": {"field": "vcp_score", "cmp": "gte", "value": 0}},
    ).json()

    scanner_rows = client.get("/api/v1/scanners/vcp").json()["results"]
    run = client.post(f"/api/v1/screens/{screen['id']}/run").json()
    assert run["total"] == len(scanner_rows)


# ---------------------------------------------------------------------------
# Backtests
# ---------------------------------------------------------------------------


def test_a_backtest_records_its_survivorship_mode_at_creation(client, scanned):
    created = client.post(
        "/api/v1/backtests",
        json={"name": "VCP 2021-2024", "start_date": "2021-01-04",
              "end_date": "2024-01-04"},
    )
    assert created.status_code == 202
    body = created.json()
    assert body["status"] == "QUEUED"
    # Present in the payload, not only in documentation nobody reads.
    assert body["survivorship_mode"] in ("POINT_IN_TIME", "CURRENT_UNIVERSE")


def test_metrics_on_a_running_backtest_are_202_not_zeroes(client, scanned):
    """Returning zeroed metrics would look like a strategy that lost nothing
    and won nothing, rather than one that has not finished."""
    run = client.post(
        "/api/v1/backtests",
        json={"name": "Pending", "start_date": "2021-01-04", "end_date": "2024-01-04"},
    ).json()
    response = client.get(f"/api/v1/backtests/{run['run_id']}/metrics")
    assert response.status_code == 202
    assert response.json()["error"]["code"] == "BACKTEST_RUNNING"


def test_metrics_over_no_trades_are_undefined_not_zero(client, scanned, session):
    from vriddhix.db.models import BacktestRun

    run = client.post(
        "/api/v1/backtests",
        json={"name": "Empty", "start_date": "2021-01-04", "end_date": "2024-01-04"},
    ).json()
    session.get(BacktestRun, run["run_id"]).status = "COMPLETED"
    session.flush()

    metrics = client.get(f"/api/v1/backtests/{run['run_id']}/metrics").json()["metrics"]
    assert metrics["trades"] == 0
    assert metrics["win_rate"] is None
    assert metrics["profit_factor"] is None


def test_comparing_runs_with_different_survivorship_modes_is_flagged(
    client, scanned, session
):
    from vriddhix.db.models import BacktestRun

    a = client.post("/api/v1/backtests", json={
        "name": "A", "start_date": "2021-01-04", "end_date": "2024-01-04"}).json()
    b = client.post("/api/v1/backtests", json={
        "name": "B", "start_date": "2021-01-04", "end_date": "2024-01-04"}).json()

    session.get(BacktestRun, b["run_id"]).survivorship_mode = "POINT_IN_TIME"
    session.get(BacktestRun, a["run_id"]).survivorship_mode = "CURRENT_UNIVERSE"
    session.flush()

    body = client.get(
        f"/api/v1/backtests/compare?run_ids={a['run_id']},{b['run_id']}"
    ).json()
    assert body["comparable"] is False
    assert "not directly comparable" in body["note"]


# ---------------------------------------------------------------------------
# AI
# ---------------------------------------------------------------------------


def test_an_explanation_echoes_the_inputs_it_was_given(client, scanned, session):
    """So any claim in the prose can be checked against the data."""
    from vriddhix.db.models import Pattern

    pattern = session.query(Pattern).first()
    if pattern is None:
        pytest.skip("no pattern detected in this fixture")

    body = client.post(
        "/api/v1/ai/explain",
        json={"subject_type": "PATTERN", "subject_id": pattern.id},
    ).json()
    assert body["inputs"], "inputs were not echoed"
    assert body["disclaimer"]
    assert body["engine_version_of_inputs"] == pattern.engine_version


def test_the_client_cannot_inject_market_facts(client, scanned, session):
    """Facts come from stored rows. A client-supplied score is ignored."""
    from vriddhix.db.models import Pattern

    pattern = session.query(Pattern).first()
    if pattern is None:
        pytest.skip("no pattern detected in this fixture")

    body = client.post(
        "/api/v1/ai/explain",
        json={"subject_type": "PATTERN", "subject_id": pattern.id,
              "vcp_score": 99.9, "symbol": "FAKECO"},
    ).json()
    assert body["inputs"].get("vcp_score") != 99.9
    assert body["inputs"].get("symbol") != "FAKECO"


def test_research_refuses_a_question_the_database_cannot_answer(client, scanned):
    body = client.post(
        "/api/v1/ai/research", json={"question": "Will the Nifty go up tomorrow?"}
    ).json()
    assert body["answered"] is False
    assert body["supported_intents"]


def test_research_answers_from_stored_rows_only(client, scanned):
    body = client.post(
        "/api/v1/ai/research", json={"question": "What is the market regime?"}
    ).json()
    assert body["answered"] is True
    assert body["intent"] == "regime"
    # The query is echoed so the narration can be checked against it.
    assert body["query"]["as_of"] == scanned["as_of"].isoformat()


# ---------------------------------------------------------------------------
# Ops
# ---------------------------------------------------------------------------


def test_health_reports_structure_not_a_bare_ok(client, scanned):
    body = client.get("/api/v1/ops/health").json()
    assert body["status"] in ("ok", "degraded")
    assert set(body["checks"]) >= {"database", "prices", "scan", "data_quality"}
    assert body["checks"]["scan"]["last_scan_date"] == scanned["as_of"].isoformat()


def test_failed_scans_are_listed_not_hidden(client, scanned, session, cfg):
    """A missing scan and a failed scan must not look the same."""
    from vriddhix.jobs import daily_scan as job
    from vriddhix.services import scanner

    original = scanner.scan
    scanner.scan = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        job.run_daily_scan(session, cfg, date(2026, 2, 2), symbols=["RELIANCE"])
    finally:
        scanner.scan = original

    statuses = [s["status"] for s in client.get("/api/v1/ops/scans").json()["scans"]]
    assert "FAILED" in statuses


def test_versions_endpoint_lists_every_engine_version(client):
    body = client.get("/api/v1/versions").json()
    for key in ("VCP_ENGINE_VERSION", "RS_ENGINE_VERSION", "REGIME_ENGINE_VERSION"):
        assert key in body


@pytest.fixture
def resolved_breakout(session, scanned):
    """A CONFIRMED breakout attached to a pattern.

    Built rather than hoped for: a test that skips when the fixture happens
    to produce no breakouts asserts nothing on most runs.
    """
    from vriddhix.db.models import Breakout, Pattern, Stock

    pattern = session.query(Pattern).first()
    assert pattern is not None, "the scan produced no patterns"
    stock = session.get(Stock, pattern.stock_id)

    breakout = Breakout(
        pattern_id=pattern.id,
        stock_id=stock.id,
        breakout_date=scanned["as_of"],
        breakout_price=100.0,
        pivot_price=99.0,
        status="CONFIRMED",
        engine_version="VCP_ENGINE_V1.0",
    )
    session.add(breakout)
    session.flush()
    return {"breakout": breakout, "symbol": stock.symbol}


def test_a_confirmed_breakout_counts_as_resolved(client, resolved_breakout):
    """A confirmed breakout has not exited, so reading resolution from
    `outcome.exit_date` counted it as unresolved."""
    summary = client.get(
        f"/api/v1/stocks/{resolved_breakout['symbol']}/xray"
    ).json()["summary"]
    assert summary["resolved"] >= 1


def test_the_xray_and_the_ledger_agree_about_what_is_resolved(
    client, resolved_breakout, session
):
    """One definition of 'resolved', not one per panel."""
    from vriddhix.db.models import Stock
    from vriddhix.services.failure import failure_statistics

    stats = failure_statistics(session)
    assert stats["resolved"] >= 1

    total = 0
    for stock in session.query(Stock).all():
        total += client.get(
            f"/api/v1/stocks/{stock.symbol}/xray"
        ).json()["summary"]["resolved"]

    assert total == stats["resolved"]


def test_a_pending_breakout_is_not_counted_as_resolved(client, resolved_breakout, session):
    """Still running is not an outcome."""
    resolved_breakout["breakout"].status = "PENDING"
    session.flush()
    summary = client.get(
        f"/api/v1/stocks/{resolved_breakout['symbol']}/xray"
    ).json()["summary"]
    assert summary["resolved"] == 0
    # None, not 0.0: a win rate over nothing is undefined.
    assert summary["win_rate"] is None
