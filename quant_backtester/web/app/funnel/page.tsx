import ConfigEditor from "@/components/ConfigEditor";
import { Pagination } from "@/components/Pagination";
import { readPageParams } from "@/lib/pagination";
import RunPanel from "@/components/RunPanel";
import { IsOosScatter } from "@/components/charts";
import { Card, CardHead, EmptyState, Explain, Pill, Stat, Table, Td, Th } from "@/components/ui";
import {
  getAllConfigurations,
  getFunnelStages,
  getLayer2Manifest,
  getScatter,
  getSurvivors,
  getFreshness,
} from "@/lib/artifacts";
import { configsForLayer } from "@/lib/config-schema";
import { universeFrom } from "@/lib/universes";
import UniverseSwitcher from "@/components/UniverseSwitcher";
import { count, drawdownTone, pct, ratio, sharpeTone } from "@/lib/format";

export const dynamic = "force-dynamic";

const GATE_MEANING: Record<number, string> = {
  1: "Too few trades for the numbers to mean anything.",
  2: "Didn't earn enough return for the risk taken, on unseen data.",
  3: "Lost too much peak-to-trough to be holdable.",
  4: "Looked far better in training than in testing — fitted to noise.",
  5: "Gross wins didn't sufficiently exceed gross losses.",
  6: "Could plausibly have looked this good by luck, given how many were tried.",
};

