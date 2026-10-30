"""Scrape mode: a listing page in, a table out (Easy Scraper style). No LLM is used.

The list is found by a script that runs inside the rendered page (app/scrape_detect.js, executed through Crawl4AI),
because telling which repeated blocks are "the list" needs layout. A BeautifulSoup version on the raw HTML is the
fallback and what the offline tests run. Same rules as the crawler: public URLs only, robots.txt honoured, blocked or
login pages are reported and never bypassed.
"""
import asyncio
import csv
import io
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from app.config import (CRAWL_DELAY_SECONDS, CRAWL_PAGE_TIMEOUT_MS, SCRAPE_DETAIL_MAX_URLS, SCRAPE_TEMPLATES_FILE,
                        SCRAPE_TOTAL_TIMEOUT_S, SETTINGS, UrlError, avalidate_public_url, clamp_scrape,
                        validate_public_url)
from app.crawl import UA, classify_failure, looks_blocked, looks_login, markdown_of

# Crawl4AI runs js_code as the body of an async function and awaits its return value, so the script must return its
# promise (the leading comment lines are dropped: `return` followed by a comment line would return undefined).
DETECT_JS = re.sub(r"\A(//[^\n]*\n)+", "", Path(__file__).with_name("scrape_detect.js").read_text(encoding="utf-8"))
RESULT_ID = "__scrape_result"
RESULT_RE = re.compile(r'<script[^>]*id="__scrape_result"[^>]*>(.*?)</script>', re.S)
MIN_ITEMS = 3
MAX_LISTS = 5
MAX_COLUMNS = 40


