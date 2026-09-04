import { Card, CardHead, EmptyState, Explain, Pill, Stat, Table, Td, Th } from "@/components/ui";
import { Pagination } from "@/components/Pagination";
import { IBLadder } from "./IBLadder";
import { EquityChart } from "./EquityChart";
import {
  INSTRUMENTS,
  type Instrument,
  equityCurve,
  getFirstBreak,
  getIntradayFreshness,
  getLiveSessions,
  getSessions,
  getStudySummary,
  getTrades,
} from "@/lib/orbis";

export const dynamic = "force-dynamic";

const OUTCOME_TONE: Record<string, string> = {
  TARGET: "green",
  STOP: "red",
  TIME: "amber",
  CUTOFF: "slate",
  NO_FILL: "slate",
  DOUBLE_BREAK: "slate",
  AMBIGUOUS: "amber",
  DEGENERATE: "red",
  NO_BREAK: "slate",
  NO_IB: "slate",
};

function pct(v: number | null, digits = 1): string {
  return v === null || Number.isNaN(v) ? "—" : `${v.toFixed(digits)}%`;
}
function sig(v: number | null, digits = 3): string {
  return v === null || Number.isNaN(v) ? "—" : v.toFixed(digits);
}

export default async function OrbisPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const raw = params.sym;
  const picked = Array.isArray(raw) ? raw[0] : raw;
  const instrument: Instrument =
    picked && (INSTRUMENTS as readonly string[]).includes(picked)
      ? (picked as Instrument)
      : "NIFTY";

  const [sessions, firstBreak, trades, study, freshness, liveSessions] = await Promise.all([
    getSessions(instrument),
    getFirstBreak(instrument),
    getTrades(instrument),
    getStudySummary(),
    getIntradayFreshness(),
    getLiveSessions(),
  ]);

  const summary = study.byInstrument[instrument];
  const meta = study.meta;
  const curve = equityCurve(trades);

  if (sessions.length === 0) {
    return (
      <div className="mx-auto max-w-[1500px] px-6 py-8 lg:px-10">
        <Card>
          <EmptyState
            title="No intraday study artifacts"
            body="Run the IB-60 study to generate results/intraday/. Until then this page has nothing real to show, and it will not invent anything."
          />
        </Card>
      </div>
    );
  }

  // Pagination for the session log.
  const sizeRaw = Array.isArray(params.ssize) ? params.ssize[0] : params.ssize;
  const pageRaw = Array.isArray(params.spage) ? params.spage[0] : params.spage;
  const pageSize = Math.min(Math.max(Number(sizeRaw) || 25, 10), 100);
  const pages = Math.max(1, Math.ceil(sessions.length / pageSize));
  const safePage = Math.min(Math.max(Number(pageRaw) || 1, 1), pages);
  const pageRows = sessions.slice((safePage - 1) * pageSize, safePage * pageSize);

  const latest = sessions.find((s) => s.ibHigh !== null) ?? sessions[0];
  const latestTrade = trades.find((t) => t.date === latest.date);

  const noEdge =
    summary && (summary.expectancy_r === null || Math.abs(summary.expectancy_r) < 0.1);

  // Live session state written by OrbisLiveEngine for today's run.
  const liveSession = liveSessions.find((s) => s.instrument === instrument) ?? null;
  const PHASE_TONE: Record<string, string> = {
    none: "slate", armed: "amber", filled: "cyan", closed: "slate",
  };

  return (
    <div className="mx-auto max-w-[1500px] px-6 py-8 lg:px-10">
      <header className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-xl font-semibold text-slate-100">ORBIS — IB-60 Engine</h1>
            <Pill tone="cyan">research</Pill>
          </div>
          <p className="mt-1.5 text-xs text-slate-500">
            Initial Balance retracement on NSE indices · live 5-minute bars from Flattrade ·
            every figure below is measured, none are illustrative
          </p>
        </div>
        <div className="flex items-center gap-2">
          {INSTRUMENTS.map((sym) => (
            <a
              key={sym}
              href={`/orbis?sym=${sym}`}
              className={`mono rounded-lg border px-3 py-1.5 text-xs transition ${
                sym === instrument
                  ? "border-emerald-500/50 bg-emerald-500/10 text-emerald-300"
                  : "border-[var(--color-line-soft)] text-slate-400 hover:text-slate-200"
              }`}
            >
              {sym}
            </a>
          ))}
        </div>
      </header>

      {/* ---- live session state (only when the engine is running today) --- */}
      {liveSession ? (
        <div className="mb-6 rounded-xl border border-cyan-500/30 bg-cyan-500/[0.05] px-5 py-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2.5">
              <span className="h-2 w-2 animate-pulse rounded-full bg-cyan-400" />
              <span className="text-sm font-semibold text-slate-100">
                Live session — {liveSession.sessionDate}
              </span>
              <Pill tone={PHASE_TONE[liveSession.phase] ?? "slate"}>
                {liveSession.phase}
              </Pill>
              {liveSession.direction ? (
                <Pill tone={liveSession.direction === "LONG" ? "green" : "red"}>
                  {liveSession.direction}
                </Pill>
              ) : null}
            </div>
            <span className="text-[11px] text-slate-500">
              updated {liveSession.updatedAt}
            </span>
          </div>
          <div className="mt-3 grid grid-cols-2 gap-x-8 gap-y-1.5 text-[11.5px] sm:grid-cols-4">
            {[
              ["IB high", liveSession.ibHigh?.toFixed(1) ?? "—"],
              ["IB low", liveSession.ibLow?.toFixed(1) ?? "—"],
              ["entry", liveSession.entry?.toFixed(1) ?? "—"],
              ["stop", liveSession.stop?.toFixed(1) ?? "—"],
              ["target", liveSession.target?.toFixed(1) ?? "—"],
              ["fill", liveSession.fillPrice?.toFixed(1) ?? "—"],
              ["exit", liveSession.exitPrice?.toFixed(1) ?? "—"],
              ["reason", liveSession.exitReason ?? "—"],
            ].map(([label, value]) => (
              <div key={label} className="flex justify-between border-b border-[var(--color-line-soft)] pb-1">
                <span className="text-slate-500">{label}</span>
                <span className="mono text-slate-200">{value}</span>
              </div>
            ))}
          </div>
          {liveSession.orderId ? (
            <p className="mono mt-2 text-[10.5px] text-slate-600">
              order {liveSession.orderId}
            </p>
          ) : null}
        </div>
      ) : null}

      {/* ---- the verdict, stated before anything else ------------------ */}
      <div
        className={`mb-6 rounded-xl border px-5 py-4 ${
          noEdge
            ? "border-amber-500/30 bg-amber-500/[0.06]"
            : "border-emerald-500/30 bg-emerald-500/[0.06]"
        }`}
      >
        <div className="flex items-start gap-3">
          <span className="mt-0.5 text-lg">{noEdge ? "⚠" : "✓"}</span>
          <div>
            <div className="text-sm font-semibold text-slate-100">
              {noEdge
                ? "No tradable edge found in this sample"
                : "Edge present — review the full statistics below"}
            </div>
            <p className="mt-1.5 max-w-4xl text-xs leading-relaxed text-slate-400">
              {meta?.conclusion ??
                "The path-dependent backtest is the deciding test; the structural frequencies above it are descriptive only."}{" "}
              Expectancy {sig(summary?.expectancy_r ?? null)} R per trade over{" "}
              {summary?.filled ?? 0} filled trades (t = {sig(summary?.t_stat ?? null, 2)}).
              A t-statistic inside ±2 means this sample cannot distinguish the result from
              noise in either direction.
            </p>
          </div>
        </div>
      </div>

      {/* ---- headline stats ------------------------------------------- */}
      <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        <Stat label="sessions" value={String(summary?.sessions ?? sessions.length)} sub="feed ceiling" />
        <Stat label="setups" value={String(summary?.setups ?? 0)} sub="break confirmed" />
        <Stat label="filled" value={String(summary?.filled ?? 0)} sub="retrace reached entry" />
        <Stat
          label="win rate"
          value={pct(summary?.win_rate ?? null)}
          sub={
            summary
              ? `95% CI ${pct(summary.win_rate_ci[0])}–${pct(summary.win_rate_ci[1])}`
              : undefined
          }
          tone={(summary?.win_rate ?? 0) >= 50 ? "text-emerald-300" : "text-rose-300"}
        />
        <Stat
          label="expectancy"
          value={`${sig(summary?.expectancy_r ?? null)} R`}
          sub="per filled trade, net of costs"
          tone={(summary?.expectancy_r ?? 0) > 0 ? "text-emerald-300" : "text-rose-300"}
        />
        <Stat
          label="profit factor"
          value={summary ? summary.profit_factor.toFixed(2) : "—"}
          sub={`max DD ${summary?.max_drawdown_points ?? 0} pts`}
          tone={(summary?.profit_factor ?? 0) >= 1 ? "text-emerald-300" : "text-rose-300"}
        />
      </div>

      <div className="grid gap-5 lg:grid-cols-[360px_1fr]">
        {/* ---- the IB ladder: latest real session --------------------- */}
        <Card>
          <CardHead
            title="Latest session"
            subtitle={`${latest.date} · IB from the first 60 minutes`}
          />
          <div className="flex gap-5 px-5 py-5">
            <IBLadder
              high={latest.ibHigh}
              low={latest.ibLow}
              mid={latest.ibMid}
              entry={latestTrade?.entry ?? null}
              stop={latestTrade?.stop ?? null}
              target={latestTrade?.target ?? null}
            />
            <dl className="flex-1 space-y-2 text-xs">
              {[
                ["IB high", latest.ibHigh?.toFixed(1) ?? "—"],
                ["IB low", latest.ibLow?.toFixed(1) ?? "—"],
                ["range", latest.ibRange?.toFixed(1) ?? "—"],
                ["midpoint", latest.ibMid?.toFixed(1) ?? "—"],
                ["bars in IB", String(latest.ibBars)],
                ["formed first", latest.formedFirst ?? "undefined"],
                ["broke first", latest.brokeFirst ?? "no break"],
              ].map(([k, v]) => (
                <div key={k} className="flex justify-between border-b border-[var(--color-line-soft)] pb-1.5">
                  <dt className="text-slate-500">{k}</dt>
                  <dd className="mono text-slate-200">{v}</dd>
                </div>
              ))}
              {latestTrade ? (
                <div className="flex justify-between pt-1">
                  <dt className="text-slate-500">outcome</dt>
                  <dd>
                    <Pill tone={OUTCOME_TONE[latestTrade.outcome] ?? "slate"}>
                      {latestTrade.outcome.toLowerCase().replace("_", " ")}
                    </Pill>
                  </dd>
                </div>
              ) : null}
            </dl>
          </div>
          <div className="border-t border-[var(--color-line-soft)] px-5 py-3">
            <p className="text-[11px] leading-relaxed text-slate-500">
              Data cached: {freshness.cachedBars.toLocaleString()} bars · last session{" "}
              {freshness.lastSession ?? "—"}
              {freshness.stale ? " · study not re-run recently" : ""}
            </p>
          </div>
        </Card>

        {/* ---- the conditional frequency ------------------------------ */}
        <Card>
          <CardHead
            title="First-break frequency"
            subtitle="Given which IB extreme formed first, which one gives way first?"
          />
          <div className="px-5 py-4">
            <Table>
              <thead>
                <tr>
                  <Th>formed first</Th>
                  <Th align="right">resolved</Th>
                  <Th align="right">broke low</Th>
                  <Th align="right">% broke low</Th>
                  <Th align="right">95% CI</Th>
                  <Th align="right">excludes 50%</Th>
                </tr>
              </thead>
              <tbody>
                {firstBreak.map((r) => (
                  <tr key={r.formedFirst}>
                    <Td>
                      <span className="mono text-slate-200">{r.formedFirst}</span>
                    </Td>
                    <Td align="right">
                      <span className="mono text-slate-400">{r.resolved}</span>
                      {r.ambiguous > 0 || r.noBreak > 0 ? (
                        <span className="ml-1 text-[10px] text-slate-600">
                          (+{r.ambiguous} amb, +{r.noBreak} no break)
                        </span>
                      ) : null}
                    </Td>
                    <Td align="right">
                      <span className="mono text-slate-400">{r.brokeLow}</span>
                    </Td>
                    <Td align="right">
                      <span className="mono font-semibold text-slate-100">
                        {pct(r.pctBrokeLow, 2)}
                      </span>
                    </Td>
                    <Td align="right">
                      <span className="mono text-slate-500">
                        {pct(r.ciLow)} – {pct(r.ciHigh)}
                      </span>
                    </Td>
                    <Td align="right">
                      <Pill tone={r.beatsCoinflip ? "green" : "slate"}>
                        {r.beatsCoinflip ? "yes" : "no"}
                      </Pill>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>

            <Explain>
              This effect is real but <strong>mechanical, not behavioural</strong>. When the IB
              high is set early and the low late, price necessarily ends the window near the
              low — and the nearer level is the one that breaks first (measured at 80.6% on
              NIFTY, a stronger predictor than “formed first” itself). So the statistic is
              largely a restatement of where price sits at 10:15, not evidence of an
              overreaction worth fading. Ambiguous sessions — where one bar breaches both
              extremes and OHLC cannot order the two events — are excluded and counted rather
              than guessed.
            </Explain>
          </div>
        </Card>
      </div>

      {/* ---- equity + exit mix ---------------------------------------- */}
      <div className="mt-5 grid gap-5 lg:grid-cols-[1fr_320px]">
        <Card>
          <CardHead
            title="Cumulative P&L"
            subtitle={`${curve.length} filled trades, in index points, net of costs`}
          />
          <div className="px-2 py-4">
            <EquityChart data={curve} />
          </div>
        </Card>

        <Card>
          <CardHead title="How trades ended" subtitle="including the ones never taken" />
          <div className="space-y-2 px-5 py-4">
            {summary
              ? (
                  [
                    ["target hit", summary.target_hits, "green"],
                    ["stopped out", summary.stop_hits, "red"],
                    ["timed out at close", summary.time_exits, "amber"],
                    ["never filled", summary.no_fill, "slate"],
                    ["entry cutoff passed", summary.cutoff, "slate"],
                    ["double break, cancelled", summary.double_break, "slate"],
                    ["ambiguous, excluded", summary.ambiguous, "amber"],
                  ] as const
                ).map(([label, count, tone]) => (
                  <div key={label} className="flex items-center justify-between">
                    <span className="text-xs text-slate-400">{label}</span>
                    <Pill tone={tone}>{count}</Pill>
                  </div>
                ))
              : null}
            {meta ? (
              <p className="mt-3 border-t border-[var(--color-line-soft)] pt-3 text-[11px] leading-relaxed text-slate-500">
                Costs: {meta.costs}. A bar containing both the stop and the target is booked
                as a loss — OHLC cannot order them, and resolving such bars favourably is how
                a backtest talks itself into an edge.
              </p>
            ) : null}
          </div>
        </Card>
      </div>

      {/* ---- session log --------------------------------------------- */}
      <Card className="mt-5">
        <CardHead
          title="Session log"
          subtitle={`${sessions.length} sessions · ${meta?.feed_limit_note ?? ""}`}
        />
        <Table>
          <thead>
            <tr>
              <Th>date</Th>
              <Th align="right">IB high</Th>
              <Th align="right">IB low</Th>
              <Th align="right">range</Th>
              <Th align="right">mid</Th>
              <Th>formed 1st</Th>
              <Th>broke 1st</Th>
              <Th>outcome</Th>
              <Th align="right">points</Th>
              <Th align="right">R</Th>
            </tr>
          </thead>
          <tbody>
            {pageRows.map((s) => {
              const t = trades.find((x) => x.date === s.date);
              return (
                <tr key={s.date}>
                  <Td>
                    <span className="mono text-slate-300">{s.date}</span>
                  </Td>
                  <Td align="right">
                    <span className="mono text-amber-300/80">{s.ibHigh?.toFixed(1) ?? "—"}</span>
                  </Td>
                  <Td align="right">
                    <span className="mono text-sky-300/80">{s.ibLow?.toFixed(1) ?? "—"}</span>
                  </Td>
                  <Td align="right">
                    <span className="mono text-slate-500">{s.ibRange?.toFixed(1) ?? "—"}</span>
                  </Td>
                  <Td align="right">
                    <span className="mono text-slate-400">{s.ibMid?.toFixed(1) ?? "—"}</span>
                  </Td>
                  <Td>
                    <span className="mono text-slate-400">{s.formedFirst ?? "—"}</span>
                  </Td>
                  <Td>
                    <span className="mono text-slate-400">{s.brokeFirst ?? "—"}</span>
                  </Td>
                  <Td>
                    {t ? (
                      <Pill tone={OUTCOME_TONE[t.outcome] ?? "slate"}>
                        {t.outcome.toLowerCase().replace("_", " ")}
                      </Pill>
                    ) : (
                      <span className="text-slate-600">—</span>
                    )}
                  </Td>
                  <Td align="right">
                    <span
                      className={`mono ${
                        !t || t.points === 0
                          ? "text-slate-600"
                          : t.points > 0
                            ? "text-emerald-300"
                            : "text-rose-300"
                      }`}
                    >
                      {t && t.points !== 0 ? t.points.toFixed(1) : "—"}
                    </span>
                  </Td>
                  <Td align="right">
                    <span
                      className={`mono ${
                        !t || t.r === 0
                          ? "text-slate-600"
                          : t.r > 0
                            ? "text-emerald-300"
                            : "text-rose-300"
                      }`}
                    >
                      {t && t.r !== 0 ? t.r.toFixed(2) : "—"}
                    </span>
                  </Td>
                </tr>
              );
            })}
          </tbody>
        </Table>
        <Pagination
          state={{ page: safePage, pageSize, total: sessions.length, pages }}
          paramPrefix="s"
          label="sessions"
        />
      </Card>

      <p className="mt-6 text-[11px] leading-relaxed text-slate-600">
        Sample limits: {meta?.sessions_available ?? sessions.length} sessions is the maximum
        the broker feed returns for 5-minute history, covering a single market regime. With
        {" "}{summary?.filled ?? 0} filled trades this rules out a large edge, not a small one.
        Prices are index spot, not the futures or options contract that would actually be
        traded.
      </p>
    </div>
  );
}
