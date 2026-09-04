"use client";

/**
 * Runs a pipeline layer and streams the result back.
 *
 * Long runs are genuinely long (a full universe sweep takes minutes), so the
 * panel makes the wait legible: it says what is happening, keeps the log
 * visible, and refreshes the page data when the run finishes rather than
 * leaving stale numbers on screen.
 */

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Pill } from "./ui";

export type RunOptions = {
  layer: "layer1" | "layer2" | "layer3" | "layer4" | "all";
  label: string;
  hint: string;
  showSymbols?: boolean;
  showTopN?: boolean;
  showGenerateSignals?: boolean;
  /** Which market to run against. Omitting it runs the default universe. */
  universe?: string;
};

export default function RunPanel({
  layer,
  label,
  hint,
  showSymbols = false,
  showTopN = false,
  showGenerateSignals = false,
  universe,
}: RunOptions) {
  const router = useRouter();
  const [symbols, setSymbols] = useState("");
  const [topN, setTopN] = useState("15");
  const [generateSignals, setGenerateSignals] = useState(true);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<{
    ok: boolean;
    stdout: string;
    stderr: string;
    durationMs: number;
  } | null>(null);
  const [showLog, setShowLog] = useState(false);

  async function run() {
    setRunning(true);
    setResult(null);
    try {
      const response = await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          layer,
          symbols: showSymbols && symbols.trim() ? symbols : undefined,
          topN: showTopN ? Number(topN) : undefined,
          generateSignals: showGenerateSignals ? generateSignals : undefined,
          universe,
        }),
      });
      const data = await response.json();
      setResult(data);
      setShowLog(!data.ok);
      // Pull the freshly-written artifacts into the page.
      router.refresh();
    } catch {
      setResult({ ok: false, stdout: "", stderr: "Network error.", durationMs: 0 });
      setShowLog(true);
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="rounded-xl border border-[var(--color-line-soft)] bg-[var(--color-panel)] p-5">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          <h3 className="text-[13px] font-semibold text-slate-200">{label}</h3>
          <p className="mt-1 max-w-2xl text-[11.5px] leading-relaxed text-slate-500">{hint}</p>
        </div>

        <div className="flex flex-wrap items-end gap-3">
          {showSymbols ? (
            <div className="flex flex-col gap-1.5">
              <label htmlFor={`symbols-${layer}`} className="text-[10.5px] font-medium text-slate-400">
                Symbols (blank = full universe)
              </label>
              <input
                id={`symbols-${layer}`}
                value={symbols}
                onChange={(e) => setSymbols(e.target.value)}
                placeholder="SPY, QQQ, XLK"
                className="mono w-56 rounded-lg border border-[var(--color-line)] bg-[var(--color-panel2)] px-3 py-2 text-[12px] text-slate-200 placeholder:text-slate-600"
              />
            </div>
          ) : null}

          {showTopN ? (
            <div className="flex flex-col gap-1.5">
              <label htmlFor={`topn-${layer}`} className="text-[10.5px] font-medium text-slate-400">
                Fallback top-N
              </label>
              <input
                id={`topn-${layer}`}
                type="number"
                min={1}
                max={500}
                value={topN}
                onChange={(e) => setTopN(e.target.value)}
                className="mono w-24 rounded-lg border border-[var(--color-line)] bg-[var(--color-panel2)] px-3 py-2 text-[12px] text-slate-200"
              />
            </div>
          ) : null}

          {showGenerateSignals ? (
            <label className="flex cursor-pointer items-center gap-2 pb-2 text-[11px] text-slate-400">
              <input
                type="checkbox"
                checked={generateSignals}
                onChange={(e) => setGenerateSignals(e.target.checked)}
                className="h-3.5 w-3.5 accent-cyan-500"
              />
              Generate signals in-process
            </label>
          ) : null}

          <button
            onClick={run}
            disabled={running}
            className="rounded-lg bg-cyan-500 px-4 py-2 text-xs font-semibold text-slate-950 transition hover:bg-cyan-400 disabled:cursor-not-allowed disabled:bg-slate-700 disabled:text-slate-400"
          >
            {running ? "Running…" : "Run"}
          </button>
        </div>
      </div>

      {running ? (
        <div className="mt-4 flex items-center gap-2.5 rounded-lg border border-cyan-500/20 bg-cyan-500/[0.05] px-3.5 py-2.5">
          <span className="h-2 w-2 animate-pulse rounded-full bg-cyan-400" />
          <span className="text-[11.5px] text-slate-400">
            Running the Python pipeline. A full universe sweep can take several minutes — this
            page updates automatically when it finishes.
          </span>
        </div>
      ) : null}

      {result ? (
        <div className="mt-4">
          <div className="flex flex-wrap items-center gap-2.5">
            <Pill tone={result.ok ? "green" : "red"}>
              {result.ok ? "Completed" : "Failed"}
            </Pill>
            <span className="text-[11px] text-slate-500">
              {(result.durationMs / 1000).toFixed(1)}s
            </span>
            <button
              onClick={() => setShowLog((v) => !v)}
              className="text-[11px] font-medium text-cyan-400 underline-offset-2 hover:underline"
            >
              {showLog ? "Hide log" : "Show log"}
            </button>
          </div>

          {showLog ? (
            <pre className="mono mt-3 max-h-72 overflow-auto rounded-lg border border-[var(--color-line)] bg-[var(--color-ink)] p-3 text-[10.5px] leading-relaxed text-slate-400">
              {[result.stdout, result.stderr].filter(Boolean).join("\n") || "(no output)"}
            </pre>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