# ---- reading the in-page result -----------------------------------------------------------------------------------
def parse_result(html: str) -> dict | None:
    """The JSON the detector script left in the page, or None when it did not run."""
    m = RESULT_RE.search(html or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def build_js(items_selector: str = "", max_items: int = 500, action: str = "", action_selector: str = "") -> str:
    """The detector script. `action` (scroll | next | more) makes it also scroll or click after reading the list."""
    args = {"itemsSelector": items_selector, "maxItems": max_items, "maxLists": MAX_LISTS,
            "action": action, "actionSelector": action_selector}
    return f"window.__scrapeArgs = {json.dumps(args)};\nreturn {DETECT_JS}"


def table_of(lst: dict) -> dict:
    """Detector list -> {selector, count, score, columns: [names], rows: [{name: value}]}."""
    cols = lst.get("columns") or []
    names = {c["id"]: c["name"] for c in cols}
    rows = [{names[k]: v for k, v in row.items() if k in names} for row in lst.get("rows") or []]
    return {"selector": lst.get("selector", ""), "count": lst.get("count", len(rows)), "score": lst.get("score", 0),
            "columns": [c["name"] for c in cols], "rows": rows}


# ---- fallback: the same idea on raw HTML (no layout) -----------------------------------------------------------
SKIP_TAGS = {"script", "style", "noscript", "meta", "link", "head", "title", "template", "br", "hr"}


def _classes(el: Tag) -> list[str]:
    return [c for c in (el.get("class") or []) if re.match(r"^[A-Za-z_][\w-]*$", c)]


def _own_text(el: Tag) -> str:
    return re.sub(r"\s+", " ", " ".join(s for s in el.find_all(string=True, recursive=False))).strip()


def _fields(item: Tag) -> list[tuple[str, str, str, str]]:
    """(relative path, attr, value, base name) for every link, image and text node of an item."""
    out = []
    for el in [item, *item.find_all(True)]:
        if el.name in SKIP_TAGS:
            continue
        path, node = [], el
        while node is not None and node is not item:
            path.append(node.name + "".join("." + c for c in _classes(node)[:2]))
            node = node.parent
        rel = " > ".join(reversed(path)) or ":scope"
        base = (_classes(el) or [el.name])[0]
        if el.name == "a" and el.get("href"):
            out.append((rel, "href", el["href"].strip(), base))
        if el.name == "a" and el.get("href") and (el.get("title") or "").strip():
            out.append((rel, "title", _clip(el["title"], 500), base))
        if el.name == "img" and el.get("src"):
            out.append((rel, "src", el["src"].strip(), base))
            if (el.get("alt") or "").strip():
                out.append((rel, "alt", _clip(el["alt"], 500), base))
        if _own_text(el):
            out.append((rel, "text", _own_text(el), base))
    return out


def _signature(item: Tag) -> str:
    cls = ".".join(sorted(_classes(item)))
    return cls or ">".join(c.name for c in item.find_all(True, recursive=False))


def detect_lists_html(html: str, base_url: str = "", max_items: int = 500) -> list[dict]:
    """Repeated sibling blocks on raw HTML, best first. Same output shape as table_of()."""
    from urllib.parse import urljoin

    soup = BeautifulSoup(html or "", "html.parser")
    found = []
    for parent in soup.find_all(True):
        kids = [c for c in parent.find_all(True, recursive=False) if c.name not in SKIP_TAGS]
        if len(kids) < MIN_ITEMS:
            continue
        tags: dict[str, int] = {}
        for k in kids:
            tags[k.name] = tags.get(k.name, 0) + 1
        tag = max(tags, key=tags.get)
        items = [k for k in kids if k.name == tag and k.get_text(strip=True)]
        sigs: dict[str, int] = {}
        for it in items:
            sigs[_signature(it)] = sigs.get(_signature(it), 0) + 1
        common = {s for s, n in sigs.items() if s and n > 1 and n / len(items) >= 0.3}
        items = [it for it in items if _signature(it) in common]
        if len(items) >= MIN_ITEMS:
            found.append((parent, items))

    lists = []
    for parent, items in found:
        per_item = [_fields(it) for it in items[:max_items]]
        cols: dict[tuple[str, str], dict] = {}
        for fields in per_item:
            for rel, attr, _value, base in fields:
                cols.setdefault((rel, attr), {"base": base, "attr": attr, "n": 0})
        rows_raw = []
        for fields in per_item:
            row = {}
            for rel, attr, value, _ in fields:
                row.setdefault((rel, attr), value)
            rows_raw.append(row)
        for key in cols:
            cols[key]["n"] = sum(1 for r in rows_raw if r.get(key))
        full = sorted((k for k in cols if cols[k]["n"] >= max(1, len(rows_raw) / 5)), key=lambda k: -cols[k]["n"])
        keep_set, seen_values, kept = set(full[:MAX_COLUMNS]), set(), []
        for k in cols:  # a column that repeats an earlier one in every row is dropped
            if k not in keep_set:
                continue
            values = tuple(r.get(k, "") for r in rows_raw)
            if values not in seen_values:
                seen_values.add(values)
                kept.append(k)
        used: dict[str, int] = {}
        names = {}
        for k in kept:
            name = cols[k]["base"] if cols[k]["attr"] == "text" else f"{cols[k]['base']} {cols[k]['attr']}"
            used[name] = used.get(name, 0) + 1
            names[k] = name if used[name] == 1 else f"{name} {used[name]}"
        rows = [{names[k]: (urljoin(base_url, r.get(k, "")) if k[1] in ("href", "src") and r.get(k) else r.get(k, ""))
                 for k in kept} for r in rows_raw]
        mean_fields = sum(len(f) for f in per_item) / max(1, len(per_item))
        lists.append({"selector": parent.name + "".join("." + c for c in _classes(parent)[:2]) + " > " + items[0].name,
                      "count": len(items), "columns": [names[k] for k in kept], "rows": rows,
                      "score": len(items) * mean_fields})
    lists.sort(key=lambda t: -t["score"])
    picked = []
    for lst in lists:  # drop nested duplicates: keep a list only if it shares no row text with a better one
        sig = {tuple(r.values())[:2] for r in lst["rows"][:5]}
        if not any(sig & {tuple(r.values())[:2] for r in p["rows"][:5]} for p in picked):
            picked.append(lst)
        if len(picked) >= MAX_LISTS:
            break
    top = picked[0]["score"] if picked else 1
    for p in picked:
        p["score"] = round(p["score"] / top, 3) if top else 0
    return picked


# ---- fetching ---------------------------------------------------------------------------------------------------
class Fetcher:
    """One browser session on the in-process Crawl4AI library. fetch() loads a URL, runs `js` in it and returns the page
    after the script finished.

    Not the Crawl4AI Docker service: it rejects every request body that carries js_code ("field 'js_code' is not
    permitted on CrawlerRunConfig from an untrusted request"), and has no setting to allow it. The detector script is
    our own code, so it runs in our own process; the service's boundary is left as it is."""

    def __init__(self):
        self._crawler = None

    async def __aenter__(self):
        from crawl4ai import AsyncWebCrawler, BrowserConfig
        self._crawler = AsyncWebCrawler(config=BrowserConfig(headless=True, user_agent=UA, verbose=False))
        await self._crawler.__aenter__()
        return self

    async def __aexit__(self, *exc):
        if self._crawler is not None:
            await self._crawler.__aexit__(*exc)

    async def close_session(self, session_id: str) -> None:
        if self._crawler is not None and session_id:
            try:
                await self._crawler.crawler_strategy.kill_session(session_id)
            except Exception:  # noqa: BLE001 - the browser is closed right after anyway
                pass

    async def fetch(self, url: str, js: str = "", wait_for: str = "", session_id: str = "", js_only: bool = False):
        from crawl4ai import CacheMode, CrawlerRunConfig
        run_cfg = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS, check_robots_txt=True, page_timeout=CRAWL_PAGE_TIMEOUT_MS,
            js_code=[js] if js else None, wait_for=wait_for or None, session_id=session_id or None, js_only=js_only,
            remove_overlay_elements=True, verbose=False)
        r = await self._crawler.arun(url=url, config=run_cfg)
        return SimpleNamespace(
            url=r.url, final_url=r.redirected_url or r.url, success=bool(r.success), html=r.html or "",
            markdown=markdown_of(r), status_code=r.status_code, error_message=r.error_message,
            failure=None if r.success else classify_failure(r))


