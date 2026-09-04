import Link from "next/link";
import { Card, CardHead, EmptyState, Explain, Pill, Stat, Table, Td, Th } from "@/components/ui";
import { getUltraRobust } from "@/lib/artifacts";
import { safeBrokerState } from "@/lib/broker";
import { FAMILY_LABELS, getInventory, type OpsModule } from "@/lib/ops";
import { count, ratio, sharpeTone } from "@/lib/format";

export const dynamic = "force-dynamic";

const FAMILY_TONE: Record<string, string> = {
  premium_selling: "amber",
  mean_reversion: "cyan",
  trend: "green",
  exit_management: "slate",
  income: "green",
  risk_overlay: "red",
};

/** The broker requirement chip.
 *
 *  "needs broker" as an amber warning is noise once a broker is configured —
 *  every live strategy needs one. Report the requirement, and colour it by
 *  whether it is actually satisfied.
 */
function statusPill(module: OpsModule) {
  if (!module.installed) return <Pill tone="red">Missing files</Pill>;
  if (module.running) return <Pill tone="green">Running</Pill>;
  // A library strategy is never "running" — it is a function the backtester
  // calls. Its meaningful status is how much walk-forward testing it has
  // been through, so report that rather than the misleading "Idle".
  if (module.backtests) {
    const { configurations, survivors } = module.backtests;
    if (survivors > 0) {
      return <Pill tone="green">{`${survivors} survivor${survivors > 1 ? "s" : ""}`}</Pill>;
    }
    return <Pill tone="cyan">{`${configurations.toLocaleString()} tested`}</Pill>;
  }
  // Every status below carries the evidence behind it as a tooltip, so
  // "why does it say that?" is answerable without reading the code.
  const why = module.activity_evidence;
  if (module.has_activity)
    return <span title={why}><Pill tone="cyan">Has history</Pill></span>;
  // Only claim "never run" when we actually looked somewhere and found
  // nothing. Otherwise say plainly that nothing is tracked.
  if (!module.activity_checked)
    return <span title={why}><Pill tone="slate">Not tracked</Pill></span>;
  return <span title={why}><Pill tone="slate">Never run</Pill></span>;
}

