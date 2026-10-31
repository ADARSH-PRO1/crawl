// Export helpers. Plain functions, no browser APIs, so they can be tested on their own.

const cell = (value) => (value == null ? "" : String(value));

/** CSV with every cell quoted only when needed. A BOM is added by the download code, not here. */
export function toCsv(columns, rows) {
  const quote = (v) => {
    const s = cell(v);
    return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = [columns.map(quote).join(",")];
  for (const row of rows) lines.push(columns.map((c) => quote(row[c])).join(","));
  return lines.join("\r\n") + "\r\n";
}

/** Tab separated text: pastes straight into Excel or Google Sheets. */
export function toTsv(columns, rows) {
  const clean = (v) => cell(v).replace(/[\t\r\n]+/g, " ");
  return [columns.map(clean).join("\t"), ...rows.map((r) => columns.map((c) => clean(r[c])).join("\t"))].join("\n");
}

/** Rows as objects with only the chosen columns, in order. */
export function toObjects(columns, rows) {
  return rows.map((r) => Object.fromEntries(columns.map((c) => [c, cell(r[c])])));
}

/** A file name that is safe on every system: scrape-<host>-<date>.<ext> */
export function fileName(pageUrl, ext, now = new Date()) {
  let host = "page";
  try { host = new URL(pageUrl).hostname.replace(/^www\./, "") || host; } catch (e) { /* keep default */ }
  const day = now.toISOString().slice(0, 10);
  return `scrape-${host.replace(/[^a-z0-9.-]/gi, "_")}-${day}.${ext}`;
}

/** Column names must be unique and non-empty, or two columns would overwrite each other in the export. */
export function uniqueNames(names) {
  const seen = new Map();
  return names.map((raw, i) => {
    const base = cell(raw).trim() || `column ${i + 1}`;
    const n = (seen.get(base) || 0) + 1;
    seen.set(base, n);
    return n === 1 ? base : `${base} ${n}`;
  });
}
