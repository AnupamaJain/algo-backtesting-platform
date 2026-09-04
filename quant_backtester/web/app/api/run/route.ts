/**
 * Triggers a pipeline layer by shelling out to `main.py`.
 *
 * Security note: the layer argument is validated against a fixed allowlist and
 * symbols against a strict pattern before reaching the process boundary, and
 * arguments are passed as an argv array (never through a shell). A user-
 * supplied string must never be able to become a shell command.
 */

import { spawn } from "node:child_process";
import path from "node:path";
import fs from "node:fs";
import { NextResponse } from "next/server";
import { PROJECT_ROOT } from "@/lib/artifacts";
import { DEFAULT_UNIVERSE, UNIVERSES } from "@/lib/universes";

export const dynamic = "force-dynamic";

const ALLOWED_LAYERS = ["layer1", "layer2", "layer3", "layer4", "pead", "all"] as const;
type Layer = (typeof ALLOWED_LAYERS)[number];

const SYMBOL_PATTERN = /^[A-Z0-9.\-]{1,12}$/;
const MAX_RUN_MS = 15 * 60 * 1000;

function resolvePython(): string {
  // Prefer the project's virtualenv so the run uses the pinned dependency set.
  const venv = path.resolve(PROJECT_ROOT, "..", "venv", "bin", "python");
  return fs.existsSync(venv) ? venv : "python3";
}

export async function POST(request: Request) {
  let body: {
    layer?: string;
    symbols?: string;
    topN?: number;
    generateSignals?: boolean;
    universe?: string;
  };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }

  const layer = body.layer as Layer;
  if (!ALLOWED_LAYERS.includes(layer)) {
    return NextResponse.json(
      { error: `layer must be one of: ${ALLOWED_LAYERS.join(", ")}` },
      { status: 400 }
    );
  }

  const args = [path.join(PROJECT_ROOT, "main.py"), layer];

  // Each market has its own universe config; without this the run would
  // always target the default one regardless of what is on screen.
  const universe = UNIVERSES.find((u) => u.id === body.universe);
  if (body.universe && !universe) {
    return NextResponse.json({ error: "unknown universe" }, { status: 400 });
  }
  if (universe && universe.id !== DEFAULT_UNIVERSE.id) {
    args.push("--universe-config", path.join(PROJECT_ROOT, "config", universe.configFile));
  }

  if (body.symbols) {
    const symbols = body.symbols
      .split(",")
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean);
    if (symbols.length === 0 || !symbols.every((s) => SYMBOL_PATTERN.test(s))) {
      return NextResponse.json({ error: "invalid symbol list" }, { status: 400 });
    }
    args.push("--symbols", symbols.join(","));
  }

  if (body.topN !== undefined) {
    const topN = Number(body.topN);
    if (!Number.isInteger(topN) || topN < 1 || topN > 500) {
      return NextResponse.json({ error: "topN must be 1-500" }, { status: 400 });
    }
    args.push("--top-n", String(topN));
  }

  if (body.generateSignals) args.push("--generate-signals");

  const python = resolvePython();
  const started = Date.now();

  const result = await new Promise<{ code: number; stdout: string; stderr: string }>(
    (resolve) => {
      const child = spawn(python, args, {
        cwd: PROJECT_ROOT,
        // No shell: argv is passed directly to execve, so nothing in the
        // arguments can be interpreted as shell syntax.
        shell: false,
      });

      let stdout = "";
      let stderr = "";
      child.stdout.on("data", (chunk) => {
        stdout += chunk.toString();
      });
      child.stderr.on("data", (chunk) => {
        stderr += chunk.toString();
      });

      const timer = setTimeout(() => {
        child.kill("SIGKILL");
        stderr += `\n[timed out after ${MAX_RUN_MS / 1000}s]`;
      }, MAX_RUN_MS);

      child.on("close", (code) => {
        clearTimeout(timer);
        resolve({ code: code ?? -1, stdout, stderr });
      });
      child.on("error", (err) => {
        clearTimeout(timer);
        resolve({ code: -1, stdout, stderr: `${stderr}\n${err.message}` });
      });
    }
  );

  // The pipeline logs progress to stderr, so a non-zero exit is the only
  // reliable failure signal — stderr content alone is not an error.
  return NextResponse.json({
    ok: result.code === 0,
    exitCode: result.code,
    durationMs: Date.now() - started,
    stdout: result.stdout.slice(-20000),
    stderr: result.stderr.slice(-20000),
  });
}
