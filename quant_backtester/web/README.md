# Strategy Lab — web dashboard

A Next.js UI over the Python backtesting pipeline. It reads the artifacts the
pipeline writes to `../results/`, lets you edit every tunable setting, and can
trigger any layer directly.

## Running

```bash
npm install
npm run dev        # http://localhost:3000
```

The Python side must be installed first (see `../requirements.txt`). The run
API prefers the project's virtualenv at `../../venv/bin/python` and falls back
to `python3`.

## How it fits together

```
config/*.yaml  ──edit──►  UI (schema-driven forms)  ──PATCH /api/config──►  config/*.yaml
                                    │
                                    └──POST /api/run──►  main.py <layer>
                                                              │
                                          results/*.csv  ◄────┘
                                                │
                                                └──►  UI reads and visualizes
```

**The dashboard never computes metrics itself.** Every number on screen is
parsed from a CSV the Python engine produced, so the browser and the research
pipeline cannot quietly disagree about a Sharpe ratio.

## Notable implementation details

- **`lib/config-schema.ts` is the single source of truth** for which settings
  are editable, their bounds, and their help text. It drives the form controls
  *and* the server-side validation, so the UI cannot offer an edit the backend
  would reject. Any path not declared there is refused.

- **YAML comments survive edits.** Config files carry a lot of hard-won
  explanation; writes go through `yaml`'s Document API rather than a
  parse/stringify round-trip. A trailing same-line comment on an edited value
  *is* dropped, because comments like `0.35 # 35% peak-to-trough` become lies
  the moment the value changes.

- **The run API takes no shell.** The layer name is checked against an
  allowlist, symbols against a strict pattern, and arguments are passed as an
  argv array to `spawn` with `shell: false`.

- **Recharts animations are disabled** (`isAnimationActive={false}`). Their
  entrance animation does not settle in this React version: lines stayed at
  their initial `stroke-dasharray` and scatter symbols at `d="M0,0"`, so charts
  rendered blank. Scatter points also use an explicit `<Dot>` renderer rather
  than Recharts' symbol path generator.

## Pages

| Route | Layer | Shows |
|---|---|---|
| `/` | — | Pipeline status, funnel summary, portfolio results |
| `/data` | 1 | Cached symbols, strategy library, universe + parameter grid |
| `/funnel` | 2 | Six validation gates, IS-vs-OOS scatter, survivors |
| `/robustness` | 3 | Parameter stability, bootstrap verdicts, ultra-robust set |
| `/regime` | 4 | Regime timeline, portfolio comparison, executions by regime |

Each page carries the settings for its own layer at the bottom, so you can
change a threshold and re-run without leaving the page.
