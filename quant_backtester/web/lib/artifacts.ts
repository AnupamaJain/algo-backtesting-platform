/**
 * Server-side access to the Python pipeline's output artifacts.
 *
 * The dashboard is a *reader* of what the backtester produced — it never
 * recomputes metrics in JavaScript. Every number shown in the UI traces back
 * to a CSV written by the Python engine, so the browser and the research
 * pipeline can never quietly disagree about a Sharpe ratio.
 */

import fs from "node:fs/promises";
import path from "node:path";
import { parseCsv, num, bool, type Row } from "./csv";
import { DEFAULT_UNIVERSE, type Universe } from "./universes";

/** Project root: quant_backtester/ (this app lives in quant_backtester/web). */
export const PROJECT_ROOT = path.resolve(process.cwd(), "..");
export const CONFIG_DIR = path.join(PROJECT_ROOT, "config");

/** Results and price-cache directories for one universe. */
export const RESULTS_DIR = path.join(PROJECT_ROOT, DEFAULT_UNIVERSE.resultsDir);
export const DATA_DIR = path.join(PROJECT_ROOT, DEFAULT_UNIVERSE.dataDir);

const resultsDirFor = (u: Universe = DEFAULT_UNIVERSE) =>
  path.join(PROJECT_ROOT, u.resultsDir);
const dataDirFor = (u: Universe = DEFAULT_UNIVERSE) => path.join(PROJECT_ROOT, u.dataDir);

async function readCsv(relativePath: string, u: Universe = DEFAULT_UNIVERSE): Promise<Row[]> {
  try {
    const text = await fs.readFile(path.join(resultsDirFor(u), relativePath), "utf-8");
    return parseCsv(text);
  } catch {
    // A missing artifact means that layer has not been run for this universe.
    return [];
  }
}

async function readJson<T>(relativePath: string, u: Universe = DEFAULT_UNIVERSE): Promise<T | null> {
  try {
    const text = await fs.readFile(path.join(resultsDirFor(u), relativePath), "utf-8");
    return JSON.parse(text) as T;
  } catch {
    return null;
  }
}

// -- Layer 1 ------------------------------------------------------------

export type CachedSymbol = { symbol: string; rows: number; sizeKb: number };

export async function getCachedSymbols(u: Universe = DEFAULT_UNIVERSE): Promise<CachedSymbol[]> {
  try {
    const files = await fs.readdir(dataDirFor(u));
    const csvs = files.filter((f) => f.endsWith(".csv"));
    return Promise.all(
      csvs.map(async (file) => {
        const full = path.join(dataDirFor(u), file);
        const [stat, text] = await Promise.all([fs.stat(full), fs.readFile(full, "utf-8")]);
        return {
          symbol: file.replace(/\.csv$/, ""),
          rows: Math.max(text.split("\n").length - 2, 0),
          sizeKb: Math.round(stat.size / 1024),
        };
      })
    );
  } catch {
    return [];
  }
}

export async function getSignalCount(u: Universe = DEFAULT_UNIVERSE): Promise<number> {
  // Layer 1 writes results/<Strategy>/<Symbol>/<param_key>.parquet
  try {
    const entries = await fs.readdir(resultsDirFor(u), { withFileTypes: true });
    let total = 0;
    for (const strategyDir of entries) {
      if (!strategyDir.isDirectory() || strategyDir.name.startsWith("layer")) continue;
      const symbols = await fs.readdir(path.join(resultsDirFor(u), strategyDir.name));
      for (const symbol of symbols) {
        const files = await fs.readdir(path.join(resultsDirFor(u), strategyDir.name, symbol));
        total += files.length;
      }
    }
    return total;
  } catch {
    return 0;
  }
}

// -- Layer 2 ------------------------------------------------------------

export type FunnelStage = {
  stage: number;
  name: string;
  description: string;
  entered: number;
  survived: number;
  rejected: number;
  survivalRate: number;
};

export async function getFunnelStages(u: Universe = DEFAULT_UNIVERSE): Promise<FunnelStage[]> {
  const rows = await readCsv("layer2/funnel_report.csv", u);
  return rows.map((r) => ({
    stage: num(r.stage) ?? 0,
    name: r.name,
    description: r.description,
    entered: num(r.entered) ?? 0,
    survived: num(r.survived) ?? 0,
    rejected: num(r.rejected) ?? 0,
    survivalRate: num(r.survival_rate) ?? 0,
  }));
}

