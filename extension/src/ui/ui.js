// The scraper window: asks the page for its lists and shows the chosen one as an editable table.
import { fileName, toCsv, toObjects, toTsv, uniqueNames } from "../lib/csv.js";
import { LIMITS, originPatterns, runLoadMore, sameSite } from "../lib/loadmore.js";
import { DETAIL_LIMITS, detailUrlColumn, runDetails, urlColumns } from "../lib/details.js";
import {
  applyToList, cleanName, columnSpec, loadStore, makeTemplate, mergeStores, putTemplate, removeTemplate, sanitizeStore, saveStore,
  templateHost, templatesFor,
} from "../lib/templates.js";

const SCRIPTS = ["detect.js", "pagedetails.js", "content.js"];

const api = globalThis.browser ?? globalThis.chrome;
const $ = (id) => document.getElementById(id);
const tabId = Number(new URLSearchParams(location.search).get("tabId"));

const state = {
  lists: [],          // [{selector, count, score, columns: [{id, name}], rows: [{id: value}]}]
  current: 0,
  hidden: new Set(),  // column ids hidden in the current list
  names: new Map(),   // column id -> name typed by the user (reset when the list changes)
  pageUrl: "",
  seconds: 0,
  store: {},          // saved templates of every site: { host: { name: template } }
  templateName: "",   // the template in use for this page ("" = detect the list)
  pages: 1,           // pages that added rows
  abort: null,        // AbortController while "Load more rows" runs
};

const UNREADABLE = "This page cannot be read. Browser pages, extension stores and PDF viewers are off limits. " +
  "Open an ordinary web page and click the toolbar icon again.";

function show(text, kind = "error") {
  const box = $("message");
  box.textContent = text;
  box.className = kind === "info" ? "message info" : "message";
  box.hidden = !text;
}

const clock = (s) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;

const ask = (message) => api.tabs.sendMessage(tabId, message);

/** The scripts are put in by the toolbar click; after a reload of this window they may be gone, so check and re-inject. */
async function ensureInjected() {
  try {
    const pong = await ask({ type: "ping" });
    if (pong && pong.ok) return;
  } catch (e) { /* not there yet */ }
  await api.scripting.executeScript({ target: { tabId }, files: SCRIPTS });
}

async function loadPageInfo() {
  try {
    const tab = await api.tabs.get(tabId);
    state.pageUrl = tab.url || "";
    $("page-title").textContent = tab.title || state.pageUrl || "This page";
    $("page-url").textContent = state.pageUrl;
  } catch (e) {
    $("page-title").textContent = "This page";
  }
}

function visibleColumns() {
  const list = state.lists[state.current];
  return list ? list.columns.filter((c) => !state.hidden.has(c.id)) : [];
}

/** Names the user sees: typed names win, then the detected names; duplicates and blanks get fixed. */
function shownNames(columns) {
  return uniqueNames(columns.map((c) => state.names.get(c.id) ?? c.name));
}

function tableData() {
  const list = state.lists[state.current];
  const cols = visibleColumns();
  const names = shownNames(cols);
  const rows = list.rows.map((r) => Object.fromEntries(cols.map((c, i) => [names[i], r[c.id] ?? ""])));
  return { names, rows };
}

function renderStats() {
  const list = state.lists[state.current];
  $("stat-pages").textContent = state.pages;
  $("stat-items").textContent = list ? list.rows.length : 0;
  $("stat-cols").textContent = visibleColumns().length;
  $("stat-time").textContent = clock(state.seconds);
}

function renderSelect() {
  const sel = $("list-select");
  sel.replaceChildren(...state.lists.map((l, i) => {
    const o = document.createElement("option");
    o.value = String(i);
    o.textContent = `List ${i + 1}: ${l.rows.length} rows, ${l.columns.length} columns`;
    return o;
  }));
  sel.value = String(state.current);
  sel.hidden = state.lists.length < 2;
  $("btn-list").hidden = state.lists.length < 2;
}

