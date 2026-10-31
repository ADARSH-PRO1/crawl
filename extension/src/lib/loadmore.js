// Load more items: scroll, "load more" button, or next-page links. Port of load_more() in app/scrape.py.
// Pure logic: reading the page and loading the next page are passed in, so it runs the same in the window and in tests.

export const LIMITS = { maxRows: 2000, maxPages: 20, delayMs: 1000, totalMs: 420000 };

const hostOf = (u) => {
  try { return new URL(u).hostname.toLowerCase().replace(/^www\./, ""); } catch (e) { return ""; }
};

/** Same site means same host, ignoring a leading "www.". */
export const sameSite = (a, b) => hostOf(a) !== "" && hostOf(a) === hostOf(b);

export const isWebUrl = (u) => {
  try { return /^https?:$/.test(new URL(u).protocol); } catch (e) { return false; }
};

const clean = (u) => u.split("#")[0].replace(/\/$/, "");

/** Permission patterns for the page's own site (with and without www), for the optional host access request. */
export function originPatterns(pageUrl) {
  const host = hostOf(pageUrl);
  if (!host) return [];
  const named = host.includes(".") && !/^[\d.]+$/.test(host) && !host.includes(":"); // "www." on an IP address is invalid
  return named ? [`*://${host}/*`, `*://www.${host}/*`] : [`*://${host}/*`];
}

/** Two rows are the same when every non-empty value matches, so a column that appears later does not break it. */
export const rowKey = (row) => JSON.stringify(Object.keys(row).filter((k) => row[k] !== "" && row[k] != null).sort().map((k) => [k, row[k]]));

/**
 * Adds the rows of `incoming` (a detected list) to `table` ({columns, rows}) and returns how many were new.
 * Columns are matched by name, because ids are numbered per page; unknown columns are added.
 */
export function mergeTable(table, incoming, seen) {
  const byName = new Map();
  const taken = {};
  table.columns.forEach((c) => { taken[c.name] = (taken[c.name] || 0) + 1; byName.set(`${c.name}#${taken[c.name]}`, c.id); });
  const idMap = {};
  const count = {};
  incoming.columns.forEach((c) => {
    count[c.name] = (count[c.name] || 0) + 1;
    const key = `${c.name}#${count[c.name]}`;
    let id = byName.get(key);
    if (!id) {
      id = `m${table.columns.length}`;
      table.columns.push({ ...c, id });
      byName.set(key, id);
    }
    idMap[c.id] = id;
  });
  const fresh = [];
  for (const row of incoming.rows) {
    const mapped = Object.fromEntries(incoming.columns.map((c) => [idMap[c.id], row[c.id] ?? ""]));
    if (!seen.has(rowKey(mapped))) fresh.push(mapped);
  }
  fresh.forEach((r) => seen.add(rowKey(r)));
  table.rows.push(...fresh);
  return fresh.length;
}

/**
 * Keeps loading until nothing new appears. `table` is changed in place.
 *   read()      -> the page's detector result (lists, acted, nextHref), run with the chosen action
 *   navigate(u) -> loads u in the tab, makes the scripts available again, returns the address it ended on
 * Returns { pages, reason, skipped, error }; reason is one of
 *   no-more | no-new-rows | row-cap | page-cap | time | stopped | other-site | error
 */
export async function runLoadMore({ table, pageUrl, read, navigate, maxPages = 5, maxRows = LIMITS.maxRows,
  delayMs = LIMITS.delayMs, totalMs = LIMITS.totalMs, signal, onProgress, sleep, now = Date.now }) {
  const wait = sleep || ((ms) => new Promise((r) => setTimeout(r, ms)));
  const pagesCap = Math.max(1, Math.min(maxPages, LIMITS.maxPages));
  const rowsCap = Math.min(maxRows, LIMITS.maxRows);
  const started = now();
  const seen = new Set(table.rows.map(rowKey));
  const visited = new Set([clean(pageUrl)]);
  const out = { pages: 1, reason: "page-cap", skipped: [], error: "" };

  const finish = (reason, error = "") => {
    out.reason = reason;
    out.error = error;
    if (table.rows.length > rowsCap) table.rows.length = rowsCap;
    return out;
  };

  let data;
  try { data = await read(); } catch (e) { return finish("error", String((e && e.message) || e)); }
  if (!data || !data.ok) return finish("error", (data && data.error) || "The page could not be read.");

  for (;;) {
    if (signal && signal.aborted) return finish("stopped");
    if (out.pages >= pagesCap) return finish("page-cap");
    if (table.rows.length >= rowsCap) return finish("row-cap");
    if (now() - started > totalMs) return finish("time");
    const next = data.nextHref;
    if (!next && !data.acted) return finish("no-more");

    await wait(delayMs); // low rate, like the crawler
    if (signal && signal.aborted) return finish("stopped");

    if (next) {
      if (!isWebUrl(next) || !sameSite(next, pageUrl)) { // the cheap check first: never leave the site the user is on
        out.skipped.push({ url: next, reason: "the next page is on another site" });
        return finish("other-site");
      }
      if (visited.has(clean(next))) return finish("no-new-rows");
      visited.add(clean(next));
      let landed;
      try { landed = await navigate(next); } catch (e) { return finish("error", String((e && e.message) || e)); }
      if (landed) visited.add(clean(landed));
      if (landed && !sameSite(landed, pageUrl)) { // a redirect took the tab elsewhere
        out.skipped.push({ url: landed, reason: "the page redirected to another site" });
        return finish("other-site");
      }
    }
    try { data = await read(); } catch (e) { return finish("error", String((e && e.message) || e)); }
    const list = data && data.ok && data.lists && data.lists[0];
    if (!list) return finish("no-new-rows");
    if (!mergeTable(table, list, seen)) return finish("no-new-rows");
    out.pages += 1; // counts only pages that added rows
    if (onProgress) onProgress({ pages: out.pages, rows: table.rows.length });
  }
}
