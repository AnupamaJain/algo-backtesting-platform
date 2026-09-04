import RunPanel from "@/components/RunPanel";
import { Card, CardHead, EmptyState, Explain, Pill, Stat, Table, Td, Th } from "@/components/ui";
import {
  getPeadSummary,
  getPeadSignals,
  getPeadUpcoming,
  getPeadEquityCurve,
} from "@/lib/artifacts";
import { universeFrom } from "@/lib/universes";
import UniverseSwitcher from "@/components/UniverseSwitcher";
import { count, pct, ratio, sharpeTone, drawdownTone } from "@/lib/format";
import { PeadEquityLine } from "./PeadEquityLine";

export const dynamic = "force-dynamic";

function surpriseTone(v: number | null): string {
  if (v === null) return "text-slate-500";
  if (v >= 10) return "text-emerald-300";
  if (v >= 5) return "text-cyan-300";
  if (v <= -10) return "text-rose-300";
  if (v <= -5) return "text-amber-300";
  return "text-slate-400";
}

function directionPill(direction: string) {
  if (direction === "LONG") return <Pill tone="green">LONG ↑</Pill>;
  if (direction === "SHORT") return <Pill tone="red">SHORT ↓</Pill>;
  if (direction === "pending") return <Pill tone="amber">pending</Pill>;
  return <Pill tone="slate">flat</Pill>;
}

function fmt(v: number | null, digits = 2): string {
  return v === null || !Number.isFinite(v) ? "—" : v.toFixed(digits);
}