export type Configuration = {
  symbol: string;
  strategy: string;
  paramKey: string;
  params: string;
  isSharpe: number | null;
  oosSharpe: number | null;
  oosMaxDrawdown: number | null;
  oosProfitFactor: number | null;
  oosTotalReturn: number | null;
  oosNumTrades: number | null;
  survived?: boolean;
  rejectedAtStage?: number | null;
};

function toConfiguration(r: Row): Configuration {
  return {
    symbol: r.symbol,
    strategy: r.strategy,
    paramKey: r.param_key ?? "",
    params: r.params ?? "",
    isSharpe: num(r.is_sharpe),
    oosSharpe: num(r.oos_sharpe),
    oosMaxDrawdown: num(r.oos_max_drawdown),
    oosProfitFactor: num(r.oos_profit_factor),
    oosTotalReturn: num(r.oos_total_return),
    oosNumTrades: num(r.oos_num_trades),
  };
}

export async function getAllConfigurations(u: Universe = DEFAULT_UNIVERSE): Promise<Configuration[]> {
  const rows = await readCsv("layer2/all_configurations.csv", u);
  return rows.map(toConfiguration);
}

export async function getSurvivors(u: Universe = DEFAULT_UNIVERSE): Promise<Configuration[]> {
  const rows = await readCsv("layer2/survivors.csv", u);
  return rows.map(toConfiguration);
}

export type ScatterPoint = {
  symbol: string;
  strategy: string;
  isSharpe: number | null;
  oosSharpe: number | null;
  survived: boolean;
  rejectedAtStage: number | null;
};

export async function getScatter(u: Universe = DEFAULT_UNIVERSE): Promise<ScatterPoint[]> {
  const rows = await readCsv("layer2/is_oos_scatter.csv", u);
  return rows.map((r) => ({
    symbol: r.symbol,
    strategy: r.strategy,
    isSharpe: num(r.is_sharpe),
    oosSharpe: num(r.oos_sharpe),
    survived: bool(r.survived),
    rejectedAtStage: num(r.rejected_at_stage),
  }));
}

export type Layer2Manifest = {
  num_configurations_tested: number;
  num_survivors: number;
  costs: Record<string, unknown>;
  walk_forward: Record<string, unknown>;
  funnel_thresholds: Record<string, unknown>;
};

export const getLayer2Manifest = (u: Universe = DEFAULT_UNIVERSE) =>
  readJson<Layer2Manifest>("layer2/manifest.json", u);

export type Freshness = {
  computedAt: string | null;
  ageHours: number | null;
  strategiesInResults: string[];
  strategiesConfigured: string[];
  missingFromResults: string[];
  stale: boolean;
};

/**
 * Compare what the results contain against what is currently configured.
 *
 * Adding a strategy without re-running leaves the funnel showing an older,
 * smaller sweep while the library page advertises the full set — which reads
 * as invented data rather than what it is: out of date.
 */
export async function getFreshness(u: Universe = DEFAULT_UNIVERSE): Promise<Freshness> {
  const [configs, configured] = await Promise.all([
    getAllConfigurations(u),
    readConfiguredStrategyNames(),
  ]);

  const inResults = Array.from(new Set(configs.map((c) => c.strategy))).sort();
  const missing = configured.filter((s) => !inResults.includes(s));

  let computedAt: string | null = null;
  let ageHours: number | null = null;
  try {
    const stat = await fs.stat(
      path.join(resultsDirFor(u), "layer2", "all_configurations.csv")
    );
    computedAt = stat.mtime.toISOString();
    ageHours = (Date.now() - stat.mtimeMs) / 3_600_000;
  } catch {
    // No results yet; that is "not run", not "stale".
  }

  return {
    computedAt,
    ageHours,
    strategiesInResults: inResults,
    strategiesConfigured: configured,
    missingFromResults: missing,
    stale: inResults.length > 0 && missing.length > 0,
  };
}

async function readConfiguredStrategyNames(): Promise<string[]> {
  try {
    const text = await fs.readFile(path.join(CONFIG_DIR, "strategy_grid.yaml"), "utf-8");
    const names: string[] = [];
    let inStrategies = false;
    for (const line of text.split(/\r?\n/)) {
      if (/^strategies:/.test(line)) { inStrategies = true; continue; }
      if (inStrategies && /^[a-z]/.test(line)) break;   // next top-level key
      const match = /^ {2}([A-Z][A-Za-z0-9]*):\s*$/.exec(line);
      if (inStrategies && match) names.push(match[1]);
    }
    return names.sort();
  } catch {
    return [];
  }
}

