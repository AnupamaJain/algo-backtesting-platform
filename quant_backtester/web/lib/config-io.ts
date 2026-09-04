/**
 * Reading and writing the pipeline's YAML configuration.
 *
 * Two properties matter here:
 *
 *  1. **Comments survive.** The config files carry a lot of hard-won
 *     explanation (why `spherical` beats `full`, why dry-run is the default).
 *     Round-tripping through a plain parse/stringify would silently delete all
 *     of it, so edits go through `yaml`'s Document API, which preserves
 *     comments and formatting and touches only the node being changed.
 *
 *  2. **The client is never trusted.** Every incoming edit is re-validated
 *     against `config-schema` on the server: unknown paths are refused,
 *     numbers are bounds-checked, enums must match. The UI's own validation is
 *     a convenience, not the control.
 */

import fs from "node:fs/promises";
import path from "node:path";
import { parseDocument, type Document } from "yaml";
import { CONFIG_DIR } from "./artifacts";
import { CONFIG_FILES, findField, getConfigFile, type Field } from "./config-schema";

export type ConfigValues = Record<string, unknown>;

/** A single validated edit. */
export type Edit = { file: string; path: (string | number)[]; value: unknown };

function configPath(file: string): string {
  // Defence in depth: the filename must be one we know, so nothing derived
  // from a request can escape the config directory.
  if (!getConfigFile(file)) throw new Error(`unknown config file: ${file}`);
  return path.join(CONFIG_DIR, path.basename(file));
}

async function loadDocument(file: string): Promise<Document> {
  const text = await fs.readFile(configPath(file), "utf-8");
  return parseDocument(text);
}

/** Read every schema-declared value for one config file. */
export async function readConfigValues(file: string): Promise<ConfigValues> {
  const config = getConfigFile(file);
  if (!config) return {};

  const doc = await loadDocument(file);
  const values: ConfigValues = {};
  for (const group of config.groups) {
    for (const field of group.fields) {
      const raw = doc.getIn(field.path, false);
      values[JSON.stringify(field.path)] = raw === undefined ? null : raw;
    }
  }
  return values;
}

export async function readAllConfigValues(): Promise<Record<string, ConfigValues>> {
  const entries = await Promise.all(
    CONFIG_FILES.map(async (c) => [c.file, await readConfigValues(c.file)] as const)
  );
  return Object.fromEntries(entries);
}

/** Validate one edit against its schema field, returning the coerced value. */
export function validateEdit(field: Field, value: unknown): unknown {
  switch (field.type) {
    case "int": {
      const n = Number(value);
      if (!Number.isInteger(n)) throw new Error(`${field.label} must be a whole number`);
      assertBounds(field, n);
      return n;
    }
    case "float": {
      const n = Number(value);
      if (!Number.isFinite(n)) throw new Error(`${field.label} must be a number`);
      assertBounds(field, n);
      return n;
    }
    case "bool":
      return Boolean(value);
    case "enum": {
      const s = String(value);
      if (!field.options?.includes(s)) {
        throw new Error(`${field.label} must be one of: ${field.options?.join(", ")}`);
      }
      return s;
    }
    case "text": {
      const s = value === null || value === undefined ? "" : String(value).trim();
      if (s.length > 64) throw new Error(`${field.label} is too long`);
      // An empty text field means "unset" — YAML null, not the string "".
      return s === "" ? null : s;
    }
    case "intList": {
      const list = asArray(value).map((v) => Number(v));
      if (list.length === 0) throw new Error(`${field.label} needs at least one value`);
      if (list.length > 40) throw new Error(`${field.label} has too many values`);
      if (!list.every((n) => Number.isInteger(n))) {
        throw new Error(`${field.label} must contain whole numbers`);
      }
      return list;
    }
    case "floatList": {
      const list = asArray(value).map((v) => Number(v));
      if (list.length === 0) throw new Error(`${field.label} needs at least one value`);
      if (list.length > 40) throw new Error(`${field.label} has too many values`);
      if (!list.every((n) => Number.isFinite(n))) {
        throw new Error(`${field.label} must contain numbers`);
      }
      return list;
    }
    case "stringList": {
      const list = asArray(value)
        .map((v) => String(v).trim())
        .filter(Boolean);
      if (list.length > 100) throw new Error(`${field.label} has too many entries`);
      if (!list.every((s) => /^[A-Za-z0-9.\-_]{1,32}$/.test(s))) {
        throw new Error(`${field.label} contains an invalid entry`);
      }
      return list;
    }
    default:
      throw new Error("unsupported field type");
  }
}