function renderHiddenNote() {
  const box = $("hidden-cols");
  const list = state.lists[state.current];
  const hidden = list ? list.columns.filter((c) => state.hidden.has(c.id)) : [];
  box.hidden = hidden.length === 0;
  if (!hidden.length) return;
  box.replaceChildren(document.createTextNode(`Hidden columns: ${hidden.map((c) => state.names.get(c.id) ?? c.name).join(", ")}`));
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = "Show all";
  b.addEventListener("click", () => { state.hidden.clear(); render(); });
  box.appendChild(b);
}

function renderGrid() {
  const list = state.lists[state.current];
  const cols = visibleColumns();
  const head = document.createElement("tr");
  const corner = document.createElement("th");
  corner.className = "num";
  head.appendChild(corner);
  cols.forEach((c) => {
    const th = document.createElement("th");
    const wrap = document.createElement("div");
    wrap.className = "head";
    const input = document.createElement("input");
    input.value = state.names.get(c.id) ?? c.name;
    input.setAttribute("aria-label", `Name of column ${c.name}`);
    input.addEventListener("change", () => { state.names.set(c.id, input.value); renderHiddenNote(); });
    const x = document.createElement("button");
    x.type = "button";
    x.className = "x";
    x.textContent = "×";
    x.title = "Hide this column";
    x.addEventListener("click", () => { state.hidden.add(c.id); render(); });
    wrap.append(input, x);
    th.appendChild(wrap);
    head.appendChild(th);
  });
  $("grid").tHead.replaceChildren(head);

  const body = document.createDocumentFragment();
  list.rows.forEach((r, i) => {
    const tr = document.createElement("tr");
    const n = document.createElement("td");
    n.className = "num";
    n.textContent = String(i + 1);
    tr.appendChild(n);
    cols.forEach((c) => {
      const td = document.createElement("td");
      const v = r[c.id] ?? "";
      td.title = v;
      td.textContent = v; // text only: page content is never inserted as HTML
      tr.appendChild(td);
    });
    body.appendChild(tr);
  });
  $("grid").tBodies[0].replaceChildren(body);
}

function render() {
  renderSelect();
  renderHiddenNote();
  renderGrid();
  renderStats();
}

function setExportEnabled(on) {
  $("btn-copy").disabled = $("btn-csv").disabled = $("btn-json").disabled = !on;
}

async function scan() {
  show("");
  $("bar").hidden = true;
  $("loadbar").hidden = true;
  $("detailbar").hidden = true;
  $("tplbar").hidden = true;
  $("grid-wrap").hidden = true;
  state.pages = 1;
  $("load-status").textContent = "";
  const t0 = Date.now();
  const tpl = state.templateName ? (state.store[templateHost(state.pageUrl)] || {})[state.templateName] : null;
  let result;
  let used = null; // the template, once it has really matched something
  let note = "";
  try {
    await ensureInjected();
    if (tpl) {
      result = await ask({ type: "findLists", args: { maxItems: 500, maxLists: 5, itemsSelector: tpl.itemsSelector } });
      if (result && result.ok && result.lists.length) used = tpl;
      else note = `The saved list of "${state.templateName}" matches nothing on this page now, so the list was detected again.`;
    }
    if (!used) result = await ask({ type: "findLists", args: { maxItems: 500, maxLists: 5 } });
  } catch (e) {
    show(UNREADABLE);
    return;
  }
  state.seconds = Math.round((Date.now() - t0) / 1000);
  if (!result || !result.ok) {
    show(`The page could not be read${result && result.error ? `: ${result.error}` : "."}`);
    return;
  }
  $("bar").hidden = false;
  if (!result.lists.length) {
    show("No list of repeated items was found on this page. Scroll down so the items are on screen, then press Rescan.", "info");
    setExportEnabled(false);
    return;
  }
  state.lists = result.lists;
  state.current = 0;
  state.hidden.clear();
  state.names.clear();
  if (used) { // the template's columns and load-more choice
    const applied = applyToList(state.lists[0], used);
    applied.hidden.forEach((id) => state.hidden.add(id));
    applied.names.forEach((v, k) => state.names.set(k, v));
    $("action").value = used.loadMore.action;
    $("action-selector").value = used.loadMore.selector;
    note = `Template "${state.templateName}" applied. Choose "None" in the Template box to detect the list again.`;
  }
  setExportEnabled(true);
  $("loadbar").hidden = false;
  $("detailbar").hidden = false;
  $("grid-wrap").hidden = false;
  syncActionControls();
  fillUrlColumns();
  refreshTemplateBar();
  render();
  if (note) show(note, "info");
}