# ---- "scrape each URL": details of every linked page -----------------------------------------------------------
DETAIL_TEXT_CHARS = 300
MAX_DETAIL_PAIRS = 20


def detail_url_column(columns: list[str], rows: list[dict]) -> str:
    """The first column whose values are mostly web addresses (a link column such as 'title href')."""
    for c in columns:
        vals = [r.get(c, "") for r in rows if r.get(c)]
        if vals and sum(v.startswith(("http://", "https://")) for v in vals) >= 0.8 * len(vals) and c.endswith(" href"):
            return c
    return ""


def _clip(text: str, n: int = DETAIL_TEXT_CHARS) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:n]


def page_details(html: str) -> dict[str, str]:
    """Key-value facts of one page, no layout needed: title, heading, description, image, a text start, and every
    label/value pair the page lists (definition lists and two-cell table rows). Keys are prefixed 'page '."""
    soup = BeautifulSoup(html or "", "html.parser")

    def meta(*names):
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if tag and tag.get("content"):
                return _clip(tag["content"])
        return ""

    d = {"page title": _clip(soup.title.get_text()) if soup.title else "",
         "page heading": _clip(soup.h1.get_text()) if soup.h1 else "",
         "page description": meta("description", "og:description"), "page image": meta("og:image")}
    pairs: dict[str, str] = {}
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            pairs.setdefault(_clip(dt.get_text(), 40).rstrip(":"), _clip(dd.get_text()))
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"], recursive=False)
        if len(cells) == 2 and cells[0].name == "th":
            pairs.setdefault(_clip(cells[0].get_text(), 40).rstrip(":"), _clip(cells[1].get_text()))
    for k, v in list(pairs.items())[:MAX_DETAIL_PAIRS]:
        if k and v:
            d.setdefault(f"page {k.lower()}", v)
    for tag in soup(["script", "style", "nav", "footer", "aside", "noscript", "template"]):
        tag.decompose()
    body = soup.find("main") or soup.find("article") or soup.body
    d["page text"] = _clip(body.get_text(" ")) if body else ""
    return {k: v for k, v in d.items() if v}


