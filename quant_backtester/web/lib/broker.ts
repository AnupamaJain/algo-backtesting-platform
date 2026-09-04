/**
 * Bridge to the Python broker layer.
 *
 * Every read and write goes through `broker_cli.py`, so Python stays the
 * single source of truth for orders, positions and P&L. Re-implementing
 * position maths in TypeScript would let the console and the trading engine
 * disagree about what is held — the one thing a trading UI must never do.
 */

import path from "node:path";
import { PROJECT_ROOT } from "./artifacts";
import { resolvePython } from "./process";
import { spawn } from "node:child_process";

const CLI = path.join(PROJECT_ROOT, "broker_cli.py");
const MAX_MS = 60_000;

/** Arguments are passed as argv with `shell: false`, so nothing user-supplied
 *  can be interpreted as shell syntax. */
export async function brokerCall<T = unknown>(
  command: string,
  args: Record<string, string | number | boolean | undefined> = {}
): Promise<T> {
  const argv = [CLI, command];
  for (const [key, value] of Object.entries(args)) {
    if (value === undefined || value === null || value === "") continue;
    const flag = `--${key.replace(/_/g, "-")}`;
    if (typeof value === "boolean") {
      if (value) argv.push(flag);
    } else {
      argv.push(flag, String(value));
    }
  }

  return new Promise<T>((resolve, reject) => {
    const child = spawn(resolvePython(), argv, { cwd: PROJECT_ROOT, shell: false });
    let stdout = "";
    let stderr = "";

    child.stdout.on("data", (c) => (stdout += c.toString()));
    child.stderr.on("data", (c) => (stderr += c.toString()));

    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error(`broker ${command} timed out`));
    }, MAX_MS);

    child.on("close", () => {
      clearTimeout(timer);
      try {
        const parsed = JSON.parse(stdout.trim());
        // The CLI reports failures as JSON with an `error` key rather than a
        // traceback, so surface that message instead of a parse failure.
        if (parsed && typeof parsed === "object" && "error" in parsed) {
          reject(new Error(String((parsed as { error: unknown }).error)));
          return;
        }
        resolve(parsed as T);
      } catch {
        reject(new Error(stderr.trim() || stdout.trim() || `broker ${command} failed`));
      }
    });
    child.on("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });
  });
}

// -- shapes returned by the CLI ------------------------------------------

export type Pagination = {
  page: number;
  page_size: number;
  total: number;
  pages: number;
  has_prev: boolean;
  has_next: boolean;
};

export type Quote = {
  symbol: string;
  last_price: number;
  bid: number | null;
  ask: number | null;
  previous_close: number | null;
  change_pct: number | null;
  timestamp: string;
  age_seconds: number;
  stale: boolean;
};

export type Position = {
  symbol: string;
  quantity: number;
  average_price: number;
  last_price: number | null;
  direction: "LONG" | "SHORT" | "FLAT";
  market_value: number;
  unrealized_pnl: number;
  realized_pnl: number;
  total_pnl: number;
  strategy: string;
  quote: Quote | null;
};

export type Order = {
  order_id: string;
  broker_order_id: string | null;
  broker: string;
  strategy: string;
  symbol: string;
  side: "BUY" | "SELL";
  quantity: number;
  order_type: string;
  product: string;
  limit_price: number | null;
  stop_price: number | null;
  status: string;
  filled_quantity: number;
  average_price: number | null;
  status_message: string;
  dry_run: boolean;
  created_at: string | null;
  updated_at: string | null;
};

export type BrokerState = {
  broker: {
    name: string;
    simulated: boolean;
    connected: boolean;
    capabilities: Record<string, boolean>;
    auth: { broker: string; kind: string; session: Record<string, unknown> | null };
  };
  safety: {
    live_trading: boolean;
    mode: "PAPER" | "DRY-RUN" | "LIVE";
    max_order_value: number;
    duplicate_window_seconds: number;
    simulated_broker: boolean;
  };
  account: {
    cash: number;
    equity: number;
    realized_pnl: number;
    unrealized_pnl: number;
    gross_exposure: number;
    net_exposure: number;
    leverage: number;
    timestamp: string | null;
  };
  positions: Position[];
  universe: string[];
  just_filled: Order[];
};

export type BrokerEvent = {
  kind: string;
  severity: string;
  message: string;
  detail: Record<string, unknown>;
  timestamp: string;
};

export type Page<T> = { rows: T[]; pagination: Pagination };
export type OrdersPage = Page<Order> & {
  filters: { symbols: string[]; statuses: string[]; strategies: string[] };
};

// -- typed helpers --------------------------------------------------------

/** Which broker a console read targets.
 *
 *  Omitted means "whatever broker.yaml has as active". Passing one lets the
 *  console show any configured book — paper alongside live — without
 *  editing config, which is the whole point of the abstraction layer.
 */
export type BrokerScope = { broker?: string };

export const getBrokerState = (scope: BrokerScope = {}) =>
  brokerCall<BrokerState>("state", scope);

export const getOrders = (
  params: {
    page?: number;
    page_size?: number;
    symbol?: string;
    status?: string;
    strategy?: string;
  } & BrokerScope
) => brokerCall<OrdersPage>("orders", params);

export const getFills = (
  params: { page?: number; page_size?: number } & BrokerScope
) => brokerCall<Page<Record<string, unknown>>>("fills", params);

export const getEvents = (
  params: { page?: number; page_size?: number } & BrokerScope
) => brokerCall<Page<BrokerEvent>>("events", params);

export const getQuotes = (symbols?: string, scope: BrokerScope = {}) =>
  brokerCall<{ quotes: Record<string, Quote> }>("quotes", { symbols, ...scope });

export type BrokerSummary = {
  name: string;
  adapter: string;
  simulated: boolean;
  exchange: string;
  universe_size: number;
  active: boolean;
};

/** The brokers configured in broker.yaml, for the console's switcher.
 *
 *  Returns an empty list rather than throwing: a console that cannot reach
 *  the broker layer should still render, saying so.
 */
export async function listBrokers(): Promise<{
  brokers: BrokerSummary[];
  active: string;
}> {
  try {
    const result = await brokerCall<{
      brokers: string[];
      active: string;
      detail: BrokerSummary[];
    }>("brokers");
    return { brokers: result.detail ?? [], active: result.active ?? "" };
  } catch {
    return { brokers: [], active: "" };
  }
}

/** Returns null instead of throwing, so a page can render an offline state
 *  rather than a 500 when the broker layer is unreachable. */
export async function safeBrokerState(
  scope: BrokerScope = {}
): Promise<BrokerState | null> {
  try {
    return await getBrokerState(scope);
  } catch {
    return null;
  }
}
