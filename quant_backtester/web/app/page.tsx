import Link from "next/link";
import RunPanel from "@/components/RunPanel";
import { Card, CardHead, Explain, Pill, Stat } from "@/components/ui";
import {
  getFunnelStages,
  getPipelineStatus,
  getPortfolioComparison,
  getUltraRobust,
} from "@/lib/artifacts";
import { universeFrom } from "@/lib/universes";
import UniverseSwitcher from "@/components/UniverseSwitcher";
import { count, pct, ratio } from "@/lib/format";

export const dynamic = "force-dynamic";

const LAYERS = [
  {
    key: "layer1" as const,
    n: 1,
    title: "Data & Signals",
    blurb: "Download 15 years of daily bars and generate signals across every strategy and parameter combination.",
    href: "/data",
  },
  {
    key: "layer2" as const,
    n: 2,
    title: "Walk-Forward & Funnel",
    blurb: "Backtest each configuration on unseen data, then push it through six validation gates.",
    href: "/funnel",
  },
  {
    key: "layer3" as const,
    n: 3,
    title: "Robustness",
    blurb: "Perturb parameters and reshuffle trades to separate real edge from lucky history.",
    href: "/robustness",
  },
  {
    key: "layer4" as const,
    n: 4,
    title: "Regime & Portfolio",
    blurb: "Detect market regimes and route capital dynamically, versus static baselines.",
    href: "/regime",
  },
];