function assertBounds(field: Field, n: number): void {
  if (field.min !== undefined && n < field.min) {
    throw new Error(`${field.label} must be at least ${field.min}`);
  }
  if (field.max !== undefined && n > field.max) {
    throw new Error(`${field.label} must be at most ${field.max}`);
  }
}

/**
 * Drop the same-line comment on a node whose value just changed.
 *
 * Trailing comments frequently describe the specific value they sit beside
 * ("0.35  # 35% peak-to-trough"). Editing the value while keeping that comment
 * leaves the file asserting something false, which is worse than having no
 * comment at all. Block comments ABOVE a key explain the setting rather than
 * its value, so those are deliberately left intact.
 */
function clearTrailingComment(doc: Document, path: (string | number)[]): void {
  const node = doc.getIn(path, true) as { comment?: string | null } | undefined;
  if (node && typeof node === "object" && "comment" in node) {
    node.comment = null;
  }
}

function asArray(value: unknown): unknown[] {
  if (Array.isArray(value)) return value;
  if (typeof value === "string") {
    return value
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  }
  return value === null || value === undefined ? [] : [value];
}

/**
 * Apply a batch of edits. Every edit is validated BEFORE anything is written,
 * so a bad value in the batch cannot leave the config half-updated.
 */
export async function applyEdits(edits: Edit[]): Promise<{ written: string[] }> {
  if (edits.length === 0) return { written: [] };
  if (edits.length > 300) throw new Error("too many edits in one request");

  // Phase 1 — validate everything.
  const validated = edits.map((edit) => {
    const field = findField(edit.file, edit.path);
    if (!field) {
      throw new Error(`${edit.file}: field ${JSON.stringify(edit.path)} is not editable`);
    }
    return { ...edit, value: validateEdit(field, edit.value) };
  });

  // Phase 2 — group by file and write each document once.
  const byFile = new Map<string, typeof validated>();
  for (const edit of validated) {
    const bucket = byFile.get(edit.file) ?? [];
    bucket.push(edit);
    byFile.set(edit.file, bucket);
  }

  const written: string[] = [];
  for (const [file, fileEdits] of byFile) {
    const doc = await loadDocument(file);
    for (const edit of fileEdits) {
      doc.setIn(edit.path, edit.value);
      clearTrailingComment(doc, edit.path);
    }
    await fs.writeFile(configPath(file), doc.toString(), "utf-8");
    written.push(file);
  }
  return { written };
}

/**
 * The strategies actually configured in `strategy_grid.yaml`, with the size of
 * each one's parameter sweep.
 *
 * The UI renders from this rather than a hardcoded list, so adding or removing
 * a strategy in config is reflected immediately and the page can never claim a
 * strategy exists when it doesn't.
 */
export async function readConfiguredStrategies(): Promise<
  { name: string; combinations: number; params: Record<string, unknown[]> }[]
> {
  const doc = await loadDocument("strategy_grid.yaml");
  const strategies = doc.toJS()?.strategies ?? {};
  return Object.entries(strategies).map(([name, grid]) => {
    const params = (grid ?? {}) as Record<string, unknown[]>;
    const combinations = Object.values(params).reduce(
      (total, values) => total * (Array.isArray(values) ? values.length : 1),
      1
    );
    return { name, combinations, params };
  });
}
