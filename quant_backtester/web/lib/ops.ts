/**
 * Bridge to the repository inventory.
 *
 * The console reports what is actually installed by asking Python, rather
 * than carrying its own list. A hardcoded strategy list drifts the moment a
 * module is renamed, and a UI that lies about what is installed is worse than
 * no list at all.
 */

import { brokerCallGeneric } from "./process";

export type ModuleState = {
  available: boolean;
  rows: number;
  last_activity: string | null;
} | null;

export type OpsModule = {
  key: string;
  name: string;
  kind: "production" | "research";
  category: "strategy" | "watchdog" | "analytics";
  family: string;
  market: "IN" | "US";
  summary: string;
  detail: string;
  modules: string[];
  entry: string;
  parameters: string[];
  requires: string[];
  dashboard: string;
  prd_ref: string;
  installed: boolean;
  missing_modules: string[];
  lines: number;
  running: boolean;
  state: ModuleState;
  has_activity: boolean;
  /** False when no state source is declared — the module may well have run,
   *  we simply have nowhere to look. Distinct from "ran and did nothing". */
  activity_checked: boolean;
  /** Where activity was looked for, so a status claim can be audited. */
  activity_evidence?: string;
  /** Walk-forward history for library strategies, which have no state file
   *  of their own — absent for production modules. */
  backtests?: {
    configurations: number;
    symbols: number;
    best_oos_sharpe: number | null;
    survivors: number;
  } | null;
};

export type InventorySummary = {
  total: number;
  installed: number;
  running: number;
  with_activity: number;
};

export type Inventory = {
  generated_at: string;
  repo_root: string;
  modules: OpsModule[];
  summary: {
    production_strategies: InventorySummary;
    watchdogs: InventorySummary;
    analytics: InventorySummary;
    research_strategies: InventorySummary;
  };
};

export async function getInventory(): Promise<Inventory | null> {
  try {
    return await brokerCallGeneric<Inventory>("ops_cli.py", ["inventory"]);
  } catch {
    return null;
  }
}

export const FAMILY_LABELS: Record<string, string> = {
  mean_reversion: "Mean reversion",
  trend: "Trend following",
  breakout: "Breakouts",
  volatility: "Volatility",
  pattern: "Chart patterns",
  unclassified: "Unclassified",
  premium_selling: "Premium selling",
  exit_management: "Exit management",
  income: "Income",
  risk_overlay: "Risk overlay",
  order_safety: "Order safety",
  exposure: "Exposure",
  reporting: "Reporting",
  observability: "Observability",
  market_structure: "Market structure",
  reference_data: "Reference data",
  alerting: "Alerting",
};
