"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const RESEARCH_LINKS = [
  { href: "/", label: "Overview", icon: "◎", note: "Pipeline status" },
  { href: "/data", label: "Data & Strategies", icon: "▤", note: "Layer 1" },
  { href: "/funnel", label: "Validation Funnel", icon: "▽", note: "Layer 2" },
  { href: "/robustness", label: "Robustness", icon: "◈", note: "Layer 3" },
  { href: "/regime", label: "Regime & Portfolio", icon: "◐", note: "Layer 4" },
  { href: "/orbis", label: "ORBIS — IB-60", icon: "◫", note: "Intraday NSE" },
];

const CONSOLE_LINKS = [
  { href: "/console", label: "Dashboard", icon: "◧", note: "Live positions" },
  { href: "/console/strategies", label: "Strategies", icon: "⬡", note: "Deployable" },
  { href: "/console/brokers", label: "Brokers", icon: "◈", note: "Adapters & gates" },
  { href: "/console/activity", label: "Order Activity", icon: "↯", note: "Orders & fills" },
  { href: "/console/watchdogs", label: "Watchdogs", icon: "◎", note: "Audit log" },
  { href: "/console/map", label: "System Map", icon: "▦", note: "Architecture" },
];

function SectionLabel({
  children,
  className = "",
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`px-3 pb-1.5 pt-2 text-[9.5px] font-semibold uppercase tracking-wider text-slate-600 ${className}`}
    >
      {children}
    </div>
  );
}

export default function Nav() {
  const pathname = usePathname();

  return (
    <nav className="flex h-full flex-col gap-0.5 overflow-y-auto p-3" aria-label="Main">
      <Link href="/" className="mb-4 flex items-center gap-2.5 px-2 py-2">
        <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-cyan-500 to-blue-700 text-[12px] font-bold text-slate-950">
          QB
        </span>
        <span className="min-w-0">
          <span className="block text-[13.5px] font-semibold text-slate-100">Strategy Lab</span>
          <span className="block text-[10px] text-slate-500">Quant backtester</span>
        </span>
      </Link>

      <SectionLabel>Research</SectionLabel>
      {RESEARCH_LINKS.map((link) => {
        const active = pathname === link.href;
        return (
          <Link
            key={link.href}
            href={link.href}
            aria-current={active ? "page" : undefined}
            className={`flex items-center gap-2.5 rounded-lg px-3 py-2.5 text-[12.5px] font-medium transition ${
              active
                ? "bg-cyan-500/12 text-cyan-300"
                : "text-slate-400 hover:bg-[var(--color-panel2)] hover:text-slate-200"
            }`}
          >
            <span className="w-4 text-center opacity-70">{link.icon}</span>
            <span className="min-w-0 flex-1">
              <span className="block truncate">{link.label}</span>
              <span className="block text-[9.5px] text-slate-600">{link.note}</span>
            </span>
          </Link>
        );
      })}

      <SectionLabel className="mt-4">Trading Ops</SectionLabel>
      {CONSOLE_LINKS.map((link) => {
        const active = pathname === link.href;
        return (
          <Link
            key={link.href}
            href={link.href}
            aria-current={active ? "page" : undefined}
            className={`flex items-center gap-2.5 rounded-lg px-3 py-2.5 text-[12.5px] font-medium transition ${
              active
                ? "bg-cyan-500/12 text-cyan-300"
                : "text-slate-400 hover:bg-[var(--color-panel2)] hover:text-slate-200"
            }`}
          >
            <span className="w-4 text-center opacity-70">{link.icon}</span>
            <span className="min-w-0 flex-1">
              <span className="block truncate">{link.label}</span>
              <span className="block text-[9.5px] text-slate-600">{link.note}</span>
            </span>
          </Link>
        );
      })}

      <div className="mt-auto rounded-lg border border-amber-500/15 bg-amber-500/[0.04] p-3">
        <p className="text-[10px] leading-relaxed text-amber-200/70">
          Research tool. Backtested results describe the past and are not a forecast or
          investment advice.
        </p>
      </div>
    </nav>
  );
}
