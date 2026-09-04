# Broker Abstraction Layer — Architecture Guide

**Companion to:** `broker-abstraction-architecture-diagram.svg` / `.png`
**Implements:** `broker-agnostic-abstraction-layer-PRD.md`
**Extends:** `ui-trading-system` (base platform)

This document walks the diagram layer by layer, in the order you'd actually build it — read it alongside the SVG/PNG open side by side.

---

## How to read the diagram

Six horizontal layers, top to bottom, each one only depending on the layer below it:

```
Strategy Processes  (unchanged)
        ↓
common_lib.py        (unchanged entry point, new internals)
        ↓
BrokerAdapter interface + MockAdapter
        ↓
Auth Strategy base classes  |  Unified Data Models  |  Factory + Resolution
        ↓
Seven concrete adapters (color-coded by rollout phase)
        ↓
Seven external broker APIs
```

The rule that makes this whole design work: **nothing above the interface line knows which broker is active.** If you ever find yourself writing `if broker == "dhan":` inside a strategy file or inside `common_lib.py`, that's a signal the abstraction has leaked and something belongs in an adapter instead.

---

## Layer 1 — Strategy Processes (no changes)

Survivor, Wave Extractor, Expiry Trade, Early Exit, Covered Calls. These call `common_lib.place_order()` today and must continue to after this build — that's the acceptance test for the whole project. If any of these files need to change to support a new broker, the abstraction has failed.

## Layer 2 — `common_lib.py` (internals change, contract doesn't)

Keeps its existing job: retry-with-backoff, the dry-run safety gate, order-state persistence. The only change is *what it calls* — instead of a Kite SDK call, it calls the active `BrokerAdapter`'s method. Build order: refactor this **first**, against the existing Zerodha behavior, before any second broker exists. This is Phase 0 in the PRD and it's the step that proves the abstraction doesn't break anything.

## Layer 3 — `BrokerAdapter` interface + `MockAdapter`

Nine methods, shown as the teal boxes in the diagram: `authenticate()`, `place_order()`, `modify_order()`, `cancel_order()`, `get_positions()`, `get_quote()`, `get_instruments()`, `subscribe_ws()`, `get_order_status()`. Every one of these takes and returns the unified models from Layer 4, never a broker-native object.

**Build `MockAdapter` immediately after the interface, before the first real broker adapter.** It's drawn separately in the diagram deliberately — it's not a nice-to-have test helper, it's the **release gate**: the same contract test suite that runs against `MockAdapter` must pass against every real adapter before that adapter is considered done. This is what lets you build adapters 2 through 7 with confidence instead of hoping they behave the same way.

## Layer 4 — three supporting subsystems (build these before any adapter beyond Zerodha)

| Subsystem | What it solves | Build note |
|---|---|---|
| **Auth Strategy base classes** | Three brokers redirect through OAuth, three use TOTP/credential login, one uses a static key — model the *family*, not each broker individually | `OAuthRedirectAuth`, `TOTPCredentialAuth`, `StaticAPIKeyAuth` — each concrete adapter picks one and supplies only its broker-specific config (URLs, field names) |
| **Unified Data Models** | Every broker names order types, position fields, and order states differently | `UnifiedInstrument`, `UnifiedOrder`/`UnifiedOrderStatus`, `UnifiedPosition` — get these right before writing the second adapter, because retrofitting them after two adapters exist means touching both |
| **Factory + Resolution** | Something has to decide *which* adapter(s) to instantiate, and something has to translate a canonical instrument into whatever identifier the active broker expects | `BrokerFactory` reads config; `Instrument Resolver` sits in front of the extended `instrument_cache.py` |

## Layer 5 — the seven adapters, color-coded by build order

This is the row of boxes in the diagram, each tagged with a phase number badge. The ordering is deliberate, not alphabetical:

| Phase | Adapter(s) | Why this order |
|---|---|---|
| **0** (navy) | `ZerodhaAdapter` | Not new work — it's the *existing* Kite Connect code, moved behind the interface with zero behavior change. This is your proof that the refactor is safe. |
| **2** (green) | `DhanAdapter` | Simplest auth family (`StaticAPIKeyAuth`, no redirect flow to build) — first real second broker, smallest new surface area. |
| **3** (teal) | `ShoonyaAdapter`, `FlattradeAdapter` | Both built on the NorenApi base — build them together and you get two adapters for barely more than the cost of one. |
| **4** (accent) | `FyersAdapter`, `UpstoxAdapter` | Both `OAuthRedirectAuth` — by now that base class is proven from Zerodha, so this is mostly URL/field-mapping work. |
| **5** (amber) | `AliceBlueAdapter` | `TOTPCredentialAuth` plus a 2FA variant — hold this for last since it's the one auth flow that doesn't cleanly match an existing base class. |
| **6** (red, not an adapter) | Multi-broker concurrent mode | Not a broker at all — this is the mode where Trade Journal, Position Guard, etc. get broker-tagged so more than one adapter can run at once. Deliberately last: it changes the data model, not just adds an adapter. |

Each adapter box in the diagram states which auth base class it uses — that's the fastest way to see how much a new adapter will actually cost you before starting it.

## Layer 6 — external broker APIs

The thing each adapter actually talks to. Not under your control, no build work here beyond integration — but worth noting in the diagram which SDK/protocol each one is (Kite's own SDK, Dhan's REST, NorenAPI shared by Shoonya/Flattrade, Fyers/Upstox REST). This is also where rate limits differ (NFR in the PRD) — budget adapter-specific backoff tuning per box in this row, not a single global constant.

---

## Build checklist, in diagram order

- [ ] Refactor existing Zerodha code into `ZerodhaAdapter` behind the new interface — verify zero behavior change (Phase 0)
- [ ] Define the nine-method `BrokerAdapter` interface + typed exceptions
- [ ] Build `UnifiedInstrument`, `UnifiedOrder`/`UnifiedOrderStatus`, `UnifiedPosition`
- [ ] Build `MockAdapter` + the contract test suite — this becomes the gate for everything after
- [ ] Build `OAuthRedirectAuth`, `TOTPCredentialAuth`, `StaticAPIKeyAuth` base classes
- [ ] Build `BrokerFactory` + config schema (`[broker]` section, single- and multi-broker modes)
- [ ] Extend `instrument_cache.py` with the canonical table + per-broker mapping (`Instrument Resolver`)
- [ ] `DhanAdapter` (Phase 2) — first adapter through the full contract suite
- [ ] `ShoonyaAdapter` + `FlattradeAdapter` (Phase 3)
- [ ] `FyersAdapter` + `UpstoxAdapter` (Phase 4)
- [ ] `AliceBlueAdapter` (Phase 5)
- [ ] Broker-tag Trade Journal, Position Guard, GTT Monitor; per-process broker assignment (Phase 6, multi-broker mode)

## Open items to resolve *before* starting (carried from the PRD, still unresolved here)

1. Which brokers actually offer a GTT-equivalent — Early Exit needs a per-adapter answer, not an assumption.
2. Whether Survivor's delta rebalancer operates per-broker or aggregated, once multi-broker mode exists.
3. Whether margin/hedge decisions (Survivor's `_with_buy_auto` logic) need cross-broker capital visibility in multi-broker mode.

These aren't drawn on the diagram because they're decisions, not components — resolve them before Phase 6, since they change what the Layer 4 data models need to hold.
