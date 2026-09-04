/** Shared presentational primitives used across every page. */

import type { ReactNode } from "react";

export function Card({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`rounded-xl border border-[var(--color-line-soft)] bg-[var(--color-panel)] ${className}`}
    >
      {children}
    </section>
  );
}

export function CardHead({
  title,
  subtitle,
  action,
}: {
  title: string;
  subtitle?: string;
  action?: ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-start justify-between gap-3 border-b border-[var(--color-line-soft)] px-5 py-4">
      <div>
        <h2 className="text-[15px] font-semibold text-slate-100">{title}</h2>
        {subtitle ? <p className="mt-1 text-xs text-slate-500">{subtitle}</p> : null}
      </div>
      {action}
    </header>
  );
}

export function Stat({
  label,
  value,
  sub,
  tone = "text-slate-100",
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: string;
}) {
  return (
    <div className="rounded-xl border border-[var(--color-line-soft)] bg-[var(--color-panel)] px-4 py-3.5">
      <div className="text-[10px] font-semibold uppercase tracking-wider text-slate-500">
        {label}
      </div>
      <div className={`mono mt-1.5 text-xl font-semibold ${tone}`}>{value}</div>
      {sub ? <div className="mt-1 text-[11px] text-slate-500">{sub}</div> : null}
    </div>
  );
}

const PILL_TONES: Record<string, string> = {
  green: "bg-emerald-500/12 text-emerald-300 ring-emerald-500/25",
  red: "bg-rose-500/12 text-rose-300 ring-rose-500/25",
  amber: "bg-amber-500/12 text-amber-300 ring-amber-500/25",
  cyan: "bg-cyan-500/12 text-cyan-300 ring-cyan-500/25",
  slate: "bg-slate-500/12 text-slate-300 ring-slate-500/25",
};

export function Pill({
  children,
  tone = "slate",
}: {
  children: ReactNode;
  tone?: keyof typeof PILL_TONES | string;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-[10px] font-semibold ring-1 ring-inset ${
        PILL_TONES[tone] ?? PILL_TONES.slate
      }`}
    >
      {children}
    </span>
  );
}

export function EmptyState({
  title,
  body,
  action,
}: {
  title: string;
  body: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 px-6 py-14 text-center">
      <div className="text-2xl opacity-40">◍</div>
      <h3 className="text-sm font-semibold text-slate-300">{title}</h3>
      <p className="max-w-md text-xs leading-relaxed text-slate-500">{body}</p>
      {action}
    </div>
  );
}

/** An inline explainer for a concept the user may not know. */
export function Explain({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-lg border border-cyan-500/15 bg-cyan-500/[0.04] px-3.5 py-2.5 text-[11.5px] leading-relaxed text-slate-400">
      {children}
    </p>
  );
}

export function Table({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] text-[12.5px]">{children}</table>
    </div>
  );
}

export function Th({
  children,
  align = "left",
}: {
  children: ReactNode;
  align?: "left" | "right";
}) {
  return (
    <th
      className={`whitespace-nowrap border-b border-[var(--color-line)] px-3 py-2.5 text-[10px] font-semibold uppercase tracking-wider text-slate-500 ${
        align === "right" ? "text-right" : "text-left"
      }`}
    >
      {children}
    </th>
  );
}

export function Td({
  children,
  align = "left",
  className = "",
}: {
  children: ReactNode;
  align?: "left" | "right";
  className?: string;
}) {
  return (
    <td
      className={`whitespace-nowrap border-b border-[var(--color-line-soft)] px-3 py-2.5 text-slate-300 ${
        align === "right" ? "text-right" : ""
      } ${className}`}
    >
      {children}
    </td>
  );
}
