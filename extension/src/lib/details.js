// Scrape each URL: the details of every page a column links to. Port of detail_url_column() and scrape_details() in app/scrape.py.
import { isWebUrl, sameSite } from "./loadmore.js";

export const DETAIL_LIMITS = { maxUrls: 30, concurrency: 2, delayMs: 1000, totalMs: 420000 };

/** The first column whose values are mostly web addresses and that is named like a link ("title href"). */
export function detailUrlColumn(columns, rows) {
  for (const c of columns) {
    if (!(c.name === "href" || c.name.endsWith(" href"))) continue;
    const vals = rows.map((r) => r[c.id]).filter(Boolean);
    if (vals.length && vals.filter((v) => /^https?:\/\//i.test(v)).length >= 0.8 * vals.length) return c.id;
  }
  return "";
}

/** Columns that could be a link column, for the choice box. */
export function urlColumns(columns, rows) {
  return columns.filter((c) => {
    const vals = rows.map((r) => r[c.id]).filter(Boolean);
    return vals.length && vals.filter((v) => /^https?:\/\//i.test(v)).length >= 0.8 * vals.length;
  });
}

/** Unique addresses to open (same site only, at most `cap`) and the ones left out. */
export function collectTargets(rows, colId, pageUrl, cap) {
  const targets = new Map();
  const skipped = [];
  const refused = new Set();
  let overCap = 0;
  for (const r of rows) {
    const u = String(r[colId] || "").split("#")[0];
    if (!u) continue;
    if (!isWebUrl(u) || !sameSite(u, pageUrl)) {
      if (!refused.has(u)) { refused.add(u); skipped.push({ url: u, reason: "not followed: the link is on another site" }); }
      continue;
    }
    if (targets.has(u)) targets.get(u).push(r);
    else if (targets.size < cap) targets.set(u, [r]);
    else overCap += 1;
  }
  return { targets, skipped, overCap };
}

/**
 * Opens each target through fetchDetails(url) -> {blocked, details}, two at a time, and adds the details to the rows as
 * columns "d:<key>". `table` ({columns, rows}) is changed in place. Returns { done, total, skipped, reason, overCap }
 * with reason done | stopped | time.
 */
export async function runDetails({ table, urlColumn, pageUrl, cap = DETAIL_LIMITS.maxUrls, fetchDetails, signal, onProgress,
  concurrency = DETAIL_LIMITS.concurrency, delayMs = DETAIL_LIMITS.delayMs, totalMs = DETAIL_LIMITS.totalMs, sleep, now = Date.now }) {
  const wait = sleep || ((ms) => new Promise((r) => setTimeout(r, ms)));
  const { targets, skipped, overCap } = collectTargets(table.rows, urlColumn, pageUrl, Math.min(cap, DETAIL_LIMITS.maxUrls));
  const queue = Array.from(targets.keys());
  const started = now();
  const out = { done: 0, total: queue.length, skipped, reason: "done", overCap };

  async function worker() {
    while (queue.length) {
      if (signal && signal.aborted) { out.reason = "stopped"; return; }
      if (now() - started > totalMs) { out.reason = "time"; return; }
      const u = queue.shift();
      await wait(delayMs); // low rate, like the crawler
      if (signal && signal.aborted) { out.reason = "stopped"; return; }
      let res;
      try { res = await fetchDetails(u); } catch (e) {
        skipped.push({ url: u, reason: String((e && e.message) || e) });
        continue;
      }
      if (!res || res.blocked) {
        skipped.push({ url: u, reason: "looks like a login, CAPTCHA or bot check: not scraped" });
        continue;
      }
      for (const row of targets.get(u)) for (const [k, v] of Object.entries(res.details || {})) row[`d:${k}`] = v;
      out.done += 1;
      if (onProgress) onProgress({ done: out.done, total: out.total });
    }
  }
  await Promise.all(Array.from({ length: Math.max(1, concurrency) }, worker));

  // New columns in the order the rows first show them, like the app.
  for (const row of table.rows) {
    for (const k of Object.keys(row)) {
      if (k.startsWith("d:") && !table.columns.some((c) => c.id === k)) table.columns.push({ id: k, name: k.slice(2) });
    }
  }
  return out;
}
