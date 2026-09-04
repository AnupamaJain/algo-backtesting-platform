/**
 * Minimal quote-aware CSV parser.
 *
 * pandas quotes any field containing a comma, and several artifact columns do
 * — `params` holds dicts like "{'rsi_period': 14, 'oversold_threshold': 30}".
 * Splitting on commas naively would shred those rows, so quotes are honoured
 * here rather than pulling in a parsing dependency for ~40 lines of work.
 */

export type Row = Record<string, string>;

function splitLine(line: string): string[] {
  const fields: string[] = [];
  let current = "";
  let inQuotes = false;

  for (let i = 0; i < line.length; i += 1) {
    const char = line[i];
    if (char === '"') {
      // A doubled quote inside a quoted field is a literal quote.
      if (inQuotes && line[i + 1] === '"') {
        current += '"';
        i += 1;
      } else {
        inQuotes = !inQuotes;
      }
    } else if (char === "," && !inQuotes) {
      fields.push(current);
      current = "";
    } else {
      current += char;
    }
  }
  fields.push(current);
  return fields;
}

export function parseCsv(text: string): Row[] {
  const lines = text.split(/\r?\n/).filter((line) => line.trim().length > 0);
  // An empty artifact (nothing survived) is a normal state, not an error.
  if (lines.length === 0) return [];

  const headers = splitLine(lines[0]).map((h) => h.trim());
  return lines.slice(1).map((line) => {
    const values = splitLine(line);
    const row: Row = {};
    headers.forEach((header, i) => {
      row[header] = (values[i] ?? "").trim();
    });
    return row;
  });
}

/** Parse a numeric cell, returning null for blanks, NaN and non-numbers. */
export function num(value: string | undefined): number | null {
  if (value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** Parse a Python-style boolean cell ("True"/"False"). */
export function bool(value: string | undefined): boolean {
  return value === "True" || value === "true";
}