// -- Layer 3 ------------------------------------------------------------

export type RobustnessRow = {
  symbol: string;
  strategy: string;
  paramKey: string;
  oosSharpe: number | null;
  sensitivityScore: number | null;
  retentionRatio: number | null;
  stabilityFraction: number | null;
  isStable: boolean;
  numNeighboursTested: number | null;
  bootstrapVerdict: string;
  bootstrapReason: string;
  p5FinalReturn: number | null;
  p95MaxDrawdown: number | null;
  drawdownAmplification: number | null;
  worstLossStreak: number | null;
  numTrades: number | null;
};

function toRobustness(r: Row): RobustnessRow {
  return {
    symbol: r.symbol,
    strategy: r.strategy,
    paramKey: r.param_key ?? "",
    oosSharpe: num(r.oos_sharpe),
    sensitivityScore: num(r.sensitivity_score),
    retentionRatio: num(r.retention_ratio),
    stabilityFraction: num(r.stability_fraction),
    isStable: bool(r.is_stable),
    numNeighboursTested: num(r.num_neighbours_tested),
    bootstrapVerdict: r.bootstrap_verdict ?? "",
    bootstrapReason: r.bootstrap_reason ?? "",
    p5FinalReturn: num(r.bootstrap_p5_final_return),
    p95MaxDrawdown: num(r.bootstrap_p95_max_drawdown),
    drawdownAmplification: num(r.bootstrap_drawdown_amplification),
    worstLossStreak: num(r.bootstrap_worst_loss_streak),
    numTrades: num(r.bootstrap_num_trades),
  };
}

export const getRobustness = async (u: Universe = DEFAULT_UNIVERSE) =>
  (await readCsv("layer3/robustness_full.csv", u)).map(toRobustness);

export const getUltraRobust = async (u: Universe = DEFAULT_UNIVERSE) =>
  (await readCsv("layer3/ultra_robust_strategies.csv", u)).map(toRobustness);

// -- Layer 4 ------------------------------------------------------------

export type PortfolioSummary = {
  portfolio: string;
  annualizedReturn: number | null;
  annualizedVolatility: number | null;
  sharpe: number | null;
  maxDrawdown: number | null;
  calmar: number | null;
  totalReturn: number | null;
  avgDailyTurnover: number | null;
};

export async function getPortfolioComparison(u: Universe = DEFAULT_UNIVERSE): Promise<PortfolioSummary[]> {
  const rows = await readCsv("layer4/portfolio_comparison.csv", u);
  return rows.map((r) => ({
    portfolio: r.portfolio,
    annualizedReturn: num(r.annualized_return),
    annualizedVolatility: num(r.annualized_volatility),
    sharpe: num(r.sharpe),
    maxDrawdown: num(r.max_drawdown),
    calmar: num(r.calmar),
    totalReturn: num(r.total_return),
    avgDailyTurnover: num(r.avg_daily_turnover),
  }));
}

export type EquityPoint = { date: string } & Record<string, number | string>;

export async function getEquityCurves(
  maxPoints = 600,
  u: Universe = DEFAULT_UNIVERSE
): Promise<{
  series: string[];
  points: EquityPoint[];
}> {
  const rows = await readCsv("layer4/equity_curves.csv", u);
  if (rows.length === 0) return { series: [], points: [] };

  const headers = Object.keys(rows[0]);
  const dateKey = headers[0];
  const series = headers.slice(1);

  // Downsample for the browser: 3,700 daily points per series is far more
  // than a chart a few hundred pixels wide can distinguish.
  const step = Math.max(1, Math.ceil(rows.length / maxPoints));
  const points: EquityPoint[] = [];
  for (let i = 0; i < rows.length; i += step) {
    const row = rows[i];
    const point: EquityPoint = { date: row[dateKey] };
    series.forEach((s) => {
      point[s] = num(row[s]) ?? 0;
    });
    points.push(point);
  }
  return { series, points };
}

export type Layer4Manifest = { diagnostics: string[]; portfolios: string[] };