// ---- templates ---------------------------------------------------------------------------------------------------
function refreshTemplateBar() {
  const mine = templatesFor(state.store, state.pageUrl);
  const sel = $("tpl-select");
  const none = document.createElement("option");
  none.value = "";
  none.textContent = mine.length ? "None (detect the list)" : "None saved for this site";
  sel.replaceChildren(none, ...mine.map(({ name }) => {
    const o = document.createElement("option");
    o.value = name;
    o.textContent = name;
    return o;
  }));
  if (!mine.some((t) => t.name === state.templateName)) state.templateName = "";
  sel.value = state.templateName;
  sel.disabled = !mine.length;
  $("btn-tpl-delete").disabled = !state.templateName;
  $("btn-tpl-export").disabled = !Object.keys(state.store).length;
  $("tplbar").hidden = false;
}

async function persistStore() {
  await saveStore(api, state.store);
}

async function saveTemplate() {
  const list = state.lists[state.current];
  const name = cleanName($("tpl-name").value) || state.templateName;
  if (!list || !list.selector) { show("Open a list of this page first; there is nothing to save yet."); return; }
  if (!name) { show("Type a name for the template first."); return; }
  const cols = visibleColumns();
  try {
    const tpl = makeTemplate({
      itemsSelector: list.selector,
      loadMore: { action: $("action").value, selector: $("action-selector").value.trim() },
      columns: columnSpec(cols, shownNames(cols)),
    });
    putTemplate(state.store, state.pageUrl, name, tpl);
    await persistStore();
  } catch (e) {
    show(`The template could not be saved: ${e.message || e}`);
    return;
  }
  state.templateName = name;
  $("tpl-name").value = "";
  refreshTemplateBar();
  show(`Template "${name}" saved for ${templateHost(state.pageUrl)}. It is applied the next time you open this site.`, "info");
}

async function deleteTemplate() {
  const name = state.templateName;
  if (!name || !confirm(`Delete the template "${name}"?`)) return;
  removeTemplate(state.store, templateHost(state.pageUrl), name);
  await persistStore();
  state.templateName = "";
  await scan();
}

function exportTemplates() {
  download(JSON.stringify(state.store, null, 2), `bizfy-templates-${new Date().toISOString().slice(0, 10)}.json`, "application/json");
}

async function importTemplates(file) {
  if (!file) return;
  if (file.size > 1_000_000) { show("That file is too big to be a template file."); return; }
  let data;
  try { data = JSON.parse(await file.text()); } catch (e) { show("That file is not valid JSON."); return; }
  const { store, kept, dropped } = sanitizeStore(data);
  if (!kept) { show("No templates were found in that file."); return; }
  mergeStores(state.store, store);
  await persistStore();
  refreshTemplateBar();
  show(`Imported ${kept} template${kept === 1 ? "" : "s"}${dropped ? `; ${dropped} entr${dropped === 1 ? "y was" : "ies were"} not valid and skipped` : ""}.`, "info");
}

// ---- settings: the numbers you set are remembered ----------------------------------------------------------------
async function loadSettings() {
  try {
    const got = await api.storage.local.get("settings");
    const s = (got && got.settings) || {};
    if (Number.isInteger(s.pages) && s.pages >= 1 && s.pages <= LIMITS.maxPages) $("pages").value = String(s.pages);
    if (Number.isInteger(s.maxUrls) && s.maxUrls >= 1 && s.maxUrls <= DETAIL_LIMITS.maxUrls) $("max-urls").value = String(s.maxUrls);
  } catch (e) { /* defaults */ }
}

function saveSettings() {
  const pages = Math.max(1, Math.min(Number($("pages").value) || 1, LIMITS.maxPages));
  const maxUrls = Math.max(1, Math.min(Number($("max-urls").value) || 1, DETAIL_LIMITS.maxUrls));
  api.storage.local.set({ settings: { pages, maxUrls } }).catch(() => {});
}

