import ConfigEditor from "@/components/ConfigEditor";
import { Pagination } from "@/components/Pagination";
import { readPageParams } from "@/lib/pagination";
import RunPanel from "@/components/RunPanel";
import { EquityChart, RegimeTimeline } from "@/components/charts";
import { Card, CardHead, EmptyState, Explain, Pill, Stat, Table, Td, Th } from "@/components/ui";
import {
  getEquityCurves,
  getLayer4Diagnostics,
  getExecutions,
  getPortfolioComparison,
  getRegimeAttribution,
  getRegimeTimeline,
} from "@/lib/artifacts";
import { configsForLayer } from "@/lib/config-schema";
import { universeFrom } from "@/lib/universes";
import UniverseSwitcher from "@/components/UniverseSwitcher";
import { count, drawdownTone, pct, ratio, sharpeTone, shortDate } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function RegimePage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const { page, pageSize } = readPageParams(params, "e", 50);
  const universe = universeFrom(params);
  const pick = (key: string) => {
    const v = params[key];
    return (Array.isArray(v) ? v[0] : v) || undefined;
  };
  const [portfolios, equity, attribution, timeline, executions, diagnostics] =
    await Promise.all([
      getPortfolioComparison(universe),
      getEquityCurves(600, universe),
      getRegimeAttribution(universe),
      getRegimeTimeline(900, universe),
      getExecutions({ page, pageSize, regime: pick("regime"), symbol: pick("sym"), universe }),
      getLayer4Diagnostics(universe),
    ]);

  const hasRun = portfolios.length > 0;
  const dynamicPortfolio = portfolios.find((p) => p.portfolio === "Dynamic Regime");
  const baselines = portfolios.filter((p) => p.portfolio !== "Dynamic Regime");
  const bestBaseline = [...baselines].sort((a, b) => (b.sharpe ?? -99) - (a.sharpe ?? -99))[0];
  const overlayWins =
    dynamicPortfolio && bestBaseline
      ? (dynamicPortfolio.sharpe ?? 0) >= (bestBaseline.sharpe ?? 0)
      : false;

  return (
    <div className="mx-auto max-w-[1500px] px-6 py-8 lg:px-10">
      <header className="mb-6">
        <div className="text-[11px] font-semibold uppercase tracking-wider text-cyan-400">
          Layer 4
        </div>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight text-slate-100">
          Regime &amp; Portfolio
        </h1>
        <p className="mt-1.5 max-w-3xl text-[13px] leading-relaxed text-slate-400">
          A Hidden Markov Model reads the market&apos;s state from volatility and trend, then
          capital is routed to whichever strategies suit that state — measured against portfolios
          that ignore regimes entirely.
        </p>
        <div className="mt-3">
          <UniverseSwitcher active={universe.id} />
        </div>
      </header>

      <div className="mb-6">
        <RunPanel
          universe={universe.id}
          layer="layer4"
          label="Run regime detection & portfolio comparison"
          hint="Fits the regime classifier, builds the dynamic portfolio, and compares it head-to-head against a static baseline and buy-and-hold."
          showTopN
        />
      </div>

      {!hasRun ? (
        <Card className="mb-6">
          <EmptyState
            title="No portfolio results yet"
            body="Run the analysis above. You'll get a regime timeline across the full history, a head-to-head comparison of three portfolios, and a log of every position change tagged by the regime it happened in."
          />
        </Card>
      ) : (
        <>
          <div className="mb-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <Stat
              label="Dynamic Sharpe"
              value={ratio(dynamicPortfolio?.sharpe)}
              sub="Regime-switched"
              tone={sharpeTone(dynamicPortfolio?.sharpe ?? null)}
            />
            <Stat
              label="Best baseline Sharpe"
              value={ratio(bestBaseline?.sharpe)}
              sub={bestBaseline?.portfolio}
              tone={sharpeTone(bestBaseline?.sharpe ?? null)}
            />
            <Stat
              label="Dynamic max drawdown"
              value={pct(dynamicPortfolio?.maxDrawdown)}
              sub="Peak to trough"
              tone={drawdownTone(dynamicPortfolio?.maxDrawdown ?? null)}
            />
            <Stat
              label="Position changes"
              value={count(executions.total)}
              sub="Tagged by regime"
            />
          </div>

          {diagnostics.length > 0 ? (
            <div className="mb-6 rounded-xl border border-amber-500/25 bg-amber-500/[0.05] px-5 py-4">
              <h3 className="text-[12.5px] font-semibold text-amber-200">
                Why part of this run held no position
              </h3>
              <ul className="mt-2 space-y-1.5">
                {diagnostics.map((note, i) => (
                  <li key={i} className="text-[11.5px] leading-relaxed text-amber-100/70">
                    {note}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {dynamicPortfolio && bestBaseline ? (
            <div className="mb-6">
              <Explain>
                {overlayWins ? (
                  <>
                    <strong className="text-emerald-300">The overlay is adding value.</strong> The
                    regime-switched portfolio earns a higher risk-adjusted return (
                    {ratio(dynamicPortfolio.sharpe)} Sharpe) than{" "}
                    {bestBaseline.portfolio} ({ratio(bestBaseline.sharpe)}).
                  </>
                ) : (
                  <>
                    <strong className="text-amber-300">
                      The overlay is not adding value here.
                    </strong>{" "}
                    Switching regimes produces a{" "}
                    <em>lower</em> risk-adjusted return ({ratio(dynamicPortfolio.sharpe)} Sharpe)
                    than simply running the same strategies all the time (
                    {ratio(bestBaseline.sharpe)}). Worth knowing before trusting it — a
                    sophisticated overlay is not automatically a better one.
                  </>
                )}
              </Explain>
            </div>
          ) : null}

          <Card className="mb-6">
            <CardHead
              title="Portfolio comparison"
              subtitle="Calmar is annual return divided by max drawdown. Turnover is the average daily position change — all of it charged execution cost, including the portfolio\u2019s own rebalancing."
            />
            <Table>
              <thead>
                <tr>
                  <Th>Portfolio</Th>
                  <Th align="right">Annual return</Th>
                  <Th align="right">Volatility</Th>
                  <Th align="right">Sharpe</Th>
                  <Th align="right">Max drawdown</Th>
                  <Th align="right">Calmar</Th>
                  <Th align="right">Turnover/day</Th>
                  <Th align="right">Total return</Th>
                </tr>
              </thead>
              <tbody>
                {portfolios.map((p) => (
                  <tr
                    key={p.portfolio}
                    className={p.portfolio === "Dynamic Regime" ? "bg-cyan-500/[0.04]" : ""}
                  >
                    <Td className="font-medium text-slate-200">
                      {p.portfolio}
                      {p.portfolio === "Dynamic Regime" ? (
                        <span className="ml-2">
                          <Pill tone="cyan">Overlay</Pill>
                        </span>
                      ) : null}
                    </Td>
                    <Td align="right" className="mono">
                      {pct(p.annualizedReturn)}
                    </Td>
                    <Td align="right" className="mono text-slate-500">
                      {pct(p.annualizedVolatility)}
                    </Td>
                    <Td align="right" className={`mono font-semibold ${sharpeTone(p.sharpe)}`}>
                      {ratio(p.sharpe)}
                    </Td>
                    <Td align="right" className={`mono ${drawdownTone(p.maxDrawdown)}`}>
                      {pct(p.maxDrawdown)}
                    </Td>
                    <Td align="right" className="mono">
                      {ratio(p.calmar)}
                    </Td>
                    <Td align="right" className="mono text-slate-500">
                      {pct(p.avgDailyTurnover, 2)}
                    </Td>
                    <Td align="right" className="mono text-slate-400">
                      {pct(p.totalReturn, 0)}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </Card>

          {equity.points.length > 0 ? (
            <Card className="mb-6">
              <CardHead
                title="Equity curves"
                subtitle="Growth of 1 unit of capital. Note the vertical scale — a steeper line is not automatically better if it also drops further."
              />
              <div className="px-3 py-4">
                <EquityChart points={equity.points} series={equity.series} />
              </div>
            </Card>
          ) : null}

          <div className="mb-6 grid gap-6 xl:grid-cols-[1.3fr_1fr]">
            {timeline.length > 0 ? (
              <Card>
                <CardHead
                  title="Market regime timeline"
                  subtitle="What the classifier saw, day by day, across the full history."
                />
                <div className="px-5 py-5">
                  <RegimeTimeline points={timeline} />
                  <div className="mt-4">
                    <Explain>
                      The model is <strong>fitted on early history only</strong> and the later
                      period is inferred with the frozen model, so it never sees its own future.
                      A new regime must also persist for several days before capital moves —
                      otherwise a single odd day would churn the whole portfolio.
                    </Explain>
                  </div>
                </div>
              </Card>
            ) : null}

            {attribution.length > 0 ? (
              <Card>
                <CardHead
                  title="Performance by regime"
                  subtitle="Where the dynamic portfolio actually made and lost money."
                />
                <Table>
                  <thead>
                    <tr>
                      <Th>Regime</Th>
                      <Th align="right">Days</Th>
                      <Th align="right">Return</Th>
                      <Th align="right">Sharpe</Th>
                      <Th align="right">Max DD</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {attribution.map((row) => (
                      <tr key={row.regime}>
                        <Td>
                          <Pill
                            tone={
                              row.regime === 0 ? "red" : row.regime === 1 ? "green" : "amber"
                            }
                          >
                            {row.regimeName}
                          </Pill>
                        </Td>
                        <Td align="right" className="mono text-slate-500">
                          {count(row.days)}
                        </Td>
                        <Td align="right" className="mono">
                          {pct(row.totalReturn, 1)}
                        </Td>
                        <Td align="right" className={`mono ${sharpeTone(row.sharpe)}`}>
                          {ratio(row.sharpe)}
                        </Td>
                        <Td align="right" className={`mono ${drawdownTone(row.maxDrawdown)}`}>
                          {pct(row.maxDrawdown)}
                        </Td>
                      </tr>
                    ))}
                  </tbody>
                </Table>
              </Card>
            ) : null}
          </div>

          {executions.rows.length > 0 ? (
            <Card className="mb-6">
              <CardHead
                title="Position changes by regime"
                subtitle={
                  executions.total === executions.totalUnfiltered
                    ? `${count(executions.totalUnfiltered)} exposure changes, all costed.`
                    : `${count(executions.total)} of ${count(executions.totalUnfiltered)} changes match the filter.`
                }
                action={
                  <div className="flex flex-wrap gap-2">
                    {Object.entries(executions.byRegime).map(([name, n]) => (
                      <Pill
                        key={name}
                        tone={
                          name.startsWith("Bear")
                            ? "red"
                            : name.startsWith("Bull")
                              ? "green"
                              : "amber"
                        }
                      >
                        {name}: {count(n)}
                      </Pill>
                    ))}
                  </div>
                }
              />
              <div className="flex flex-wrap gap-2 border-b border-[var(--color-line-soft)] px-4 py-3">
                <form method="get" className="flex flex-wrap gap-2">
                  <select
                    name="regime"
                    defaultValue={pick("regime") ?? ""}
                    className="mono rounded-md border border-[var(--color-line)] bg-[var(--color-panel2)] px-2 py-1.5 text-[11px] text-slate-300"
                    aria-label="Filter by regime"
                  >
                    <option value="">All regimes</option>
                    {Object.keys(executions.byRegime).map((name) => (
                      <option key={name} value={name}>{name}</option>
                    ))}
                  </select>
                  <select
                    name="sym"
                    defaultValue={pick("sym") ?? ""}
                    className="mono rounded-md border border-[var(--color-line)] bg-[var(--color-panel2)] px-2 py-1.5 text-[11px] text-slate-300"
                    aria-label="Filter by symbol"
                  >
                    <option value="">All symbols</option>
                    {executions.symbols.map((s) => (
                      <option key={s} value={s}>{s}</option>
                    ))}
                  </select>
                  <button className="rounded-md bg-cyan-500 px-3 py-1.5 text-[11px] font-semibold text-slate-950 hover:bg-cyan-400">
                    Apply
                  </button>
                </form>
              </div>
              <Table>
                <thead>
                  <tr>
                    <Th>Date</Th>
                    <Th>Symbol</Th>
                    <Th>Strategy</Th>
                    <Th align="right">Change</Th>
                    <Th align="right">Exposure after</Th>
                    <Th>Regime</Th>
                  </tr>
                </thead>
                <tbody>
                  {executions.rows.map((row, i) => (
                    <tr key={i} className="hover:bg-[var(--color-panel2)]">
                      <Td className="mono text-slate-500">{shortDate(row.date)}</Td>
                      <Td className="mono font-semibold text-slate-200">{row.symbol}</Td>
                      <Td>{row.strategy}</Td>
                      <Td
                        align="right"
                        className={`mono ${
                          (row.exposureChange ?? 0) >= 0 ? "text-emerald-400" : "text-rose-400"
                        }`}
                      >
                        {(row.exposureChange ?? 0) >= 0 ? "+" : ""}
                        {ratio(row.exposureChange, 3)}
                      </Td>
                      <Td align="right" className="mono text-slate-400">
                        {ratio(row.exposureAfter, 3)}
                      </Td>
                      <Td>
                        <Pill
                          tone={
                            row.regimeName?.startsWith("Bear")
                              ? "red"
                              : row.regimeName?.startsWith("Bull")
                                ? "green"
                                : "amber"
                          }
                        >
                          {row.regimeName}
                        </Pill>
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
              <Pagination
                state={{
                  page: executions.page,
                  pageSize: executions.pageSize,
                  total: executions.total,
                  pages: executions.pages,
                }}
                paramPrefix="e"
                label="exposure changes"
              />
            </Card>
          ) : null}
        </>
      )}

      <ConfigEditor
        files={configsForLayer(4)}
        title="Layer 4 settings"
        description="Regime detection, position sizing and risk gates, and which strategy families receive capital in each market state."
      />
    </div>
  );
}
