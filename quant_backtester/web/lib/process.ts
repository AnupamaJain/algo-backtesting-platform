/**
 * Runs a Python CLI in this repository and parses its JSON output.
 *
 * Shared by the broker bridge and the inventory reader. Arguments are passed
 * as argv with `shell: false`, so nothing here can be interpreted as shell
 * syntax.
 */

import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { PROJECT_ROOT } from "./artifacts";

const MAX_MS = 60_000;

export function resolvePython(): string {
  const venv = path.resolve(PROJECT_ROOT, "..", "venv", "bin", "python");
  return fs.existsSync(venv) ? venv : "python3";
}

export async function brokerCallGeneric<T>(script: string, args: string[]): Promise<T> {
  const argv = [path.join(PROJECT_ROOT, script), ...args];

  return new Promise<T>((resolve, reject) => {
    const child = spawn(resolvePython(), argv, { cwd: PROJECT_ROOT, shell: false });
    let stdout = "";
    let stderr = "";

    child.stdout.on("data", (c) => (stdout += c.toString()));
    child.stderr.on("data", (c) => (stderr += c.toString()));

    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error(`${script} timed out`));
    }, MAX_MS);

    child.on("close", () => {
      clearTimeout(timer);
      try {
        const parsed = JSON.parse(stdout.trim());
        if (parsed && typeof parsed === "object" && "error" in parsed) {
          reject(new Error(String((parsed as { error: unknown }).error)));
          return;
        }
        resolve(parsed as T);
      } catch {
        reject(new Error(stderr.trim() || stdout.trim() || `${script} failed`));
      }
    });
    child.on("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });
  });
}
