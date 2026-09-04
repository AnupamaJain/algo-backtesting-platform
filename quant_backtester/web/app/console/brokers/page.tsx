import { Card, CardHead, EmptyState, Explain, Pill, Table, Td, Th } from "@/components/ui";
import { safeBrokerState } from "@/lib/broker";

export const dynamic = "force-dynamic";

const CAPABILITY_LABELS: Record<string, string> = {
  gtt: "GTT orders",
  streaming: "Live streaming",
  short_selling: "Short selling",
  options: "Options",
  modify: "Modify orders",
  bracket_orders: "Bracket orders",
  fractional: "Fractional qty",
};

export default async function BrokersPage() {
  const state = await safeBrokerState();
  if (!state) {
    return (
      <div className="mx-auto max-w-[1500px] px-6 py-8 lg:px-10">
        <Card><EmptyState title="Broker unreachable" body="Could not load broker state." /></Card>
      </div>
    );
  }

  const { broker, safety } = state;

  return (
    <div className="mx-auto max-w-[1500px] px-6 py-8 lg:px-10">
      <header className="mb-6">
        <div className="text-[11px] font-semibold uppercase tracking-wider text-cyan-400">
          Trading Ops
        </div>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight text-slate-100">Brokers</h1>
        <p className="mt-1.5 max-w-3xl text-[13px] leading-relaxed text-slate-400">
          Which broker is executing, what it can do, and the safety gates standing in front of it.
        </p>
      </header>

      <div className="mb-6">
        <Explain>
          Every broker sits behind one <strong>BrokerAdapter</strong> interface, so strategies
          never contain broker-specific code. Switching execution to a different broker is a
          config change and a restart — no strategy is touched.
        </Explain>
      </div>

      <div className="grid gap-6 xl:grid-cols-2">
        <Card>
          <CardHead
            title={`Active: ${broker.name}`}
            subtitle="The adapter currently executing orders."
            action={
              <div className="flex gap-2">
                <Pill tone={broker.connected ? "green" : "red"}>
                  {broker.connected ? "Connected" : "Offline"}
                </Pill>
                {broker.simulated ? <Pill tone="cyan">Simulated</Pill> : <Pill tone="red">Real money</Pill>}
              </div>
            }
          />
          <div className="space-y-2.5 px-5 py-4 text-[11.5px]">
            <Row label="Adapter" value={broker.name} />
            <Row label="Auth strategy" value={broker.auth.kind} />
            <Row
              label="Session"
              value={
                broker.auth.session
                  ? String((broker.auth.session as Record<string, unknown>).valid ? "valid" : "expired")
                  : "none"
              }
            />
            <Row label="Universe size" value={`${state.universe.length} symbols`} />
          </div>
        </Card>

        <Card>
          <CardHead
            title="Capabilities"
            subtitle="Declared, not discovered — callers branch on these instead of failing on an unsupported call."
          />
          <div className="grid grid-cols-2 gap-2 px-5 py-4">
            {Object.entries(broker.capabilities).map(([key, supported]) => (
              <div key={key} className="flex items-center justify-between gap-2 text-[11.5px]">
                <span className="text-slate-400">{CAPABILITY_LABELS[key] ?? key}</span>
                <Pill tone={supported ? "green" : "slate"}>{supported ? "yes" : "no"}</Pill>
              </div>
            ))}
          </div>
        </Card>
      </div>

      <Card className="mt-6">
        <CardHead
          title="Safety gates"
          subtitle="Applied once, above every adapter, so a new broker integration cannot omit them."
        />
        <Table>
          <thead>
            <tr>
              <Th>Gate</Th>
              <Th>Setting</Th>
              <Th>What it stops</Th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <Td className="font-medium text-slate-200">Live trading gate</Td>
              <Td>
                <Pill tone={safety.mode === "LIVE" ? "red" : safety.mode === "PAPER" ? "cyan" : "amber"}>
                  {safety.mode}
                </Pill>
              </Td>
              <Td className="text-slate-500">
                {safety.simulated_broker
                  ? "Adapter is simulated, so no real money is at risk regardless of this flag."
                  : "Blocks orders from reaching a real broker while disabled."}
              </Td>
            </tr>
            <tr>
              <Td className="font-medium text-slate-200">Max order value</Td>
              <Td className="mono">{safety.max_order_value.toLocaleString()}</Td>
              <Td className="text-slate-500">A fat-finger order far larger than intended.</Td>
            </tr>
            <tr>
              <Td className="font-medium text-slate-200">Duplicate suppression</Td>
              <Td className="mono">{safety.duplicate_window_seconds}s</Td>
              <Td className="text-slate-500">
                The same intent fired twice — a double-click or a retry loop.
              </Td>
            </tr>
            <tr>
              <Td className="font-medium text-slate-200">Retry policy</Td>
              <Td className="mono">transient only</Td>
              <Td className="text-slate-500">
                Rate limits and outages are retried; rejections never are.
              </Td>
            </tr>
          </tbody>
        </Table>
      </Card>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-slate-500">{label}</span>
      <span className="mono text-slate-300">{value}</span>
    </div>
  );
}
