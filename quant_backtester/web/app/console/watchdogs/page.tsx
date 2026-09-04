import { Pagination } from "@/components/Pagination";
import { readPageParams } from "@/lib/pagination";
import { Card, CardHead, EmptyState, Explain, Pill, Stat, Table, Td, Th } from "@/components/ui";
import { getEvents } from "@/lib/broker";
import { FAMILY_LABELS, getInventory, type OpsModule } from "@/lib/ops";
import { count } from "@/lib/format";

export const dynamic = "force-dynamic";

const SEVERITY_TONE: Record<string, string> = {
  error: "red",
  warning: "amber",
  info: "slate",
};

function moduleStatus(module: OpsModule) {
  if (!module.installed) return <Pill tone="red">Missing files</Pill>;
  if (module.running) return <Pill tone="green">Running</Pill>;
  if (module.has_activity) return <Pill tone="cyan">Has history</Pill>;
  return <Pill tone="slate">Idle</Pill>;
}

export default async function WatchdogsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const { page, pageSize } = readPageParams(params, "", 50);

  const [inventory, events] = await Promise.all([
    getInventory(),
    getEvents({ page, page_size: pageSize }).catch(() => null),
  ]);

  const watchdogs = inventory?.modules.filter((m) => m.category === "watchdog") ?? [];
  const analytics = inventory?.modules.filter((m) => m.category === "analytics") ?? [];

  const bySeverity = (events?.rows ?? []).reduce<Record<string, number>>((acc, e) => {
    acc[e.severity] = (acc[e.severity] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <div className="mx-auto max-w-[1500px] px-6 py-8 lg:px-10">
      <header className="mb-6">
        <div className="text-[11px] font-semibold uppercase tracking-wider text-cyan-400">
          Trading Ops
        </div>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight text-slate-100">
          Watchdogs &amp; Audit
        </h1>
        <p className="mt-1.5 max-w-3xl text-[13px] leading-relaxed text-slate-400">
          The modules that guard live trading, and the audit trail of everything the platform
          did — including what it refused to do.
        </p>
      </header>

      <div className="mb-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Stat label="Watchdogs" value={count(watchdogs.length)} sub="Order & exposure guards" />
        <Stat label="Analytics modules" value={count(analytics.length)} sub="Journals & monitors" />
        <Stat
          label="Audit events"
          value={count(events?.pagination.total ?? 0)}
          sub="Broker layer, all time"
        />
        <Stat
          label="Blocked / failed"
          value={count(bySeverity.error ?? 0)}
          sub="On this page"
          tone={(bySeverity.error ?? 0) > 0 ? "text-rose-400" : "text-slate-400"}
        />
      </div>

      <Card className="mb-6">
        <CardHead
          title="Watchdog modules"
          subtitle="Independent guards over live order flow. Each is deliberately separate, so one failure cannot disable the rest."
        />
        {watchdogs.length === 0 ? (
          <EmptyState title="Inventory unavailable" body="Could not read the module inventory." />
        ) : (
          <ul className="divide-y divide-[var(--color-line-soft)]">
            {watchdogs.map((module) => (
              <li key={module.key} className="flex flex-wrap items-start gap-4 px-5 py-4">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-[13px] font-semibold text-slate-200">{module.name}</span>
                    {moduleStatus(module)}
                    {module.prd_ref ? (
                      <span className="mono text-[9.5px] text-slate-600">{module.prd_ref}</span>
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
                </div>
                <div className="shrink-0 text-right">
                  <div className="mono text-[11px] text-slate-500">{count(module.lines)} lines</div>
                  {module.state?.available ? (
                    <div className="mono mt-1 text-[10px] text-slate-500">
                      {count(module.state.rows)} rows
                    </div>
                  ) : null}
                  <div className="mono mt-1 text-[9.5px] text-slate-600">
                    {module.modules.join(", ")}
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card className="mb-6">
        <CardHead
          title="Analytics & reference modules"
          subtitle="Journals, monitors and reference data that support the trading loop."
        />
        <Table>
          <thead>
            <tr>
              <Th>Module</Th>
              <Th>Purpose</Th>
              <Th>Role</Th>
              <Th align="right">Records</Th>
              <Th>Status</Th>
            </tr>
          </thead>
          <tbody>
            {analytics.map((module) => (
              <tr key={module.key} className="hover:bg-[var(--color-panel2)]">
                <Td className="font-medium text-slate-200">{module.name}</Td>
                <Td className="whitespace-normal text-slate-400">{module.summary}</Td>
                <Td className="text-slate-500">
                  {FAMILY_LABELS[module.family] ?? module.family}
                </Td>
                <Td align="right" className="mono text-slate-500">
                  {module.state?.available ? count(module.state.rows) : "—"}
                </Td>
                <Td>{moduleStatus(module)}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </Card>

      <div className="mb-6">
        <Explain>
          Every module above is installed and complete, but none has recorded activity in this
          checkout — they reach whichever broker <code>config/broker.yaml</code> selects, and need
          credentials plus a running dashboard process. &ldquo;Idle&rdquo; here means{" "}
          <em>never run</em>, which is a different thing from broken.
        </Explain>
      </div>

      <Card>
        <CardHead
          title="Broker audit log"
          subtitle="Live from the broker layer. Blocked orders are recorded alongside successful ones."
        />
        {!events || events.rows.length === 0 ? (
          <EmptyState
            title="No events yet"
            body="Activity appears here once orders start flowing through the broker layer."
          />
        ) : (
          <>
            <Table>
              <thead>
                <tr>
                  <Th>Time</Th>
                  <Th>Severity</Th>
                  <Th>Kind</Th>
                  <Th>Message</Th>
                </tr>
              </thead>
              <tbody>
                {events.rows.map((e, i) => (
                  <tr key={i} className="hover:bg-[var(--color-panel2)]">
                    <Td className="mono text-slate-500">
                      {e.timestamp.slice(5, 19).replace("T", " ")}
                    </Td>
                    <Td>
                      <Pill tone={SEVERITY_TONE[e.severity] ?? "slate"}>{e.severity}</Pill>
                    </Td>
                    <Td className="mono text-[11px] text-slate-400">{e.kind}</Td>
                    <Td className="whitespace-normal text-slate-300">{e.message}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
            <Pagination
              state={{
                page: events.pagination.page,
                pageSize: events.pagination.page_size,
                total: events.pagination.total,
                pages: events.pagination.pages,
              }}
              label="events"
            />
          </>
        )}
      </Card>
    </div>
  );
}