/** The link-column choice: every column that holds web addresses, the detected one first. */
function fillUrlColumns() {
  const list = state.lists[state.current];
  const sel = $("url-col");
  const cols = list ? urlColumns(list.columns, list.rows) : [];
  const best = list ? detailUrlColumn(list.columns, list.rows) : "";
  sel.replaceChildren(...cols.map((c) => {
    const o = document.createElement("option");
    o.value = c.id;
    o.textContent = state.names.get(c.id) ?? c.name;
    return o;
  }));
  if (best) sel.value = best;
  $("btn-details").disabled = !cols.length;
  $("btn-details").title = cols.length ? "Open each linked page of this site and add its details as columns"
    : "This list has no column of web addresses to open";
}

// ---- load more: scroll, "load more" button, next-page links ----------------------------------------------------
const STOP_TEXT = {
  "no-more": "Stopped: nothing more to load.",
  "no-new-rows": "Stopped: the last step added no new rows.",
  "row-cap": `Stopped: reached the limit of ${LIMITS.maxRows} rows.`,
  "page-cap": "Stopped: reached the page limit you set.",
  time: "Stopped: the time limit was reached.",
  stopped: "Stopped by you.",
  "other-site": "Stopped: the next page is on another site, and this scraper stays on the site you started on.",
};

function syncActionControls() {
  const action = $("action").value;
  $("action-selector").hidden = action !== "next" && action !== "more";
  $("pages").disabled = action === "none";
  $("btn-load").disabled = action === "none" || !state.lists.length || !!state.abort;
}

function setRunning(on) {
  $("btn-stop").hidden = !on;
  $("btn-load").hidden = on;
  $("btn-details").hidden = on;
  const ids = ["btn-rescan", "btn-list", "list-select", "action", "action-selector", "pages", "url-col", "max-urls", "btn-page",
    "tpl-select", "tpl-name", "btn-tpl-save", "btn-tpl-delete", "btn-tpl-import", "btn-tpl-export"];
  for (const id of ids) $(id).disabled = on;
  if (on) setExportEnabled(false); else { setExportEnabled(true); syncActionControls(); fillUrlColumns(); refreshTemplateBar(); }
}

/**
 * Asks for access to the site the page is on (just this site). Called first in a click handler, because browsers only
 * show the question for a click. Returns false, with a message shown, when the user says no.
 */
async function askSiteAccess(what) {
  let granted = false;
  try { granted = await api.permissions.request({ origins: originPatterns(state.pageUrl) }); } catch (e) { /* refused */ }
  if (!granted) {
    show(`To ${what}, the scraper needs access to this site. Press the button again and allow it.`, "info");
  }
  return granted;
}

/** Loads the next page in the user's own tab and waits for it, then puts the scripts back. Returns the address it ended on. */
async function navigateTab(url) {
  await new Promise((resolve, reject) => {
    let started = false;
    const done = (fn, v) => { clearTimeout(timer); api.tabs.onUpdated.removeListener(on); fn(v); };
    const timer = setTimeout(() => done(reject, new Error("The next page took too long to load.")), 30000);
    function on(id, info) {
      if (id !== tabId) return;
      if (info.status === "loading") started = true;
      if (info.status === "complete" && started) done(resolve);
    }
    api.tabs.onUpdated.addListener(on);
    api.tabs.update(tabId, { url }).catch((e) => done(reject, e));
  });
  const tab = await api.tabs.get(tabId);
  await ensureInjected();
  return tab.url || url;
}

