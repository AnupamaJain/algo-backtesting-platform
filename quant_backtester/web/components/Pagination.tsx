"use client";

/**
 * Pagination control.
 *
 * Always shows the TOTAL, not just the current slice. "Showing 1–50 of 4,844"
 * tells an operator how much they are not looking at; "50 rows" hides it.
 */

import { useCallback } from "react";
import { useRouter, useSearchParams, usePathname } from "next/navigation";
import type { PaginationState } from "@/lib/pagination";

const PAGE_SIZES = [25, 50, 100, 250];

export function Pagination({
  state,
  paramPrefix = "",
  label = "rows",
}: {
  state: PaginationState;
  /** Prefix so two paginated tables can coexist on one page. */
  paramPrefix?: string;
  label?: string;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const pageKey = `${paramPrefix}page`;
  const sizeKey = `${paramPrefix}size`;

  const navigate = useCallback(
    (updates: Record<string, string | number>) => {
      const params = new URLSearchParams(searchParams.toString());
      for (const [key, value] of Object.entries(updates)) {
        params.set(key, String(value));
      }
      router.push(`${pathname}?${params.toString()}`, { scroll: false });
    },
    [pathname, router, searchParams]
  );

  const { page, pageSize, total, pages } = state;
  if (total === 0) return null;

  const first = (page - 1) * pageSize + 1;
  const last = Math.min(page * pageSize, total);

  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-t border-[var(--color-line-soft)] px-4 py-3">
      <div className="flex items-center gap-3">
        <span className="mono text-[11px] text-slate-500">
          {first.toLocaleString()}–{last.toLocaleString()} of{" "}
          <span className="text-slate-300">{total.toLocaleString()}</span> {label}
        </span>
        <label className="flex items-center gap-1.5 text-[11px] text-slate-500">
          <span className="sr-only">Rows per page</span>
          <select
            value={pageSize}
            onChange={(e) => navigate({ [sizeKey]: e.target.value, [pageKey]: 1 })}
            className="mono rounded-md border border-[var(--color-line)] bg-[var(--color-panel2)] px-2 py-1 text-[11px] text-slate-300"
            aria-label="Rows per page"
          >
            {PAGE_SIZES.map((size) => (
              <option key={size} value={size}>
                {size} / page
              </option>
            ))}
          </select>
        </label>
      </div>

      <nav className="flex items-center gap-1" aria-label="Pagination">
        <PageButton onClick={() => navigate({ [pageKey]: 1 })} disabled={page <= 1} label="First">
          «
        </PageButton>
        <PageButton
          onClick={() => navigate({ [pageKey]: page - 1 })}
          disabled={page <= 1}
          label="Previous"
        >
          ‹
        </PageButton>

        {pageWindow(page, pages).map((entry, i) =>
          entry === "gap" ? (
            <span key={`gap-${i}`} className="px-1.5 text-[11px] text-slate-600">
              …
            </span>
          ) : (
            <button
              key={entry}
              onClick={() => navigate({ [pageKey]: entry })}
              aria-current={entry === page ? "page" : undefined}
              className={`mono min-w-[28px] rounded-md px-2 py-1 text-[11px] transition ${
                entry === page
                  ? "bg-cyan-500 font-semibold text-slate-950"
                  : "border border-[var(--color-line)] text-slate-400 hover:text-slate-100"
              }`}
            >
              {entry}
            </button>
          )
        )}

        <PageButton
          onClick={() => navigate({ [pageKey]: page + 1 })}
          disabled={page >= pages}
          label="Next"
        >
          ›
        </PageButton>
        <PageButton
          onClick={() => navigate({ [pageKey]: pages })}
          disabled={page >= pages}
          label="Last"
        >
          »
        </PageButton>
      </nav>
    </div>
  );
}

function PageButton({
  children,
  onClick,
  disabled,
  label,
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled: boolean;
  label: string;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      className="mono rounded-md border border-[var(--color-line)] px-2 py-1 text-[11px] text-slate-400 transition hover:text-slate-100 disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:text-slate-400"
    >
      {children}
    </button>
  );
}

/** Page numbers with ellipsis, so 200 pages don't render 200 buttons. */
function pageWindow(page: number, pages: number): (number | "gap")[] {
  if (pages <= 7) return Array.from({ length: pages }, (_, i) => i + 1);

  const entries: (number | "gap")[] = [1];
  const start = Math.max(2, page - 1);
  const end = Math.min(pages - 1, page + 1);

  if (start > 2) entries.push("gap");
  for (let i = start; i <= end; i += 1) entries.push(i);
  if (end < pages - 1) entries.push("gap");
  entries.push(pages);
  return entries;
}