export default async function StrategiesPage() {
  const [inventory, ultra, brokerState] = await Promise.all([
    getInventory(),
    getUltraRobust(),
    // Whether the broker requirement these modules declare is actually met.
    safeBrokerState(),
  ]);

  const brokerReady = Boolean(brokerState?.broker?.name);
  const brokerName = brokerState?.broker?.name ?? "";

  if (!inventory) {
    return (
      <div className="mx-auto max-w-[1500px] px-6 py-8 lg:px-10">
        <Card>
          <EmptyState
            title="Inventory unavailable"
            body="Could not read the repository inventory. Check that quant_backtester/ops_cli.py runs."
          />
        </Card>
      </div>
    );
  }

  const production = inventory.modules.filter(
    (m) => m.kind === "production" && m.category === "strategy"
  );
  const research = inventory.modules.filter((m) => m.kind === "research");
  const { summary } = inventory;

  // Group production strategies by family so the list reads as a portfolio of
  // approaches rather than a flat dump of twelve scripts.
  const byFamily = production.reduce<Record<string, OpsModule[]>>((acc, module) => {
    (acc[module.family] ??= []).push(module);
    return acc;
  }, {});

  const ultraByKey = new Map(ultra.map((r) => [`${r.symbol}|${r.strategy}`, r]));

  return (
    <div className="mx-auto max-w-[1500px] px-6 py-8 lg:px-10">
      <header className="mb-6">
        <div className="text-[11px] font-semibold uppercase tracking-wider text-cyan-400">
          Trading Ops
        </div>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight text-slate-100">Strategies</h1>
        <p className="mt-1.5 max-w-3xl text-[13px] leading-relaxed text-slate-400">
          Every strategy in this repository — the Indian options and equity systems built for live
          trading, and the research strategies the backtester sweeps.
        </p>
      </header>

      <div className="mb-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Stat
          label="Production strategies"
          value={count(summary.production_strategies.total)}
          sub={`${summary.production_strategies.installed} installed · Indian markets`}
        />
        <Stat
          label="Research strategies"
          value={count(summary.research_strategies.total)}
          sub="Backtester library"
        />
        <Stat
          label="Watchdogs & analytics"
          value={count(summary.watchdogs.total + summary.analytics.total)}
          sub="Operational modules"
        />
        <Stat
          label="Currently running"
          value={count(
            summary.production_strategies.running + summary.research_strategies.running
          )}
          sub="Live processes"
          tone={
            summary.production_strategies.running > 0 ? "text-emerald-400" : "text-slate-400"
          }
        />
      </div>

      <div className="mb-6">
        <Explain>
          The production strategies trade NSE/BSE through the{" "}
          <strong>configured broker</strong> — whichever <code>config/broker.yaml</code> selects.
          They reach it via the <code>BrokerAdapter</code> abstraction, so none of them names a
          vendor. They are installed and complete, but none is running here: they need broker
          credentials and a running dashboard process. Status below is read from disk and the
          process table, not assumed. Library strategies show how much walk-forward testing
          they have been through, since they are functions the backtester calls rather than
          processes that run.
        </Explain>
      </div>

      {/* ---------------- Production ---------------- */}

      <h2 className="mb-3 text-[15px] font-semibold text-slate-100">
        Production — Indian markets
      </h2>

      <div className="mb-8 space-y-5">
        {Object.entries(byFamily).map(([family, modules]) => (
          <Card key={family}>
            <CardHead
              title={FAMILY_LABELS[family] ?? family}
              subtitle={`${modules.length} ${modules.length === 1 ? "strategy" : "strategies"}`}
              action={<Pill tone={FAMILY_TONE[family] ?? "slate"}>{family.replace(/_/g, " ")}</Pill>}
            />
            <ul className="divide-y divide-[var(--color-line-soft)]">
              {modules.map((module) => (
                <li key={module.key} className="px-5 py-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-[13px] font-semibold text-slate-200">
                          {module.name}
                        </span>
                        {statusPill(module)}
                        {module.prd_ref ? (
                          <span className="mono text-[9.5px] text-slate-600">
                            {module.prd_ref}
                          </span>
                        ) : null}
                      </div>
                      <p className="mt-1.5 max-w-3xl text-[11.5px] leading-relaxed text-slate-400">
                        {module.summary}
                      </p>
                      {module.detail ? (
                        <p className="mt-1.5 max-w-3xl text-[11px] leading-relaxed text-slate-500">
                          {module.detail}
                        </p>
                      ) : null}

                      {module.parameters.length > 0 ? (
                        <div className="mt-2.5 flex flex-wrap gap-1.5">
                          {module.parameters.map((p) => (
                            <span
                              key={p}
                              className="mono rounded bg-[var(--color-panel2)] px-1.5 py-0.5 text-[9.5px] text-slate-500"
                            >
                              {p}
                            </span>
                          ))}
                        </div>
                      ) : null}
                    </div>

                    <div className="shrink-0 text-right">
                      <div className="mono text-[11px] text-slate-500">
                        {count(module.lines)} lines
                      </div>
                      <div className="mono mt-1 text-[9.5px] text-slate-600">
                        {module.modules.join(", ")}
                      </div>
                      {module.requires.length > 0 ? (
                        <div className="mt-1.5">
                          <Pill tone={brokerReady ? "slate" : "amber"}>
                            {brokerReady
                              ? `broker: ${brokerName}`
                              : `needs ${module.requires.join(", ")}`}
                          </Pill>
                        </div>
                      ) : null}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          </Card>
        ))}
      </div>

      {/* ---------------- Research ---------------- */}

      <h2 className="mb-3 text-[15px] font-semibold text-slate-100">
        Research — backtester library
      </h2>

      <Card className="mb-6">
        <CardHead
          title="Swept by the validation pipeline"
          subtitle="Bare strategies with no stop losses or targets, so the raw edge is what gets measured."
          action={
            ultra.length > 0 ? (
              <Pill tone="green">{ultra.length} cleared the gauntlet</Pill>
            ) : (
              <Pill tone="amber">None cleared the gauntlet</Pill>
            )
          }
        />
        <Table>
          <thead>
            <tr>
              <Th>Strategy</Th>
              <Th>Family</Th>
              <Th>Parameters</Th>
              <Th>Class</Th>
              <Th>Status</Th>
            </tr>
          </thead>
          <tbody>
            {research.map((module) => (
              <tr key={module.key} className="hover:bg-[var(--color-panel2)]">
                <Td className="font-medium text-slate-200">
                  {module.name}
                  <div className="mt-0.5 text-[10.5px] font-normal text-slate-500">
                    {module.summary}
                  </div>
                </Td>
                <Td className="text-slate-500">{FAMILY_LABELS[module.family] ?? module.family}</Td>
                <Td className="mono text-[10.5px] text-slate-500">
                  {module.parameters.join(", ")}
                </Td>
                <Td className="mono text-[11px] text-slate-400">{module.entry}</Td>
                <Td>{statusPill(module)}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </Card>

      {ultra.length > 0 ? (
        <Card>
          <CardHead
            title="Cleared for deployment"
            subtitle="Survived the six validation gates and both robustness tests."
          />
          <Table>
            <thead>
              <tr>
                <Th>Symbol</Th>
                <Th>Strategy</Th>
                <Th align="right">OOS Sharpe</Th>
                <Th align="right">Stability</Th>
                <Th>Bootstrap</Th>
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
                  <Td align="right" className="mono">{ratio(row.sensitivityScore)}</Td>
                  <Td>
                    <Pill tone={row.bootstrapVerdict === "PASS" ? "green" : "slate"}>
                      {row.bootstrapVerdict}
                    </Pill>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </Card>
      ) : (
        <Card>
          <EmptyState
            title="No research strategy has cleared the gauntlet"
            body="Only strategies surviving both the validation funnel and the robustness tests are offered for deployment. That is a result, not an error."
            action={
              <Link
                href="/funnel"
                className="rounded-lg bg-cyan-500 px-3.5 py-2 text-xs font-semibold text-slate-950 hover:bg-cyan-400"
              >
                Open the validation funnel
              </Link>
            }
          />
        </Card>
      )}
    </div>
  );
}
