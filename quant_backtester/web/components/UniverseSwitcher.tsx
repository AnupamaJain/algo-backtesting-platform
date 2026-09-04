"use client";

/**
 * Switches which market's results are on screen.
 *
 * The active universe is stated prominently rather than hidden in a menu:
 * the two markets have disjoint instruments and separate result trees, so
 * "which market am I looking at?" must never be ambiguous.
 */

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { UNIVERSES } from "@/lib/universes";

export default function UniverseSwitcher({ active }: { active: string }) {
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const hrefFor = (id: string) => {
    const params = new URLSearchParams(searchParams.toString());
    params.set("universe", id);
    // Paging is per-universe; carrying a page number across would land on a
    // page that may not exist in the other market.
    for (const key of [...params.keys()]) {
      if (key.endsWith("page") || key.endsWith("size")) params.delete(key);
    }
    return `${pathname}?${params.toString()}`;
  };

  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-500">
        Market
      </span>
      <div className="flex overflow-hidden rounded-lg border border-[var(--color-line)]">
        {UNIVERSES.map((u) => {
          const on = u.id === active;
          return (
            <Link
              key={u.id}
              href={hrefFor(u.id)}
              aria-current={on ? "true" : undefined}
              title={`${u.market} · ${u.provider}`}
              className={`px-3 py-1.5 text-[11.5px] font-medium transition ${
                on
                  ? "bg-cyan-500 text-slate-950"
                  : "bg-[var(--color-panel2)] text-slate-400 hover:text-slate-100"
              }`}
            >
              {u.label}
              <span className={`ml-1.5 text-[9.5px] ${on ? "text-slate-800" : "text-slate-600"}`}>
                {u.provider}
              </span>
            </Link>
          );
        })}
      </div>
    </div>
  );
}
