"""Phase 1 command line.

    python -m vriddhix.cli init                  # apply migrations
    python -m vriddhix.cli bootstrap             # seed reference data
    python -m vriddhix.cli ingest [SYMBOL ...]   # fetch, validate, persist
    python -m vriddhix.cli status                # what is in the database

Deliberately thin. Everything it does is a call into the library, so that the
daily pipeline in Phase 5 orchestrates the same functions rather than shelling
out to this.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from sqlalchemy import delete, func, select

from .config import PROJECT_ROOT, get_config
from .data.ingest import ingest_universe
from .data.providers import build_provider
from .db.base import session_scope
from .db.models import (
    DataQualityEvent,
    Industry,
    MarketIndex,
    OhlcvDaily,
    Sector,
    Stock,
    TechnicalFeature,
    UniverseMember,
)
from .universe.service import UniverseService

logger = logging.getLogger("vriddhix")

#: Minimal NSE sector map for the symbols in the local cache. Real deployments
#: load this from an exchange master; it is inlined here only so that Phase 1
#: is runnable without an external dependency.
SECTOR_MAP: dict[str, tuple[str, str]] = {
    "RELIANCE": ("ENERGY", "REFINING"),
    "ONGC": ("ENERGY", "OIL_EXPLORATION"),
    "BPCL": ("ENERGY", "REFINING"),
    "TCS": ("IT", "SOFTWARE"),
    "INFY": ("IT", "SOFTWARE"),
    "WIPRO": ("IT", "SOFTWARE"),
    "HCLTECH": ("IT", "SOFTWARE"),
    "TECHM": ("IT", "SOFTWARE"),
    "HDFCBANK": ("FINANCIALS", "BANKS"),
    "ICICIBANK": ("FINANCIALS", "BANKS"),
    "SBIN": ("FINANCIALS", "BANKS"),
    "KOTAKBANK": ("FINANCIALS", "BANKS"),
    "AXISBANK": ("FINANCIALS", "BANKS"),
    "BANKBARODA": ("FINANCIALS", "BANKS"),
    "INDUSINDBK": ("FINANCIALS", "BANKS"),
    "BAJFINANCE": ("FINANCIALS", "NBFC"),
    "HINDUNILVR": ("FMCG", "HOUSEHOLD"),
    "ITC": ("FMCG", "TOBACCO"),
    "NESTLEIND": ("FMCG", "PACKAGED_FOODS"),
    "BRITANNIA": ("FMCG", "PACKAGED_FOODS"),
    "MARUTI": ("AUTO", "PASSENGER_VEHICLES"),
    "M&M": ("AUTO", "PASSENGER_VEHICLES"),
    "BAJAJ-AUTO": ("AUTO", "TWO_WHEELERS"),
    "HEROMOTOCO": ("AUTO", "TWO_WHEELERS"),
    "EICHERMOT": ("AUTO", "TWO_WHEELERS"),
    "SUNPHARMA": ("PHARMA", "PHARMACEUTICALS"),
    "CIPLA": ("PHARMA", "PHARMACEUTICALS"),
    "DRREDDY": ("PHARMA", "PHARMACEUTICALS"),
    "DIVISLAB": ("PHARMA", "PHARMACEUTICALS"),
    "TITAN": ("CONSUMER_DURABLES", "JEWELLERY"),
    "ASIANPAINT": ("CHEMICALS", "PAINTS"),
    "ULTRACEMCO": ("MATERIALS", "CEMENT"),
    "GRASIM": ("MATERIALS", "CEMENT"),
    "SHREECEM": ("MATERIALS", "CEMENT"),
    "JSWSTEEL": ("METALS", "STEEL"),
    "TATASTEEL": ("METALS", "STEEL"),
    "HINDALCO": ("METALS", "ALUMINIUM"),
    "COALINDIA": ("METALS", "MINING"),
    "LT": ("CAPGOODS", "CONSTRUCTION"),
    "BHARTIARTL": ("TELECOM", "TELECOM_SERVICES"),
    "NTPC": ("UTILITIES", "POWER_GENERATION"),
    "POWERGRID": ("UTILITIES", "POWER_TRANSMISSION"),
    "ADANIENT": ("CONGLOMERATE", "DIVERSIFIED"),
    "ADANIPORTS": ("INFRASTRUCTURE", "PORTS"),
}

SECTOR_NAMES = {
    "ENERGY": "Energy", "IT": "Information Technology", "FINANCIALS": "Financials",
    "FMCG": "Fast Moving Consumer Goods", "AUTO": "Automobiles", "PHARMA": "Pharmaceuticals",
    "CONSUMER_DURABLES": "Consumer Durables", "CHEMICALS": "Chemicals",
    "MATERIALS": "Materials", "METALS": "Metals & Mining", "CAPGOODS": "Capital Goods",
    "TELECOM": "Telecommunications", "UTILITIES": "Utilities",
    "CONGLOMERATE": "Conglomerates", "INFRASTRUCTURE": "Infrastructure",
    "ETF": "Exchange Traded Funds",
}


def discover_cached_symbols(cache_dir: Path) -> list[str]:
    """Symbols available in the local cache, normalised to plain NSE symbols."""
    if not cache_dir.is_dir():
        return []
    return sorted(
        {p.stem.removesuffix("-EQ") for p in cache_dir.glob("*.csv") if not p.stem.endswith(".meta")}
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init(_args) -> int:
    from alembic import command
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    print(f"schema applied -> {get_config().database_url}")
    return 0


def load_universe_file(path: Path) -> tuple[str, str, list[dict]]:
    """Read a universe definition: index identity plus classified members.

    Returns ``(index_code, index_name, members)``. A file like this is the
    difference between a real constituent list and whatever happened to be
    cached on disk -- and it carries the sector for each name, so nothing
    falls back to a placeholder classification.
    """
    import yaml

    data = yaml.safe_load(path.read_text())
    index = data.get("index") or {}
    members = data.get("members") or []
    return index.get("code", "UNIVERSE"), index.get("name", "Universe"), members


def cmd_bootstrap(args) -> int:
    """Seed sectors, industries, stocks and index membership."""
    cfg = get_config()

    classified: dict[str, tuple[str, str]] = {}
    display_names: dict[str, str] = {}
    index_name = None

    if args.universe:
        path = Path(args.universe)
        if not path.is_absolute():
            path = PROJECT_ROOT / "config" / "universes" / path.name
        if not path.exists():
            print(f"no universe file at {path}", file=sys.stderr)
            return 1
        index_code, index_name, members = load_universe_file(path)
        symbols = [m["symbol"] for m in members]
        classified = {m["symbol"]: (m["sector"], m["industry"]) for m in members}
        display_names = {m["symbol"]: m.get("name") or m["symbol"] for m in members}
        print(f"loaded {len(symbols)} members from {path.name} ({index_code})")
    else:
        symbols = discover_cached_symbols(cfg.cache_dir)
        if not symbols:
            print(f"no cached symbols found in {cfg.cache_dir}", file=sys.stderr)
            return 1
        index_code = cfg.get("universe.default_index")

    with session_scope() as session:
        sectors: dict[str, Sector] = {}
        industries: dict[tuple[str, str], Industry] = {}

        for symbol in symbols:
            sector_code, industry_code = classified.get(
                symbol, SECTOR_MAP.get(symbol, ("ETF", "INDEX_FUND"))
            )

            if sector_code not in sectors:
                existing = session.scalar(select(Sector).where(Sector.code == sector_code))
                if existing is None:
                    existing = Sector(code=sector_code,
                                      name=SECTOR_NAMES.get(sector_code, sector_code.title()))
                    session.add(existing)
                    session.flush()
                sectors[sector_code] = existing

            key = (sector_code, industry_code)
            if key not in industries:
                existing = session.scalar(
                    select(Industry).where(
                        Industry.sector_id == sectors[sector_code].id,
                        Industry.code == industry_code,
                    )
                )
                if existing is None:
                    existing = Industry(
                        sector_id=sectors[sector_code].id,
                        code=industry_code,
                        name=industry_code.replace("_", " ").title(),
                    )
                    session.add(existing)
                    session.flush()
                industries[key] = existing

            existing_stock = session.scalar(select(Stock).where(Stock.symbol == symbol))
            if existing_stock is None:
                session.add(Stock(
                    symbol=symbol,
                    name=display_names.get(symbol) or symbol.replace("-", " ").title(),
                    exchange="NSE", industry_id=industries[key].id,
                ))
            elif symbol in classified:
                # A reclassification corrects the row rather than leaving a
                # stale sector behind: sector rotation reads this, and a
                # bank filed under ETF would quietly distort the aggregate.
                existing_stock.industry_id = industries[key].id
                if display_names.get(symbol):
                    existing_stock.name = display_names[symbol]
        session.flush()

        index = session.scalar(select(MarketIndex).where(MarketIndex.code == index_code))
        if index is None:
            index = MarketIndex(code=index_code,
                                name=index_name or index_code, is_benchmark=True)
            session.add(index)
            session.flush()

        service = UniverseService(session)
        added = 0
        for symbol in symbols:
            stock = session.scalar(select(Stock).where(Stock.symbol == symbol))
            exists = session.scalar(
                select(UniverseMember).where(
                    UniverseMember.index_id == index.id,
                    UniverseMember.stock_id == stock.id,
                )
            )
            if exists is None:
                # 'current_only': this is today's list backfilled, not real
                # membership history. Resolution will flag it accordingly
                # rather than pretending the dates are meaningful.
                service.add_member(index_code, symbol, args.effective_from, source="current_only")
                added += 1

        print(f"bootstrapped {len(symbols)} symbols, {len(sectors)} sectors, "
              f"{added} new index memberships")
    return 0


def cmd_ingest(args) -> int:
    cfg = get_config()
    provider = build_provider(cfg)

    # Without an explicit start, honour the configured lookback. Leaving it
    # None lets yfinance apply its own default of one month, which silently
    # produces a frame too short to compute a 200-day EMA from -- a quiet
    # wrong answer rather than a loud failure.
    start = args.start
    if start is None:
        years = int(cfg.get("data.lookback_years", 10))
        start = date.today() - timedelta(days=int(years * 365.25))

    with session_scope() as session:
        symbols = args.symbols or [s for (s,) in session.execute(select(Stock.symbol)).all()]
        if not symbols:
            print("no symbols; run `bootstrap` first", file=sys.stderr)
            return 1

        print(f"fetching {len(symbols)} symbols from {start} …")
        report = ingest_universe(
            session, symbols, provider, cfg,
            start=start, end=args.end, as_of=args.as_of or date.today(),
        )

        print(f"\n{report.summary()}")
        if report.failed:
            print("\nfailed:")
            for result in report.failed:
                print(f"  {result.symbol:<14} {result.error}")
    return 0 if not report.failed else 2


def cmd_revalidate(args) -> int:
    """Re-run the quality rules over stored bars, without re-fetching.

    A rule added after an ingest would otherwise only ever apply to data
    arriving later, leaving the existing decade unexamined by it. Nothing is
    corrected and no bar is touched -- this only records what the rules find,
    replacing the previous findings for each symbol so repeated runs do not
    accumulate duplicates of the same event.
    """
    from .data.quality import validate_from_config
    from .db.models import DataQualityEvent

    with session_scope() as session:
        symbols = args.symbols or [s for (s,) in session.execute(
            select(Stock.symbol).order_by(Stock.symbol)).all()]
        if not symbols:
            print("no symbols; run `bootstrap` first", file=sys.stderr)
            return 1

        found: dict[str, int] = {}
        for symbol in symbols:
            stock_id = session.scalar(select(Stock.id).where(Stock.symbol == symbol))
            if stock_id is None:
                continue
            rows = session.execute(
                select(OhlcvDaily.date, OhlcvDaily.open, OhlcvDaily.high,
                       OhlcvDaily.low, OhlcvDaily.close, OhlcvDaily.volume)
                .where(OhlcvDaily.stock_id == stock_id)
                .order_by(OhlcvDaily.date)
            ).all()
            if len(rows) < 3:
                continue

            frame = pd.DataFrame(
                rows, columns=["date", "open", "high", "low", "close", "volume"]
            )
            frame["date"] = pd.to_datetime(frame["date"])
            frame = frame.set_index("date").astype(float)

            _, findings = validate_from_config(frame, symbol, get_config())

            session.execute(
                delete(DataQualityEvent).where(DataQualityEvent.stock_id == stock_id)
            )
            for finding in findings:
                session.add(DataQualityEvent(
                    stock_id=stock_id,
                    date=finding.bar_date,
                    severity=finding.severity.value,
                    category=finding.issue.value,
                    detail=json.dumps(finding.detail) if finding.detail else None,
                ))
            if findings:
                found[symbol] = len(findings)
            session.commit()

        errors = session.execute(
            select(DataQualityEvent.category, func.count())
            .where(DataQualityEvent.severity == "ERROR")
            .group_by(DataQualityEvent.category)
        ).all()

        print(f"revalidated {len(symbols)} symbols · "
              f"{sum(found.values())} findings on {len(found)} of them")
        if errors:
            print("\nerrors:")
            for category, count in errors:
                print(f"  {category:<18} {count}")
    return 0


def cmd_status(_args) -> int:
    with session_scope() as session:
        counts = {
            "sectors": session.scalar(select(func.count()).select_from(Sector)),
            "industries": session.scalar(select(func.count()).select_from(Industry)),
            "stocks": session.scalar(select(func.count()).select_from(Stock)),
            "bars": session.scalar(select(func.count()).select_from(OhlcvDaily)),
            "features": session.scalar(select(func.count()).select_from(TechnicalFeature)),
            "quality events": session.scalar(select(func.count()).select_from(DataQualityEvent)),
        }
        print(f"database: {get_config().database_url}\n")
        for label, value in counts.items():
            print(f"  {label:<16} {value:>9,}")

        span = session.execute(
            select(func.min(OhlcvDaily.date), func.max(OhlcvDaily.date))
        ).first()
        if span and span[0]:
            print(f"\n  coverage       {span[0]} .. {span[1]}")

        rows = session.execute(
            select(DataQualityEvent.category, DataQualityEvent.severity, func.count())
            .group_by(DataQualityEvent.category, DataQualityEvent.severity)
            .order_by(func.count().desc())
        ).all()
        if rows:
            print("\n  data quality")
            for category, severity, count in rows:
                print(f"    {severity:<6} {category:<20} {count:>6,}")
    return 0


def cmd_scan(args) -> int:
    """Run the daily pipeline for one date, or backfill a range."""
    import pandas as pd

    from .db.models import OhlcvDaily
    from .jobs.daily_scan import backfill, run_daily_scan, summarise

    cfg = get_config()
    with session_scope() as session:
        as_of = args.as_of or session.scalar(select(func.max(OhlcvDaily.date)))
        if as_of is None:
            print("No price history. Run `vriddhix ingest` first.")
            return 1

        if args.since:
            # Oldest first: RS trend and regime hysteresis both read the
            # previous stored value, so replaying out of order would compute
            # each day against a future baseline.
            dates = [
                d for (d,) in session.execute(
                    select(OhlcvDaily.date)
                    .where(OhlcvDaily.date >= args.since, OhlcvDaily.date <= as_of)
                    .distinct()
                    .order_by(OhlcvDaily.date)
                ).all()
            ]
            reports = backfill(
                session, cfg, dates,
                index_code=args.index, with_structure=not args.no_structure,
            )
            for report in reports:
                print(summarise(report))
            return 0 if all(r.ok for r in reports) else 1

        report = run_daily_scan(
            session, cfg, as_of,
            index_code=args.index, with_structure=not args.no_structure,
        )
        print(summarise(report))
        if report.context:
            print(
                f"  context: {report.context.rs_rows} RS, "
                f"{report.context.sector_rows} sectors, "
                f"{report.context.regime_rows} regime"
            )
        if report.survivorship_warning:
            print(f"  WARNING: {report.survivorship_warning}")
        for error in report.errors:
            print(f"  ERROR: {error}")
        return 0 if report.ok else 1


def cmd_backfill(args) -> int:
    """Fetch new bars, then scan every session still missing a scan."""
    from .jobs.nightly import describe, run_once, serve_scheduler

    if args.schedule:
        serve_scheduler(
            hour=args.hour, minute=args.minute,
            since=args.since, ingest=not args.no_ingest,
            max_catchup_days=args.max_catchup,
            index_code=args.index, with_structure=not args.no_structure,
        )
        return 0

    report = run_once(
        since=args.since, ingest=not args.no_ingest,
        max_catchup_days=args.max_catchup,
        index_code=args.index, with_structure=not args.no_structure,
    )
    print(describe(report))
    if report.skipped_beyond_cap:
        oldest = report.skipped_beyond_cap[0]
        print(f"  {len(report.skipped_beyond_cap)} older sessions were not scanned.")
        print(f"  To replay them: vriddhix backfill --since {oldest} --max-catchup 0")
    for error in report.errors:
        print(f"  ERROR: {error}")
    return 0 if report.ok else 1


def cmd_backtests(args) -> int:
    """Run queued backtests."""
    from .jobs.backtest_worker import describe, run_once

    report = run_once(limit=args.limit)
    print(describe(report))
    for error in report.errors:
        print(f"  ERROR: {error}")
    return 0 if report.ok else 1


def cmd_serve(args) -> int:
    """Run the read API."""
    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed. pip install 'uvicorn[standard]'")
        return 1

    uvicorn.run(
        "vriddhix.api.main:app", host=args.host, port=args.port, reload=args.reload
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vriddhix", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="apply migrations").set_defaults(func=cmd_init)

    revalidate = sub.add_parser(
        "revalidate", help="re-run quality rules over stored bars")
    revalidate.add_argument("symbols", nargs="*", help="default: every symbol")
    revalidate.set_defaults(func=cmd_revalidate)

    bootstrap = sub.add_parser("bootstrap", help="seed reference data")
    bootstrap.add_argument("--universe", default=None,
                           help="universe YAML in config/universes/ (e.g. nse_fno.yaml)")
    bootstrap.add_argument("--effective-from", type=date.fromisoformat, default=date(2015, 1, 1))
    bootstrap.set_defaults(func=cmd_bootstrap)

    ingest = sub.add_parser("ingest", help="fetch, validate and persist bars")
    ingest.add_argument("symbols", nargs="*")
    ingest.add_argument("--start", type=date.fromisoformat)
    ingest.add_argument("--end", type=date.fromisoformat)
    ingest.add_argument("--as-of", type=date.fromisoformat)
    ingest.set_defaults(func=cmd_ingest)

    scan = sub.add_parser("scan", help="run the daily pipeline")
    scan.add_argument("--as-of", type=date.fromisoformat,
                      help="scan date (default: the latest bar)")
    scan.add_argument("--since", type=date.fromisoformat,
                      help="backfill every trading day from this date")
    scan.add_argument("--index", default=None)
    scan.add_argument("--no-structure", action="store_true",
                      help="skip SMC/FVG analysis")
    scan.set_defaults(func=cmd_scan)

    backfill = sub.add_parser(
        "backfill", help="fetch new bars and scan every missing session"
    )
    backfill.add_argument("--since", type=date.fromisoformat,
                          help="only consider sessions from this date")
    backfill.add_argument("--no-ingest", action="store_true",
                          help="scan only; do not fetch bars")
    backfill.add_argument("--no-structure", action="store_true")
    backfill.add_argument("--index", default=None)
    backfill.add_argument("--max-catchup", type=int, default=15,
                          help="cap sessions per run; 0 means no cap")
    backfill.add_argument("--schedule", action="store_true",
                          help="stay resident and run on a weekday cron")
    backfill.add_argument("--hour", type=int, default=19)
    backfill.add_argument("--minute", type=int, default=0)
    backfill.set_defaults(func=cmd_backfill)

    backtests = sub.add_parser("backtests", help="run queued backtests")
    backtests.add_argument("--limit", type=int, default=5)
    backtests.set_defaults(func=cmd_backtests)

    serve = sub.add_parser("serve", help="run the read API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    sub.add_parser("status", help="what is in the database").set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
