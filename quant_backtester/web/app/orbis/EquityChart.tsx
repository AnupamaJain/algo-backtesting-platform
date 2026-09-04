"use client";

/**
 * Cumulative P&L of the IB trades, in index points.
 *
 * Plotted from the trade ledger rather than a smoothed curve: each step is
 * one closed trade, so a flat stretch means no setup filled, not a holding
 * period. The zero line is drawn because the whole question here is which
 * side of it the curve ends on.
 */

import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

const AXIS = { stroke: "#475069", fontSize: 10 };

export function EquityChart({
  data,
}: {
  data: { date: string; equity: number; points: number }[];
}) {
  if (data.length === 0) {
    return (
      <div className="flex h-[260px] items-center justify-center">
        <span className="text-xs text-slate-600">no filled trades to plot</span>
      </div>
    );
  }

  const final = data[data.length - 1].equity;
  const positive = final >= 0;
  const stroke = positive ? "#34d399" : "#fb7185";

  return (
    <ResponsiveContainer width="100%" height={260}>
      <AreaChart data={data} margin={{ top: 8, right: 16, bottom: 4, left: 4 }}>
        <defs>
          <linearGradient id="orbisEquity" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={stroke} stopOpacity={0.28} />
            <stop offset="100%" stopColor={stroke} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke="#1a2233" vertical={false} />
        <XAxis dataKey="date" tick={AXIS} tickLine={false} minTickGap={40} />
        <YAxis tick={AXIS} tickLine={false} width={56} />
        <ReferenceLine y={0} stroke="#64748b" strokeDasharray="3 3" />
        <Tooltip
          contentStyle={{
            background: "#0d131f",
            border: "1px solid #1a2233",
            borderRadius: 8,
            fontSize: 11,
          }}
          formatter={(value: number, name: string) => [
            `${value.toFixed(1)} pts`,
            name === "equity" ? "cumulative" : name,
          ]}
        />
        <Area
          type="stepAfter"
          dataKey="equity"
          stroke={stroke}
          strokeWidth={1.6}
          fill="url(#orbisEquity)"
          isAnimationActive={false}
          dot={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}
