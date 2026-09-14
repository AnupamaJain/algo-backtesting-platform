"""Point-in-time universe resolution.

This module exists to answer one question honestly:

    "Which stocks were in this index on this date?"

Answering it with *today's* membership is survivorship bias in its purest
form -- today's Nifty 500 is, by construction, the set of companies that did
not collapse. A strategy backtested against it inherits that selection and
looks better than it was.

Where real membership history exists, it is used. Where it does not, today's
list is substituted and the result is stamped ``CURRENT_UNIVERSE`` so the bias
travels with the numbers into every run, report and chart that consumes it,
instead of living in a footnote nobody reads.
"""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.types import SurvivorshipMode, UniverseResolution
from ..db.models import MarketIndex, Stock, UniverseMember, UniverseSnapshot

logger = logging.getLogger(__name__)


class UniverseError(RuntimeError):
    pass


class UniverseService:
    def __init__(self, session: Session, *, allow_current_fallback: bool = True) -> None:
        self.session = session
        self.allow_current_fallback = allow_current_fallback

    # -- reference -------------------------------------------------------

    def get_index(self, code: str) -> MarketIndex:
        index = self.session.scalar(select(MarketIndex).where(MarketIndex.code == code))
        if index is None:
            raise UniverseError(f"unknown index: {code!r}")
        return index

    # -- resolution ------------------------------------------------------

    def members_as_of(self, index_code: str, as_of: date) -> UniverseResolution:
        """Membership as it stood on ``as_of``.

        A member is included when its interval covers the date:
        ``effective_from <= as_of < effective_to`` (open-ended ``effective_to``
        meaning still a member). The upper bound is exclusive so that a stock
        removed *on* a date is not counted as present that day.
        """
        index = self.get_index(index_code)

        rows = self.session.execute(
            select(Stock.symbol, UniverseMember.source)
            .join(UniverseMember, UniverseMember.stock_id == Stock.id)
            .where(
                UniverseMember.index_id == index.id,
                UniverseMember.effective_from <= as_of,
                (UniverseMember.effective_to.is_(None)) | (UniverseMember.effective_to > as_of),
            )
            .order_by(Stock.symbol)
        ).all()

        if rows:
            symbols = tuple(r[0] for r in rows)
            # A membership set built entirely from backfilled rows is today's
            # list wearing a date. Report it as such.
            synthetic = all(r[1] == "current_only" for r in rows)
            if synthetic:
                return UniverseResolution(
                    index_code=index_code,
                    as_of=as_of,
                    symbols=symbols,
                    mode=SurvivorshipMode.CURRENT_UNIVERSE,
                    warning=(
                        f"Membership for {index_code} is backfilled from the current "
                        f"constituent list; results for {as_of.isoformat()} may overstate "
                        "performance through survivorship bias."
                    ),
                )
            return UniverseResolution(
                index_code=index_code,
                as_of=as_of,
                symbols=symbols,
                mode=SurvivorshipMode.POINT_IN_TIME,
            )

        if not self.allow_current_fallback:
            raise UniverseError(
                f"no membership history for {index_code} as of {as_of} and "
                "current-universe fallback is disabled"
            )

        current = self.current_members(index_code)
        logger.warning(
            "no membership history for %s as of %s; falling back to current list (%d symbols)",
            index_code, as_of, len(current),
        )
        return UniverseResolution(
            index_code=index_code,
            as_of=as_of,
            symbols=current,
            mode=SurvivorshipMode.CURRENT_UNIVERSE,
            warning=(
                f"No membership history for {index_code} on or before "
                f"{as_of.isoformat()}; the current constituent list was used. "
                "Results may overstate performance through survivorship bias."
            ),
        )

    def current_members(self, index_code: str) -> tuple[str, ...]:
        index = self.get_index(index_code)
        rows = self.session.execute(
            select(Stock.symbol)
            .join(UniverseMember, UniverseMember.stock_id == Stock.id)
            .where(
                UniverseMember.index_id == index.id,
                UniverseMember.effective_to.is_(None),
            )
            .order_by(Stock.symbol)
        ).all()
        return tuple(r[0] for r in rows)

    # -- mutation --------------------------------------------------------

    def add_member(
        self,
        index_code: str,
        symbol: str,
        effective_from: date,
        effective_to: date | None = None,
        source: str = "declared",
    ) -> UniverseMember:
        index = self.get_index(index_code)
        stock = self.session.scalar(select(Stock).where(Stock.symbol == symbol))
        if stock is None:
            raise UniverseError(f"unknown symbol: {symbol!r}")

        member = UniverseMember(
            index_id=index.id,
            stock_id=stock.id,
            effective_from=effective_from,
            effective_to=effective_to,
            source=source,
        )
        self.session.add(member)
        self.session.flush()
        return member

    def remove_member(self, index_code: str, symbol: str, effective_to: date) -> None:
        """Close a membership interval.

        Closing rather than deleting: the row is the evidence that the stock
        *was* a member, which is precisely what a historical backtest needs.
        """
        index = self.get_index(index_code)
        stock = self.session.scalar(select(Stock).where(Stock.symbol == symbol))
        if stock is None:
            raise UniverseError(f"unknown symbol: {symbol!r}")

        member = self.session.scalar(
            select(UniverseMember)
            .where(
                UniverseMember.index_id == index.id,
                UniverseMember.stock_id == stock.id,
                UniverseMember.effective_to.is_(None),
            )
            .order_by(UniverseMember.effective_from.desc())
        )
        if member is None:
            raise UniverseError(f"{symbol} is not a current member of {index_code}")
        member.effective_to = effective_to
        self.session.flush()

    # -- snapshots -------------------------------------------------------

    def create_snapshot(self, index_code: str, as_of: date, version: str) -> UniverseSnapshot:
        """Freeze a resolution under a name so a backtest can be re-run exactly.

        ``is_complete=False`` records that the snapshot was built without real
        membership history; any run referencing it must surface the warning.
        """
        resolution = self.members_as_of(index_code, as_of)
        index = self.get_index(index_code)

        snapshot = UniverseSnapshot(
            index_id=index.id,
            version=version,
            as_of=as_of,
            member_count=len(resolution.symbols),
            is_complete=resolution.mode is SurvivorshipMode.POINT_IN_TIME,
        )
        self.session.add(snapshot)
        self.session.flush()
        return snapshot