export default async function FunnelPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const { page, pageSize } = readPageParams(params, "c", 50);
  const universe = universeFrom(params);
  const [stages, scatter, configs, survivors, manifest, freshness] = await Promise.all([
    getFunnelStages(universe),
    getScatter(universe),
    getAllConfigurations(universe),
    getSurvivors(universe),
    getLayer2Manifest(universe),
    getFreshness(universe),
  ]);

  const hasRun = stages.length > 0;
  // Show survivors when there are any; otherwise the full ranked list, paged
  // rather than truncated so nothing is silently hidden.
  const ranked = [...configs]
    .filter((c) => c.oosSharpe !== null)
    .sort((a, b) => (b.oosSharpe ?? 0) - (a.oosSharpe ?? 0));
  const source = survivors.length > 0 ? survivors : ranked;
  const pages = Math.max(Math.ceil(source.length / pageSize), 1);
  const safePage = Math.min(page, pages);
  const visible = source.slice((safePage - 1) * pageSize, safePage * pageSize);

  const overfitCount = scatter.filter(
    (p) => p.isSharpe !== null && p.oosSharpe !== null && p.isSharpe > p.oosSharpe
  ).length;

  return (
    <div className="mx-auto max-w-[1500px] px-6 py-8 lg:px-10">
      <header className="mb-6">
        <div className="text-[11px] font-semibold uppercase tracking-wider text-cyan-400">
          Layer 2
        </div>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight text-slate-100">
          Validation Funnel
        </h1>
        <p className="mt-1.5 max-w-3xl text-[13px] leading-relaxed text-slate-400">
          Every configuration is tuned on one slice of history and judged on the next, untouched
          slice. Then six gates try to kill it. What survives has earned a second look.
        </p>
        <div className="mt-3">
          <UniverseSwitcher active={universe.id} />
        </div>
      </header>

      <div className="mb-6">
        <RunPanel
          universe={universe.id}
          layer="layer2"
          label="Run walk-forward backtest & funnel"
          hint="Scores every asset × strategy × parameter combination on out-of-sample data, then applies the six gates configured below."
          showSymbols
          showGenerateSignals
        />
      </div>

      {freshness.stale ? (
        <div className="mb-6 rounded-xl border border-amber-500/30 bg-amber-500/[0.06] px-5 py-4">
          <h3 className="text-[12.5px] font-semibold text-amber-200">
            These results are out of date
          </h3>
          <p className="mt-1.5 text-[11.5px] leading-relaxed text-amber-100/70">
            {freshness.missingFromResults.length} configured{" "}
            {freshness.missingFromResults.length === 1 ? "strategy has" : "strategies have"} no
            results here — {freshness.missingFromResults.slice(0, 6).join(", ")}
            {freshness.missingFromResults.length > 6 ? "…" : ""}. The numbers below were computed
            before they were added. Re-run the backtest above to include them.
          </p>
        </div>
      ) : null}

      {!hasRun ? (
        <Card>
          <EmptyState
            title="No backtest results yet"
            body="Run the walk-forward backtest above. You'll get a per-configuration score on unseen data, plus a breakdown of which validation gate eliminated each one."
          />
        </Card>
      ) : (
        <>
          <div className="mb-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <Stat
              label="Tested"
              value={count(configs.length)}
              sub={
                freshness.computedAt
                  ? `${freshness.strategiesInResults.length} strategies · ${
                      freshness.ageHours !== null && freshness.ageHours < 1
                        ? "just now"
                        : `${Math.round(freshness.ageHours ?? 0)}h ago`
                    }`
                  : "Configurations"
              }
            />
            <Stat
              label="Survivors"
              value={count(survivors.length)}
              sub={configs.length ? pct(survivors.length / configs.length, 1) : undefined}
              tone={survivors.length > 0 ? "text-emerald-400" : "text-slate-400"}
            />
            <Stat
              label="Worse out-of-sample"
              value={count(overfitCount)}
              sub={scatter.length ? `${pct(overfitCount / scatter.length, 0)} of all tested` : undefined}
              tone="text-amber-400"
            />
            <Stat
              label="Walk-forward"
              value={`${manifest?.walk_forward?.is_years ?? "—"}y / ${manifest?.walk_forward?.oos_years ?? "—"}y`}
              sub="Train / test window"
            />
          </div>

          <div className="mb-6 grid gap-6 xl:grid-cols-[1fr_1.15fr]">
            <Card>
              <CardHead
                title="The six gates"
                subtitle="Each bar shows how many configurations were still alive after that gate."
              />
              <div className="space-y-3.5 px-5 py-5">
                {stages.map((stage) => {
                  const total = stages[0]?.entered || 1;
                  const width = (stage.survived / total) * 100;
                  return (
                    <div key={stage.stage}>
                      <div className="flex flex-wrap items-baseline justify-between gap-2">
                        <span className="text-[12px] font-medium text-slate-200">
                          <span className="text-slate-500">Gate {stage.stage} · </span>
                          {stage.name}
                        </span>
                        <span className="mono text-[11px] text-slate-400">
                          {count(stage.entered)} → {count(stage.survived)}
                          <span className="ml-2 text-rose-400/80">
                            −{count(stage.rejected)}
                          </span>
                        </span>
                      </div>
                      <div className="mt-1.5 h-2.5 overflow-hidden rounded-full bg-[var(--color-panel2)]">
                        <div
                          className={`h-full rounded-full transition-all ${
                            stage.survived === 0 ? "bg-rose-500/70" : "bg-cyan-500/70"
                          }`}
                          style={{ width: `${Math.max(width, stage.survived > 0 ? 1.5 : 0)}%` }}
                        />
                      </div>
                      <p className="mt-1.5 text-[10.5px] leading-relaxed text-slate-500">
                        <span className="mono text-slate-600">{stage.description}</span>
                        {" — "}
                        {GATE_MEANING[stage.stage]}
                      </p>
                    </div>
                  );
                })}
              </div>
            </Card>

            <Card>
              <CardHead
                title="In-sample vs out-of-sample"
                subtitle="Each dot is one configuration. The dashed diagonal is where training and testing agree."
              />
              <div className="px-3 py-4">
                <IsOosScatter points={scatter} />
              </div>
              <div className="px-5 pb-5">
                <Explain>
                  Points <strong>below the diagonal</strong> did worse on unseen data than on the
                  data they were tuned on — the visual signature of overfitting. A genuinely
                  robust strategy sits near the line. Points far to the right but near the bottom
                  are the dangerous ones: spectacular in training, worthless in reality.
                </Explain>
              </div>
            </Card>
          </div>

          <Card className="mb-6">
            <CardHead
              title={survivors.length > 0 ? "Survivors" : "Best configurations (none survived)"}
              subtitle={
                survivors.length > 0
                  ? "Cleared all six gates."
                  : "Nothing cleared every gate. These scored highest out-of-sample — useful for diagnosis, not for deployment."
              }
              action={
                survivors.length === 0 ? <Pill tone="amber">0 survivors</Pill> : (
                  <Pill tone="green">{survivors.length} survivors</Pill>
                )
              }
            />
            <Table>
              <thead>
                <tr>
                  <Th>Symbol</Th>
                  <Th>Strategy</Th>
                  <Th>Parameters</Th>
                  <Th align="right">IS Sharpe</Th>
                  <Th align="right">OOS Sharpe</Th>
                  <Th align="right">Max DD</Th>
                  <Th align="right">Profit factor</Th>
                  <Th align="right">Trades</Th>
                </tr>
              </thead>
              <tbody>
                {visible.map((row, i) => (
                  <tr key={i} className="hover:bg-[var(--color-panel2)]">
                    <Td className="mono font-semibold text-slate-200">{row.symbol}</Td>
                    <Td>{row.strategy}</Td>
                    <Td className="mono max-w-[280px] truncate text-[11px] text-slate-500">
                      {row.paramKey || "—"}
                    </Td>
                    <Td align="right" className="mono">
                      {ratio(row.isSharpe)}
                    </Td>
                    <Td align="right" className={`mono font-semibold ${sharpeTone(row.oosSharpe)}`}>
                      {ratio(row.oosSharpe)}
                    </Td>
                    <Td align="right" className={`mono ${drawdownTone(row.oosMaxDrawdown)}`}>
                      {pct(row.oosMaxDrawdown)}
                    </Td>
                    <Td align="right" className="mono">
                      {ratio(row.oosProfitFactor)}
                    </Td>
                    <Td align="right" className="mono text-slate-500">
                      {count(row.oosNumTrades)}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
            <Pagination
              state={{ page: safePage, pageSize, total: source.length, pages }}
              paramPrefix="c"
              label="configurations"
            />
          </Card>
        </>
      )}

      <ConfigEditor
        files={configsForLayer(2)}
        title="Layer 2 settings"
        description="Execution costs, walk-forward windows, and the six gate thresholds. Loosening a gate lets more strategies through — including ones that only look good by chance."
      />
    </div>
  );
}
