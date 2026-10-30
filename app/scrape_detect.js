// In-page list detector and extractor for scrape mode. Runs inside the rendered page (through Crawl4AI js_code).
// It needs layout (getBoundingClientRect), which is why it cannot run on raw HTML.
// Arguments come from window.__scrapeArgs = {itemsSelector, maxItems, maxLists}.
// The result is written into <script type="application/json" id="__scrape_result"> for Python to read.
(() => {
  const stale = document.getElementById("__scrape_result");  // a session reuses the page: drop the last round's result
  if (stale) stale.remove();
  const args = window.__scrapeArgs || {};
  const maxItems = args.maxItems || 500;
  const maxLists = args.maxLists || 5;
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "META", "LINK", "HEAD", "TITLE", "TEMPLATE", "BR", "HR"]);
  const MEDIA = new Set(["IMG", "VIDEO", "AUDIO", "SOURCE"]);
  const MIN_ITEMS = 3;

  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== "hidden" && cs.display !== "none";
  };
  const ownText = (el) => Array.from(el.childNodes).some((n) => n.nodeType === 3 && n.textContent.trim());
  const isLink = (el) => el.tagName === "A" && !!el.getAttribute("href") && !!el.href;
  const isMedia = (el) => MEDIA.has(el.tagName) && !!(el.currentSrc || el.src);
  const isField = (el) => !SKIP.has(el.tagName) && visible(el) && (isLink(el) || isMedia(el) || ownText(el));
  const isItem = (el) => !SKIP.has(el.tagName) && visible(el) && ((el.innerText || "").trim().length > 0 || isField(el));
  const fieldElements = (item) => [item, ...item.querySelectorAll("*")].filter(isField);

  // Direct children that share the parent's most common tag (Easy Scraper: OR + uS).
  const dominantTag = (parent) => {
    const counts = {};
    for (const c of parent.children) counts[c.tagName] = (counts[c.tagName] || 0) + 1;
    let best = null;
    for (const t in counts) if (best === null || counts[t] > counts[best]) best = t;
    return best;
  };
  const candidateItems = (parent) => {
    const tag = dominantTag(parent);
    return tag ? Array.from(parent.children).filter((c) => c.tagName === tag && isItem(c)) : [];
  };

  // Similarity signatures: where the first link sits inside the item, the class list, the child-tag shape.
  const bucket = (n) => Math.round(n / 10) * 10;
  const linkSig = (item) => {
    const a = Array.from(item.querySelectorAll("a[href]")).find(visible);
    if (!a) return null;
    const ir = item.getBoundingClientRect();
    const ar = a.getBoundingClientRect();
    return bucket(ar.x - ir.x) + "," + bucket(ar.y - ir.y);
  };
  const classSig = (item) => (item.className && typeof item.className === "string" ? item.className.trim().split(/\s+/).sort().join(".") : null);
  const shapeSig = (item) => Array.from(item.children).map((c) => c.tagName).join(">") || null;
  const commonSigs = (items, sigFn) => {
    const counts = {};
    items.forEach((i) => { const s = sigFn(i); if (s) counts[s] = (counts[s] || 0) + 1; });
    return new Set(Object.keys(counts).filter((s) => counts[s] > 1 && counts[s] / items.length >= 0.3));
  };
  const filterBy = (items, sigFn, keep) => items.filter((i) => keep.has(sigFn(i)));
  const similarItems = (items) => {
    const byLink = filterBy(items, linkSig, commonSigs(items, linkSig));
    const byClass = filterBy(items, classSig, commonSigs(items, classSig));
    // The class filter is the designer's own grouping: prefer it unless the link filter finds far more items.
    if (byClass.length >= MIN_ITEMS && byClass.length >= 0.4 * byLink.length) return byClass;
    if (byLink.length || byClass.length) return byLink.length > byClass.length ? byLink : byClass;
    return filterBy(items, shapeSig, commonSigs(items, shapeSig));
  };

  // Stable CSS path: tag + up to two classes, :nth-of-type only when siblings look the same.
  const classPart = (el) => {
    const cls = typeof el.className === "string" ? el.className.trim().split(/\s+/).filter((c) => c && /^[A-Za-z_][\w-]*$/.test(c)) : [];
    return cls.slice(0, 2).map((c) => "." + c).join("");
  };
  const segment = (el) => {
    const base = el.tagName.toLowerCase() + classPart(el);
    const same = Array.from(el.parentElement ? el.parentElement.children : []).filter((s) => s.tagName === el.tagName && classPart(s) === classPart(el));
    return same.length > 1 ? base + ":nth-of-type(" + (Array.from(el.parentElement.children).filter((s) => s.tagName === el.tagName).indexOf(el) + 1) + ")" : base;
  };
  const pathTo = (el, stop) => {
    const parts = [];
    for (let n = el; n && n !== stop && n.nodeType === 1; n = n.parentElement) {
      if (n.id && /^[A-Za-z][\w-]*$/.test(n.id) && document.querySelectorAll("#" + n.id).length === 1) { parts.unshift("#" + n.id); break; }
      parts.unshift(segment(n));
      if (n === document.body) break;
    }
    return parts.join(" > ");
  };

  // Per-item fields, keyed by the path of the element relative to the item.
  const extractFields = (item) => {
    const out = [];
    fieldElements(item).forEach((el) => {
      const rel = el === item ? ":scope" : pathTo(el, item.parentElement).split(" > ").slice(1).join(" > ") || ":scope";
      const attrs = [];
      if (isLink(el)) attrs.push(["href", el.href.trim()]);
      if (isMedia(el)) attrs.push(["src", (el.currentSrc || el.src).trim()]);
      // The full text of a shortened label lives in title (links) or alt (images).
      const tip = (el.getAttribute("title") || "").replace(/\s+/g, " ").trim();
      if (isLink(el) && tip) attrs.push(["title", tip]);
      const alt = (el.getAttribute("alt") || "").replace(/\s+/g, " ").trim();
      if (isMedia(el) && alt) attrs.push(["alt", alt]);
      if (ownText(el)) attrs.push(["text", Array.from(el.childNodes).filter((n) => n.nodeType === 3).map((n) => n.textContent).join(" ").replace(/\s+/g, " ").trim()]);
      attrs.forEach(([attr, value]) => out.push({ selector: rel, attr, value, tag: el.tagName.toLowerCase(), cls: classPart(el) }));
    });
    return out;
  };

  const buildTable = (items) => {
    const columns = [];
    const index = {};
    const rows = items.map((item) => {
      const row = {};
      extractFields(item).forEach((f) => {
        const key = f.selector + "|" + f.attr;
        if (!(key in index)) {
          index[key] = columns.length;
          const first = f.cls ? f.cls.split(".")[1] : f.tag;
          columns.push({ id: "c" + columns.length, selector: f.selector, attr: f.attr, base: first, name: "" });
        }
        const id = columns[index[key]].id;
        if (!(id in row)) row[id] = f.value;
      });
      return row;
    });
    // Keep columns that carry data in at least a fifth of the rows, name them like Easy Scraper ("underline href").
    const fill = (c) => rows.filter((r) => r[c.id]).length;
    const MAX_COLUMNS = 40;
    const keepIds = new Set(columns.filter((c) => fill(c) >= Math.max(1, rows.length / 5))
      .sort((a, b) => fill(b) - fill(a)).slice(0, MAX_COLUMNS).map((c) => c.id));
    // A column that repeats an earlier one in every row (an image link and a title link to the same page) is dropped.
    const seenValues = new Set();
    const kept = columns.filter((c) => {
      if (!keepIds.has(c.id)) return false;
      const key = rows.map((r) => r[c.id] || "").join("");
      if (seenValues.has(key)) return false;
      seenValues.add(key);
      return true;
    });
    const used = {};
    kept.forEach((c) => {
      let name = c.attr === "text" ? c.base : c.base + " " + c.attr;
      used[name] = (used[name] || 0) + 1;
      c.name = used[name] > 1 ? name + " " + used[name] : name;
    });
    return { columns: kept, rows: rows.map((r) => Object.fromEntries(kept.map((c) => [c.id, r[c.id] || ""]))) };
  };

  const parentOf = (items) => items[0].parentElement;
  const itemsSelectorFor = (items) => pathTo(parentOf(items), null) + " > " + items[0].tagName.toLowerCase();

  const describe = (items) => {
    const table = buildTable(items.slice(0, maxItems));
    return { selector: itemsSelectorFor(items), count: items.length, columns: table.columns, rows: table.rows };
  };

  // ---- main ----
  const detect = () => {
  let result;
  try {
    if (args.itemsSelector) {
      const items = Array.from(document.querySelectorAll(args.itemsSelector)).filter(isItem);
      result = { ok: true, lists: items.length ? [Object.assign(describe(items), { score: 1 })] : [] };
    } else {
      const found = [];
      document.querySelectorAll("*").forEach((parent) => {
        if (parent.children.length < MIN_ITEMS || SKIP.has(parent.tagName)) return;
        const items = similarItems(candidateItems(parent));
        if (items.length >= MIN_ITEMS) found.push({ parent, items });
      });
      const vh = window.innerHeight || 800, vw = window.innerWidth || 1200;
      const stats = found.map((f) => {
        const r = f.parent.getBoundingClientRect();
        const fields = f.items.reduce((s, i) => s + fieldElements(i).length, 0) / f.items.length;
        const vis = Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0)) * Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0));
        // Real list items have similar heights; a page-layout wrapper (header row, main row, ...) does not.
        const hs = f.items.map((i) => i.getBoundingClientRect().height);
        const mean = hs.reduce((a, b) => a + b, 0) / hs.length;
        const sd = Math.sqrt(hs.reduce((a, b) => a + (b - mean) * (b - mean), 0) / hs.length);
        const uniform = mean > 0 ? Math.max(0, 1 - sd / mean) : 0;
        return { count: f.items.length, area: r.width * r.height, numFields: fields, viewport: vis, uniform };
      });
      const norm = (key, v) => {
        const vals = stats.map((s) => s[key]);
        const lo = Math.min(...vals), hi = Math.max(...vals);
        return hi === lo ? 0 : (v - lo) / (hi - lo);
      };
      const W = { count: 0.3, area: 0.3, numFields: 0.2, viewport: 0.2 };
      found.forEach((f, i) => {
        const base = Object.keys(W).reduce((s, k) => s + W[k] * norm(k, stats[i][k]), 0);
        f.score = base * (0.25 + 0.75 * stats[i].uniform);
      });
      found.sort((a, b) => b.score - a.score);
      // Nested lists repeat the same items: drop a candidate whose parent sits inside a better one's items.
      const picked = [];
      for (const f of found) {
        if (picked.some((p) => p.items.some((i) => i.contains(f.parent)) || f.items.some((i) => i.contains(p.parent)))) continue;
        picked.push(f);
        if (picked.length >= maxLists) break;
      }
      result = { ok: true, lists: picked.map((f) => Object.assign(describe(f.items), { score: Math.round(f.score * 1000) / 1000 })) };
    }
  } catch (e) {
    result = { ok: false, error: String(e && e.message || e), lists: [] };
  }
  return result;
  };

  const publish = (result) => {
    const old = document.getElementById("__scrape_result");
    if (old) old.remove();
    const node = document.createElement("script");
    node.type = "application/json";
    node.id = "__scrape_result";
    node.textContent = JSON.stringify(result).replace(/</g, "\\u003c");
    document.body.appendChild(node);
  };

  // ---- load more: scroll, "next" or "load more" (args.action = scroll | next | more) ----
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const snapshot = () => document.body.innerText.length + ":" + document.querySelectorAll("*").length;
  const waitChange = async (before) => {
    const limit = args.waitMs || 5000;
    for (let t = 0; t < limit && snapshot() === before; t += 250) await sleep(250);
    await sleep(400);
  };
  const NEXT_RE = /^(next|next page|older|older posts|›|»|>|→)$/i;
  const MORE_RE = /^(load more|show more|view more|see more|more|more results|load more results)$/i;
  const disabled = (el) => el.disabled || el.getAttribute("aria-disabled") === "true" || /\bdisabled\b/i.test(el.className || "");
  const labelOf = (el) => (el.innerText || el.getAttribute("aria-label") || el.getAttribute("title") || "").trim();
  const findButton = (mode, selector) => {
    if (selector) return document.querySelector(selector);
    const all = Array.from(document.querySelectorAll("a, button, [role=button]")).filter(visible);
    if (mode === "next") {
      return all.find((e) => e.matches("a[rel~=next]")) ||
        all.find((e) => /\bnext\b/i.test(e.getAttribute("aria-label") || "") || NEXT_RE.test(labelOf(e)));
    }
    return all.find((e) => MORE_RE.test(labelOf(e)));
  };
  const act = async () => {
    const mode = args.action;
    if (mode === "scroll") {
      const before = snapshot();
      window.scrollTo(0, document.documentElement.scrollHeight);
      await waitChange(before);
      return { acted: true };
    }
    const btn = findButton(mode, args.actionSelector);
    if (!btn || disabled(btn)) return { acted: false };
    const href = btn.tagName === "A" ? btn.getAttribute("href") : null;
    // A real link: report where it goes, so Python can check the address before loading it.
    if (href && !href.startsWith("#") && !/^javascript:/i.test(href)) return { acted: false, nextHref: btn.href };
    const before = snapshot();
    btn.scrollIntoView({ block: "center" });
    btn.click();
    await waitChange(before);
    return { acted: true };
  };

  // Single-page apps render after the load event: retry for a few seconds while nothing is found.
  return (async () => {
    let result = detect();
    for (let i = 0; i < 8 && result.ok && !result.lists.length; i++) {
      await sleep(500);
      result = detect();
    }
    if (args.action && result.ok && result.lists.length) {
      try { Object.assign(result, await act()); } catch (e) { result.acted = false; result.actError = String(e); }
    }
    publish(result);
  })();
})();
