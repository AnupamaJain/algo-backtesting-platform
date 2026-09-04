# Product Requirements Document
## Broker Abstraction Layer — Making the Platform Multi-Broker

| | |
|---|---|
| **Status** | Draft — new capability, extends the base platform PRD |
| **Parent system** | `ui-trading-system` (see `algorithmic-options-trading-platform-PRD.md`) |
| **Target brokers (v1 scope)** | Zerodha (existing), Dhan, Fyers, Upstox, Alice Blue, Shoonya (Finvasia), Flattrade |
| **Owner** | _(assign)_ |
| **Last updated** | _(fill in)_ |
| **Audience** | Engineering, whoever owns broker relationships/API keys, risk reviewer |

> **How to read this PRD:** Same convention as the base platform PRD. **[New]** = a requirement this document introduces that doesn't exist in the source system today. **[Migration]** = an existing requirement from the base PRD that must change to support this feature. Nothing here is copied from broker documentation directly — field names, auth flow shapes, and product-type conventions are drawn from public developer-facing differences between these brokers and should be verified against each broker's current API docs before implementation, since broker APIs change.

---

## 1. Problem statement

The platform's order execution, position tracking, and market data currently assume a single broker (Zerodha Kite Connect) at every layer — `common_lib.place_order()` calls the Kite SDK directly, `instrument_cache.py` resolves Zerodha's `instrument_token` scheme, and Greeks/positions assume Kite's response shape. An operator who wants to trade through Dhan, Fyers, Upstox, Alice Blue, Shoonya, or Flattrade — whether to consolidate to a cheaper broker, distribute execution risk across brokers, or simply because that's where their capital already sits — cannot do so without forking the codebase per broker.

## 2. Goals

| Goal | Metric |
|---|---|
| Decouple every strategy from any specific broker's SDK/API shape | Zero direct broker-SDK imports outside the adapter layer |
| Support adding a new broker without touching strategy code | A new adapter implementing the interface is sufficient; `place_order_at_nifty.py`, `ticker_single_scraper_new.py`, etc. require no changes |
| Preserve all existing safety guarantees across every broker | Dry-run gate, retry-with-backoff, and state persistence behave identically regardless of active broker |
| Let the operator choose single-broker or multi-broker operation | Both modes configurable without code changes |
| Normalize instrument identity across brokers | One canonical instrument key (underlying + expiry + strike + option type) resolves correctly for whichever broker(s) are active |

### Non-goals
- **Not** building a broker-comparison/best-execution router that automatically picks the cheapest/fastest broker per order — routing is operator-configured, not automatic, in this scope.
- **Not** supporting brokers outside the seven listed above in v1 — the interface should make adding more brokers straightforward, but this PRD scopes exactly these seven.
- **Not** changing any strategy's trading logic (Survivor, Wave Extractor, etc.) — this is purely an execution/data-layer abstraction.

## 3. Users & personas

| Persona | Need |
|---|---|
| **Operator** | Pick their broker(s) in config, keep using every existing strategy unmodified |
| **Maintainer adding a new broker** | Clear interface contract, a reference adapter to copy, a test suite that proves compliance before shipping |
| **Multi-broker operator** | Run different strategies against different brokers concurrently, with position/journal visibility unified across all of them |

## 4. Architecture overview **[New]**

```
Strategy processes (unchanged)
        │  calls common_lib.place_order(), .positions(), etc.
        ▼
common_lib.py  ──────────►  BrokerAdapter interface
                                   │
                    ┌──────────────┼───────────────┬─────────────┬─────────────┬──────────────┬───────────────┐
                    ▼              ▼                ▼             ▼             ▼              ▼               ▼
              ZerodhaAdapter  DhanAdapter     FyersAdapter  UpstoxAdapter  AliceBlueAdapter ShoonyaAdapter FlattradeAdapter
                    │              │                │             │             │              │               │
              Kite Connect     Dhan API         Fyers API     Upstox API    AliceBlue API   NorenAPI       NorenAPI
                 (SDK)         (REST/WS)        (REST/WS)     (REST/WS)      (REST/WS)      (Shoonya)      (Flattrade)

Cross-cutting:
  BrokerFactory       — reads [broker] config, instantiates the active adapter(s)
  Unified data models — UnifiedOrder, UnifiedPosition, UnifiedQuote, UnifiedInstrument
  Instrument Resolver — canonical instrument key -> broker-specific identifier, per active broker
  Auth Strategy base  — OAuthRedirectAuth | TOTPCredentialAuth | StaticAPIKeyAuth
```

