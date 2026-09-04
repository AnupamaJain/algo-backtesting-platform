import Link from "next/link";
import OrderTicket from "@/components/OrderTicket";
import { Card, CardHead, EmptyState, Explain, Pill, Stat, Table, Td, Th } from "@/components/ui";
import { getEvents, listBrokers, safeBrokerState } from "@/lib/broker";
import { BrokerSwitcher } from "@/components/BrokerSwitcher";
import { count, pct, ratio } from "@/lib/format";

export const dynamic = "force-dynamic";

function money(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export default async function ConsoleDashboard({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const raw = params.broker;
  // Which book to display. Absent means whatever broker.yaml calls active.
  const brokerParam = (Array.isArray(raw) ? raw[0] : raw) || undefined;

  const [state, events, brokerList] = await Promise.all([
    safeBrokerState({ broker: brokerParam }),
    getEvents({ page: 1, page_size: 8, broker: brokerParam }).catch(() => null),
    listBrokers(),
  ]);

  const viewing = brokerParam || brokerList.active;
  const viewingBroker = brokerList.brokers.find((b) => b.name === viewing);

  if (!state) {
    return (
      <div className="mx-auto max-w-[1500px] px-6 py-8 lg:px-10">
        <div className="mb-4">
          <BrokerSwitcher brokers={brokerList.brokers} />
        </div>
        <Card>
          <EmptyState
            title="Broker layer unreachable"
            body={`Could not read the ${viewing || "active"} book. For a live broker this usually means the token has lapsed — check with the broker's token script.`}
          />
        </Card>
      </div>
    );
  }

  const { account, positions, safety, broker } = state;
  const dayPnl = account.realized_pnl + account.unrealized_pnl;
  const staleQuotes = positions.filter((p) => p.quote?.stale).length;

  return (
    <div className="mx-auto max-w-[1500px] px-6 py-8 lg:px-10">
      <header className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-wider text-cyan-400">
            Trading Ops
          </div>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight text-slate-100">Dashboard</h1>
          <div className="mt-4 flex flex-wrap items-center gap-3">
            <BrokerSwitcher brokers={brokerList.brokers} />
            {viewingBroker ? (
              <Pill tone={viewingBroker.simulated ? "cyan" : "red"}>
                {viewingBroker.simulated ? "simulated money" : "REAL ACCOUNT"}
              </Pill>
            ) : null}
          </div>
          <p className="mt-3 max-w-3xl text-[13px] leading-relaxed text-slate-400">
            Live account state from the broker layer. Positions are marked against real market
            quotes;{" "}
            {viewingBroker && !viewingBroker.simulated
              ? "this is a REAL account."
              : "the money is simulated."}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Pill tone={safety.mode === "LIVE" ? "red" : safety.mode === "PAPER" ? "cyan" : "amber"}>
            {safety.mode}
          </Pill>
          <Pill tone={broker.connected ? "green" : "red"}>
            {broker.name} {broker.connected ? "connected" : "offline"}
          </Pill>
        </div>
      </header>

      <div className="mb-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        <Stat label="Equity" value={money(account.equity)} sub="Cash + positions" />
        <Stat label="Cash" value={money(account.cash)} sub="Available" />
        <Stat
          label="Unrealized P&L"
          value={money(account.unrealized_pnl)}
          sub="Open positions"
          tone={account.unrealized_pnl >= 0 ? "text-emerald-400" : "text-rose-400"}
        />
        <Stat
          label="Realized P&L"
          value={money(account.realized_pnl)}
          sub="Closed trades"
          tone={account.realized_pnl >= 0 ? "text-emerald-400" : "text-rose-400"}
        />
        <Stat
          label="Gross exposure"
          value={money(account.gross_exposure)}
          sub={`${ratio(account.leverage)}x leverage`}
        />
      </div>

      {staleQuotes > 0 ? (
        <div className="mb-6">
          <Explain>
            {staleQuotes} position{staleQuotes === 1 ? "" : "s"} {staleQuotes === 1 ? "is" : "are"}{" "}
            marked against the last cached close rather than a live tick — the market feed was
            unreachable. P&L is real but not current.
          </Explain>
        </div>
      ) : null}

      <div className="grid gap-6 xl:grid-cols-[1.6fr_1fr]">
        <div className="space-y-6">
          <Card>
            <CardHead
              title="Open positions"
              subtitle={`${count(positions.length)} held · marked to live market prices`}
            />
            {positions.length === 0 ? (
              <EmptyState
                title="No open positions"
                body="Place an order using the ticket to open one. Orders fill against real market quotes."
              />
            ) : (
              <Table>
                <thead>
                  <tr>
                    <Th>Symbol</Th>
                    <Th>Side</Th>
                    <Th align="right">Qty</Th>
                    <Th align="right">Avg price</Th>
                    <Th align="right">Market</Th>
                    <Th align="right">Value</Th>
                    <Th align="right">Unrealized</Th>
                    <Th align="right">Day</Th>
                  </tr>
                </thead>
                <tbody>
                  {positions.map((p) => (
                    <tr key={p.symbol} className="hover:bg-[var(--color-panel2)]">
                      <Td className="mono font-semibold text-slate-200">{p.symbol}</Td>
                      <Td>
                        <Pill tone={p.direction === "LONG" ? "green" : "red"}>{p.direction}</Pill>
                      </Td>
                      <Td align="right" className="mono">{p.quantity}</Td>
                      <Td align="right" className="mono">{money(p.average_price)}</Td>
                      <Td align="right" className="mono">
                        {money(p.last_price)}
                        {p.quote?.stale ? (
                          <span className="ml-1.5 text-[9px] text-amber-400">stale</span>
                        ) : null}
                      </Td>
                      <Td align="right" className="mono text-slate-400">{money(p.market_value)}</Td>
                      <Td
                        align="right"
                        className={`mono font-semibold ${
                          p.unrealized_pnl >= 0 ? "text-emerald-400" : "text-rose-400"
                        }`}
                      >
                        {money(p.unrealized_pnl)}
                      </Td>
                      <Td
                        align="right"
                        className={`mono ${
                          (p.quote?.change_pct ?? 0) >= 0 ? "text-emerald-400" : "text-rose-400"
                        }`}
                      >
                        {pct(p.quote?.change_pct ?? null)}
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            )}
          </Card>

          {events && events.rows.length > 0 ? (
            <Card>
              <CardHead
                title="Recent activity"
                subtitle="Every order, fill and blocked attempt is recorded."
                action={
                  <Link
                    href="/console/watchdogs"
                    className="text-[11px] font-medium text-cyan-400 hover:underline"
                  >
                    View all
                  </Link>
                }
              />
              <ul className="divide-y divide-[var(--color-line-soft)]">
                {events.rows.map((event, i) => (
                  <li key={i} className="flex items-start gap-3 px-5 py-3">
                    <Pill
                      tone={
                        event.severity === "error"
                          ? "red"
                          : event.severity === "warning"
                            ? "amber"
                            : "slate"
                      }
                    >
                      {event.kind.replace(/_/g, " ")}
                    </Pill>
                    <span className="min-w-0 flex-1 text-[11.5px] text-slate-300">
                      {event.message}
                    </span>
                    <span className="mono shrink-0 text-[10px] text-slate-600">
                      {event.timestamp.slice(11, 19)}
                    </span>
                  </li>
                ))}
              </ul>
            </Card>
          ) : null}
        </div>

        <div className="space-y-6">
          <OrderTicket universe={state.universe} mode={safety.mode} />

          <Card>
            <CardHead title="Session" subtitle="Broker and safety configuration." />
            <div className="space-y-2.5 px-5 py-4 text-[11.5px]">
              <Row label="Broker" value={broker.name} />
              <Row label="Simulated" value={broker.simulated ? "yes" : "no"} />
              <Row label="Auth" value={broker.auth.kind} />
              <Row label="Live trading" value={safety.live_trading ? "ENABLED" : "disabled"} />
              <Row label="Max order value" value={money(safety.max_order_value)} />
              <Row
                label="Duplicate window"
                value={`${safety.duplicate_window_seconds}s`}
              />
              <Row label="Day P&L" value={money(dayPnl)} />
            </div>
          </Card>
        </div>
      </div>
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
