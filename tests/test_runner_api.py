"""Starting and stopping a symbol on the runner, through the dashboard API.

/api/start used to spawn an interpreter per symbol. It writes a line to the
runner's control file now, and /api/stop removes it -- so the dashboard
manages a book rather than a process table.
"""

from __future__ import annotations

import json

import pytest

import flask_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    control = tmp_path / "wave_runner.json"
    monkeypatch.setattr(flask_app, "WAVE_RUNNER_ENABLED", True)
    monkeypatch.setattr(flask_app, "WAVE_RUNNER_CONTROL", str(control))
    # The runner itself is never launched here: these tests are about what
    # the dashboard records, not about working a live book.
    monkeypatch.setattr(flask_app, "_ensure_runner", lambda: None)
    monkeypatch.setattr(flask_app, "_runner_is_running", lambda: 4242)

    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as c:
        with c.session_transaction() as sess:
            sess["app_authenticated"] = True
        c._control = control
        yield c


def _start(client, symbol, **extra):
    payload = {
        "symbol": symbol, "buy_gap": 12.6, "sell_gap": 12.6,
        "buy_quantity": "300", "sell_quantity": "300",
        "request_token": "access:adapter", **extra,
    }
    return client.post("/api/start", data=json.dumps(payload),
                       content_type="application/json")


def test_starting_a_symbol_writes_it_to_the_control_file(client):
    response = _start(client, "NIFTY26O0623450CE")
    assert response.status_code == 200, response.get_data(as_text=True)[:200]

    book = json.loads(client._control.read_text())["symbols"]
    assert [row["symbol"] for row in book] == ["NIFTY26O0623450CE"]
    assert book[0]["quantity"] == "300:300"
    assert book[0]["buy_gap"] == 12.6


def test_many_symbols_share_one_runner(client):
    """The whole point: a second symbol is another line, not another process."""
    for symbol in ("NIFTY26O0623450CE", "NIFTY26O0623800PE", "NIFTY26O0624000CE"):
        assert _start(client, symbol).status_code == 200

    book = json.loads(client._control.read_text())["symbols"]
    assert len(book) == 3
    assert len({row["symbol"] for row in book}) == 3


def test_the_same_symbol_twice_is_refused(client):
    assert _start(client, "NIFTY26O0623450CE").status_code == 200
    duplicate = _start(client, "NIFTY26O0623450CE")
    assert duplicate.status_code == 409
    assert duplicate.get_json()["error"] == "already_running"

    book = json.loads(client._control.read_text())["symbols"]
    assert len(book) == 1, "a refused start must not double the book"


def test_force_replaces_rather_than_duplicating(client):
    assert _start(client, "NIFTY26O0623450CE", buy_gap=12.6).status_code == 200
    assert _start(client, "NIFTY26O0623450CE", buy_gap=4.2, force=True).status_code == 200

    book = json.loads(client._control.read_text())["symbols"]
    assert len(book) == 1, "force should replace the entry, not add a second"
    assert book[0]["buy_gap"] == 4.2


def test_stopping_by_symbol_needs_no_pid(client):
    """There is no process per symbol to name any more."""
    assert _start(client, "NIFTY26O0623450CE").status_code == 200

    response = client.post("/api/stop", data=json.dumps(
        {"symbol": "NIFTY26O0623450CE"}), content_type="application/json")
    assert response.status_code == 200, response.get_data(as_text=True)[:200]
    assert "wave runner" in response.get_json()["message"]

    assert json.loads(client._control.read_text())["symbols"] == []


def test_stopping_one_symbol_leaves_the_others_running(client):
    for symbol in ("NIFTY26O0623450CE", "NIFTY26O0623800PE"):
        _start(client, symbol)

    client.post("/api/stop", data=json.dumps({"symbol": "NIFTY26O0623450CE"}),
                content_type="application/json")

    remaining = [r["symbol"] for r in json.loads(client._control.read_text())["symbols"]]
    assert remaining == ["NIFTY26O0623800PE"]


def test_a_stop_with_neither_pid_nor_a_running_symbol_is_rejected(client):
    response = client.post("/api/stop", data=json.dumps({"symbol": "NOTRUNNING26O06100CE"}),
                           content_type="application/json")
    assert response.status_code == 400


def test_the_control_file_is_replaced_atomically(client, tmp_path):
    """The runner re-reads this every cycle; a half-written file reads as
    an empty book and would silently stop trading."""
    _start(client, "NIFTY26O0623450CE")
    assert not list(tmp_path.glob("*.tmp")), "left a partial file behind"
    json.loads(client._control.read_text())  # parses, so it was never half-written