## 5. Functional requirements

### 5.1 `BrokerAdapter` interface **[New]**

| ID | Requirement |
|---|---|
| FR-B1 | Interface must define, at minimum: `authenticate()`, `place_order()`, `modify_order()`, `cancel_order()`, `get_positions()`, `get_quote()`, `get_instruments()`, `subscribe_ws()`, `get_order_status()`. |
| FR-B2 | Every method must accept/return the platform's unified data models (§5.3), never a broker-native object, so strategy code never needs broker-specific branching. |
| FR-B3 | `place_order()` must preserve the existing dry-run contract from the base PRD (FR-2.3–2.4): the safety gate check happens once, in `common_lib`, before any adapter is called — not duplicated per-adapter. |
| FR-B4 | Every adapter must raise a common set of typed exceptions (`AuthError`, `OrderRejected`, `RateLimited`, `InstrumentNotFound`, `BrokerUnavailable`) so `retry_with_backoff()` and strategy error handling stay broker-agnostic. |
| FR-B5 | Interface must be testable via a `MockAdapter` that requires no live credentials, for CI and for strategy development without a funded account. |

### 5.2 Authentication strategy base classes **[New]**

| ID | Requirement |
|---|---|
| FR-A1 | `OAuthRedirectAuth` base must handle the redirect-based login flow shared by Zerodha, Fyers, and Upstox — differing only in endpoint URLs, token exchange payload shape, and token field names. |
| FR-A2 | `TOTPCredentialAuth` base must handle the username/password/TOTP login flow shared by Shoonya and Flattrade (both NorenApi-based) and, with a 2FA variant, Alice Blue. |
| FR-A3 | `StaticAPIKeyAuth` must handle Dhan's simpler API-key + access-token model (no interactive redirect required). |
| FR-A4 | Every auth strategy must expose the same post-auth contract to its adapter: a `Session` object with `is_valid()` and `refresh()`, regardless of how the underlying broker issues/expires tokens. |
| FR-A5 | Token expiry behavior differs by broker (e.g., Zerodha's daily 6 AM invalidation) — each adapter must declare its own expiry/refresh policy, and the dashboard's "reconnect" UI must work identically regardless of which policy applies. |

### 5.3 Unified data models **[New]**

| ID | Requirement |
|---|---|
| FR-U1 | `UnifiedInstrument` must be keyed canonically by `(underlying, expiry, strike, option_type, exchange)` — not by any single broker's token scheme. |
| FR-U2 | `UnifiedOrder` must normalize product-type vocabulary across brokers (e.g., Zerodha's `NRML`/`MIS`/`CNC`, Upstox's `D`/`I`/`CO`, NorenApi's `M`/`I`/`C`) into one platform-level enum (`INTRADAY`, `CARRYFORWARD`, `DELIVERY`, `COVER`), with each adapter mapping to/from its broker's native values. |
| FR-U3 | `UnifiedPosition` must normalize quantity sign conventions and average-price fields, which differ across brokers' position response shapes. |
| FR-U4 | `UnifiedOrderStatus` must normalize order lifecycle states (e.g., `OPEN`, `COMPLETE`, `REJECTED`, `CANCELLED`) across brokers whose native state names and transition timing differ. |

### 5.4 Instrument resolution **[New / Migration]**
| ID | Requirement |
|---|---|
| FR-I1 | `instrument_cache.py` must be extended (not replaced) so its daily sync populates the canonical `UnifiedInstrument` table, plus a per-broker mapping table resolving each canonical instrument to that broker's native identifier. |
| FR-I2 | Each adapter must supply its own instrument-master ingestion (file/API format differs per broker) but must emit records into the same canonical schema. |
| FR-I3 | If an instrument doesn't exist on the currently active broker (e.g., a symbol delisted or not offered), instrument resolution must fail loudly (`InstrumentNotFound`) rather than silently placing a malformed order. |

### 5.5 Market data / WebSocket normalization **[New]**
| ID | Requirement |
|---|---|
| FR-M1 | `subscribe_ws()` must present one callback signature to strategies regardless of whether the underlying broker streams binary ticks (Zerodha), Protobuf (Upstox), or JSON (Fyers, Dhan, Alice Blue, NorenApi brokers). |
| FR-M2 | Reconnection/heartbeat behavior differs per broker's WebSocket implementation — each adapter owns its own reconnect logic, but must surface a consistent `on_disconnect` / `on_reconnect` event to `common_lib`. |

### 5.6 Broker selection & configuration **[New]**
| ID | Requirement |
|---|---|
| FR-C1 | `configfile.ini` must support declaring one or more active brokers, each with its own credentials block. |
| FR-C2 | **Single-broker mode**: one `[broker] active = "<name>"` entry; `BrokerFactory` returns exactly one adapter; existing single-account assumptions in Position Guard, Trade Journal, etc. remain valid unchanged. |
| FR-C3 | **Multi-broker mode**: config declares multiple active brokers; each strategy process is launched with an explicit broker assignment (e.g., Survivor → Zerodha, Wave Extractor → Dhan); `BrokerFactory` must support returning the correct adapter per calling process. |
| FR-C4 | Switching the active broker (single-broker mode) must not require code changes — config + restart only. |

### 5.7 Migration of existing platform behavior **[Migration]**
| ID | Requirement |
|---|---|
| FR-MG1 | Every requirement in the base PRD's §5.2 (Order placement) must continue to hold true when any adapter — not just Zerodha's — is active: shared `place_order()` entry point, retry-with-backoff, dry-run gate, state persistence, dual WebSocket/poll reconciliation. |
| FR-MG2 | The existing `place_order_at_nifty_with_buy_auto.py` margin/hedge decision logic (base PRD FR-S9) must remain broker-agnostic — it reasons about premium and margin, not about any broker-specific field, so it should require no changes beyond calling the adapter interface. |
| FR-MG3 | Watchdogs (GTT Monitor, Position Guard, Duplicate Order Monitor, Trade Journal) must be updated to tag every record with the originating broker, and — in multi-broker mode — reconcile/aggregate across brokers rather than assuming a single account. |
| FR-MG4 | GTT support is Zerodha-specific terminology/feature; adapters for brokers without a native GTT equivalent must either implement an equivalent (e.g., a local trigger-price watcher that places a regular order when hit) or declare the feature unsupported — Early Exit and GTT Monitor must handle both cases explicitly, not assume GTT exists everywhere. |

---

## 6. Non-functional requirements

| Category | Requirement |
|---|---|
| **Rate limiting** | Each broker enforces different API rate limits; `retry_with_backoff()` must become rate-limit-aware per adapter (configurable requests/sec ceiling), not a single hardcoded backoff curve. |
| **Testability** | Every adapter must pass a shared contract test suite (place/cancel/modify order, fetch positions, resolve instrument) against `MockAdapter` before being considered release-ready. |
| **Isolation** | A failure or outage in one broker's adapter (in multi-broker mode) must not block or crash strategies running against a different broker. |
| **Credential security** | Each broker's credentials must be stored in the same git-ignored config pattern as today — no broker's credential handling should be less secure than the current Zerodha implementation. |
| **Backward compatibility** | An operator running Zerodha-only, single-broker mode today must see no behavior change after this feature ships — the `ZerodhaAdapter` is a refactor of existing logic, not a rewrite. |

## 7. Data model changes **[Migration]**

| Entity | Change |
|---|---|
| Order-state JSON / trade journal | Add `broker` field to every record |
| `instruments.db` | Add canonical `UnifiedInstrument` table + per-broker mapping table (§5.4) |
| `configfile.ini` | Restructure `[broker]` section to support one-or-many broker blocks (§5.6) |
| Position Guard / Trade Journal queries | Must filter/aggregate by broker where relevant (multi-broker mode) |

## 8. Rollout plan (proposed) **[Proposed]**

| Phase | Scope |
|---|---|
| **Phase 0** | Extract `BrokerAdapter` interface + unified models; refactor existing Kite Connect code into `ZerodhaAdapter` with zero behavior change (proves the abstraction doesn't break anything) |
| **Phase 1** | Add `MockAdapter` + contract test suite; this becomes the acceptance bar for every subsequent adapter |
| **Phase 2** | Add **Dhan** adapter (simplest auth model — static API key, no redirect flow) as the first real second broker |
| **Phase 3** | Add **Shoonya** and **Flattrade** together (shared NorenApi base — most reuse between two adapters) |
| **Phase 4** | Add **Fyers** and **Upstox** (OAuth-redirect family, reuse `OAuthRedirectAuth`) |
| **Phase 5** | Add **Alice Blue** (2FA variant) |
| **Phase 6** | Multi-broker concurrent mode — broker-tagging in Trade Journal/Position Guard, per-process broker assignment |

Ordering rationale: start with the broker requiring the least new auth infrastructure (Dhan), then the pair with the most code reuse (Shoonya/Flattrade), saving multi-broker concurrency for last since it's the highest-risk change to the data model.

## 9. Open questions / risks

| # | Question | Why it matters |
|---|---|---|
| 1 | Does every target broker offer a GTT-equivalent, or does Early Exit need a broker-side fallback (local trigger-watcher)? | Directly affects FR-MG4 scope |
| 2 | Do all seven brokers offer options-chain / instrument-master data in a way that supports Survivor's and Wave Extractor's strike-selection logic (OTM-by-points, ATM detection)? | If any broker's instrument feed lacks strike/expiry granularity, that adapter may not support every existing strategy |
| 3 | What's the actual current API stability/SDK maturity for each broker (official SDK vs. community-maintained)? | Determines both build effort and long-term maintenance risk — should be assessed broker-by-broker before committing to the Phase ordering in §8 |
| 4 | In multi-broker mode, how should Survivor's portfolio-delta rebalancer (base PRD FR-S10) behave — per-broker delta, or aggregated across all active brokers? | Not specified in the base system; needs an explicit decision, since "portfolio" could mean either |
| 5 | Should margin/capital availability checks (Survivor's hedge-vs-close decision, FR-S9) account for capital sitting in a *different* broker account in multi-broker mode? | Real risk: an operator could be capital-constrained on one broker while flush on another, and the strategy has no visibility into that today |

## 10. Success metrics **[Proposed]**
- Zero strategy-code changes required across Phases 2–5 (only new adapter files added).
- Every new adapter passes the full contract test suite before its first live-mode use.
- Dry-run/live safety gate behavior verified identical across all seven adapters via the same test suite.
- Time to add a subsequent broker beyond the seven in scope (using the finished interface): target under 3 days for an engineer familiar with that broker's API.

## 11. Appendix — broker landscape reference

| Broker | Auth model | Order ID | Instrument ID | WebSocket | Product types |
|---|---|---|---|---|---|
| Zerodha (Kite) | OAuth redirect + request_token | numeric string | `instrument_token` (int) | binary ticks | MIS / CNC / NRML |
| Upstox | OAuth2 redirect | UUID | `instrument_key` string | Protobuf | I / D / CO |
| Fyers | OAuth2 redirect + app_id | numeric string | symbol string (`NSE:SBIN-EQ`) | JSON | INTRADAY / CNC / MARGIN |
| Dhan | Static API key + access token | numeric string | `security_id` (int) + segment | JSON | INTRADAY / CNC / MARGIN |
| Alice Blue | user/pass + API key + 2FA | alphanumeric | per-exchange token (int) | JSON | MIS / CNC / NRML |
| Shoonya (Finvasia) | user/pass + TOTP + vendor code (NorenApi) | numeric | `EXCHANGE:token` | JSON (Noren) | I / M / C |
| Flattrade | user/pass + TOTP (NorenApi) | numeric | token | JSON (Noren) | I / M / C |

> Field names and flows above are drawn from each broker's publicly known developer-facing conventions as of this writing and **must be verified against current API documentation** before implementation — broker APIs change, and this table is a planning reference, not a source of truth for coding against.
