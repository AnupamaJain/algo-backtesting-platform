# VriddhiX — Repository Structure

Directories marked **[P1]** exist now (Phase 1). The rest are the planned
shape, listed so that later phases add files to a known place rather than
inventing a layout under time pressure.

```
vriddhix/
├── README.md                          [P1]
├── requirements.txt                   [P1]
├── alembic.ini                        [P1]
│
├── config/                            [P1]  no trading constant lives in code
│   ├── vriddhix.yaml                        master config: all tunables
│   └── universes/
│       └── nifty500.yaml                    universe definition + snapshots
│
├── docs/                              [P1]
│   ├── 01-system-architecture.md
│   ├── 02-repository-structure.md
│   ├── 03-database-erd.md
│   ├── 04-data-model.md
│   ├── 05-vcp-specification.md
│   ├── 06-smc-fvg-specification.md
│   ├── 07-market-regime-specification.md
│   ├── 08-rs-sector-specification.md
│   ├── 09-api-contracts.md
│   └── 10-test-strategy.md
│
├── migrations/                        [P1]  Alembic
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       └── 0001_phase1_foundation.py
│
├── state/                             [P1]  local DB + artefacts (gitignored)
│
├── src/vriddhix/
│   ├── __init__.py                    [P1]
│   ├── config.py                      [P1]  typed config loader
│   ├── versioning.py                  [P1]  engine version constants
│   │
│   ├── domain/                        [P1]  vocabulary. No I/O, no deps.
│   │   ├── __init__.py
│   │   └── types.py                         enums + frozen dataclasses
│   │
│   ├── db/                            [P1]
│   │   ├── __init__.py
│   │   ├── base.py                          engine, session, Base
│   │   └── models.py                        SQLAlchemy ORM models
│   │
│   ├── data/                          [P1]
│   │   ├── __init__.py
│   │   ├── providers.py                     OHLCVProvider ABC + impls
│   │   ├── quality.py                       validation rules
│   │   └── ingest.py                        ingestion pipeline
│   │
│   ├── universe/                      [P1]
│   │   ├── __init__.py
│   │   └── service.py                       point-in-time membership
│   │
│   ├── features/                      [P1]  L2 — pure, causal, vectorised
│   │   ├── __init__.py
│   │   └── technical.py                     EMA/ATR/returns/52w/rel-volume
│   │
│   ├── engines/                       [P2+] L3 — THE SOURCE OF TRUTH
│   │   ├── __init__.py
│   │   ├── vcp.py                     [P2]  base · contractions · pivot · score
│   │   ├── swings.py                  [P2]  shared by VCP and SMC
│   │   ├── rs.py                      [P3]  relative strength
│   │   ├── sector.py                  [P3]  sector aggregation
│   │   ├── regime.py                  [P3]  breadth and market regime
│   │   ├── smc.py                     [P4]  BOS · CHoCH · OB · liquidity
│   │   └── fvg.py                     [P5]  fair value gaps + confluence
│   │
│   ├── services/                      [P4+] L4 — stateful orchestration
│   │   ├── scanner.py                       runs engines over a universe
│   │   ├── conditions.py                    THE condition-tree evaluator
│   │   ├── lifecycle.py                     pattern state machine
│   │   ├── failure.py                       resolves open breakouts
│   │   ├── context.py                       persists RS/sector/breadth/regime
│   │   ├── structure.py                     persists SMC/FVG output
│   │   ├── xray.py                          replays the detector over history
│   │   ├── backtest.py                      replays signals into trades
│   │   └── alerts.py
│   │
│   ├── jobs/                          [P8]  L5 — scheduled work
│   │   ├── __init__.py
│   │   ├── daily_scan.py                    one run: scan → persist → alert
│   │   └── nightly.py                       catch-up backfill + scheduler
│   │
│   ├── api/                           [P6+] L6 — FastAPI. Reads only.
│   │   ├── main.py                          app assembly
│   │   ├── deps.py                          session · provenance · auth · paging
│   │   ├── auth.py                          scrypt hashing · signup · login
│   │   ├── errors.py                        the typed error envelope
│   │   ├── serialise.py                     row → JSON, in ONE place
│   │   ├── templates/                       landing · signup · learn
│   │   ├── static/                          one stylesheet
│   │   └── routers/                         market · sectors · stocks · scanners
│   │                                        breakouts · research · product
│   │                                        ai · ops · pages
│   │
│   └── ai/                            [P9]  explanation layer (no computation)
│       └── explain.py
│
├── tests/
│   ├── conftest.py                    [P1]
│   ├── golden/                        [P1]  fixed inputs, asserted outputs
│   ├── test_config.py                 [P1]
│   ├── test_domain.py                 [P1]
│   ├── test_models.py                 [P1]
│   ├── test_providers.py              [P1]
│   ├── test_quality.py                [P1]
│   ├── test_universe.py               [P1]
│   ├── test_features.py               [P1]
│   ├── test_ingest.py                 [P1]
│   ├── test_causality.py              [P1]  features may not see the future
│   ├── test_vcp.py                    [P2]  includes scan(T) invariance
│   ├── test_market_context.py         [P3]  RS · sector · breadth · regime
│   ├── test_structure.py              [P4+] swings · SMC · FVG
│   ├── test_services.py               [P6+] scanner · lifecycle · backtest · AI
│   ├── test_pipeline.py               [P8]  persistence · failure engine · job
│   ├── test_api.py                    [P6+] provenance · refusal · nulls · scoping
│   ├── test_web.py                    [P10] password handling · pages
│   └── test_engine_purity.py          [P2+] AST check: engines import nothing impure
│
└── deploy/                            [P8]  launchd agent + scheduling notes
    ├── app/
    ├── components/
    └── lib/
```

## Import rules (enforced, not advisory)

| Package | May import |
|---|---|
| `domain` | stdlib only |
| `features` | `domain`, numpy, pandas |
| `engines` | `domain`, `features`, numpy, pandas |
| `data`, `universe` | `domain`, `db`, `config`, providers |
| `services` | everything below it |
| `jobs` | `services`, `db` |
| `api` | `services`, `db`, `domain` |
| `ai` | `domain` only (plus its LLM client) |

`engines` importing `db` is the failure this layout exists to prevent: it is
the first step towards an engine that behaves differently in a backtest,
because it can reach for state that only exists live.

## Naming

- Modules and functions: `snake_case`.
- Classes, enums: `PascalCase`.
- Config keys: `snake_case`, mirroring the dataclass field they populate.
- Engine versions: `<ENGINE>_ENGINE_V<major>.<minor>` in `versioning.py`.
- Migration files: `NNNN_short_description.py`, monotonically numbered.

## Why `src/` layout

The package is installed (`pip install -e .`) rather than imported by path.
Tests therefore exercise the same import machinery production does, and a
module that only resolves because of a relative path cannot pass.
