import fs from "node:fs/promises";
import path from "node:path";
import { PROJECT_ROOT } from "./artifacts";

/**
 * Readers for the intraday IB-60 study.
 *
 * Every figure on the ORBIS page comes from these artifacts, which are
 * written by the Python study against live NSE 5-minute bars. Nothing here
 * synthesises a session, a break, or a hit-rate: if an artifact is missing
 * the page says so rather than showing a plausible-looking number.
 */

const INTRADAY_RESULTS = path.join(PROJECT_ROOT, "results", "intraday");
const INTRADAY_DATA = path.join(PROJECT_ROOT, "data_intraday");

type Row = Record<string, string>;

function parseCsv(text: string): Row[] {
  const lines = text.trim().split("\n");
  if (lines.length < 2) return [];
  const headers = lines[0].split(",").map((h) => h.trim());
  return lines.slice(1).map((line) => {
    // Values here are numbers, ISO dates and bare labels — no quoted commas.
    const cells = line.split(",");
    return Object.fromEntries(headers.map((h, i) => [h, (cells[i] ?? "").trim()]));
  });
}

async function readCsv(file: string): Promise<Row[]> {
  try {
    return parseCsv(await fs.readFile(path.join(INTRADAY_RESULTS, file), "utf-8"));
  } catch {
    return [];
  }
}

const num = (v: string | undefined): number | null => {
  if (v === undefined || v === "" || v === "nan") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};

export const INSTRUMENTS = ["NIFTY", "BANKNIFTY"] as const;
export type Instrument = (typeof INSTRUMENTS)[number];

// -- sessions -----------------------------------------------------------

export type Session = {
  date: string;
  ibHigh: number | null;
  ibLow: number | null;
  ibRange: number | null;
  ibMid: number | null;
  ibBars: number;
  formedFirst: string | null;
  brokeFirst: string | null;
  breakTime: string | null;
  note: string;
};

export async function getSessions(instrument: Instrument): Promise<Session[]> {
  const rows = await readCsv(`${instrument}_sessions.csv`);
  return rows
    .map((r) => ({
      date: r.date,
      ibHigh: num(r.ib_high),
      ibLow: num(r.ib_low),
      ibRange: num(r.ib_range),
      ibMid: num(r.ib_mid),
      ibBars: num(r.ib_bars) ?? 0,
      formedFirst: r.formed_first || null,
      brokeFirst: r.broke_first || null,
      breakTime: r.break_time || null,
      note: r.note ?? "",
    }))
    .sort((a, b) => b.date.localeCompare(a.date));
}

// -- the conditional first-break study ----------------------------------

export type FirstBreakRow = {
  formedFirst: string;
  sessions: number;
  resolved: number;
  ambiguous: number;
  noBreak: number;
  brokeHigh: number;
  brokeLow: number;
  pctBrokeLow: number | null;
  ciLow: number | null;
  ciHigh: number | null;
  beatsCoinflip: boolean;
};

export async function getFirstBreak(instrument: Instrument): Promise<FirstBreakRow[]> {
  const rows = await readCsv(`${instrument}_first_break.csv`);
  return rows.map((r) => ({
    formedFirst: r.formed_first,
    sessions: num(r.sessions) ?? 0,
    resolved: num(r.resolved) ?? 0,
    ambiguous: num(r.ambiguous) ?? 0,
    noBreak: num(r.no_break) ?? 0,
    brokeHigh: num(r.broke_high) ?? 0,
    brokeLow: num(r.broke_low) ?? 0,
    pctBrokeLow: num(r.pct_broke_low),
    ciLow: num(r.ci_low),
    ciHigh: num(r.ci_high),
    beatsCoinflip: String(r.beats_coinflip).toLowerCase() === "true",
  }));
}

// -- the path-dependent backtest ----------------------------------------

export type IBTrade = {
  date: string;
  brokeFirst: string | null;
  direction: string | null;
  entry: number | null;
  stop: number | null;
  target: number | null;
  exitPrice: number | null;
  outcome: string;
  points: number;
  r: number;
  ibRange: number | null;
};

export async function getTrades(instrument: Instrument): Promise<IBTrade[]> {
  const rows = await readCsv(`${instrument}_ib_trades.csv`);
  return rows
    .map((r) => ({
      date: r.date,
      brokeFirst: r.broke_first || null,
      direction: r.direction || null,
      entry: num(r.entry),
      stop: num(r.stop),
      target: num(r.target),
      exitPrice: num(r.exit_price),
      outcome: r.outcome || "NO_SETUP",
      points: num(r.points) ?? 0,
      r: num(r.r) ?? 0,
      ibRange: num(r.ib_range),
    }))
    .sort((a, b) => b.date.localeCompare(a.date));
}

