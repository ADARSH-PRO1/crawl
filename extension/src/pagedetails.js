// Runs inside a page: the key facts of that one page. Port of page_details() in app/scrape.py (keys start with "page ").
globalThis.__pageDetails = function () {
  const clip = (text, n = 300) => (text || "").replace(/\s+/g, " ").trim().slice(0, n);
  const meta = (...names) => {
    for (const n of names) {
      const tag = document.querySelector(`meta[property="${n}"], meta[name="${n}"]`);
      if (tag && tag.getAttribute("content")) return clip(tag.getAttribute("content"));
    }
    return "";
  };
  const d = {
    "page title": clip(document.title),
    "page heading": clip(document.querySelector("h1") && document.querySelector("h1").textContent),
    "page description": meta("description", "og:description"),
    "page image": meta("og:image"),
  };

  // Label / value pairs the page lists: definition lists and two-cell table rows that start with a header cell.
  const pairs = new Map();
  const put = (k, v) => { if (!pairs.has(k)) pairs.set(k, v); };
  document.querySelectorAll("dt").forEach((dt) => {
    let dd = dt.nextElementSibling;
    while (dd && dd.tagName !== "DD") dd = dd.nextElementSibling;
    if (dd) put(clip(dt.textContent, 40).replace(/:$/, ""), clip(dd.textContent));
  });
  document.querySelectorAll("tr").forEach((tr) => {
    const cells = Array.from(tr.children).filter((c) => c.tagName === "TH" || c.tagName === "TD");
    if (cells.length === 2 && cells[0].tagName === "TH") put(clip(cells[0].textContent, 40).replace(/:$/, ""), clip(cells[1].textContent));
  });
  let n = 0;
  for (const [k, v] of pairs) {
    if (n++ >= 20) break;
    if (k && v && !(`page ${k.toLowerCase()}` in d)) d[`page ${k.toLowerCase()}`] = v;
  }

  // The visible text of the main part of the page, words joined with spaces.
  const skip = new Set(["SCRIPT", "STYLE", "NAV", "FOOTER", "ASIDE", "NOSCRIPT", "TEMPLATE"]);
  const root = document.querySelector("main") || document.querySelector("article") || document.body;
  const parts = [];
  const walk = (el) => {
    for (const node of el.childNodes) {
      if (node.nodeType === 3) { const t = node.textContent.trim(); if (t) parts.push(t); }
      else if (node.nodeType === 1 && !skip.has(node.tagName)) walk(node);
      if (parts.join(" ").length > 400) return; // enough for a 300 character clip
    }
  };
  if (root) walk(root);
  d["page text"] = clip(parts.join(" "));

  const out = {};
  for (const [k, v] of Object.entries(d)) if (v) out[k] = v;
  // A login wall or bot check is reported, never worked around.
  const wall = /just a moment|attention required|access denied|captcha|verify you are (a )?human|unusual traffic|are you a robot/i;
  const short = (document.body ? document.body.innerText.length : 0) < 1500;
  const blocked = wall.test(document.title) || (short && wall.test(document.body ? document.body.innerText : ""));
  return { ok: true, blocked, details: out };
};
