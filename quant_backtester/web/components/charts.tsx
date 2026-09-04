"use client";

/**
 * Chart components.
 *
 * Colour is used consistently across the app: emerald = survived / passing
 * evidence, rose = rejected / risk, cyan = the thing under study, amber =
 * caution. Every chart also encodes its meaning in text or shape so it does
 * not rely on colour alone.
 */

import {
  Area,
  AreaChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

const AXIS = { stroke: "#475069", fontSize: 10 };
const GRID = "#1a2233";

const TOOLTIP_STYLE = {
  contentStyle: {
    background: "#10141f",
    border: "1px solid #232c40",
    borderRadius: 10,
    fontSize: 11,
  },
  labelStyle: { color: "#94a3b8", fontSize: 10 },
} as const;

export const SERIES_COLORS = ["#22d3ee", "#34d399", "#fbbf24", "#f472b6", "#a78bfa"];

/**
 * Explicit dot renderer for scatter plots.
 *
 * Recharts' built-in symbol generator emits `d="M0,0"` here — a zero-size
 * path — so the points land at the right coordinates but paint nothing.
 * Rendering the circle ourselves sidesteps that entirely and gives direct
 * control over radius and opacity.
 */
function Dot(props: {
  cx?: number;
  cy?: number;
  fill?: string;
  fillOpacity?: number;
  r?: number;
}) {
  const { cx, cy, fill, fillOpacity = 1, r = 4.5 } = props;
  if (cx === undefined || cy === undefined) return null;
  return <circle cx={cx} cy={cy} r={r} fill={fill} fillOpacity={fillOpacity} />;
}

// -- Layer 2: IS vs OOS scatter ----------------------------------------

export function IsOosScatter({
  points,
}: {
  points: { isSharpe: number | null; oosSharpe: number | null; survived: boolean; symbol: string; strategy: string; rejectedAtStage: number | null }[];
}) {
  const data = points
    .filter((p) => p.isSharpe !== null && p.oosSharpe !== null)
    .map((p) => ({ ...p, x: p.isSharpe as number, y: p.oosSharpe as number }));

  if (data.length === 0) return null;

  const survived = data.filter((d) => d.survived);
  const rejected = data.filter((d) => !d.survived);

  return (
    <ResponsiveContainer width="100%" height={340}>
      <ScatterChart margin={{ top: 10, right: 16, bottom: 28, left: 4 }}>
        <CartesianGrid stroke={GRID} />
        <XAxis
          type="number"
          dataKey="x"
          name="In-sample Sharpe"
          {...AXIS}
          label={{ value: "In-sample Sharpe", position: "insideBottom", offset: -16, fill: "#64748b", fontSize: 11 }}
        />
        <YAxis
          type="number"
          dataKey="y"
          name="Out-of-sample Sharpe"
          {...AXIS}
          label={{ value: "OOS Sharpe", angle: -90, position: "insideLeft", fill: "#64748b", fontSize: 11 }}
        />
        {/* y = x: points below this line did worse on unseen data. */}
        <ReferenceLine
          ifOverflow="extendDomain"
          segment={[
            { x: -1.5, y: -1.5 },
            { x: 2.5, y: 2.5 },
          ]}
          stroke="#475069"
          strokeDasharray="4 4"
        />
        <ReferenceLine y={0} stroke="#334155" />
        <Tooltip
          {...TOOLTIP_STYLE}
          cursor={{ strokeDasharray: "3 3" }}
          content={({ active, payload }) => {
            if (!active || !payload?.length) return null;
            const d = payload[0].payload;
            return (
              <div className="rounded-lg border border-[#232c40] bg-[#10141f] p-2.5 text-[11px]">
                <div className="font-semibold text-slate-200">
                  {d.symbol} · {d.strategy}
                </div>
                <div className="mono mt-1 text-slate-400">IS {d.x.toFixed(2)} → OOS {d.y.toFixed(2)}</div>
                <div className="mt-1 text-[10px] text-slate-500">
                  {d.survived ? "Survived all gates" : `Rejected at gate ${d.rejectedAtStage ?? "—"}`}
                </div>
              </div>
            );
          }}
        />
        <Legend
          verticalAlign="top"
          height={28}
          formatter={(value) => <span style={{ color: "#94a3b8", fontSize: 11 }}>{value}</span>}
        />
        <Scatter isAnimationActive={false} name="Rejected" data={rejected} fill="#f43f5e" fillOpacity={0.5} shape={<Dot />} />
        <Scatter isAnimationActive={false} name="Survived" data={survived} fill="#34d399" shape={<Dot r={5.5} />} />
      </ScatterChart>
    </ResponsiveContainer>
  );
}

// -- Layer 4: equity curves --------------------------------------------

export function EquityChart({
  points,
  series,
}: {
  points: Record<string, number | string>[];
  series: string[];
}) {
  if (points.length === 0) return null;
  return (
    <ResponsiveContainer width="100%" height={320}>
      <LineChart data={points} margin={{ top: 8, right: 16, bottom: 4, left: 4 }}>
        <CartesianGrid stroke={GRID} />
        <XAxis dataKey="date" {...AXIS} minTickGap={48} tickFormatter={(v) => String(v).slice(0, 7)} />
        <YAxis {...AXIS} tickFormatter={(v) => `${Number(v).toFixed(1)}x`} />
        <Tooltip {...TOOLTIP_STYLE} formatter={(v: number) => `${v.toFixed(2)}x`} />
        <Legend
          verticalAlign="top"
          height={28}
          formatter={(value) => <span style={{ color: "#94a3b8", fontSize: 11 }}>{value}</span>}
        />
        {series.map((name, i) => (
          <Line
            key={name}
            type="monotone"
            dataKey={name}
            stroke={SERIES_COLORS[i % SERIES_COLORS.length]}
            strokeWidth={1.8}
            dot={false}
            isAnimationActive={false}
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}

// -- Layer 4: regime timeline ------------------------------------------

const REGIME_COLORS: Record<number, string> = {
  0: "#f43f5e", // bear
  1: "#34d399", // trending
  2: "#fbbf24", // ranging
};
const REGIME_NAMES: Record<number, string> = {
  0: "Bear / Crash",
  1: "Bull / Trending",
  2: "Choppy / Ranging",
};

export function RegimeTimeline({ points }: { points: { date: string; regime: number }[] }) {
  if (points.length === 0) return null;

  return (
    <div>
      <div className="flex h-14 w-full overflow-hidden rounded-lg border border-[var(--color-line)]">
        {points.map((p, i) => (
          <div
            key={i}
            className="h-full flex-1"
            style={{ background: REGIME_COLORS[p.regime] ?? "#475069" }}
            title={`${p.date}: ${REGIME_NAMES[p.regime] ?? "Unknown"}`}
          />
        ))}
      </div>
      <div className="mt-2 flex justify-between text-[10px] text-slate-500">
        <span>{points[0]?.date}</span>
        <span>{points[points.length - 1]?.date}</span>
      </div>
      <div className="mt-3 flex flex-wrap gap-4">
        {Object.entries(REGIME_NAMES).map(([code, name]) => (
          <span key={code} className="flex items-center gap-1.5 text-[11px] text-slate-400">
            <span
              className="h-2.5 w-2.5 rounded-sm"
              style={{ background: REGIME_COLORS[Number(code)] }}
            />
            {name}
          </span>
        ))}
      </div>
    </div>
  );
}

// -- Layer 3: sensitivity vs bootstrap ---------------------------------

export function RobustnessScatter({
  points,
}: {
  points: {
    symbol: string;
    strategy: string;
    sensitivityScore: number | null;
    drawdownAmplification: number | null;
    bootstrapVerdict: string;
  }[];
}) {
  const data = points
    .filter((p) => p.sensitivityScore !== null && p.drawdownAmplification !== null)
    .map((p) => ({
      ...p,
      x: p.sensitivityScore as number,
      y: p.drawdownAmplification as number,
    }));

  if (data.length === 0) return null;

  return (
    <ResponsiveContainer width="100%" height={300}>
      <ScatterChart margin={{ top: 10, right: 16, bottom: 28, left: 4 }}>
        <CartesianGrid stroke={GRID} />
        <XAxis
          type="number"
          dataKey="x"
          domain={[0, 1]}
          {...AXIS}
          label={{ value: "Parameter stability →", position: "insideBottom", offset: -16, fill: "#64748b", fontSize: 11 }}
        />
        <YAxis
          type="number"
          dataKey="y"
          {...AXIS}
          label={{ value: "Drawdown blow-up", angle: -90, position: "insideLeft", fill: "#64748b", fontSize: 11 }}
        />
        {/* Above 1.0, reshuffling made the drawdown worse than history did. */}
        <ReferenceLine y={1} stroke="#475069" strokeDasharray="4 4" />
        <Tooltip
          {...TOOLTIP_STYLE}
          content={({ active, payload }) => {
            if (!active || !payload?.length) return null;
            const d = payload[0].payload;
            return (
              <div className="rounded-lg border border-[#232c40] bg-[#10141f] p-2.5 text-[11px]">
                <div className="font-semibold text-slate-200">
                  {d.symbol} · {d.strategy}
                </div>
                <div className="mono mt-1 text-slate-400">
                  stability {d.x.toFixed(2)} · drawdown ×{d.y.toFixed(2)}
                </div>
                <div className="mt-1 text-[10px] text-slate-500">{d.bootstrapVerdict}</div>
              </div>
            );
          }}
        />
        <Scatter isAnimationActive={false} data={data} shape={<Dot r={5.5} />}>
          {data.map((d, i) => (
            <Cell
              key={i}
              fill={
                d.bootstrapVerdict === "PASS"
                  ? "#34d399"
                  : d.bootstrapVerdict === "FAIL"
                    ? "#f43f5e"
                    : "#64748b"
              }
            />
          ))}
        </Scatter>
      </ScatterChart>
    </ResponsiveContainer>
  );
}

// -- Small inline sparkline for stat cards — defined for potential future use.
// Currently not rendered by any page; kept here for easy adoption.
// export function MiniArea(...) { ... }
