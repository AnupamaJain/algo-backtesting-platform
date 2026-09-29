"use client";

/**
 * Portfolio equity for the PEAD study, compounded from 1.0.
 *
 * A multiple rather than the index points the ORBIS chart plots, because
 * this one compounds returns across many names: 1.0 is the starting capital
 * and the reference line is drawn there, since the question is whether the
 * curve ends above or below the money you began with.
 *
 * Nothing is smoothed. Every point is a stored row from
 * results/pead/equity_curve.csv, thinned by the loader when the series is
 * longer than the chart can usefully draw.
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

export function PeadEquityLine({
  data,
}: {
  data: { date: string; equity: number }[];
}) {
  if (data.length === 0) {
    return (
      <div className="flex h-[260px] items-center justify-center">
        <span className="text-xs text-slate-600">no equity curve stored</span>
      </div>
    );
  }

  const final = data[data.length - 1].equity;
  const positive = final >= 1;
  const stroke = positive ? "#34d399" : "#fb7185";

  // Padded a little so the line never runs along the frame, and always
  // including 1.0 -- a curve that stayed underwater would otherwise be drawn
  // without the level it never got back to.
  const values = data.map((d) => d.equity);
  const low = Math.min(1, ...values);
  const high = Math.max(1, ...values);
  const pad = (high - low) * 0.08 || 0.05;

  return (
    <ResponsiveContainer width="100%" height={260}>
      <AreaChart data={data} margin={{ top: 8, right: 16, bottom: 4, left: 4 }}>
        <defs>
          <linearGradient id="peadEquity" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={stroke} stopOpacity={0.28} />
            <stop offset="100%" stopColor={stroke} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke="#1a2233" vertical={false} />
        <XAxis dataKey="date" tick={AXIS} tickLine={false} minTickGap={40} />
        <YAxis
          tick={AXIS}
          tickLine={false}
          width={56}
          domain={[low - pad, high + pad]}
          tickFormatter={(v: number) => `${v.toFixed(2)}x`}
        />
        <ReferenceLine y={1} stroke="#64748b" strokeDasharray="3 3" />
        <Tooltip
          contentStyle={{
            background: "#0d131f",
            border: "1px solid #1a2233",
            borderRadius: 8,
            fontSize: 11,
          }}
          formatter={(value: number) => [
            `${value.toFixed(3)}x  (${((value - 1) * 100).toFixed(1)}%)`,
            "equity",
          ]}
        />
        <Area
          type="monotone"
          dataKey="equity"
          stroke={stroke}
          strokeWidth={1.6}
          fill="url(#peadEquity)"
          isAnimationActive={false}
          dot={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}
