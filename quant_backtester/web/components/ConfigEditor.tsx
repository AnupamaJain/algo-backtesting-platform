"use client";

/**
 * Schema-driven settings editor.
 *
 * Renders whatever `config-schema` declares, so adding a new tunable parameter
 * to the pipeline needs no UI change. Edits are staged locally and only
 * written when the user saves, so a half-typed number never reaches disk.
 */

import { useEffect, useMemo, useState } from "react";
import type { ConfigFile, Field } from "@/lib/config-schema";
import { Card, CardHead, Pill } from "./ui";

type Values = Record<string, Record<string, unknown>>;
type Draft = Record<string, unknown>; // key: `${file}::${JSON.stringify(path)}`

const key = (file: string, path: (string | number)[]) => `${file}::${JSON.stringify(path)}`;

function toInput(field: Field, value: unknown): string {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) return value.join(", ");
  if (field.asPercent && typeof value === "number") {
    // Show 0.0005 as 0.05 so the user types percentages, not fractions.
    return String(Number((value * 100).toFixed(6)));
  }
  return String(value);
}

function fromInput(field: Field, raw: string): unknown {
  switch (field.type) {
    case "int":
    case "float": {
      const n = Number(raw);
      if (!Number.isFinite(n)) return raw;
      return field.asPercent ? Number((n / 100).toFixed(10)) : n;
    }
    case "intList":
    case "floatList":
      return raw
        .split(",")
        .map((s) => Number(s.trim()))
        .filter((n) => Number.isFinite(n));
    case "stringList":
      return raw
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
    default:
      return raw;
  }
}

export default function ConfigEditor({
  files,
  title,
  description,
}: {
  files: ConfigFile[];
  title: string;
  description: string;
}) {
  const [values, setValues] = useState<Values | null>(null);
  const [draft, setDraft] = useState<Draft>({});
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ tone: "green" | "red"; text: string } | null>(null);

  useEffect(() => {
    fetch("/api/config")
      .then((r) => r.json())
      .then((data) => setValues(data.values))
      .catch(() => setMessage({ tone: "red", text: "Could not load configuration." }));
  }, []);

  const dirtyCount = Object.keys(draft).length;

  const currentValue = useMemo(
    () => (file: string, field: Field) => {
      const k = key(file, field.path);
      if (k in draft) return draft[k];
      return values?.[file]?.[JSON.stringify(field.path)] ?? null;
    },
    [draft, values]
  );

  function update(file: string, field: Field, raw: string | boolean) {
    const value = typeof raw === "boolean" ? raw : fromInput(field, raw);
    setDraft((prev) => ({ ...prev, [key(file, field.path)]: value }));
    setMessage(null);
  }

  async function save() {
    setSaving(true);
    setMessage(null);
    const edits = Object.entries(draft).map(([k, value]) => {
      const [file, pathJson] = k.split("::");
      return { file, path: JSON.parse(pathJson), value };
    });

    try {
      const response = await fetch("/api/config", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ edits }),
      });
      const data = await response.json();
      if (!response.ok) {
        setMessage({ tone: "red", text: data.error ?? "Save failed." });
      } else {
        setValues(data.values);
        setDraft({});
        setMessage({
          tone: "green",
          text: `Saved. Re-run the layer to apply the new settings.`,
        });
      }
    } catch {
      setMessage({ tone: "red", text: "Network error while saving." });
    } finally {
      setSaving(false);
    }
  }

  if (!values) {
    return (
      <Card>
        <CardHead title={title} subtitle={description} />
        <div className="px-5 py-10 text-center text-xs text-slate-500">Loading settings…</div>
      </Card>
    );
  }

  return (
    <Card>
      <CardHead
        title={title}
        subtitle={description}
        action={
          <div className="flex items-center gap-2.5">
            {message ? <Pill tone={message.tone}>{message.text}</Pill> : null}
            {dirtyCount > 0 ? <Pill tone="amber">{dirtyCount} unsaved</Pill> : null}
            <button
              onClick={save}
              disabled={dirtyCount === 0 || saving}
              className="rounded-lg bg-cyan-500 px-3.5 py-2 text-xs font-semibold text-slate-950 transition hover:bg-cyan-400 disabled:cursor-not-allowed disabled:bg-slate-700 disabled:text-slate-500"
            >
              {saving ? "Saving…" : "Save changes"}
            </button>
          </div>
        }
      />

      <div className="divide-y divide-[var(--color-line-soft)]">
        {files.flatMap((file) =>
          file.groups.map((group) => (
            <div key={`${file.file}-${group.title}`} className="px-5 py-5">
              <h3 className="text-[13px] font-semibold text-slate-200">{group.title}</h3>
              <p className="mt-1 max-w-3xl text-[11.5px] leading-relaxed text-slate-500">
                {group.description}
              </p>

              <div className="mt-4 grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
                {group.fields.map((field) => {
                  const value = currentValue(file.file, field);
                  const isDirty = key(file.file, field.path) in draft;
                  const id = `${file.file}-${field.path.join("-")}`;

                  return (
                    <div key={id} className="flex flex-col gap-1.5">
                      <label
                        htmlFor={id}
                        className="flex items-center gap-2 text-[11.5px] font-medium text-slate-300"
                      >
                        {field.label}
                        {isDirty ? (
                          <span className="h-1.5 w-1.5 rounded-full bg-amber-400" aria-label="modified" />
                        ) : null}
                      </label>

                      {field.type === "bool" ? (
                        <button
                          id={id}
                          type="button"
                          role="switch"
                          aria-checked={Boolean(value)}
                          onClick={() => update(file.file, field, !value)}
                          className={`flex h-8 w-14 items-center rounded-full px-1 transition ${
                            value ? "bg-emerald-500/80" : "bg-slate-700"
                          }`}
                        >
                          <span
                            className={`h-6 w-6 rounded-full bg-white transition ${
                              value ? "translate-x-6" : ""
                            }`}
                          />
                        </button>
                      ) : field.type === "enum" ? (
                        <select
                          id={id}
                          value={String(value ?? "")}
                          onChange={(e) => update(file.file, field, e.target.value)}
                          className="mono rounded-lg border border-[var(--color-line)] bg-[var(--color-panel2)] px-3 py-2 text-[12px] text-slate-200"
                        >
                          {field.options?.map((option) => (
                            <option key={option} value={option}>
                              {option}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <div className="relative">
                          <input
                            id={id}
                            type={field.type === "int" || field.type === "float" ? "number" : "text"}
                            inputMode={
                              field.type === "int"
                                ? "numeric"
                                : field.type === "float"
                                  ? "decimal"
                                  : "text"
                            }
                            step={field.asPercent ? "any" : (field.step ?? (field.type === "int" ? 1 : "any"))}
                            value={toInput(field, value)}
                            onChange={(e) => update(file.file, field, e.target.value)}
                            className="mono w-full rounded-lg border border-[var(--color-line)] bg-[var(--color-panel2)] px-3 py-2 text-[12px] text-slate-200 placeholder:text-slate-600"
                            placeholder={
                              field.type.endsWith("List") ? "comma, separated, values" : ""
                            }
                          />
                          {field.asPercent ? (
                            <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-[11px] text-slate-500">
                              %
                            </span>
                          ) : null}
                        </div>
                      )}

                      <p className="text-[10.5px] leading-relaxed text-slate-500">{field.help}</p>
                    </div>
                  );
                })}
              </div>
            </div>
          ))
        )}
      </div>
    </Card>
  );
}