async function loadMore() {
  const list = state.lists[state.current];
  const action = $("action").value;
  if (!list || action === "none") return;
  // Moving to other pages needs access to this site; ask for just this site, only now, from the click.
  if (action === "next" && !(await askSiteAccess("open the next pages"))) {
    show("To open the next pages, the scraper needs access to this site. Press the button again and allow it, " +
      "or use \"Scroll down\" or \"Click load more\", which need no extra access.", "info");
    return;
  }
  show("");
  state.abort = new AbortController();
  setRunning(true);
  const table = { columns: list.columns, rows: list.rows };
  const t0 = Date.now();
  const timer = setInterval(() => { state.seconds = Math.round((Date.now() - t0) / 1000); renderStats(); }, 1000);
  const args = { maxItems: LIMITS.maxRows, maxLists: 5, itemsSelector: list.selector, action, actionSelector: $("action-selector").value.trim() };
  let out;
  try {
    out = await runLoadMore({
      table,
      pageUrl: state.pageUrl,
      maxPages: Math.max(1, Math.min(Number($("pages").value) || 1, LIMITS.maxPages)),
      signal: state.abort.signal,
      read: () => ask({ type: "findLists", args }),
      navigate: navigateTab,
      onProgress: ({ pages, rows }) => {
        state.pages = pages;
        $("load-status").textContent = `Page ${pages}: ${rows} rows so far...`;
        render();
      },
    });
  } finally {
    clearInterval(timer);
    state.abort = null;
  }
  setRunning(false);
  render();
  const base = action === "next" ? " The browser tab stays on the last page it opened." : "";
  if (out.reason === "error") {
    show(`Loading more stopped on an error: ${out.error}`);
    $("load-status").textContent = "";
  } else {
    $("load-status").textContent = `${STOP_TEXT[out.reason] || ""} ${list.rows.length} rows from ${out.pages} page${out.pages === 1 ? "" : "s"}.${base}`;
  }
  if (out.skipped.length) show(`Not followed: ${out.skipped.map((s) => `${s.url} (${s.reason})`).join("; ")}`, "info");
}

// ---- scrape each URL, and the details of this page ---------------------------------------------------------------
/** Waits until a tab has really loaded a web page (not the blank page a new tab starts with). */
function waitForWebPage(id) {
  return new Promise((resolve, reject) => {
    const done = (fn, v) => { clearTimeout(timer); api.tabs.onUpdated.removeListener(on); fn(v); };
    const timer = setTimeout(() => done(reject, new Error("the page took too long to load")), 30000);
    const check = async () => {
      const t = await api.tabs.get(id).catch(() => null);
      if (!t) done(reject, new Error("the tab was closed"));
      else if (t.status === "complete" && /^https?:/i.test(t.url || "")) done(resolve, t);
    };
    function on(tid, info) { if (tid === id && info.status === "complete") check(); }
    api.tabs.onUpdated.addListener(on);
    check();
  });
}

/** Opens the address in a background tab of the user's window, reads its details, closes the tab. */
async function detailsOf(url) {
  const home = await api.tabs.get(tabId);
  const tab = await api.tabs.create({ url, active: false, windowId: home.windowId });
  try {
    const loaded = await waitForWebPage(tab.id);
    if (!sameSite(loaded.url, state.pageUrl)) throw new Error("the page redirected to another site");
    await api.scripting.executeScript({ target: { tabId: tab.id }, files: SCRIPTS });
    const res = await api.tabs.sendMessage(tab.id, { type: "pageDetails" });
    if (!res || !res.ok) throw new Error((res && res.error) || "the page could not be read");
    return res;
  } finally {
    api.tabs.remove(tab.id).catch(() => {});
  }
}

async function scrapeEach() {
  const list = state.lists[state.current];
  const urlColumn = $("url-col").value;
  if (!list || !urlColumn) return;
  if (!(await askSiteAccess("open the linked pages"))) return;
  show("");
  state.abort = new AbortController();
  setRunning(true);
  const cap = Math.max(1, Math.min(Number($("max-urls").value) || 1, DETAIL_LIMITS.maxUrls));
  $("load-status").textContent = "Opening the linked pages...";
  const table = { columns: list.columns, rows: list.rows };
  let out;
  try {
    out = await runDetails({
      table, urlColumn, pageUrl: state.pageUrl, cap, signal: state.abort.signal, fetchDetails: detailsOf,
      onProgress: ({ done, total }) => { $("load-status").textContent = `Details ${done}/${total}...`; render(); },
    });
  } finally {
    state.abort = null;
  }
  setRunning(false);
  render();
  const why = { done: "Done.", stopped: "Stopped by you.", time: "Stopped: the time limit was reached." }[out.reason];
  const over = out.overCap ? ` ${out.overCap} more rows were left out (limit ${cap} URLs).` : "";
  $("load-status").textContent = `${why} Details for ${out.done} of ${out.total} pages.${over}`;
  if (out.skipped.length) {
    show(`Not scraped: ${out.skipped.slice(0, 5).map((s) => `${s.url} (${s.reason})`).join("; ")}` +
      `${out.skipped.length > 5 ? `; and ${out.skipped.length - 5} more` : ""}`, "info");
  }
}