/** Notes the allocator emitted about anomalies in the run (e.g. a regime that
 *  held no position because nothing it routes to was deployed). */
export async function getLayer4Diagnostics(u: Universe = DEFAULT_UNIVERSE): Promise<string[]> {
  const manifest = await readJson<Layer4Manifest>("layer4/manifest.json", u);
  return manifest?.diagnostics ?? [];
}

export type RegimeAttribution = {
  regime: number;
  regimeName: string;
  days: number;
  totalReturn: number | null;
  sharpe: number | null;
  maxDrawdown: number | null;
};

export async function getRegimeAttribution(u: Universe = DEFAULT_UNIVERSE): Promise<RegimeAttribution[]> {
  const rows = await readCsv("layer4/regime_attribution.csv", u);
  return rows.map((r) => ({
    regime: num(r.regime) ?? 0,
    regimeName: r.regime_name,
    days: num(r.days) ?? 0,
    totalReturn: num(r.total_return),
    sharpe: num(r.sharpe),
    maxDrawdown: num(r.max_drawdown),
  }));
}

export type RegimePoint = { date: string; regime: number };

export async function getRegimeTimeline(
  maxPoints = 900,
  u: Universe = DEFAULT_UNIVERSE
): Promise<RegimePoint[]> {
  const rows = await readCsv("layer4/regime_timeline.csv", u);
  if (rows.length === 0) return [];
  const headers = Object.keys(rows[0]);
  const dateKey = headers[0];
  const step = Math.max(1, Math.ceil(rows.length / maxPoints));
  const points: RegimePoint[] = [];
  for (let i = 0; i < rows.length; i += step) {
    const regime = num(rows[i].regime);
    if (regime === null) continue;
    points.push({ date: rows[i][dateKey], regime });
  }
  return points;
}

export type ExecutionRow = {
  date: string;
  symbol: string;
  strategy: string;
  exposureChange: number | null;
  exposureAfter: number | null;
  regimeName: string;
};

export async function getExecutions({
  page = 1,
  pageSize = 50,
  regime,
  symbol,
  universe = DEFAULT_UNIVERSE,
}: {
  page?: number;
  pageSize?: number;
  regime?: string;
  symbol?: string;
  universe?: Universe;
} = {}): Promise<{
  rows: ExecutionRow[];
  total: number;
  totalUnfiltered: number;
  pages: number;
  page: number;
  pageSize: number;
  byRegime: Record<string, number>;
  symbols: string[];
}> {
  const raw = await readCsv("layer4/executions_by_regime.csv", universe);

  // Regime counts describe the WHOLE file, not the current page — otherwise
  // the summary would change every time you turned a page.
  const byRegime: Record<string, number> = {};
  raw.forEach((r) => {
    const name = r.regime_name || "Unclassified";
    byRegime[name] = (byRegime[name] ?? 0) + 1;
  });
  const symbols = Array.from(new Set(raw.map((r) => r.symbol))).sort();

  const filtered = raw.filter(
    (r) =>
      (!regime || r.regime_name === regime) && (!symbol || r.symbol === symbol)
  );

  const total = filtered.length;
  const pages = Math.max(Math.ceil(total / pageSize), 1);
  const safePage = Math.min(Math.max(page, 1), pages);
  const start = (safePage - 1) * pageSize;

  const rows = filtered.slice(start, start + pageSize).map((r) => ({
    date: r.date,
    symbol: r.symbol,
    strategy: r.strategy,
    exposureChange: num(r.exposure_change),
    exposureAfter: num(r.exposure_after),
    regimeName: r.regime_name,
  }));

  return {
    rows,
    total,
    totalUnfiltered: raw.length,
    pages,
    page: safePage,
    pageSize,
    byRegime,
    symbols,
  };
}

// -- Pipeline status ----------------------------------------------------

export type PipelineStatus = {
  layer1: { done: boolean; symbols: number; signals: number };
  layer2: { done: boolean; tested: number; survivors: number };
  layer3: { done: boolean; evaluated: number; ultraRobust: number };
  layer4: { done: boolean; portfolios: number };
};

