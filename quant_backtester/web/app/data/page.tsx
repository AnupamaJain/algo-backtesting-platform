import Link from "next/link";
import ConfigEditor from "@/components/ConfigEditor";
import RunPanel from "@/components/RunPanel";
import { Card, CardHead, EmptyState, Explain, Table, Td, Th } from "@/components/ui";
import { getCachedSymbols, getSignalCount } from "@/lib/artifacts";
import { configsForLayer } from "@/lib/config-schema";
import { readConfiguredStrategies } from "@/lib/config-io";
import { universeFrom } from "@/lib/universes";
import UniverseSwitcher from "@/components/UniverseSwitcher";
import { count } from "@/lib/format";

export const dynamic = "force-dynamic";

/**
 * Human-readable copy for each strategy. The list rendered on the page comes
 * from the live config, not from here — this only supplies wording, so a
 * strategy added to config still appears (with a neutral description) rather
 * than silently going missing.
 */
const STRATEGY_COPY: Record<string, { label: string; family: string; desc: string }> = {
  // Mean reversion
  RSIReversion: {
    label: "RSI Snapback",
    family: "Mean reversion",
    desc: "Buys when RSI falls below the oversold line, flips short when it crosses overbought.",
  },
  BBReversion: {
    label: "Bollinger Reversion",
    family: "Mean reversion",
    desc: "Buys when price closes below the lower band, flips short above the upper band.",
  },
  KeltnerReversion: {
    label: "Keltner Reversion",
    family: "Mean reversion",
    desc: "Same idea as Bollinger, but the envelope is built from ATR rather than standard deviation.",
  },
  ZScoreReversion: {
    label: "Z-Score Reversion",
    family: "Mean reversion",
    desc: "Fades statistically extreme deviations from a rolling mean. Scale-free — the same threshold means the same thing on any asset.",
  },
  WilliamsRReversion: {
    label: "Williams %R Reversion",
    family: "Mean reversion",
    desc: "Buys extreme Williams %R oversold readings, flips short at overbought extremes.",
  },
  CCIReversion: {
    label: "CCI Reversion",
    family: "Mean reversion",
    desc: "Fades Commodity Channel Index extremes away from the typical price.",
  },
  // Trend following
  MACrossover: {
    label: "MA Crossover",
    family: "Trend following",
    desc: "Long while the fast average is above the slow one — the classic golden cross.",
  },
  MACDTrend: {
    label: "MACD Trend",
    family: "Trend following",
    desc: "Long while the MACD line leads its signal line, short while it lags.",
  },
  SupertrendFollow: {
    label: "Supertrend Follow",
    family: "Trend following",
    desc: "Follows the Supertrend band direction, which flips when price crosses the ATR-based envelope.",
  },
  ADXTrend: {
    label: "ADX Trend Filter",
    family: "Trend following",
    desc: "Takes a directional position only when ADX confirms trend strength, standing aside in choppy markets.",
  },
  // Momentum
  SimpleMomentum: {
    label: "Simple Momentum",
    family: "Momentum",
    desc: "Long if price is above where it sat N days ago; short if below.",
  },
  ROCMomentum: {
    label: "ROC Momentum",
    family: "Momentum",
    desc: "Long when the trailing rate-of-change clears a threshold, short when it falls below. Dead-bands noise around zero.",
  },
  DualMomentum: {
    label: "Dual Momentum",
    family: "Momentum",
    desc: "Requires both a short and a long lookback to agree before taking a position — trades less, holds longer.",
  },
  // Breakout
  TurtleBreakout: {
    label: "Donchian Breakout",
    family: "Breakout",
    desc: "The Turtle system: enter on an N-day high/low breakout, exit on a shorter channel break.",
  },
  VolatilityBreakout: {
    label: "Volatility Breakout",
    family: "Breakout",
    desc: "Enters when price travels more than N × ATR from the prior close, self-adjusting to current volatility.",
  },
  SqueezeBreakout: {
    label: "Squeeze Breakout",
    family: "Breakout",
    desc: "Identifies Bollinger Band volatility squeezes, then trades the directional expansion that follows.",
  },
  // Volatility
  VolatilityRegime: {
    label: "Volatility Regime",
    family: "Volatility",
    desc: "Holds risk only while realized volatility is below its own historical quantile. Drove 180 of 220 ultra-robust survivors.",
  },
  VolatilityMeanReversion: {
    label: "Volatility Mean Reversion",
    family: "Volatility",
    desc: "Buys after a volatility spike exhausts itself — volatility is strongly mean-reverting.",
  },
  // Chart patterns
  StructureBreak: {
    label: "Structure Break",
    family: "Chart patterns",
    desc: "Trades market structure: enters long on new swing highs, short on new swing lows.",
  },
  InsideBarBreakout: {
    label: "Inside Bar Breakout",
    family: "Chart patterns",
    desc: "An inside bar marks compression; trades the break of the prior bar's range.",
  },
  EngulfingReversal: {
    label: "Engulfing Reversal",
    family: "Chart patterns",
    desc: "Bullish/bearish engulfing candle patterns, optionally filtered to only trade against the prevailing trend.",
  },
};


