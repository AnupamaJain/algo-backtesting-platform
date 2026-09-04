/**
 * The market universes this platform can analyse.
 *
 * Two markets, two data providers, two disjoint instrument sets: yfinance
 * cannot price RELIANCE-EQ and Flattrade cannot price SPY. Their results are
 * therefore kept in separate trees, and the UI has to know which one it is
 * looking at — a page that silently mixes them would show NSE strategies
 * priced against US data, which is exactly the bug that made this necessary.
 */

export type Universe = {
  id: string;
  label: string;
  market: string;
  provider: string;
  /** Results directory, relative to the quant_backtester root. */
  resultsDir: string;
  /** Price cache directory, relative to the quant_backtester root. */
  dataDir: string;
  configFile: string;
};

export const UNIVERSES: Universe[] = [
  {
    id: "global",
    label: "Global",
    market: "US equities, ETFs & crypto",
    provider: "yfinance",
    resultsDir: "results",
    dataDir: "data",
    configFile: "universe.yaml",
  },
  {
    id: "global5y",
    label: "Global · 5y",
    market: "Same US universe, last 5 years",
    provider: "yfinance",
    resultsDir: "results_5y",
    dataDir: "data",
    configFile: "universe_5y.yaml",
  },
  {
    id: "india",
    label: "India",
    market: "NSE equities & ETFs",
    provider: "Flattrade",
    resultsDir: "results_india",
    dataDir: "data_india",
    configFile: "universe_india.yaml",
  },
];

export const DEFAULT_UNIVERSE = UNIVERSES[0];

export function resolveUniverse(id: string | string[] | undefined): Universe {
  const key = Array.isArray(id) ? id[0] : id;
  return UNIVERSES.find((u) => u.id === key) ?? DEFAULT_UNIVERSE;
}

/** Read the active universe from a server component's searchParams. */
export function universeFrom(
  searchParams: Record<string, string | string[] | undefined>
): Universe {
  return resolveUniverse(searchParams.universe);
}
