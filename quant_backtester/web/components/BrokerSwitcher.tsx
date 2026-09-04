"use client";

/**
 * Switches which broker's book the console is reading.
 *
 * This is a *view* control, not a configuration change: picking a broker
 * here adds `?broker=` to the URL and the server components read that book.
 * It never edits broker.yaml, so it cannot change where orders would
 * actually be sent — the active broker stays whatever config says, and is
 * labelled as such.
 */

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback } from "react";

export type BrokerOption = {
  name: string;
  simulated: boolean;
  active: boolean;
  exchange: string;
};

export function BrokerSwitcher({ brokers }: { brokers: BrokerOption[] }) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const current = searchParams.get("broker") ?? brokers.find((b) => b.active)?.name ?? "";

  const select = useCallback(
    (name: string) => {
      const params = new URLSearchParams(searchParams.toString());
      const isActive = brokers.find((b) => b.name === name)?.active;
      // The active broker is the default view, so it needs no parameter.
      if (isActive) params.delete("broker");
      else params.set("broker", name);
      // Paging belongs to the previous book.
      params.delete("page");
      params.delete("opage");
      params.delete("fpage");
      router.push(`${pathname}?${params.toString()}`);
    },
    [brokers, pathname, router, searchParams]
  );

  if (brokers.length === 0) return null;

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="mono mr-1 text-[10px] uppercase tracking-wider text-slate-500">
        book
      </span>
      {brokers.map((broker) => {
        const selected = broker.name === current;
        return (
          <button
            key={broker.name}
            onClick={() => select(broker.name)}
            title={
              broker.simulated
                ? `${broker.name} — simulated money`
                : `${broker.name} — REAL account`
            }
            className={`mono rounded-lg border px-2.5 py-1 text-[11px] transition ${
              selected
                ? "border-emerald-500/50 bg-emerald-500/10 text-emerald-300"
                : "border-[var(--color-line-soft)] text-slate-400 hover:text-slate-200"
            }`}
          >
            {broker.name}
            {!broker.simulated ? (
              // Real-money books are marked everywhere they appear.
              <span className="ml-1 text-rose-400" title="real account">
                ●
              </span>
            ) : null}
            {broker.active ? (
              <span className="ml-1 text-[9px] text-slate-500">active</span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}
