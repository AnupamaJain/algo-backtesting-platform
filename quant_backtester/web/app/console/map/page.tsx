import { Card, CardHead, Explain, Pill } from "@/components/ui";
import { safeBrokerState } from "@/lib/broker";
import { getPipelineStatus } from "@/lib/artifacts";

export const dynamic = "force-dynamic";

export default async function SystemMapPage() {
  const [state, pipeline] = await Promise.all([safeBrokerState(), getPipelineStatus()]);

  const layers = [
    {
      name: "Layer 1 · Data & Signals",
      detail: "Back-adjusted OHLCV, per-asset calendars, signal generation",
      ok: pipeline.layer1.done,
      meta: `${pipeline.layer1.symbols} symbols · ${pipeline.layer1.signals} signal files`,
    },
    {
      name: "Layer 2 · Walk-forward & Funnel",
      detail: "Out-of-sample scoring, six validation gates",
      ok: pipeline.layer2.done,
      meta: `${pipeline.layer2.tested} tested · ${pipeline.layer2.survivors} survivors`,
    },
    {
      name: "Layer 3 · Robustness",
      detail: "Parameter sensitivity, bootstrap stress testing",
      ok: pipeline.layer3.done,
      meta: `${pipeline.layer3.evaluated} evaluated · ${pipeline.layer3.ultraRobust} ultra-robust`,
    },
    {
      name: "Layer 4 · Regime & Portfolio",
      detail: "HMM regimes, ATR sizing, risk gates, costed rebalancing",
      ok: pipeline.layer4.done,
      meta: `${pipeline.layer4.portfolios} portfolios compared`,
    },
    {
      name: "Broker Layer",
      detail: "BrokerAdapter, unified models, safety gates, durable ledger",
      ok: Boolean(state?.broker.connected),
      meta: state ? `${state.broker.name} · ${state.safety.mode}` : "unreachable",
    },
  ];

  return (
    <div className="mx-auto max-w-[1500px] px-6 py-8 lg:px-10">
      <header className="mb-6">
        <div className="text-[11px] font-semibold uppercase tracking-wider text-cyan-400">
          Trading Ops
        </div>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight text-slate-100">System Map</h1>
        <p className="mt-1.5 max-w-3xl text-[13px] leading-relaxed text-slate-400">
          How research becomes execution. Each layer feeds the next, and nothing reaches the
          broker without clearing everything above it.
        </p>
      </header>

      <Card className="mb-6">
        <CardHead title="Pipeline" subtitle="Live status of every layer." />
        <ul className="divide-y divide-[var(--color-line-soft)]">
          {layers.map((layer, i) => (
            <li key={layer.name} className="flex items-center gap-4 px-5 py-4">
              <span
                className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-[12px] font-bold ${
                  layer.ok ? "bg-emerald-500/15 text-emerald-300" : "bg-slate-700/40 text-slate-500"
                }`}
              >
                {i + 1}
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-[13px] font-semibold text-slate-200">{layer.name}</span>
                  <Pill tone={layer.ok ? "green" : "slate"}>{layer.ok ? "ready" : "not run"}</Pill>
                </div>
                <p className="mt-1 text-[11.5px] text-slate-500">{layer.detail}</p>
              </div>
              <span className="mono shrink-0 text-[10.5px] text-slate-500">{layer.meta}</span>
            </li>
          ))}
        </ul>
      </Card>

      <Card className="mb-6">
        <CardHead title="Order path" subtitle="Every order takes exactly this route." />
        <div className="px-5 py-5">
          <ol className="space-y-2.5">
            {[
              ["Strategy or operator", "Decides what to trade. Contains no broker code."],
              ["OrderService", "Notional ceiling → duplicate suppression → live-trading gate."],
              ["Retry policy", "Transient failures retried with backoff; rejections never."],
              ["BrokerAdapter", "Translates unified models into the broker's own vocabulary."],
              ["Durable ledger", "Order and fill written to SQLite before the call returns."],
            ].map(([title, detail], i) => (
              <li key={title} className="flex items-start gap-3">
                <span className="mono mt-0.5 text-[10px] text-slate-600">{i + 1}</span>
                <div>
                  <span className="text-[12.5px] font-medium text-slate-200">{title}</span>
                  <p className="text-[11px] text-slate-500">{detail}</p>
                </div>
              </li>
            ))}
          </ol>
        </div>
      </Card>

      <Explain>
        Positions are never stored — they are derived by replaying the fill ledger, so the
        console can only ever show a state consistent with what actually executed. A crash
        mid-session loses nothing.
      </Explain>
    </div>
  );
}