export default async function DataPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const universe = universeFrom(await searchParams);
  const [symbols, signals, strategies] = await Promise.all([
    getCachedSymbols(universe),
    getSignalCount(universe),
    readConfiguredStrategies(),
  ]);

  return (
    <div className="mx-auto max-w-[1500px] px-6 py-8 lg:px-10">
      <header className="mb-6">
        <div className="text-[11px] font-semibold uppercase tracking-wider text-cyan-400">
          Layer 1
        </div>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight text-slate-100">
          Data &amp; Strategies
        </h1>
        <p className="mt-1.5 max-w-3xl text-[13px] leading-relaxed text-slate-400">
          Downloads daily price history for your asset universe and generates trading signals for
          every strategy and parameter combination you define below.
        </p>
        <div className="mt-3">
          <UniverseSwitcher active={universe.id} />
        </div>
      </header>

      <Card className="mb-6">
        <CardHead
          title="How to use this page"
          subtitle="This is step 1 of 4. Each step feeds the next."
        />
        <div className="grid gap-3 px-5 py-5 sm:grid-cols-2 xl:grid-cols-4">
          {[
            {
              n: 1,
              here: true,
              title: "Define & generate",
              body: "Set the asset universe and the parameter grid below, then hit Generate signals. Every value you list is swept as a separate configuration.",
              href: "/data",
            },
            {
              n: 2,
              title: "Validate",
              body: "Walk-forward backtest on unseen data, then six gates that try to kill each configuration.",
              href: "/funnel",
            },
            {
              n: 3,
              title: "Stress test",
              body: "Perturb the parameters and reshuffle the trades, to separate real edge from a lucky sequence.",
              href: "/robustness",
            },
            {
              n: 4,
              title: "Build portfolios",
              body: "Detect market regimes and route capital, measured against static baselines.",
              href: "/regime",
            },
          ].map((step) => (
            <Link
              key={step.n}
              href={step.href}
              className={`rounded-lg border px-4 py-3 transition ${
                step.here
                  ? "border-cyan-500/40 bg-cyan-500/[0.06]"
                  : "border-[var(--color-line-soft)] bg-[var(--color-panel2)] hover:border-cyan-500/30"
              }`}
            >
              <div className="flex items-center gap-2">
                <span
                  className={`flex h-5 w-5 items-center justify-center rounded text-[10px] font-bold ${
                    step.here ? "bg-cyan-500 text-slate-950" : "bg-slate-700 text-slate-300"
                  }`}
                >
                  {step.n}
                </span>
                <span className="text-[12.5px] font-semibold text-slate-200">{step.title}</span>
                {step.here ? (
                  <span className="text-[9.5px] font-semibold uppercase tracking-wide text-cyan-400">
                    you are here
                  </span>
                ) : null}
              </div>
              <p className="mt-1.5 text-[11px] leading-relaxed text-slate-500">{step.body}</p>
            </Link>
          ))}
        </div>
      </Card>

      <div className="mb-6">
        <Explain>
          Prices are <strong>back-adjusted</strong>: Open, High and Low are scaled by the same
          factor as the adjusted close, so splits and dividends can&apos;t distort range-based
          indicators like ATR or Keltner channels. Data is cached locally, so re-running is fast
          and won&apos;t re-download.
        </Explain>
      </div>

      <div className="mb-6">
        <RunPanel
          universe={universe.id}
          layer="layer1"
          label="Generate signals"
          hint="Downloads any missing price history, then writes one signal file per asset × strategy × parameter combination. Leave symbols blank to use the full configured universe."
          showSymbols
        />
      </div>

      <div className="mb-6 grid gap-6 xl:grid-cols-[1fr_1.4fr]">
        <Card>
          <CardHead
            title="Cached price data"
            subtitle={`${count(symbols.length)} symbols · ${count(signals)} signal files`}
          />
          {symbols.length === 0 ? (
            <EmptyState
              title="No data cached yet"
              body="Run the signal generator above to download price history. Symbols are cached locally as CSV, so you only pay the download cost once."
            />
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th>Symbol</Th>
                  <Th align="right">Rows</Th>
                  <Th align="right">Size</Th>
                </tr>
              </thead>
              <tbody>
                {symbols.map((s) => (
                  <tr key={s.symbol}>
                    <Td className="mono font-semibold text-slate-200">{s.symbol}</Td>
                    <Td align="right" className="mono">
                      {count(s.rows)}
                    </Td>
                    <Td align="right" className="mono text-slate-500">
                      {s.sizeKb} KB
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
        </Card>

        <Card>
          <CardHead
            title="Strategy library"
            subtitle={`${strategies.length} strategies from your config, in bare form — no stop losses or targets, so the raw edge is what gets measured.`}
          />
          <div className="grid gap-3 px-5 py-5 sm:grid-cols-2">
            {strategies.map((s) => {
              const copy = STRATEGY_COPY[s.name];
              return (
                <div
                  key={s.name}
                  className="rounded-lg border border-[var(--color-line-soft)] bg-[var(--color-panel2)] px-4 py-3"
                >
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="text-[12.5px] font-semibold text-slate-200">
                      {copy?.label ?? s.name}
                    </span>
                    <span className="text-[9.5px] uppercase tracking-wide text-slate-500">
                      {copy?.family ?? "Strategy"}
                    </span>
                  </div>
                  <p className="mt-1.5 text-[11px] leading-relaxed text-slate-500">
                    {copy?.desc ?? "Configured in strategy_grid.yaml."}
                  </p>
                  <p className="mono mt-2 text-[10px] text-slate-600">
                    {Object.keys(s.params).length} params ·{" "}
                    <span className="text-cyan-400/70">{s.combinations} combinations</span>
                    {symbols.length > 0 ? (
                      <span className="text-slate-600">
                        {" "}× {symbols.length} symbols ={" "}
                        {(s.combinations * symbols.length).toLocaleString()} backtests
                      </span>
                    ) : null}
                  </p>
                </div>
              );
            })}
          </div>
        </Card>
      </div>

      <ConfigEditor
        files={configsForLayer(1)}
        title="Layer 1 settings"
        description="Asset universe, date range, and the parameter grid swept for each strategy. Every value you list is tested as a separate configuration."
      />
    </div>
  );
}