export async function getPipelineStatus(u: Universe = DEFAULT_UNIVERSE): Promise<PipelineStatus> {
  const [symbols, signals, manifest, robustness, ultra, portfolios] = await Promise.all([
    getCachedSymbols(u),
    getSignalCount(u),
    getLayer2Manifest(u),
    getRobustness(u),
    getUltraRobust(u),
    getPortfolioComparison(u),
  ]);

  return {
    layer1: { done: symbols.length > 0, symbols: symbols.length, signals },
    layer2: {
      done: manifest !== null,
      tested: manifest?.num_configurations_tested ?? 0,
      survivors: manifest?.num_survivors ?? 0,
    },
    layer3: {
      done: robustness.length > 0,
      evaluated: robustness.length,
      ultraRobust: ultra.length,
    },
    layer4: { done: portfolios.length > 0, portfolios: portfolios.length },
  };
}

// -- PEAD (Post-Earnings Announcement Drift) ----------------------------

export type PeadSummary = {
  config: {
    min_surprise_pct: number;
    holding_days: number;
    direction: string;
  };
  exposure: {
    bars: number;
    bars_in_market: number;
    time_in_market_pct: number;
    position_changes: number;
  };
  metrics: {
    total_return: number;
    cagr: number;
    annual_volatility: number;
    sharpe: number;
    max_drawdown: number;
    profit_factor: number;
    num_trades: number;
    win_rate: number;
    num_periods: number;
  };
  symbols_with_data: string[];
  symbols_missing_data: string[];
  active_signals: number;
  upcoming_events: number;
  as_of: string;
};

export type PeadSignal = {
  symbol: string;
  signal: number;               // -1 | 0 | 1
  direction: string;            // LONG | SHORT | FLAT
  surprise_pct: number | null;
  announced_at: string;
  entry_date: string;
  days_held: number | null;
  days_remaining: number | null;
  window_end: string;
  eps_estimate: number | null;
  eps_reported: number | null;
};

export type PeadUpcoming = {
  symbol: string;
  announced_at: string;
  days_until: number | null;
  eps_estimate: number | null;
  eps_reported: number | null;
  surprise_pct: number | null;
  would_trigger: boolean | null;
  direction: string;            // LONG | SHORT | pending
};

export type PeadEquityPoint = { date: string; equity: number };

/** Summary JSON written by `python main.py pead`. Null when not yet run. */
export const getPeadSummary = (u: Universe = DEFAULT_UNIVERSE) =>
  readJson<PeadSummary>("pead/summary.json", u);

/** Currently active PEAD drift positions — the "stocks in the window now" signal. */
export async function getPeadSignals(u: Universe = DEFAULT_UNIVERSE): Promise<PeadSignal[]> {
  const rows = await readCsv("pead/signals.csv", u);
  return rows.map((r) => ({
    symbol: r.symbol,
    signal: num(r.signal) ?? 0,
    direction: r.direction ?? "FLAT",
    surprise_pct: num(r.surprise_pct),
    announced_at: r.announced_at ?? "",
    entry_date: r.entry_date ?? "",
    days_held: num(r.days_held),
    days_remaining: num(r.days_remaining),
    window_end: r.window_end ?? "",
    eps_estimate: num(r.eps_estimate),
    eps_reported: num(r.eps_reported),
  }));
}

/** Upcoming earnings (within 30 days) that would/may trigger a new PEAD trade. */
export async function getPeadUpcoming(u: Universe = DEFAULT_UNIVERSE): Promise<PeadUpcoming[]> {
  const rows = await readCsv("pead/upcoming.csv", u);
  return rows.map((r) => ({
    symbol: r.symbol,
    announced_at: r.announced_at ?? "",
    days_until: num(r.days_until),
    eps_estimate: num(r.eps_estimate),
    eps_reported: num(r.eps_reported),
    surprise_pct: num(r.surprise_pct),
    would_trigger: r.would_trigger === "True" ? true : r.would_trigger === "False" ? false : null,
    direction: r.direction ?? "pending",
  }));
}

/** Portfolio equity curve (compounded returns, starting at 1.0). */
export async function getPeadEquityCurve(
  maxPoints = 500,
  u: Universe = DEFAULT_UNIVERSE
): Promise<PeadEquityPoint[]> {
  const rows = await readCsv("pead/equity_curve.csv", u);
  if (rows.length === 0) return [];
  const step = Math.max(1, Math.ceil(rows.length / maxPoints));
  const points: PeadEquityPoint[] = [];
  for (let i = 0; i < rows.length; i += step) {
    const v = num(rows[i].equity);
    if (v !== null) points.push({ date: rows[i].date, equity: v });
  }
  return points;
}
