import type { Metadata } from "next";
import Nav from "@/components/Nav";
import "./globals.css";

export const metadata: Metadata = {
  title: "Strategy Lab — Quant Backtester",
  description:
    "Walk-forward backtesting, validation funnel, robustness testing and regime-switching portfolio analysis.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="flex min-h-screen">
          <aside className="sticky top-0 hidden h-screen w-60 shrink-0 border-r border-[var(--color-line-soft)] bg-[var(--color-panel)] lg:block">
            <Nav />
          </aside>
          <main className="min-w-0 flex-1">{children}</main>
        </div>
      </body>
    </html>
  );
}
