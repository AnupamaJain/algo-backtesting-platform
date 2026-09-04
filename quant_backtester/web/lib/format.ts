/** Shared display formatting. Keeps number rendering consistent everywhere. */

export function pct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

export function ratio(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return "—";
  if (!Number.isFinite(value)) return value > 0 ? "∞" : "—";
  return value.toFixed(digits);
}

export function count(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toLocaleString();
}

export function shortDate(value: string): string {
  if (!value) return "";
  return value.slice(0, 10);
}

/** Turn "rsi_period=14_oversold_threshold=30" into readable chips. */
export function paramChips(paramKey: string): { name: string; value: string }[] {
  if (!paramKey) return [];
  const parts: { name: string; value: string }[] = [];
  const regex = /([a-z_]+)=([^_]+(?:_(?![a-z_]+=)[^_]+)*)/g;
  let match;
  while ((match = regex.exec(paramKey)) !== null) {
    parts.push({ name: match[1].replace(/_/g, " "), value: match[2] });
  }
  return parts;
}

/** Colour token for a metric that is "good when high". */
export function sharpeTone(value: number | null): string {
  if (value === null) return "text-slate-500";
  if (value >= 1) return "text-emerald-400";
  if (value >= 0.5) return "text-cyan-400";
  if (value >= 0) return "text-amber-400";
  return "text-rose-400";
}

/** Colour token for drawdown, where lower is better. */
export function drawdownTone(value: number | null): string {
  if (value === null) return "text-slate-500";
  if (value <= 0.15) return "text-emerald-400";
  if (value <= 0.35) return "text-amber-400";
  return "text-rose-400";
}