/** The key facts of the page itself as a one-row table (title, heading, description, labelled values, text start). */
async function pageDetails() {
  show("");
  let res;
  try {
    await ensureInjected();
    res = await ask({ type: "pageDetails" });
  } catch (e) {
    show(UNREADABLE);
    return;
  }
  if (!res || !res.ok) { show(`The page could not be read${res && res.error ? `: ${res.error}` : "."}`); return; }
  if (res.blocked) show("This page looks like a login, CAPTCHA or bot check. The scraper does not work around those.", "info");
  const keys = Object.keys(res.details);
  if (!keys.length) { show("No details were found on this page."); return; }
  const row = Object.fromEntries(keys.map((k) => [`d:${k}`, res.details[k]]));
  state.lists = [{ selector: "", count: 1, columns: keys.map((k) => ({ id: `d:${k}`, name: k })), rows: [row] }];
  state.current = 0;
  state.hidden.clear();
  state.names.clear();
  state.pages = 1;
  $("loadbar").hidden = true;
  $("detailbar").hidden = true;
  $("tplbar").hidden = true;
  $("grid-wrap").hidden = false;
  setExportEnabled(true);
  $("load-status").textContent = "";
  render();
}

function download(text, name, mime, bom = false) {
  const blob = new Blob([bom ? "﻿" : "", text], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    const ta = document.createElement("textarea"); // older browsers and some extension pages
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
}

function switchList(index) {
  state.current = index;
  state.hidden.clear();
  state.names.clear();
  fillUrlColumns();
  render();
}

$("list-select").addEventListener("change", (e) => switchList(Number(e.target.value)));
$("btn-list").addEventListener("click", () => switchList((state.current + 1) % state.lists.length));
$("btn-rescan").addEventListener("click", () => scan());
$("tpl-select").addEventListener("change", (e) => { state.templateName = e.target.value; scan(); });
$("btn-tpl-save").addEventListener("click", saveTemplate);
$("btn-tpl-delete").addEventListener("click", deleteTemplate);
$("btn-tpl-export").addEventListener("click", exportTemplates);
$("btn-tpl-import").addEventListener("click", () => $("tpl-file").click());
$("tpl-file").addEventListener("change", async (e) => { await importTemplates(e.target.files[0]); e.target.value = ""; });
$("pages").addEventListener("change", saveSettings);
$("max-urls").addEventListener("change", saveSettings);
$("action").addEventListener("change", syncActionControls);
$("btn-load").addEventListener("click", loadMore);
$("btn-details").addEventListener("click", scrapeEach);
$("btn-page").addEventListener("click", pageDetails);
$("btn-stop").addEventListener("click", () => { if (state.abort) state.abort.abort(); });
$("btn-copy").addEventListener("click", async () => {
  const { names, rows } = tableData();
  await copyText(toTsv(names, rows));
  const b = $("btn-copy");
  b.textContent = "Copied";
  setTimeout(() => { b.textContent = "Copy"; }, 1500);
});
$("btn-csv").addEventListener("click", () => {
  const { names, rows } = tableData();
  download(toCsv(names, rows), fileName(state.pageUrl, "csv"), "text/csv", true); // BOM: Excel reads UTF-8 correctly
});
$("btn-json").addEventListener("click", () => {
  const { names, rows } = tableData();
  download(JSON.stringify(toObjects(names, rows), null, 2), fileName(state.pageUrl, "json"), "application/json");
});

if (!Number.isInteger(tabId)) {
  show("Open this window with the toolbar icon on the page you want to scrape.");
} else {
  (async () => {
    await loadPageInfo();
    await loadSettings();
    try {
      state.store = await loadStore(api);
      const known = templatesFor(state.store, state.pageUrl);
      state.templateName = known.length ? known[0].name : ""; // a site you saved before opens with its newest template
    } catch (e) { /* no storage: work without templates */ }
    await scan();
  })();
}
