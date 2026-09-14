"""Point-in-time universe resolution.

These tests guard the difference between measuring a strategy and measuring
the fact that today's index members are the companies that survived.
"""

from __future__ import annotations

from datetime import date

import pytest

from vriddhix.domain.types import SurvivorshipMode
from vriddhix.universe.service import UniverseError, UniverseService


@pytest.fixture
def service(session, seeded):
    return UniverseService(session)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_resolves_membership_as_of_a_past_date(service):
    result = service.members_as_of("NIFTY500", date(2021, 6, 1))
    assert result.mode is SurvivorshipMode.POINT_IN_TIME
    # INFY only joined in 2022; DELISTED was still a member in 2021.
    assert set(result.symbols) == {"RELIANCE", "TCS", "DELISTED"}


def test_a_stock_that_left_the_index_still_appears_in_history(service):
    """This is the survivorship test. DELISTED was removed in 2023; a 2021
    backtest that cannot see it is measuring survivors only."""
    result = service.members_as_of("NIFTY500", date(2021, 1, 1))
    assert "DELISTED" in result.symbols


def test_a_stock_that_left_is_absent_after_its_removal(service):
    result = service.members_as_of("NIFTY500", date(2024, 1, 1))
    assert "DELISTED" not in result.symbols
    assert "INFY" in result.symbols


def test_membership_upper_bound_is_exclusive(service):
    """A stock removed on date D is not a member on D."""
    assert "DELISTED" in service.members_as_of("NIFTY500", date(2023, 6, 30)).symbols
    assert "DELISTED" not in service.members_as_of("NIFTY500", date(2023, 7, 1)).symbols


def test_a_stock_is_absent_before_it_joined(service):
    assert "INFY" not in service.members_as_of("NIFTY500", date(2021, 1, 1)).symbols
    assert "INFY" in service.members_as_of("NIFTY500", date(2022, 6, 1)).symbols


def test_current_members_excludes_removed_names(service):
    current = service.current_members("NIFTY500")
    assert "DELISTED" not in current
    assert set(current) == {"RELIANCE", "TCS", "INFY"}


# ---------------------------------------------------------------------------
# Survivorship fallback
# ---------------------------------------------------------------------------


def test_dates_before_any_history_fall_back_and_are_flagged(service):
    """The bias must travel with the numbers, not live in a footnote."""
    result = service.members_as_of("NIFTY500", date(2005, 1, 1))
    assert result.mode is SurvivorshipMode.CURRENT_UNIVERSE
    assert result.is_biased is True
    assert "survivorship" in result.warning.lower()


def test_fallback_can_be_refused(session, seeded):
    """A research run that must be point-in-time can demand it and get an
    error rather than a quietly biased answer."""
    strict = UniverseService(session, allow_current_fallback=False)
    with pytest.raises(UniverseError, match="fallback is disabled"):
        strict.members_as_of("NIFTY500", date(2005, 1, 1))


def test_membership_built_only_from_backfill_is_flagged(session, seeded):
    """Rows sourced 'current_only' are today's list wearing a date."""
    from vriddhix.db.models import MarketIndex, Stock, UniverseMember

    index = MarketIndex(code="NIFTYNEXT50", name="Nifty Next 50")
    session.add(index)
    session.flush()
    session.add(
        UniverseMember(
            index_id=index.id,
            stock_id=seeded["stocks"]["TCS"].id,
            effective_from=date(2015, 1, 1),
            source="current_only",
        )
    )
    session.flush()

    result = UniverseService(session).members_as_of("NIFTYNEXT50", date(2018, 1, 1))
    assert result.mode is SurvivorshipMode.CURRENT_UNIVERSE
    assert "backfilled" in result.warning


# ---------------------------------------------------------------------------
# Mutation
# ---------------------------------------------------------------------------


def test_removing_a_member_closes_the_interval_rather_than_deleting(service, session):
    from vriddhix.db.models import UniverseMember

    before = session.query(UniverseMember).count()
    service.remove_member("NIFTY500", "TCS", date(2025, 1, 1))
    after = session.query(UniverseMember).count()

    assert after == before, "the row is the evidence that TCS was a member"
    assert "TCS" in service.members_as_of("NIFTY500", date(2024, 1, 1)).symbols
    assert "TCS" not in service.members_as_of("NIFTY500", date(2025, 6, 1)).symbols


def test_adding_an_unknown_symbol_raises(service):
    with pytest.raises(UniverseError, match="unknown symbol"):
        service.add_member("NIFTY500", "NOSUCH", date(2024, 1, 1))


def test_unknown_index_raises(service):
    with pytest.raises(UniverseError, match="unknown index"):
        service.members_as_of("NOSUCHINDEX", date(2024, 1, 1))


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def test_snapshot_records_completeness(service):
    good = service.create_snapshot("NIFTY500", date(2021, 6, 1), "NIFTY500_2021H1")
    assert good.is_complete is True
    assert good.member_count == 3

    synthetic = service.create_snapshot("NIFTY500", date(2005, 1, 1), "NIFTY500_2005")
    assert synthetic.is_complete is False, "a snapshot without history must say so"
