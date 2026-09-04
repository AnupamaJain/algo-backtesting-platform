/**
 * Broker read/write endpoint.
 *
 * Commands are allowlisted and split by HTTP verb: GET can only read, POST
 * can act. A read endpoint that could place an order would be one stray link
 * away from an accidental trade.
 */

import { NextResponse } from "next/server";
import { brokerCall } from "@/lib/broker";

export const dynamic = "force-dynamic";

const READ_COMMANDS = new Set(["state", "orders", "fills", "events", "quote", "quotes"]);
const WRITE_COMMANDS = new Set(["place", "cancel", "poll", "reset"]);

const SYMBOL = /^[A-Z0-9.\-]{1,12}$/;

export async function GET(request: Request) {
  const url = new URL(request.url);
  const command = url.searchParams.get("command") ?? "state";
  if (!READ_COMMANDS.has(command)) {
    return NextResponse.json({ error: `unknown read command: ${command}` }, { status: 400 });
  }

  const args: Record<string, string | number> = {};
  for (const key of ["page", "page_size", "symbol", "symbols", "status", "strategy"]) {
    const value = url.searchParams.get(key);
    if (value) args[key] = key === "page" || key === "page_size" ? Number(value) : value;
  }

  try {
    return NextResponse.json(await brokerCall(command, args));
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "broker read failed" },
      { status: 502 }
    );
  }
}

export async function POST(request: Request) {
  let body: Record<string, unknown>;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }

  const command = String(body.command ?? "");
  if (!WRITE_COMMANDS.has(command)) {
    return NextResponse.json({ error: `unknown action: ${command}` }, { status: 400 });
  }

  const args: Record<string, string | number | boolean | undefined> = {};

  if (command === "place") {
    const symbol = String(body.symbol ?? "").toUpperCase();
    if (!SYMBOL.test(symbol)) {
      return NextResponse.json({ error: "invalid symbol" }, { status: 400 });
    }
    const side = String(body.side ?? "").toUpperCase();
    if (side !== "BUY" && side !== "SELL") {
      return NextResponse.json({ error: "side must be BUY or SELL" }, { status: 400 });
    }
    const quantity = Number(body.quantity);
    if (!Number.isFinite(quantity) || quantity <= 0 || quantity > 1_000_000) {
      return NextResponse.json({ error: "quantity must be between 0 and 1,000,000" }, { status: 400 });
    }
    const orderType = String(body.order_type ?? "MARKET").toUpperCase();
    if (!["MARKET", "LIMIT", "STOP", "STOP_LIMIT"].includes(orderType)) {
      return NextResponse.json({ error: "invalid order type" }, { status: 400 });
    }
    Object.assign(args, {
      symbol,
      side,
      quantity,
      order_type: orderType,
      limit_price: body.limit_price ? Number(body.limit_price) : undefined,
      stop_price: body.stop_price ? Number(body.stop_price) : undefined,
      strategy: body.strategy ? String(body.strategy).slice(0, 32) : "manual",
      force: Boolean(body.force),
    });
  }

  if (command === "cancel") {
    const orderId = String(body.order_id ?? "");
    if (!/^[A-Za-z0-9\-]{1,40}$/.test(orderId)) {
      return NextResponse.json({ error: "invalid order id" }, { status: 400 });
    }
    args.order_id = orderId;
  }

  try {
    return NextResponse.json(await brokerCall(command, args));
  } catch (error) {
    // A rejected order is the user's problem to fix, not a server fault.
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "broker action failed" },
      { status: 400 }
    );
  }
}