async def scrape_details(f: Fetcher, out: dict, rows: list[dict], columns: list[str], url_col: str, base_url: str,
                         cap: int, t0: float) -> int:
    """Fetch the page behind each row's link (same site only, public addresses, 2 at a time) and add its details as
    extra columns. Rows keep their order; a page that cannot be read is listed in `skipped` and its row stays as is."""
    targets: dict[str, list[dict]] = {}
    for r in rows:
        u = (r.get(url_col) or "").split("#")[0]
        if u and len(targets) < cap or u in targets:
            targets.setdefault(u, []).append(r)
    gate = asyncio.Semaphore(2)
    done = 0

    async def one(u: str) -> None:
        nonlocal done
        async with gate:
            if time.time() - t0 > SCRAPE_TOTAL_TIMEOUT_S:
                out["timed_out"] = True
                return
            try:
                if not _same_host(u, base_url):
                    raise UrlError("the link is on another site")
                await avalidate_public_url(u)
                await asyncio.sleep(CRAWL_DELAY_SECONDS)
                r = await f.fetch(u)
            except UrlError as e:
                out["skipped"].append({"url": u, "reason": f"not followed: {e}"})
                return
            if not r.success:
                out["skipped"].append({"url": u, "reason": r.failure or "load failed"})
                return
            try:
                validate_public_url(r.final_url)
            except UrlError as e:
                out["skipped"].append({"url": u, "reason": f"redirect_blocked: {e}"})
                return
            details = page_details(r.html)
            for row in targets[u]:
                row.update(details)
            done += 1
            print(f"[scrape +{time.time() - t0:.0f}s] details {done}/{len(targets)}: {u}", flush=True)

    await asyncio.gather(*(one(u) for u in targets))
    for r in rows:
        columns.extend(k for k in r if k not in columns)
    return done


# ---- templates: what to scrape on a site, saved by host --------------------------------------------------------
def template_host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def load_templates(path: Path | None = None) -> dict:
    path = path or SCRAPE_TEMPLATES_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get_template(url: str, name: str, path: Path | None = None) -> dict | None:
    return (load_templates(path).get(template_host(url)) or {}).get(name)


def save_template(url: str, name: str, items_selector: str, load_more: dict | None = None,
                  columns: list[dict] | None = None, path: Path | None = None) -> dict:
    """Store {itemsSelector, loadMore, columns} for this site. `columns` is [{"name": found, "as": new name}] in the
    order to keep; None keeps every column. Selectors only: no page content is saved."""
    path = path or SCRAPE_TEMPLATES_FILE
    name = re.sub(r"[^\w .-]", "", name).strip()[:40]
    if not name or not items_selector:
        raise ValueError("A template needs a name and a list selector.")
    tpl = {"itemsSelector": items_selector, "loadMore": load_more or {"action": "none", "selector": ""},
           "columns": columns, "savedAt": time.strftime("%Y-%m-%d %H:%M:%S")}
    data = load_templates(path)
    data.setdefault(template_host(url), {})[name] = tpl
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return tpl


def apply_template_columns(columns: list[str], rows: list[dict], tpl: dict) -> tuple[list[str], list[dict]]:
    """Keep and rename columns as the template says; columns it does not know are dropped, missing ones stay empty."""
    spec = tpl.get("columns")
    if not spec:
        return columns, rows
    pairs = [(c["name"], c.get("as") or c["name"]) for c in spec if c.get("name") in columns]
    if not pairs:
        return columns, rows
    return [new for _old, new in pairs], [{new: r.get(old, "") for old, new in pairs} for r in rows]


# ---- optional: askg1 chooses between detected lists from a plain-English instruction ---------------------------
PICK_PROMPT = ("A web page has several lists. For each list, the column names and the first row are shown.\n\n{lists}\n\n"
               "Which list best matches this request: {instruction}\nReply with only the list number.")
PICK_CELL_CHARS = 60


def pick_prompt(lists: list[dict], instruction: str) -> str:
    """Candidate lists as numbered one-liners (about 100 tokens each), so the prompt stays far below 4096 tokens."""
    lines = []
    for i, lst in enumerate(lists[:MAX_LISTS], 1):
        first = (lst["rows"] or [{}])[0]
        sample = " | ".join(str(first.get(c, ""))[:PICK_CELL_CHARS] for c in lst["columns"][:6])
        lines.append(f"{i}. {len(lst['rows'])} rows; columns: {', '.join(lst['columns'][:8])}; first row: {sample}")
    return PICK_PROMPT.format(lists="\n".join(lines), instruction=instruction.strip()[:200])


