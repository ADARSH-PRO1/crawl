// Templates: what to scrape on a site, saved by host. Same JSON shape as reports/scrape_templates.json in the app:
//   { "<host>": { "<name>": { itemsSelector, loadMore: {action, selector}, columns: [{name, as}] | null, savedAt } } }
// Selectors and column names only: no page content is ever stored.

const ACTIONS = new Set(["none", "scroll", "next", "more"]);
const MAX_COLUMNS = 40;
const MAX_TEXT = 500;

export const templateHost = (url) => {
  try { return new URL(url).hostname.toLowerCase().replace(/^www\./, ""); } catch (e) { return ""; }
};

export const cleanName = (name) => String(name || "").replace(/[^\p{L}\p{N}_ .-]/gu, "").trim().slice(0, 40);

const stamp = (d) => {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
};

/** [{name, as}] for the columns kept, in order. `names` are the names the user sees. */
export const columnSpec = (columns, names) => columns.map((c, i) => ({ name: c.name, as: names[i] }));

export function makeTemplate({ itemsSelector, loadMore, columns }, now = new Date()) {
  if (!itemsSelector) throw new Error("A template needs a list selector.");
  const action = loadMore && ACTIONS.has(loadMore.action) ? loadMore.action : "none";
  return {
    itemsSelector,
    loadMore: { action, selector: action === "next" || action === "more" ? String((loadMore && loadMore.selector) || "") : "" },
    columns: columns && columns.length ? columns : null,
    savedAt: stamp(now),
  };
}

/** Adds or replaces one template. Returns the store (changed in place). */
export function putTemplate(store, url, name, tpl) {
  const host = templateHost(url);
  const clean = cleanName(name);
  if (!host || !clean) throw new Error("A template needs a name and a web page.");
  (store[host] = store[host] || {})[clean] = tpl;
  return store;
}

export function removeTemplate(store, host, name) {
  if (store[host]) {
    delete store[host][name];
    if (!Object.keys(store[host]).length) delete store[host];
  }
  return store;
}

/** The templates saved for this page's site, newest first. */
export function templatesFor(store, url) {
  const mine = store[templateHost(url)] || {};
  return Object.entries(mine).map(([name, tpl]) => ({ name, tpl }))
    .sort((a, b) => String(b.tpl.savedAt || "").localeCompare(String(a.tpl.savedAt || "")) || a.name.localeCompare(b.name));
}

const text = (v, max = MAX_TEXT) => (typeof v === "string" ? v.slice(0, max) : "");

/** Checks something read from a file. Anything that is not a well-formed template is dropped. Returns { store, kept, dropped }. */
export function sanitizeStore(data) {
  const store = {};
  let kept = 0;
  let dropped = 0;
  if (!data || typeof data !== "object" || Array.isArray(data)) return { store, kept, dropped: 1 };
  for (const [host, byName] of Object.entries(data)) {
    if (!byName || typeof byName !== "object" || Array.isArray(byName) || !/^[a-z0-9.-]+$/i.test(host)) { dropped += 1; continue; }
    for (const [name, tpl] of Object.entries(byName)) {
      const clean = cleanName(name);
      if (!clean || !tpl || typeof tpl !== "object" || typeof tpl.itemsSelector !== "string" || !tpl.itemsSelector.trim()) { dropped += 1; continue; }
      const lm = tpl.loadMore && typeof tpl.loadMore === "object" ? tpl.loadMore : {};
      const columns = Array.isArray(tpl.columns)
        ? tpl.columns.filter((c) => c && typeof c.name === "string").slice(0, MAX_COLUMNS)
          .map((c) => ({ name: text(c.name, 200), as: text(c.as, 200) || text(c.name, 200) }))
        : null;
      (store[host.toLowerCase()] = store[host.toLowerCase()] || {})[clean] = {
        itemsSelector: text(tpl.itemsSelector),
        loadMore: { action: ACTIONS.has(lm.action) ? lm.action : "none", selector: text(lm.selector) },
        columns: columns && columns.length ? columns : null,
        savedAt: text(tpl.savedAt, 30),
      };
      kept += 1;
    }
  }
  return { store, kept, dropped };
}

/** Puts `incoming` into `store`; a template with the same site and name is replaced. */
export function mergeStores(store, incoming) {
  for (const [host, byName] of Object.entries(incoming)) Object.assign(store[host] = store[host] || {}, byName);
  return store;
}

/**
 * Applies a template's column choices to a detected list: columns it does not name are hidden (still there under
 * "Show all"), the named ones come first in its order and carry its names. Returns { hidden, names, matched }.
 */
export function applyToList(list, tpl) {
  const hidden = new Set();
  const names = new Map();
  const spec = tpl && tpl.columns;
  if (!spec || !spec.length) return { hidden, names, matched: false };
  const byName = new Map(list.columns.map((c) => [c.name, c]));
  const ordered = [];
  for (const s of spec) {
    const col = byName.get(s.name);
    if (!col || ordered.includes(col)) continue;
    ordered.push(col);
    if (s.as && s.as !== col.name) names.set(col.id, s.as);
  }
  if (!ordered.length) return { hidden, names: new Map(), matched: false };
  const rest = list.columns.filter((c) => !ordered.includes(c));
  rest.forEach((c) => hidden.add(c.id));
  list.columns.splice(0, list.columns.length, ...ordered, ...rest);
  return { hidden, names, matched: true };
}

// ---- storage (the browser's own storage for this extension; nothing leaves the browser) ---------------------------
export async function loadStore(api) {
  const got = await api.storage.local.get("templates");
  return sanitizeStore(got && got.templates).store;
}

export async function saveStore(api, store) {
  await api.storage.local.set({ templates: store });
}