export default async function OverviewPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const universe = universeFrom(await searchParams);
  const [status, stages, portfolios, ultra] = await Promise.all([
    getPipelineStatus(universe),
    getFunnelStages(universe),
    getPortfolioComparison(universe),
    getUltraRobust(universe),
  ]);

  const nothingRun = !status.layer1.done && !status.layer2.done;
  const dynamicPortfolio = portfolios.find((p) => p.portfolio === "Dynamic Regime");
  const bestBaseline = portfolios
    .filter((p) => p.portfolio !== "Dynamic Regime")
    .sort((a, b) => (b.sharpe ?? -99) - (a.sharpe ?? -99))[0];

  return (
    <div className="mx-auto max-w-[1500px] px-6 py-8 lg:px-10">
      <header className="mb-7">
        <h1 className="text-2xl font-semibold tracking-tight text-slate-100">Overview</h1>
        <p className="mt-1.5 max-w-3xl text-[13px] leading-relaxed text-slate-400">
          A four-stage research pipeline for testing whether a trading strategy has a real edge —
          or just looks good on the data it was tuned on.
        </p>
        <div className="mt-3">
          <UniverseSwitcher active={universe.id} />
        </div>
      </header>

      {nothingRun ? (
        <Card className="mb-6">
          <div className="px-5 py-6">
            <h2 className="text-[15px] font-semibold text-slate-100">Start here</h2>
            <p className="mt-1.5 max-w-2xl text-[12.5px] leading-relaxed text-slate-400">
              Nothing has been run yet. The quickest way to see the whole system work is to run
              Layers 2–4 on a handful of symbols — that downloads the data, generates signals,
              validates them and builds the portfolios in one pass.
            </p>
            <div className="mt-5 space-y-3">
              <RunPanel
          universe={universe.id}
                layer="layer2"
                label="Quick start — validate a few symbols"
                hint="Downloads data, generates signals in-process and runs the walk-forward backtest plus the six-gate funnel."
                showSymbols
                showGenerateSignals
              />
            </div>
          </div>
        </Card>
      ) : null}

      <div className="mb-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Stat
          label="Symbols cached"
          value={count(status.layer1.symbols)}
          sub={status.layer1.signals > 0 ? `${count(status.layer1.signals)} signal files` : "Layer 1"}
        />
        <Stat
          label="Configurations tested"
          value={count(status.layer2.tested)}
          sub="Asset × strategy × parameters"
        />
        <Stat
          label="Cleared the funnel"
          value={count(status.layer2.survivors)}
          sub={
            status.layer2.tested > 0
              ? `${pct(status.layer2.survivors / status.layer2.tested, 1)} survival rate`
              : "Layer 2"
          }
          tone={status.layer2.survivors > 0 ? "text-emerald-400" : "text-slate-400"}
        />
        <Stat
          label="Ultra-robust"
          value={count(status.layer3.ultraRobust)}
          sub={status.layer3.done ? `of ${count(status.layer3.evaluated)} stress-tested` : "Layer 3"}
          tone={status.layer3.ultraRobust > 0 ? "text-emerald-400" : "text-slate-400"}
        />
      </div>

      <div className="grid gap-6 xl:grid-cols-[1.25fr_1fr]">
        <div className="space-y-6">
          <Card>
            <CardHead
              title="Pipeline"
              subtitle="Each layer feeds the next. Run them in order, or jump to any stage."
            />
            <ul className="divide-y divide-[var(--color-line-soft)]">
              {LAYERS.map((layer) => {
                const state = status[layer.key];
                return (
                  <li key={layer.key} className="flex items-center gap-4 px-5 py-4">
                    <span
                      className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-[12px] font-bold ${
                        state.done
                          ? "bg-emerald-500/15 text-emerald-300"
                          : "bg-slate-700/40 text-slate-500"
                      }`}
                    >
                      {layer.n}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <Link
                          href={layer.href}
                          className="text-[13px] font-semibold text-slate-200 hover:text-cyan-300"
                        >
                          {layer.title}
                        </Link>
                        {state.done ? (
                          <Pill tone="green">Complete</Pill>
                        ) : (
                          <Pill tone="slate">Not run</Pill>
                        )}
                      </div>
                      <p className="mt-1 text-[11.5px] leading-relaxed text-slate-500">
                        {layer.blurb}
                      </p>
                    </div>
                    <Link
                      href={layer.href}
                      className="shrink-0 rounded-lg border border-[var(--color-line)] px-3 py-1.5 text-[11px] font-medium text-slate-300 transition hover:border-cyan-500/40 hover:text-cyan-300"
                    >
                      Open
                    </Link>
                  </li>
                );
              })}
            </ul>
          </Card>

          {stages.length > 0 ? (
            <Card>
              <CardHead
                title="Where strategies died"
                subtitle="Survivors after each validation gate."
              />
              <div className="space-y-2.5 px-5 py-5">
                {stages.map((stage) => {
                  const width = stage.entered > 0 ? (stage.survived / stages[0].entered) * 100 : 0;
                  return (
                    <div key={stage.stage}>
                      <div className="flex items-baseline justify-between gap-3 text-[11.5px]">
                        <span className="text-slate-300">
                          <span className="text-slate-500">{stage.stage}.</span> {stage.name}
                        </span>
                        <span className="mono text-slate-400">
                          {count(stage.entered)} → {count(stage.survived)}
                        </span>
                      </div>
                      <div className="mt-1.5 h-2 overflow-hidden rounded-full bg-[var(--color-panel2)]">
                        <div
                          className={`h-full rounded-full ${
                            stage.survived === 0 ? "bg-rose-500/70" : "bg-cyan-500/70"
                          }`}
                          style={{ width: `${Math.max(width, stage.survived > 0 ? 1.5 : 0)}%` }}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>
            </Card>
          ) : null}
        </div>

        <div className="space-y-6">
          {portfolios.length > 0 ? (
            <Card>
              <CardHead title="Portfolio results" subtitle="Layer 4 head-to-head." />
              <div className="space-y-3 px-5 py-5">
                {portfolios.map((p) => (
                  <div
                    key={p.portfolio}
                    className="rounded-lg border border-[var(--color-line-soft)] bg-[var(--color-panel2)] px-4 py-3"
                  >
                    <div className="flex items-center justify-between gap-3">
                      <span className="text-[12.5px] font-medium text-slate-200">
                        {p.portfolio}
                      </span>
                      <span className="mono text-[13px] font-semibold text-cyan-300">
                        {ratio(p.sharpe)}
                      </span>
                    </div>
                    <div className="mono mt-1.5 flex gap-4 text-[10.5px] text-slate-500">
                      <span>ret {pct(p.annualizedReturn)}</span>
                      <span>dd {pct(p.maxDrawdown)}</span>
                      <span>calmar {ratio(p.calmar)}</span>
                    </div>
                  </div>
                ))}
              </div>
              {dynamicPortfolio && bestBaseline ? (
                <div className="px-5 pb-5">
                  <Explain>
                    {(dynamicPortfolio.sharpe ?? 0) >= (bestBaseline.sharpe ?? 0) ? (
                      <>
                        The regime overlay is currently <strong>beating</strong> its best baseline
                        on risk-adjusted return ({ratio(dynamicPortfolio.sharpe)} vs{" "}
                        {ratio(bestBaseline.sharpe)} Sharpe).
                      </>
                    ) : (
                      <>
                        The regime overlay is currently <strong>underperforming</strong>{" "}
                        {bestBaseline.portfolio} ({ratio(dynamicPortfolio.sharpe)} vs{" "}
                        {ratio(bestBaseline.sharpe)} Sharpe). Switching regimes is costing
                        risk-adjusted return rather than adding it — worth knowing before
                        trusting the overlay.
                      </>
                    )}
                  </Explain>
                </div>
              ) : null}
            </Card>
          ) : null}

          {ultra.length > 0 ? (
            <Card>
              <CardHead
                title="Ultra-robust strategies"
                subtitle="Passed every gate, stable parameters, survived reshuffling."
              />
              <ul className="divide-y divide-[var(--color-line-soft)]">
                {ultra.slice(0, 6).map((row, i) => (
                  <li key={i} className="flex items-center justify-between gap-3 px-5 py-3">
                    <span className="min-w-0">
                      <span className="mono text-[12px] font-semibold text-slate-200">
                        {row.symbol}
                      </span>
                      <span className="ml-2 text-[11.5px] text-slate-400">{row.strategy}</span>
                    </span>
                    <span className="mono shrink-0 text-[11.5px] text-emerald-400">
                      {ratio(row.oosSharpe)}
                    </span>
                  </li>
                ))}
              </ul>
            </Card>
          ) : null}

          <Card>
            <CardHead title="Run everything" subtitle="All four layers in sequence." />
            <div className="p-5">
              <RunPanel
          universe={universe.id}
                layer="all"
                label="Full pipeline"
                hint="Runs Layers 1 → 4 end to end using the current configuration. This is the slowest option; expect several minutes on the full universe."
                showTopN
              />
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}