async def pick_list(lists: list[dict], instruction: str, chat=None) -> int | None:
    """Index of the list askg1 picks, or None when it is unavailable or its answer is not a valid list number.
    The model only chooses among lists the code found; it never writes data."""
    from app.llm import chat_once  # lazy: scrape mode itself needs no askg1

    try:
        reply = await (chat or chat_once)(pick_prompt(lists, instruction), max_tokens=8)
    except Exception as e:  # noqa: BLE001 - the best-scored list is a fine default
        print(f"[scrape] askg1 list choice skipped: {type(e).__name__}: {str(e)[:120]}", flush=True)
        return None
    m = re.search(r"\d+", reply or "")
    n = int(m.group()) if m else 0
    return n - 1 if 1 <= n <= min(len(lists), MAX_LISTS) else None


# ---- run --------------------------------------------------------------------------------------------------------
def to_csv(columns: list[str], rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    for r in rows:
        w.writerow([r.get(c, "") for c in columns])
    return buf.getvalue()


def _row_key(row: dict) -> tuple:
    return tuple(row.get(k, "") for k in sorted(row))


def _same_host(a: str, b: str) -> bool:
    host = lambda u: (urlparse(u).hostname or "").lower().removeprefix("www.")  # noqa: E731
    return host(a) == host(b)


async def load_more(f: Fetcher, out: dict, sel: str, columns: list[str], rows: list[dict], data: dict, cur_url: str,
                    *, action: str, action_selector: str, items_cap: int, pages_cap: int, t0: float) -> int:
    """Scroll or click through further pages, adding unseen rows. Returns how many pages were read.

    A "next" that is a real link is checked (public address, same site, not seen before) and then loaded; a button is
    clicked in place. Stops when nothing new appears, the row or page cap is hit, or the time budget runs out."""
    sid = f"scrape-{int(t0 * 1000)}"
    seen = {_row_key(r) for r in rows}
    visited = {cur_url.rstrip("/")}
    pages = 1
    js = build_js(sel, items_cap, action, action_selector)
    while pages < pages_cap and len(rows) < items_cap:
        if time.time() - t0 > SCRAPE_TOTAL_TIMEOUT_S:
            out["timed_out"] = True
            break
        nxt = data.get("nextHref")
        if not nxt and not data.get("acted"):
            break
        await asyncio.sleep(CRAWL_DELAY_SECONDS)  # low rate, like the crawler
        if nxt:
            try:
                if not _same_host(nxt, cur_url):  # cheapest check first: no DNS lookup for an address we skip anyway
                    raise UrlError("the next page is on another site")
                if nxt.rstrip("/") in visited:
                    break
                await avalidate_public_url(nxt)
            except UrlError as e:
                out["skipped"].append({"url": nxt, "reason": f"not followed: {e}"})
                break
            r = await f.fetch(nxt, js=js, session_id=sid)
            visited.add(nxt.rstrip("/"))
        else:
            r = await f.fetch(cur_url, js=js, session_id=sid, js_only=True)
        if not r.success:
            out["skipped"].append({"url": nxt or cur_url, "reason": r.failure or "load failed"})
            break
        cur_url = r.final_url
        data = parse_result(r.html) or {}
        if not data.get("ok") or not data.get("lists"):
            break
        table = table_of(data["lists"][0])
        fresh = [x for x in table["rows"] if _row_key(x) not in seen]
        print(f"[scrape +{time.time() - t0:.0f}s] page {pages + 1}: {len(fresh)} new rows", flush=True)
        if not fresh:
            break
        pages += 1  # counts only pages that added rows
        seen.update(_row_key(x) for x in fresh)
        rows.extend(fresh)
        columns.extend(c for c in table["columns"] if c not in columns)
    await f.close_session(sid)
    del rows[items_cap:]
    return pages


async def run_scrape(url: str, list_index: int = 0, max_items: int | None = None, max_pages: int | None = None,
                     items_selector: str = "", load_more_action: str = "", action_selector: str = "",
                     each_url: bool = False, detail_column: str = "", detail_max: int | None = None,
                     template: str = "", save_as: str = "", instruction: str = "", chat=None) -> dict:
    """Load `url`, find the lists on it, return them all. With a load_more_action (scroll | next | more) the chosen
    list is extended page by page. `template` reuses a saved list selector (falls back to detection when it matches
    nothing); `save_as` stores the chosen list as a template. With an `instruction` and several lists on the page,
    askg1 (when configured) picks the list; without it the best-scored list is used."""
    t0 = time.time()
    items_cap, pages_cap = clamp_scrape(max_items, max_pages)
    out: dict = {"lists": [], "chosen": 0, "skipped": []}
    tpl = None
    if template:
        tpl = get_template(url, template)
        if not tpl:
            out["error"] = f"No template named {template!r} is saved for {template_host(url) or url}."
            return out
        items_selector = items_selector or tpl["itemsSelector"]
        if not load_more_action or load_more_action == "none":
            load_more_action = (tpl.get("loadMore") or {}).get("action", "")
            action_selector = action_selector or (tpl.get("loadMore") or {}).get("selector", "")
    action = load_more_action if load_more_action in ("scroll", "next", "more") and pages_cap > 1 else ""
    out["load_more"] = {"action": action or "none", "selector": action_selector}
    await avalidate_public_url(url)
    out["backend"] = "local"  # in-process library; see Fetcher
    pages = 1

    async with Fetcher() as f:
        sid = f"scrape-{int(t0 * 1000)}" if action else ""
        r = await f.fetch(url, js=build_js(items_selector, items_cap, action, action_selector), session_id=sid)
        out["final_url"] = r.final_url
        try:
            validate_public_url(r.final_url)
        except UrlError as e:
            out["error"] = f"redirect_blocked: {e}"
            return out
        if not r.success and not r.html:
            out["error"] = r.failure or "The page could not be loaded."
            return out

        data = parse_result(r.html)
        used_template = bool(tpl and data and data.get("ok") and data.get("lists"))
        if tpl and not used_template:  # the saved selector matches nothing now (the site changed): detect again
            out["template_note"] = f"The saved selector for {template!r} matched nothing; the list was detected again."
            print(f"[scrape] {out['template_note']}", flush=True)
            r = await f.fetch(url, js=build_js("", items_cap, action, action_selector), session_id=sid)
            data = parse_result(r.html)
        if data and data.get("ok") and data.get("lists"):
            out["method"] = "browser"
            lists = [table_of(x) for x in data["lists"]]
        else:
            out["method"] = "html"
            if data and data.get("error"):
                print(f"[scrape] in-page detector failed: {data['error']}", flush=True)
            lists = detect_lists_html(r.html, r.final_url, items_cap)
            action = ""  # no browser-side detector, so nothing to scroll or click

        idx = max(0, min(list_index, len(lists) - 1)) if lists else 0
        if len(lists) > 1 and instruction.strip() and list_index == 0 and not used_template                 and (chat or SETTINGS.askg1_enabled):
            choice = await pick_list(lists, instruction, chat)
            if choice is not None:
                idx = choice
                out["picked_by"] = "askg1"
        if lists and action:
            out["chosen"] = idx
            chosen = lists[idx]
            pages = await load_more(f, out, chosen["selector"], chosen["columns"], chosen["rows"], data, r.final_url,
                                    action=action, action_selector=action_selector, items_cap=items_cap,
                                    pages_cap=pages_cap, t0=t0)

        if lists and used_template:
            lists[0]["columns"], lists[0]["rows"] = apply_template_columns(lists[0]["columns"], lists[0]["rows"], tpl)
            out["template"] = template
        if lists and save_as:
            pick = lists[idx]
            if out.get("method") == "browser":
                save_template(url, save_as, pick["selector"], {"action": action or "none", "selector": action_selector},
                              [{"name": c, "as": c} for c in pick["columns"]])
                out["saved_template"] = save_as
            else:
                out["skipped"].append({"url": "", "reason": "template not saved: the in-page detector did not run"})
        if lists and each_url:
            chosen = lists[idx]
            col = detail_column if detail_column in chosen["columns"] else detail_url_column(chosen["columns"], chosen["rows"])
            if not col:
                out["skipped"].append({"url": "", "reason": "scrape each URL: no column of links in this list"})
            else:
                cap = max(1, min(detail_max or SCRAPE_DETAIL_MAX_URLS, SCRAPE_DETAIL_MAX_URLS))
                n = await scrape_details(f, out, chosen["rows"], chosen["columns"], col, r.final_url, cap, t0)
                out["details"] = {"column": col, "fetched": n, "limit": cap}

    if not lists:
        if looks_blocked(r.markdown) or looks_login(r.markdown):
            out["error"] = ("The site blocked automated access or asks for a login. Nothing was bypassed.")
        else:
            out["error"] = "No list of repeated items was found on this page."
        return out

    out["lists"] = lists
    out["chosen"] = idx
    chosen = lists[idx]
    chosen["count"] = len(chosen["rows"])
    out.update(ok=True, columns=chosen["columns"], rows=chosen["rows"], items=len(chosen["rows"]), pages=pages,
               seconds=round(time.time() - t0, 1))
    return out


# ---- one page's details (Easy Scraper's "Scrape Page Details") --------------------------------------------------
def _single_row_result(out: dict, details: dict, method: str, t0: float) -> dict:
    cols = list(details)
    out["lists"] = [{"selector": "(whole page)", "count": 1, "score": 1, "columns": cols, "rows": [details]}]
    out.update(ok=True, method=method, chosen=0, columns=cols, rows=[details], items=1, pages=1,
               seconds=round(time.time() - t0, 1))
    return out


async def run_page_details(url: str) -> dict:
    """Title, heading, description, image, text and every label/value pair of ONE page, as a single row."""
    t0 = time.time()
    out: dict = {"lists": [], "chosen": 0, "skipped": [], "backend": "local", "load_more": {"action": "none", "selector": ""}}
    await avalidate_public_url(url)
    async with Fetcher() as f:
        r = await f.fetch(url)
    out["final_url"] = r.final_url
    try:
        validate_public_url(r.final_url)
    except UrlError as e:
        out["error"] = f"redirect_blocked: {e}"
        return out
    if not r.success and not r.html:
        out["error"] = r.failure or "The page could not be loaded."
        return out
    details = page_details(r.html)
    if not details:
        out["error"] = ("The site blocked automated access or asks for a login. Nothing was bypassed."
                        if looks_blocked(r.markdown) or looks_login(r.markdown) else "No details were found on this page.")
        return out
    return _single_row_result(out, details, "page details", t0)


# ---- a page you saved yourself (login-only pages: nothing is fetched, nothing is logged into) -----------------
MAX_HTML_BYTES = 5_000_000


async def run_scrape_html(page_html: str, base_url: str = "", list_index: int = 0, max_items: int | None = None,
                          details_only: bool = False) -> dict:
    """Scrape HTML the user supplies (Save page as, or View source). The page is rendered locally from the text; no
    request is made to the site and no credentials are involved. `base_url` only makes relative links absolute."""
    from html import escape

    t0 = time.time()
    items_cap, _ = clamp_scrape(max_items, 1)
    out: dict = {"lists": [], "chosen": 0, "skipped": [], "backend": "local", "source": "html",
                 "load_more": {"action": "none", "selector": ""}}
    if len(page_html.encode("utf-8", "ignore")) > MAX_HTML_BYTES:
        out["error"] = f"The HTML is larger than {MAX_HTML_BYTES // 1_000_000} MB."
        return out
    if base_url:
        if urlparse(base_url).scheme not in ("http", "https") or not urlparse(base_url).hostname:
            out["error"] = "The base URL must start with http:// or https://."
            return out
        page_html = f'<base href="{escape(base_url, quote=True)}">' + page_html  # relative links resolve against it
    if details_only:
        details = page_details(page_html)
        if not details:
            out["error"] = "No details were found in this HTML."
            return out
        return _single_row_result(out, details, "page details", t0)

    async with Fetcher() as f:
        r = await f.fetch("raw:" + page_html, js=build_js("", items_cap))
    data = parse_result(r.html)
    if data and data.get("ok") and data.get("lists"):
        out["method"] = "browser"
        lists = [table_of(x) for x in data["lists"]]
    else:
        out["method"] = "html"
        lists = detect_lists_html(page_html, base_url, items_cap)
    if not lists:
        out["error"] = "No list of repeated items was found in this HTML."
        return out
    out["lists"] = lists
    out["chosen"] = max(0, min(list_index, len(lists) - 1))
    chosen = lists[out["chosen"]]
    out.update(ok=True, columns=chosen["columns"], rows=chosen["rows"], items=len(chosen["rows"]), pages=1,
               seconds=round(time.time() - t0, 1))
    return out
