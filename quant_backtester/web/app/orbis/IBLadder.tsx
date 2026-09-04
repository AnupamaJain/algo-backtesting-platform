/**
 * The Initial Balance ladder — the one visual worth keeping from the ORBIS
 * prototype. It renders the IB box with its quartile levels and, when a
 * setup existed that session, the entry/stop/target overlaid at true scale.
 *
 * Server component: it takes measured levels as props and draws them. There
 * is no simulated price, no tick loop, and nothing to animate — the session
 * it shows is over.
 */

export function IBLadder({
  high,
  low,
  mid,
  entry,
  stop,
  target,
  height = 240,
  width = 150,
}: {
  high: number | null;
  low: number | null;
  mid: number | null;
  entry?: number | null;
  stop?: number | null;
  target?: number | null;
  height?: number;
  width?: number;
}) {
  if (high === null || low === null || high <= low) {
    return (
      <div
        style={{ height, width }}
        className="flex items-center justify-center rounded-lg border border-dashed border-[var(--color-line-soft)]"
      >
        <span className="mono text-[10px] text-slate-600">no IB</span>
      </div>
    );
  }

  // Include the trade levels in the drawn range so a stop below the IB low
  // is visible rather than clipped off the bottom of the box.
  const values = [high, low, entry, stop, target].filter(
    (v): v is number => typeof v === "number" && Number.isFinite(v),
  );
  const rawTop = Math.max(...values);
  const rawBot = Math.min(...values);
  const pad = (rawTop - rawBot) * 0.12 || 1;
  const top = rawTop + pad;
  const bot = rawBot - pad;
  const span = top - bot;
  const y = (v: number) => ((top - v) / span) * height;

  const range = high - low;
  const levels = [
    { v: high, label: "HIGH", color: "#fbbf24", dash: "none" },
    { v: low + 0.75 * range, label: "75", color: "#475569", dash: "3 3" },
    { v: mid ?? low + 0.5 * range, label: "50", color: "#34d399", dash: "none" },
    { v: low + 0.25 * range, label: "25", color: "#475569", dash: "3 3" },
    { v: low, label: "LOW", color: "#38bdf8", dash: "none" },
  ];

  const overlays = [
    { v: entry, label: "entry", color: "#34d399" },
    { v: stop, label: "stop", color: "#fb7185" },
    { v: target, label: "target", color: "#22d3ee" },
  ].filter((o): o is { v: number; label: string; color: string } =>
    typeof o.v === "number" && Number.isFinite(o.v),
  );

  return (
    <svg width={width} height={height} className="overflow-visible">
      <rect
        x={30}
        y={y(high)}
        width={width - 58}
        height={Math.max(1, y(low) - y(high))}
        fill="rgba(52,211,153,0.05)"
        stroke="var(--color-line-soft)"
      />
      {levels.map((l) => (
        <g key={l.label}>
          <line
            x1={30}
            y1={y(l.v)}
            x2={width - 28}
            y2={y(l.v)}
            stroke={l.color}
            strokeWidth={l.label === "50" ? 1.4 : 1}
            strokeDasharray={l.dash}
            opacity={0.85}
          />
          <text x={0} y={y(l.v) + 3} fill={l.color} fontSize={9} className="mono">
            {l.label}
          </text>
          <text
            x={width - 24}
            y={y(l.v) + 3}
            fill="#64748b"
            fontSize={9}
            className="mono"
          >
            {l.v.toFixed(0)}
          </text>
        </g>
      ))}
      {overlays.map((o) => (
        <g key={o.label}>
          <line
            x1={30}
            y1={y(o.v)}
            x2={width - 28}
            y2={y(o.v)}
            stroke={o.color}
            strokeWidth={1}
            strokeDasharray="2 2"
            opacity={0.75}
          />
          <text x={34} y={y(o.v) - 3} fill={o.color} fontSize={8} className="mono">
            {o.label}
          </text>
        </g>
      ))}
    </svg>
  );
}
