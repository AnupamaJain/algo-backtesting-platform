"""The contract table, and where it comes from.

sync_instruments used to read kite.instruments(), which under the adapter
layer returns the configured universe and nothing else: 23 cash rows, with
segment, expiry and strike all NULL because the shim's instrument dict
never carried them. Every F&O query against this table therefore matched
nothing -- no expiry dropdowns, no days_to_expiry, no theta, and
"Instrument NIFTY26O0623450CE not found in cache or database" from a
strategy asked to trade a contract the interface had just offered it.

It builds from Dhan's published scrip master now: complete, and needing no
broker session, which is the point -- this table is what the app falls
back on when a token has expired.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import instrument_cache as ic

MASTER = Path(ic.DHAN_SCRIP_MASTER)
pytestmark = pytest.mark.skipif(
    not MASTER.exists(), reason="the scrip master is a cached download, not in the repository"
)


@pytest.fixture(scope="module")
def rows():
    return ic.instruments_from_dhan_master()


def test_the_master_carries_derivatives_not_just_cash(rows):
    """23 cash rows was the bug."""
    segments = {r["segment"] for r in rows}
    assert {"NFO-OPT", "NFO-FUT", "BFO-OPT", "NSE"} <= segments, sorted(segments)
    assert sum(1 for r in rows if r["segment"] == "NFO-OPT") > 10_000


def test_every_row_carries_what_the_queries_filter_on(rows):
    """segment, expiry and strike were all NULL, so nothing ever matched."""
    for row in rows:
        assert row["segment"], row
        assert row["exchange"], row
        assert row["instrument_token"], row
        if row["segment"].endswith("-OPT"):
            assert row["expiry"], row
            assert row["strike"] is not None, row
            assert row["instrument_type"] in ("CE", "PE"), row


def test_symbols_are_written_the_way_the_exchange_writes_them(rows):
    """The terminal, the strategies and the dropdowns all speak this."""
    by_symbol = {r["tradingsymbol"]: r for r in rows}

    weekly = by_symbol.get("NIFTY26O0623450CE")
    assert weekly, "a weekly contract is missing"
    assert weekly["instrument_token"] == 40757
    assert weekly["expiry"] == "2026-10-06"
    assert weekly["lot_size"] == 65

    assert "NIFTY26OCT23000CE" in by_symbol, "a monthly contract is missing"
    assert by_symbol["RELIANCE-EQ"]["segment"] == "NSE"


def test_a_reused_security_id_does_not_fail_the_whole_insert(rows):
    """instrument_token is the primary key and Dhan reuses ids across
    exchanges; first one wins rather than the sync aborting."""
    tokens = [r["instrument_token"] for r in rows]
    assert len(tokens) == len(set(tokens))


def test_an_absent_master_is_not_a_crash(tmp_path):
    """The sync falls back to the broker's own list, and says so."""
    assert ic.instruments_from_dhan_master(str(tmp_path / "nothing.csv")) == []


def test_expiries_keep_their_documented_format():
    """The table declares `expiry DATE` and the connection parses types, so
    sqlite hands back date objects -- which JSON renders as "Tue, 06 Oct
    2026 00:00:00 GMT", filling every expiry dropdown with HTTP timestamps."""
    dates = ic.get_upcoming_expiries("NIFTY", 2)
    if not dates:
        pytest.skip("no NIFTY contracts in the local cache")
    for value in dates:
        assert isinstance(value, str), f"{value!r} is not a string"
        assert len(value) == 10 and value[4] == "-" and value[7] == "-", value


def test_every_expiry_reader_returns_a_string():
    """One helper, because this bug arrived twice in an hour.

    The table declares `expiry DATE` and the connection parses types, so
    sqlite returns date objects while every function here documents a
    string. With an empty table nobody saw it; with a full one it showed up
    as "Tue, 06 Oct 2026 00:00:00 GMT" in the expiry dropdowns and as
    "strptime() argument 1 must be str, not datetime.date" from the
    amplitude table.
    """
    upcoming = ic.get_upcoming_expiries("NIFTY", 2)
    if not upcoming:
        pytest.skip("no NIFTY contracts in the local cache")

    for value in upcoming:
        assert isinstance(value, str), f"{value!r} is not a string"

    nxt = ic.get_next_expiry("NIFTY", upcoming[0])
    assert nxt is None or isinstance(nxt, str), f"{nxt!r} is not a string"


def test_the_iso_helper_takes_either_shape():
    import datetime as _dt

    assert ic._as_iso(_dt.date(2026, 10, 6)) == "2026-10-06"
    assert ic._as_iso("2026-10-06") == "2026-10-06"
    assert ic._as_iso(None) is None
