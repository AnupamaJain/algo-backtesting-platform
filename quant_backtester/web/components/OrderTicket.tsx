"use client";

/**
 * Order entry.
 *
 * The mode banner is not decoration. An operator must be able to tell at a
 * glance whether the next click simulates a trade or spends real money, so
 * the mode is stated on the ticket itself rather than only in a settings page.
 */

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Pill } from "./ui";

type Mode = "PAPER" | "DRY-RUN" | "LIVE";

const MODE_COPY: Record<Mode, { tone: string; text: string }> = {
  PAPER: {
    tone: "cyan",
    text: "Paper account — orders execute against live market prices with simulated money.",
  },
  "DRY-RUN": {
    tone: "amber",
    text: "Dry run — orders are logged but never sent to the broker.",
  },
  LIVE: {
    tone: "red",
    text: "LIVE — orders are sent to a real broker and spend real money.",
  },
};

export default function OrderTicket({
  universe,
  mode,
}: {
  universe: string[];
  mode: Mode;
}) {
  const router = useRouter();
  const [symbol, setSymbol] = useState(universe[0] ?? "SPY");
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [quantity, setQuantity] = useState("10");
  const [orderType, setOrderType] = useState("MARKET");
  const [limitPrice, setLimitPrice] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  async function submit() {
    setBusy(true);
    setResult(null);
    try {
      const response = await fetch("/api/broker", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          command: "place",
          symbol,
          side,
          quantity: Number(quantity),
          order_type: orderType,
          limit_price: orderType.includes("LIMIT") ? Number(limitPrice) : undefined,
          strategy: "manual",
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        setResult({ ok: false, text: data.error ?? "Order failed." });
      } else {
        const order = data.order;
        setResult({
          ok: true,
          text:
            order.status === "COMPLETE"
              ? `Filled ${order.filled_quantity} ${order.symbol} @ ${Number(
                  order.average_price
                ).toFixed(4)}`
              : `${order.status}: ${order.order_id}`,
        });
        router.refresh();
      }
    } catch {
      setResult({ ok: false, text: "Network error." });
    } finally {
      setBusy(false);
    }
  }

  const copy = MODE_COPY[mode];

  return (
    <div className="rounded-xl border border-[var(--color-line-soft)] bg-[var(--color-panel)]">
      <div className="flex items-center justify-between gap-3 border-b border-[var(--color-line-soft)] px-5 py-4">
        <h2 className="text-[15px] font-semibold text-slate-100">New order</h2>
        <Pill tone={copy.tone}>{mode}</Pill>
      </div>

      <div className="px-5 py-4">
        <p
          className={`rounded-lg px-3.5 py-2.5 text-[11.5px] leading-relaxed ${
            mode === "LIVE"
              ? "border border-rose-500/30 bg-rose-500/[0.07] text-rose-200"
              : "border border-cyan-500/15 bg-cyan-500/[0.04] text-slate-400"
          }`}
        >
          {copy.text}
        </p>

        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <Field label="Symbol">
            <select
              value={symbol}
              onChange={(e) => setSymbol(e.target.value)}
              className="mono w-full rounded-lg border border-[var(--color-line)] bg-[var(--color-panel2)] px-3 py-2 text-[12px] text-slate-200"
            >
              {universe.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Side">
            <div className="flex gap-2">
              {(["BUY", "SELL"] as const).map((option) => (
                <button
                  key={option}
                  onClick={() => setSide(option)}
                  className={`flex-1 rounded-lg px-3 py-2 text-[12px] font-semibold transition ${
                    side === option
                      ? option === "BUY"
                        ? "bg-emerald-500/20 text-emerald-300 ring-1 ring-emerald-500/40"
                        : "bg-rose-500/20 text-rose-300 ring-1 ring-rose-500/40"
                      : "border border-[var(--color-line)] text-slate-400 hover:text-slate-200"
                  }`}
                >
                  {option}
                </button>
              ))}
            </div>
          </Field>

          <Field label="Quantity">
            <input
              type="number"
              min="0"
              step="any"
              value={quantity}
              onChange={(e) => setQuantity(e.target.value)}
              className="mono w-full rounded-lg border border-[var(--color-line)] bg-[var(--color-panel2)] px-3 py-2 text-[12px] text-slate-200"
            />
          </Field>

          <Field label="Order type">
            <select
              value={orderType}
              onChange={(e) => setOrderType(e.target.value)}
              className="mono w-full rounded-lg border border-[var(--color-line)] bg-[var(--color-panel2)] px-3 py-2 text-[12px] text-slate-200"
            >
              {["MARKET", "LIMIT", "STOP"].map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </Field>

          {orderType !== "MARKET" ? (
            <Field label={orderType === "LIMIT" ? "Limit price" : "Stop price"}>
              <input
                type="number"
                step="any"
                value={limitPrice}
                onChange={(e) => setLimitPrice(e.target.value)}
                className="mono w-full rounded-lg border border-[var(--color-line)] bg-[var(--color-panel2)] px-3 py-2 text-[12px] text-slate-200"
              />
            </Field>
          ) : null}
        </div>

        <button
          onClick={submit}
          disabled={busy || !quantity}
          className={`mt-4 w-full rounded-lg px-4 py-2.5 text-[13px] font-semibold transition disabled:cursor-not-allowed disabled:bg-slate-700 disabled:text-slate-500 ${
            side === "BUY"
              ? "bg-emerald-500 text-slate-950 hover:bg-emerald-400"
              : "bg-rose-500 text-slate-950 hover:bg-rose-400"
          }`}
        >
          {busy ? "Submitting…" : `${side} ${quantity || "0"} ${symbol}`}
        </button>

        {result ? (
          <p
            className={`mono mt-3 rounded-lg px-3 py-2 text-[11.5px] ${
              result.ok
                ? "bg-emerald-500/10 text-emerald-300"
                : "bg-rose-500/10 text-rose-300"
            }`}
          >
            {result.text}
          </p>
        ) : null}
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-[10.5px] font-medium text-slate-400">{label}</span>
      {children}
    </label>
  );
}