export default async function PeadPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const universe = universeFrom(params);

  const [summary, signals, upcoming, curve] = await Promise.all([
    getPeadSummary(universe),
    getPeadSignals(universe),
    getPeadUpcoming(universe),
    getPeadEquityCurve(500, universe),
  ]);

  const hasRun = summary !== null;
  const activeSignals = signals.filter((s) => s.signal !== 0);
  const triggerSoon = upcoming.filter((u) => u.would_trigger === true || u.would_trigger === null);

  return (
    <div className="mx-auto max-w-[1500px] px-6 py-8 lg:px-10">

      {/* ---- Header --------------------------------------------------- */}
      <header className="mb-6">
        <div className="text-[11px] font-semibold uppercase tracking-wider text-amber-400">
          Event-driven
        </div>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight text-slate-100">
          PEAD — Post-Earnings Drift
        </h1>
        <p className="mt-1.5 max-w-3xl text-[13px] leading-relaxed text-slate-400">
          After a company beats or misses estimates, its price tends to keep drifting in the
          same direction for weeks. This is one of the most replicated anomalies in finance
          — it has a stated cause, a 40-year literature, and it is not something a parameter
          sweep can find.
        </p>
        <div className="mt-3">
          <UniverseSwitcher active={universe.id} />
        </div>
      </header>

      {/* ---- Methodology note ----------------------------------------- */}
      <div className="mb-6 rounded-xl border border-amber-500/20 bg-amber-500/[0.04] px-5 py-4">
        <div className="flex items-start gap-3">
          <span className="mt-0.5 text-base opacity-70">⚠</span>
          <div className="text-[11.5px] leading-relaxed text-slate-400">
            <strong className="text-slate-200">Look-ahead is prevented by design.</strong>{" "}
            Entry is always the first session <em>strictly after</em> the announcement —
            most US firms report after the close, so the announcement date itself is never
            traded. The surprise figure comes from the vendor&apos;s current consensus revision,
            which may not match what was knowable on the day. Both effects tend to flatter the
            result, so treat the magnitude as an upper bound.
          </div>
        </div>
      </div>

      {/* ---- Active signals banner (the "coming in" panel) ------------- */}
      {hasRun && activeSignals.length > 0 ? (
        <div className="mb-6 rounded-xl border border-cyan-500/30 bg-cyan-500/[0.05] px-5 py-4">
          <div className="mb-3 flex items-center gap-2.5">
            <span className="h-2 w-2 animate-pulse rounded-full bg-cyan-400" />
            <span className="text-sm font-semibold text-slate-100">
              {activeSignals.length} stock{activeSignals.length !== 1 ? "s" : ""} currently in the drift window
            </span>
            <span className="text-[11px] text-slate-500">as of {summary?.as_of}</span>
          </div>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {activeSignals.map((s) => (
              <div
                key={s.symbol}
                className={`rounded-lg border px-4 py-3 ${
                  s.direction === "LONG"
                    ? "border-emerald-500/25 bg-emerald-500/[0.05]"
                    : "border-rose-500/25 bg-rose-500/[0.05]"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="mono text-[13px] font-semibold text-slate-100">{s.symbol}</span>
                  {directionPill(s.direction)}
                </div>
                <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-[11px]">
                  <div className="flex justify-between">
                    <span className="text-slate-500">Surprise</span>
                    <span className={`mono font-semibold ${surpriseTone(s.surprise_pct)}`}>
                      {s.surprise_pct !== null ? `${s.surprise_pct > 0 ? "+" : ""}${s.surprise_pct.toFixed(1)}%` : "—"}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-500">EPS est.</span>
                    <span className="mono text-slate-400">{fmt(s.eps_estimate)}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-500">EPS rep.</span>
                    <span className="mono text-slate-300">{fmt(s.eps_reported)}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-500">Announced</span>
                    <span className="mono text-slate-400">{s.announced_at}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-500">Entry</span>
                    <span className="mono text-slate-400">{s.entry_date}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-500">Days held</span>
                    <span className="mono text-slate-300">{s.days_held ?? "—"}</span>
                  </div>
                  <div className="flex justify-between col-span-2">
                    <span className="text-slate-500">Days remaining</span>
                    <span className={`mono font-semibold ${(s.days_remaining ?? 0) <= 3 ? "text-amber-300" : "text-slate-200"}`}>
                      {s.days_remaining ?? "—"} / exits {s.window_end}
                    </span>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      ) : hasRun ? (
        <div className="mb-6 rounded-xl border border-[var(--color-line-soft)] px-5 py-4">
          <p className="text-[12px] text-slate-500">
            No stocks are currently in a drift window. The next positions open when the next
            qualifying earnings announcement is released.
          </p>
        </div>
      ) : null}

      {/* ---- Run panel ------------------------------------------------ */}
      <div className="mb-6">
        <RunPanel
          universe={universe.id}
          layer="pead"
          label="Run PEAD study"
          hint="Downloads earnings calendars, builds drift signals, backtests the portfolio, and refreshes the signals above."
        />
      </div>

      {!hasRun ? (
        <Card className="mb-6">
          <EmptyState
            title="No PEAD results yet"
            body="Run the study above. It downloads earnings announcement dates, builds drift signals for every qualifying surprise, and backtests the equal-weight portfolio across the universe."
          />
        </Card>
      ) : (
        <>
          {/* ---- Stats ------------------------------------------------ */}
          <div className="mb-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <Stat
              label="Sharpe ratio"
              value={ratio(summary.metrics.sharpe)}
              sub="Annualized, net of costs"
              tone={sharpeTone(summary.metrics.sharpe)}
            />
            <Stat
              label="Total return"
              value={pct(summary.metrics.total_return, 0)}
              sub={`CAGR ${pct(summary.metrics.cagr, 1)}`}
              tone={summary.metrics.total_return >= 0 ? "text-emerald-400" : "text-rose-400"}
            />
            <Stat
              label="Max drawdown"
              value={pct(summary.metrics.max_drawdown)}
              sub={`Profit factor ${ratio(summary.metrics.profit_factor)}`}
              tone={drawdownTone(summary.metrics.max_drawdown)}
            />
            <Stat
              label="Trades"
              value={count(summary.metrics.num_trades)}
              sub={`Win rate ${pct(summary.metrics.win_rate)} · ${summary.exposure.time_in_market_pct.toFixed(1)}% time in market`}
            />
          </div>

          {/* ---- Config + Coverage ------------------------------------ */}
          <div className="mb-6 grid gap-5 xl:grid-cols-[1fr_1fr]">
            <Card>
              <CardHead title="Study parameters" subtitle="Configuration used for this run." />
              <dl className="grid grid-cols-2 gap-x-6 gap-y-2 px-5 py-4 text-[12px]">
                {[
                  ["Min surprise", `${summary.config.min_surprise_pct}%`],
                  ["Holding days", `${summary.config.holding_days} sessions`],
                  ["Direction", summary.config.direction],
                  ["Time in market", `${summary.exposure.time_in_market_pct.toFixed(1)}%`],
                  ["Position changes", count(summary.exposure.position_changes)],
                  ["As of", summary.as_of],
                ].map(([k, v]) => (
                  <div key={k} className="flex justify-between border-b border-[var(--color-line-soft)] pb-1.5">
                    <dt className="text-slate-500">{k}</dt>
                    <dd className="mono text-slate-200">{v}</dd>
                  </div>
                ))}
              </dl>
            </Card>

            <Card>
              <CardHead
                title="Universe coverage"
                subtitle={`${summary.symbols_with_data.length} of ${
                  summary.symbols_with_data.length + summary.symbols_missing_data.length
                } symbols have earnings data.`}
              />
              <div className="px-5 py-4">
                {summary.symbols_missing_data.length > 0 ? (
                  <Explain>
                    <strong>{summary.symbols_missing_data.length} symbol(s)</strong> have no
                    earnings calendar in{" "}
                    <span className="mono">data_events/</span>:{" "}
                    {summary.symbols_missing_data.slice(0, 8).join(", ")}
                    {summary.symbols_missing_data.length > 8 ? " …" : ""}. These are excluded from
                    the study. Add their earnings JSON files to include them.
                  </Explain>
                ) : (
                  <p className="text-[12px] text-emerald-400">
                    All {summary.symbols_with_data.length} symbols have earnings data.
                  </p>
                )}
                <div className="mt-3 flex flex-wrap gap-1.5">
                  {summary.symbols_with_data.map((s) => (
                    <span key={s} className="mono rounded bg-[var(--color-panel2)] px-2 py-0.5 text-[10.5px] text-slate-300">
                      {s}
                    </span>
                  ))}
                </div>
              </div>
            </Card>
          </div>

          {/* ---- Equity curve ----------------------------------------- */}
          {curve.length > 0 ? (
            <Card className="mb-6">
              <CardHead
                title="Portfolio equity curve"
                subtitle="Equal-weight across active positions, compounded daily, net of costs. Starting value = 1.0."
              />
              <div className="px-3 py-5">
                <PeadEquityLine data={curve} />
              </div>
              <div className="px-5 pb-4">
                <Explain>
                  Capital is spread only across names <strong>currently holding a position</strong>,
                  not the whole universe — reporting the return of a book that is 90% in cash as
                  if it were fully invested would understate the strategy by an order of magnitude.
                  Days with no open position return zero.
                </Explain>
              </div>
            </Card>
          ) : null}

          {/* ---- Upcoming earnings ------------------------------------ */}
          <Card className="mb-6">
            <CardHead
              title="Upcoming earnings — next 30 days"
              subtitle="Announcements that may trigger a new drift trade. Surprise% is known only for past announcements."
              action={
                triggerSoon.length > 0 ? (
                  <Pill tone="amber">{triggerSoon.length} to watch</Pill>
                ) : null
              }
            />
            {upcoming.length === 0 ? (
              <EmptyState
                title="No upcoming earnings in the next 30 days"
                body="Re-run the study closer to earnings season to populate this table."
              />
            ) : (
              <Table>
                <thead>
                  <tr>
                    <Th>Symbol</Th>
                    <Th align="right">Announce date</Th>
                    <Th align="right">Days away</Th>
                    <Th align="right">EPS estimate</Th>
                    <Th align="right">Surprise %</Th>
                    <Th>Would trigger</Th>
                    <Th>Direction</Th>
                  </tr>
                </thead>
                <tbody>
                  {upcoming.map((u, i) => (
                    <tr key={i} className={u.would_trigger ? "bg-amber-500/[0.03]" : ""}>
                      <Td className="mono font-semibold text-slate-200">{u.symbol}</Td>
                      <Td align="right" className="mono text-slate-400">{u.announced_at}</Td>
                      <Td align="right">
                        <span className={`mono ${(u.days_until ?? 99) <= 5 ? "text-amber-300 font-semibold" : "text-slate-400"}`}>
                          {u.days_until ?? "—"}d
                        </span>
                      </Td>
                      <Td align="right" className="mono text-slate-500">{fmt(u.eps_estimate)}</Td>
                      <Td align="right">
                        <span className={`mono ${surpriseTone(u.surprise_pct)}`}>
                          {u.surprise_pct !== null
                            ? `${u.surprise_pct > 0 ? "+" : ""}${u.surprise_pct.toFixed(1)}%`
                            : "—"}
                        </span>
                      </Td>
                      <Td>
                        {u.would_trigger === null ? (
                          <Pill tone="amber">pending result</Pill>
                        ) : u.would_trigger ? (
                          <Pill tone="green">yes</Pill>
                        ) : (
                          <Pill tone="slate">no</Pill>
                        )}
                      </Td>
                      <Td>{directionPill(u.direction)}</Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            )}
          </Card>

          {/* ---- Active signals table ---------------------------------- */}
          <Card className="mb-6">
            <CardHead
              title="Active drift positions"
              subtitle="Symbols currently inside a drift window, with days remaining."
              action={
                activeSignals.length > 0 ? (
                  <Pill tone="cyan">{activeSignals.length} active</Pill>
                ) : (
                  <Pill tone="slate">none</Pill>
                )
              }
            />
            {activeSignals.length === 0 ? (
              <EmptyState
                title="No active drift positions"
                body="Positions open on the first session after a qualifying earnings announcement and close after the configured holding period."
              />
            ) : (
              <Table>
                <thead>
                  <tr>
                    <Th>Symbol</Th>
                    <Th>Direction</Th>
                    <Th align="right">Surprise %</Th>
                    <Th align="right">EPS est.</Th>
                    <Th align="right">EPS rep.</Th>
                    <Th align="right">Announced</Th>
                    <Th align="right">Entry date</Th>
                    <Th align="right">Days held</Th>
                    <Th align="right">Days left</Th>
                    <Th align="right">Window end</Th>
                  </tr>
                </thead>
                <tbody>
                  {activeSignals.map((s, i) => (
                    <tr key={i}>
                      <Td className="mono font-semibold text-slate-100">{s.symbol}</Td>
                      <Td>{directionPill(s.direction)}</Td>
                      <Td align="right">
                        <span className={`mono font-semibold ${surpriseTone(s.surprise_pct)}`}>
                          {s.surprise_pct !== null
                            ? `${s.surprise_pct > 0 ? "+" : ""}${s.surprise_pct.toFixed(1)}%`
                            : "—"}
                        </span>
                      </Td>
                      <Td align="right" className="mono text-slate-500">{fmt(s.eps_estimate)}</Td>
                      <Td align="right" className="mono text-slate-300">{fmt(s.eps_reported)}</Td>
                      <Td align="right" className="mono text-slate-400">{s.announced_at}</Td>
                      <Td align="right" className="mono text-slate-400">{s.entry_date}</Td>
                      <Td align="right" className="mono text-slate-300">{s.days_held ?? "—"}</Td>
                      <Td align="right">
                        <span className={`mono font-semibold ${(s.days_remaining ?? 99) <= 3 ? "text-amber-300" : "text-slate-200"}`}>
                          {s.days_remaining ?? "—"}
                        </span>
                      </Td>
                      <Td align="right" className="mono text-slate-400">{s.window_end}</Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