export type StudySummary = {
  sessions: number;
  setups: number;
  filled: number;
  no_fill: number;
  cutoff: number;
  double_break: number;
  ambiguous: number;
  degenerate?: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  win_rate_ci: [number, number];
  target_hits: number;
  stop_hits: number;
  time_exits: number;
  total_points: number;
  avg_points: number | null;
  expectancy_r: number | null;
  profit_factor: number;
  max_drawdown_points: number;
  t_stat: number | null;
};

export type StudyMeta = {
  generated: string;
  sessions_available: number;
  feed_limit_note: string;
  costs: string;
  conclusion: string;
};

export async function getStudySummary(): Promise<{
  byInstrument: Partial<Record<Instrument, StudySummary>>;
  meta: StudyMeta | null;
}> {
  try {
    const text = await fs.readFile(path.join(INTRADAY_RESULTS, "ib_study_summary.json"), "utf-8");
    const raw = JSON.parse(text) as Record<string, unknown>;
    const meta = (raw._meta as StudyMeta) ?? null;
    const byInstrument: Partial<Record<Instrument, StudySummary>> = {};
    for (const key of INSTRUMENTS) {
      if (raw[key]) byInstrument[key] = raw[key] as StudySummary;
    }
    return { byInstrument, meta };
  } catch {
    return { byInstrument: {}, meta: null };
  }
}

// -- equity curve, derived from the trades themselves --------------------

export type EquityPoint = { date: string; equity: number; points: number };

export function equityCurve(trades: IBTrade[]): EquityPoint[] {
  const filled = trades
    .filter((t) => t.outcome === "TARGET" || t.outcome === "STOP" || t.outcome === "TIME")
    .slice()
    .sort((a, b) => a.date.localeCompare(b.date));
  let running = 0;
  return filled.map((t) => {
    running += t.points;
    return { date: t.date, equity: Number(running.toFixed(1)), points: t.points };
  });
}

// -- today's live session ------------------------------------------------

const ORBIS_STATE = path.join(PROJECT_ROOT, "state", "orbis");

export type LiveSession = {
  instrument: string;
  sessionDate: string;
  ibHigh: number | null;
  ibLow: number | null;
  formedFirst: string | null;
  brokeFirst: string | null;
  phase: string;
  direction: string | null;
  entry: number | null;
  stop: number | null;
  target: number | null;
  fillPrice: number | null;
  exitPrice: number | null;
  exitReason: string | null;
  orderId: string | null;
  updatedAt: string;
};

/** Today's live IB state per instrument, written by the intraday engine. */
export async function getLiveSessions(): Promise<LiveSession[]> {
  const today = new Date().toISOString().slice(0, 10);
  let names: string[] = [];
  try {
    names = await fs.readdir(ORBIS_STATE);
  } catch {
    return [];
  }

  const out: LiveSession[] = [];
  for (const name of names) {
    // Only today's files: a stale session must not read as live.
    if (!name.endsWith(`${today}.json`)) continue;
    try {
      const raw = JSON.parse(await fs.readFile(path.join(ORBIS_STATE, name), "utf-8"));
      out.push({
        instrument: raw.instrument,
        sessionDate: raw.session_date,
        ibHigh: raw.ib_high ?? null,
        ibLow: raw.ib_low ?? null,
        formedFirst: raw.formed_first ?? null,
        brokeFirst: raw.broke_first ?? null,
        phase: raw.phase ?? "none",
        direction: raw.direction ?? null,
        entry: raw.entry ?? null,
        stop: raw.stop ?? null,
        target: raw.target ?? null,
        fillPrice: raw.fill_price ?? null,
        exitPrice: raw.exit_price ?? null,
        exitReason: raw.exit_reason ?? null,
        orderId: raw.order_id ?? null,
        updatedAt: raw.updated_at ?? "",
      });
    } catch {
      // A corrupt state file must not blank the whole panel.
      continue;
    }
  }
  return out.sort((a, b) => a.instrument.localeCompare(b.instrument));
}

// -- freshness ----------------------------------------------------------

export type IntradayFreshness = {
  lastSession: string | null;
  cachedBars: number;
  generated: string | null;
  stale: boolean;
};

export async function getIntradayFreshness(): Promise<IntradayFreshness> {
  const sessions = await getSessions("NIFTY");
  const { meta } = await getStudySummary();
  let cachedBars = 0;
  try {
    const text = await fs.readFile(path.join(INTRADAY_DATA, "NIFTY_5m.csv"), "utf-8");
    cachedBars = Math.max(0, text.trim().split("\n").length - 1);
  } catch {
    cachedBars = 0;
  }
  const lastSession = sessions[0]?.date ?? null;
  // The feed only ever holds ~4 months, so "stale" here means the study has
  // not been re-run recently rather than that data is missing.
  const stale = lastSession
    ? (Date.now() - new Date(lastSession).getTime()) / 86_400_000 > 5
    : true;
  return { lastSession, cachedBars, generated: meta?.generated ?? null, stale };
}
