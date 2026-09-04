import ConfigEditor from "@/components/ConfigEditor";
import { Pagination } from "@/components/Pagination";
import { readPageParams } from "@/lib/pagination";
import RunPanel from "@/components/RunPanel";
import { RobustnessScatter } from "@/components/charts";
import { Card, CardHead, EmptyState, Explain, Pill, Stat, Table, Td, Th } from "@/components/ui";
import { getRobustness, getUltraRobust } from "@/lib/artifacts";
import { configsForLayer } from "@/lib/config-schema";
import { universeFrom } from "@/lib/universes";
import UniverseSwitcher from "@/components/UniverseSwitcher";
import { count, pct, ratio, sharpeTone } from "@/lib/format";

export const dynamic = "force-dynamic";

function verdictTone(verdict: string) {
  if (verdict === "PASS") return "green";
  if (verdict === "FAIL") return "red";
  return "slate";
}

export default async function RobustnessPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const { page, pageSize } = readPageParams(params, "r", 50);
  const universe = universeFrom(params);
  const [rows, ultra] = await Promise.all([getRobustness(universe), getUltraRobust(universe)]);
  const hasRun = rows.length > 0;
  const pages = Math.max(Math.ceil(rows.length / pageSize), 1);
  const safePage = Math.min(page, pages);
  const visible = rows.slice((safePage - 1) * pageSize, safePage * pageSize);

  const stable = rows.filter((r) => r.isStable).length;
  const passed = rows.filter((r) => r.bootstrapVerdict === "PASS").length;
  const insufficient = rows.filter((r) => r.bootstrapVerdict === "INSUFFICIENT_DATA").length;

  return (
    <div className="mx-auto max-w-[1500px] px-6 py-8 lg:px-10">
      <header className="mb-6">
        <div className="text-[11px] font-semibold uppercase tracking-wider text-cyan-400">
          Layer 3
        </div>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight text-slate-100">Robustness</h1>
        <p className="mt-1.5 max-w-3xl text-[13px] leading-relaxed text-slate-400">
          Two independent stress tests. One asks whether the exact parameter values mattered too
          much. The other asks whether the result depended on a lucky order of trades.
        </p>
        <div className="mt-3">
          <UniverseSwitcher active={universe.id} />
        </div>
      </header>

      <div className="mb-6 grid gap-4 lg:grid-cols-2">
        <div className="rounded-xl border border-[var(--color-line-soft)] bg-[var(--color-panel)] p-5">
          <h3 className="text-[13px] font-semibold text-slate-200">Parameter sensitivity</h3>
          <p className="mt-1.5 text-[11.5px] leading-relaxed text-slate-500">
            Nudges each parameter to nearby values and re-tests. A robust strategy sits on a{" "}
            <strong className="text-slate-400">plateau</strong> where the neighbours also work. An
            overfit one is an <strong className="text-slate-400">island</strong> — move the RSI
            period by one and the edge vanishes.
          </p>
        </div>
        <div className="rounded-xl border border-[var(--color-line-soft)] bg-[var(--color-panel)] p-5">
          <h3 className="text-[13px] font-semibold text-slate-200">Bootstrap stress test</h3>
          <p className="mt-1.5 text-[11.5px] leading-relaxed text-slate-500">
            Reshuffles the exact same trades hundreds of times to build alternate histories. If
            the drawdown explodes when the order changes, the strategy was relying on a specific
            lucky sequence rather than an edge.
          </p>
        </div>
      </div>

      <div className="mb-6">
        <RunPanel
          universe={universe.id}
          layer="layer3"
          label="Run robustness analysis"
          hint="Stress-tests everything that cleared the funnel. If nothing survived Layer 2, set a fallback top-N to examine the best-scoring configurations anyway."
          showTopN
        />
      </div>

      {!hasRun ? (
        <Card className="mb-6">
          <EmptyState
            title="No robustness results yet"
            body="Run the analysis above. Each surviving configuration gets its parameter neighbourhood re-tested and its trade sequence reshuffled hundreds of times."
          />
        </Card>
      ) : (
        <>
          <div className="mb-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <Stat label="Evaluated" value={count(rows.length)} sub="Configurations" />
            <Stat
              label="Stable parameters"
              value={count(stable)}
              sub="On a plateau, not an island"
              tone={stable > 0 ? "text-emerald-400" : "text-slate-400"}
            />
            <Stat
              label="Survived reshuffling"
              value={count(passed)}
              sub={insufficient > 0 ? `${count(insufficient)} had too few trades to judge` : undefined}
              tone={passed > 0 ? "text-emerald-400" : "text-slate-400"}
            />
            <Stat
              label="Ultra-robust"
              value={count(ultra.length)}
              sub="Passed both tests"
              tone={ultra.length > 0 ? "text-emerald-400" : "text-slate-400"}
            />
          </div>

          <div className="mb-6 grid gap-6 xl:grid-cols-[1.1fr_1fr]">
            <Card>
              <CardHead
                title="Stability vs sequence risk"
                subtitle="The safe corner is bottom-right: stable parameters, and reshuffling doesn't worsen the drawdown."
              />
              <div className="px-3 py-4">
                <RobustnessScatter points={rows} />
              </div>
              <div className="px-5 pb-5">
                <Explain>
                  The horizontal axis is parameter stability (1.0 = every neighbour works). The
                  vertical axis is how much worse the drawdown got under reshuffling — anything{" "}
                  <strong>above the dashed line</strong> means history was kinder than an average
                  alternate universe would have been.
                </Explain>
              </div>
            </Card>

            <Card>
              <CardHead
                title="Ultra-robust set"
                subtitle="Stable under parameter changes AND under trade reshuffling."
                action={
                  ultra.length > 0 ? (
                    <Pill tone="green">{ultra.length} passed</Pill>
                  ) : (
                    <Pill tone="amber">None</Pill>
                  )
                }
              />
              {ultra.length === 0 ? (
                <EmptyState
                  title="Nothing passed both tests"
                  body="That is a legitimate result, not an error. Naked strategies with no risk management frequently fail one of these two checks — which is exactly what the layer exists to reveal."
                />
              ) : (
                <Table>
                  <thead>
                    <tr>
                      <Th>Symbol</Th>
                      <Th>Strategy</Th>
                      <Th align="right">OOS Sharpe</Th>
                      <Th align="right">Stability</Th>
                      <Th align="right">5th pct return</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {ultra.map((row, i) => (
                      <tr key={i}>
                        <Td className="mono font-semibold text-slate-200">{row.symbol}</Td>
                        <Td>{row.strategy}</Td>
                        <Td align="right" className={`mono ${sharpeTone(row.oosSharpe)}`}>
                          {ratio(row.oosSharpe)}
                        </Td>
                        <Td align="right" className="mono">
                          {ratio(row.sensitivityScore)}
                        </Td>
                        <Td align="right" className="mono text-emerald-400">
                          {pct(row.p5FinalReturn, 0)}
                        </Td>
                      </tr>
                    ))}
                  </tbody>
                </Table>
              )}
            </Card>
          </div>

          <Card className="mb-6">
            <CardHead
              title="All stress-test results"
              subtitle="Drawdown blow-up above 1.0 means reshuffling produced a worse drawdown than history actually delivered."
            />
            <Table>
              <thead>
                <tr>
                  <Th>Symbol</Th>
                  <Th>Strategy</Th>
                  <Th align="right">OOS Sharpe</Th>
                  <Th align="right">Stability</Th>
                  <Th align="right">Neighbours</Th>
                  <Th align="right">5th pct return</Th>
                  <Th align="right">95th pct DD</Th>
                  <Th align="right">Blow-up</Th>
                  <Th align="right">Loss streak</Th>
                  <Th>Verdict</Th>
                </tr>
              </thead>
              <tbody>
                {visible.map((row, i) => (
                  <tr key={i} className="hover:bg-[var(--color-panel2)]">
                    <Td className="mono font-semibold text-slate-200">{row.symbol}</Td>
                    <Td>{row.strategy}</Td>
                    <Td align="right" className={`mono ${sharpeTone(row.oosSharpe)}`}>
                      {ratio(row.oosSharpe)}
                    </Td>
                    <Td
                      align="right"
                      className={`mono ${row.isStable ? "text-emerald-400" : "text-amber-400"}`}
                    >
                      {ratio(row.sensitivityScore)}
                    </Td>
                    <Td align="right" className="mono text-slate-500">
                      {count(row.numNeighboursTested)}
                    </Td>
                    <Td align="right" className="mono">
                      {pct(row.p5FinalReturn, 0)}
                    </Td>
                    <Td align="right" className="mono">
                      {pct(row.p95MaxDrawdown)}
                    </Td>
                    <Td
                      align="right"
                      className={`mono ${
                        (row.drawdownAmplification ?? 0) > 1.3 ? "text-rose-400" : "text-slate-400"
                      }`}
                    >
                      {row.drawdownAmplification === null
                        ? "—"
                        : `×${ratio(row.drawdownAmplification)}`}
                    </Td>
                    <Td align="right" className="mono text-slate-500">
                      {count(row.worstLossStreak)}
                    </Td>
                    <Td>
                      <span title={row.bootstrapReason}>
                        <Pill tone={verdictTone(row.bootstrapVerdict)}>
                          {row.bootstrapVerdict === "INSUFFICIENT_DATA"
                            ? "Too few trades"
                            : row.bootstrapVerdict}
                        </Pill>
                      </span>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
            <Pagination
              state={{ page: safePage, pageSize, total: rows.length, pages }}
              paramPrefix="r"
              label="results"
            />
          </Card>
        </>
      )}

      <ConfigEditor
        files={configsForLayer(3)}
        title="Layer 3 settings"
        description="How far parameters are perturbed, how many alternate universes to simulate, and the pass/fail rules for both tests."
      />
    </div>
  );
}
