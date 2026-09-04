import { Pagination } from "@/components/Pagination";
import { readPageParams } from "@/lib/pagination";
import { Card, CardHead, EmptyState, Pill, Stat, Table, Td, Th } from "@/components/ui";
import { getFills, getOrders, listBrokers } from "@/lib/broker";
import { BrokerSwitcher } from "@/components/BrokerSwitcher";
import { count } from "@/lib/format";

export const dynamic = "force-dynamic";

const STATUS_TONE: Record<string, string> = {
  COMPLETE: "green",
  OPEN: "cyan",
  PENDING: "cyan",
  PARTIAL: "amber",
  CANCELLED: "slate",
  REJECTED: "red",
};

function money(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export default async function ActivityPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const orderPage = readPageParams(params, "o", 50);
  const fillPage = readPageParams(params, "f", 25);

  const pick = (key: string) => {
    const v = params[key];
    return (Array.isArray(v) ? v[0] : v) || undefined;
  };

  // Which book to read. Absent means the broker config calls active.
  const broker = pick("broker");

  const [orders, fills, brokerList] = await Promise.all([
    getOrders({
      page: orderPage.page,
      page_size: orderPage.pageSize,
      symbol: pick("symbol"),
      status: pick("status"),
      strategy: pick("strategy"),
      broker,
    }).catch(() => null),
    getFills({
      page: fillPage.page,
      page_size: fillPage.pageSize,
      broker,
    }).catch(() => null),
    listBrokers(),
  ]);

  const viewing = broker || brokerList.active;
  const viewingBroker = brokerList.brokers.find((b) => b.name === viewing);

  if (!orders) {
    return (
      <div className="mx-auto max-w-[1500px] px-6 py-8 lg:px-10">
        <div className="mb-4">
          <BrokerSwitcher brokers={brokerList.brokers} />
        </div>
        <Card>
          <EmptyState
            title="Broker unreachable"
            body={`Could not load order history for ${viewing || "the active broker"}.`}
          />
        </Card>
      </div>
    );
  }

  const filled = orders.rows.filter((o) => o.status === "COMPLETE").length;

  return (
    <div className="mx-auto max-w-[1500px] px-6 py-8 lg:px-10">
      <header className="mb-6">
        <div className="text-[11px] font-semibold uppercase tracking-wider text-cyan-400">
          Trading Ops
        </div>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight text-slate-100">
          Order Activity
        </h1>
        <p className="mt-1.5 max-w-3xl text-[13px] leading-relaxed text-slate-400">
          Every order the platform has placed, including the ones a safety gate blocked.
        </p>
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <BrokerSwitcher brokers={brokerList.brokers} />
          {viewingBroker ? (
            <Pill tone={viewingBroker.simulated ? "cyan" : "red"}>
              {viewingBroker.simulated ? "simulated money" : "REAL ACCOUNT"}
            </Pill>
          ) : null}
        </div>
      </header>

      <div className="mb-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Stat label="Total orders" value={count(orders.pagination.total)} sub="All time" />
        <Stat label="Filled on this page" value={count(filled)} sub={`of ${orders.rows.length}`} />
        <Stat label="Total fills" value={count(fills?.pagination.total ?? 0)} sub="Executions" />
        <Stat label="Symbols traded" value={count(orders.filters.symbols.length)} sub="Distinct" />
      </div>

      <Card className="mb-6">
        <CardHead
          title="Orders"
          subtitle="Filter and page through the full history."
          action={
            <form className="flex flex-wrap gap-2" method="get">
              <select
                name="symbol"
                defaultValue={pick("symbol") ?? ""}
                className="mono rounded-md border border-[var(--color-line)] bg-[var(--color-panel2)] px-2 py-1.5 text-[11px] text-slate-300"
                aria-label="Filter by symbol"
              >
                <option value="">All symbols</option>
                {orders.filters.symbols.map((s) => (
                  <option key={s} value={s}>{s}</option>
                ))}
              </select>
              <select
                name="status"
                defaultValue={pick("status") ?? ""}
                className="mono rounded-md border border-[var(--color-line)] bg-[var(--color-panel2)] px-2 py-1.5 text-[11px] text-slate-300"
                aria-label="Filter by status"
              >
                <option value="">All statuses</option>
                {orders.filters.statuses.map((s) => (
                  <option key={s} value={s}>{s}</option>
                ))}
              </select>
              <select
                name="strategy"
                defaultValue={pick("strategy") ?? ""}
                className="mono rounded-md border border-[var(--color-line)] bg-[var(--color-panel2)] px-2 py-1.5 text-[11px] text-slate-300"
                aria-label="Filter by strategy"
              >
                <option value="">All strategies</option>
                {orders.filters.strategies.map((s) => (
                  <option key={s} value={s}>{s}</option>
                ))}
              </select>
              <button className="rounded-md bg-cyan-500 px-3 py-1.5 text-[11px] font-semibold text-slate-950 hover:bg-cyan-400">
                Apply
              </button>
            </form>
          }
        />
        {orders.rows.length === 0 ? (
          <EmptyState
            title="No orders match"
            body="Nothing has been traded yet, or the filters exclude everything. Place an order from the dashboard."
          />
        ) : (
          <>
            <Table>
              <thead>
                <tr>
                  <Th>Time</Th>
                  <Th>Order ID</Th>
                  <Th>Symbol</Th>
                  <Th>Side</Th>
                  <Th align="right">Qty</Th>
                  <Th>Type</Th>
                  <Th align="right">Avg price</Th>
                  <Th>Strategy</Th>
                  <Th>Status</Th>
                </tr>
              </thead>
              <tbody>
                {orders.rows.map((o) => (
                  <tr key={o.order_id} className="hover:bg-[var(--color-panel2)]">
                    <Td className="mono text-slate-500">
                      {o.created_at?.slice(5, 19).replace("T", " ") ?? "—"}
                    </Td>
                    <Td className="mono text-[11px] text-slate-500">{o.order_id}</Td>
                    <Td className="mono font-semibold text-slate-200">{o.symbol}</Td>
                    <Td>
                      <span className={o.side === "BUY" ? "text-emerald-400" : "text-rose-400"}>
                        {o.side}
                      </span>
                    </Td>
                    <Td align="right" className="mono">{o.quantity}</Td>
                    <Td className="text-slate-500">{o.order_type}</Td>
                    <Td align="right" className="mono">{money(o.average_price, 4)}</Td>
                    <Td className="text-slate-500">{o.strategy || "—"}</Td>
                    <Td>
                      <span title={o.status_message}>
                        <Pill tone={STATUS_TONE[o.status] ?? "slate"}>
                          {o.dry_run ? "DRY-RUN" : o.status}
                        </Pill>
                      </span>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
            <Pagination
              state={{
                page: orders.pagination.page,
                pageSize: orders.pagination.page_size,
                total: orders.pagination.total,
                pages: orders.pagination.pages,
              }}
              paramPrefix="o"
              label="orders"
            />
          </>
        )}
      </Card>

      {fills && fills.rows.length > 0 ? (
        <Card>
          <CardHead title="Fills" subtitle="Executions, with the cost actually charged." />
          <Table>
            <thead>
              <tr>
                <Th>Time</Th>
                <Th>Symbol</Th>
                <Th>Side</Th>
                <Th align="right">Qty</Th>
                <Th align="right">Price</Th>
                <Th align="right">Notional</Th>
                <Th align="right">Commission</Th>
              </tr>
            </thead>
            <tbody>
              {fills.rows.map((f, i) => (
                <tr key={i} className="hover:bg-[var(--color-panel2)]">
                  <Td className="mono text-slate-500">
                    {String(f.timestamp).slice(5, 19).replace("T", " ")}
                  </Td>
                  <Td className="mono font-semibold text-slate-200">{String(f.symbol)}</Td>
                  <Td>
                    <span className={f.side === "BUY" ? "text-emerald-400" : "text-rose-400"}>
                      {String(f.side)}
                    </span>
                  </Td>
                  <Td align="right" className="mono">{Number(f.quantity)}</Td>
                  <Td align="right" className="mono">{money(Number(f.price), 4)}</Td>
                  <Td align="right" className="mono text-slate-400">{money(Number(f.notional))}</Td>
                  <Td align="right" className="mono text-amber-400/80">
                    {money(Number(f.commission), 4)}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
          <Pagination
            state={{
              page: fills.pagination.page,
              pageSize: fills.pagination.page_size,
              total: fills.pagination.total,
              pages: fills.pagination.pages,
            }}
            paramPrefix="f"
            label="fills"
          />
        </Card>
      ) : null}
    </div>
  );
}
