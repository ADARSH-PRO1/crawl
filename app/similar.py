"""Similar-tools mode: a URL plus an instruction in, a table of similar tools out (Perplexity style).

Not part of the PDF (it has no search step), so it is a documented deviation. Search runs through a local SearXNG
(a search proxy, not an AI service); askg1 stays the only model and every call is one small prompt inside the 4096-token
window. Pages are read through the normal crawler (robots.txt honoured, blocked or login pages reported, never bypassed).

Links are never written by the model: tool URLs and GitHub repos come from search results and page links, and a tool name
is kept only if it literally appears in the page it was read from.
"""
import asyncio
import csv
import io
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

import app.config as cfg
from app.config import CRAWL_OUT_DIR, SEARXNG_URL, SIMILAR_MAX_LIST_PAGES, SIMILAR_TOTAL_TIMEOUT_S
from app.llm import chat_once

STOPWORDS = frozenset(
    "a an and are as at be by for from how i in is it of on or that the this to was what when where which who "
    "why with find me show tell give list similar like best top about into your you can do does tools tool".split())
GENERIC_NAMES = frozenset({"none", "n/a", "na", "browser", "browsers", "tool", "tools", "ai", "agent", "agents", "and", "etc"})
# Sites that list or discuss tools, or social sites: never a tool's own website.
NOT_OFFICIAL_HOSTS = ("alternativeto.net", "g2.com", "producthunt.com", "reddit.com", "capterra.com", "trustpilot.com",
                      "medium.com", "youtube.com", "quora.com", "linkedin.com", "facebook.com", "x.com", "twitter.com",
                      "wikipedia.org", "github.com", "news.ycombinator.com",
                      # image, favicon and asset hosts: a link to these is a picture, not the tool's website
                      "githubusercontent.com", "google.com", "gstatic.com", "googleusercontent.com", "cloudfront.net",
                      "imgur.com", "wp.com", "gravatar.com")
# List sites that block automated browsers (seen on every run): not worth a page read, and never bypassed.
ANTI_BOT_HOSTS = ("g2.com", "producthunt.com", "alternativeto.net", "sourceforge.net", "capterra.com", "trustradius.com")
GENERIC_TOKENS = frozenset({"ai", "browser", "browsers", "agent", "agents", "tool", "tools", "app", "apps", "software",
                            "automation", "open", "source", "web"})
GITHUB_NON_OWNERS = frozenset({"features", "login", "about", "pricing", "topics", "orgs", "sponsors", "marketplace", "apps",
                               "settings", "collections", "enterprise", "explore", "search", "site", "contact", "events",
                               "readme", "security", "resources", "signup", "join", "customer-stories", "solutions"})

PROFILE_PROMPT = ("Page title: {title}\n\nText:\n{chunk}\n\n"
                  "What tool, product or project is this page about? Reply in exactly this format:\n"
                  "NAME: <its name>\nDOES: <what it is, as a short noun phrase of at most 10 words, for example "
                  "'open-source note-taking app'>\nKEYWORDS: <3 to 6 words, comma separated>")
NAMES_PROMPT = ("Text:\n{chunk}\n\nList the names of tools or products similar to {name} ({does}) that are mentioned in "
                "the text. Names only, comma separated, at most 10. If there are none, reply NONE.")
ROW_PROMPT = ("Target tool: {name} ({does})\nUser focus: {focus}\n\nCandidate tool: {cand}\nText about the candidate:\n{chunk}\n\n"
              "Compare {cand} with the target using only the text above. Reply in exactly this format, one short line each:\n"
              "SIMILAR: <what they have in common>\nDIFFERENT: <what makes {cand} different>\n"
              "Example for two other tools (Notion and Obsidian):\nSIMILAR: Both organize notes in linked pages.\n"
              "DIFFERENT: Obsidian keeps notes as local files, Notion is cloud-first.\n"
              "If the text says nothing for a line, write - after the label.")


# ---- text helpers (no LLM) ---------------------------------------------------------------------------------------
def terms(text: str) -> set[str]:
    words = (w.strip(".-") for w in re.findall(r"[a-z0-9][a-z0-9+#.-]*", text.lower()))
    return {w for w in words if w not in STOPWORDS and len(w) > 1}


def clean_text(markdown: str) -> str:
    text = re.sub(r"<!--.*?-->", "", markdown, flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def pick_evidence(markdown: str, query: str, max_chars: int = 1800) -> str:
    """Lines of the page that share the most words with the query, in page order, up to max_chars.

    Falls back to the start of the page when nothing matches."""
    text = clean_text(markdown)
    q = terms(query)
    lines = [ln.strip() for ln in text.split("\n") if len(ln.strip()) >= 30]
    scored = sorted(((len(q & terms(ln)), i) for i, ln in enumerate(lines)), key=lambda t: (-t[0], t[1]))
    chosen: list[int] = []
    used = 0
    for score, i in scored:
        if score == 0 or used + len(lines[i]) > max_chars:
            continue
        chosen.append(i)
        used += len(lines[i]) + 1
    return "\n".join(lines[i] for i in sorted(chosen)) if chosen else text[:max_chars]


def norm_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def name_tokens(name: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", name.lower()) if len(t) >= 3]


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


FILE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".pdf", ".zip", ".mp4", ".css", ".js")


def is_official_candidate(url: str, skip_host: str = "") -> bool:
    host = host_of(url)
    if not host or host == skip_host or urlparse(url).path.lower().endswith(FILE_EXTENSIONS):
        return False
    return not any(host == h or host.endswith("." + h) for h in NOT_OFFICIAL_HOSTS)


# ---- profile, queries, names -------------------------------------------------------------------------------------
def parse_profile(reply: str, fallback_name: str, instruction: str) -> dict:
    def field(label: str) -> str:
        m = re.search(rf"^\s*{label}\s*:\s*(.+)$", reply, re.I | re.M)
        return m.group(1).strip() if m else ""

    name = field("NAME").strip(" *\"'") or fallback_name
    keywords = [k.strip() for k in field("KEYWORDS").split(",") if k.strip()][:6]
    return {"name": name[:60], "does": field("DOES")[:200], "keywords": keywords or sorted(terms(instruction))[:4]}


def build_queries(profile: dict) -> list[tuple[str, list[str] | None]]:
    """(query, engines) pairs; engines None = SearXNG's default web engines."""
    name, kw = profile["name"], " ".join(profile["keywords"][:4])
    qs: list[tuple[str, list[str] | None]] = [(f"alternatives to {name}", None),
                                              (f"{name} alternatives open source", None)]
    if kw:
        qs += [(f"tools like {kw}", None), (f"best {kw} tools", None), (kw, ["github"])]
    return qs


def extract_names(reply: str, evidence: str, exclude: str) -> list[str]:
    """Names from the model's comma list, kept only if they literally appear in the evidence it was shown."""
    if reply.strip().upper().startswith("NONE"):
        return []
    low = evidence.lower()
    ex = norm_key(exclude)
    out: list[str] = []
    for raw in re.split(r"[,\n;]|\band\b", reply):
        name = re.sub(r"^[\s\-*\d.)]+", "", raw).strip(" .*\"'()[]")
        key = norm_key(name)
        tokens = re.findall(r"[a-z0-9]+", name.lower())
        if (len(name) < 2 or len(name) > 40 or name.lower() in GENERIC_NAMES or not key
                or all(t in GENERIC_TOKENS for t in tokens) or key == ex or (len(ex) >= 4 and ex in key) or name.lower() not in low or key in {norm_key(n) for n in out}):
            continue
        out.append(name)
    return out


def merge_names(per_page: list[dict]) -> list[dict]:
    """per_page: [{url, engines, names}] -> candidates ranked by distinct pages (x2) plus distinct engines."""
    merged: dict[str, dict] = {}
    for p in per_page:
        for name in p["names"]:
            m = merged.setdefault(norm_key(name), {"name": name, "pages": set(), "engines": set()})
            m["pages"].add(p["url"])
            m["engines"] |= set(p["engines"])
    ranked = sorted(merged.values(), key=lambda m: (-(2 * len(m["pages"]) + len(m["engines"])), m["name"].lower()))
    return [{"name": m["name"], "mentions": len(m["pages"]), "engines": sorted(m["engines"]),
             "pages": sorted(m["pages"])} for m in ranked]


def relevance(hit: dict, profile: dict) -> int:
    """How likely a search hit is a page about alternatives to the target: shared words with the target's name and
    keywords (title and snippet), plus a bonus when the target's name appears."""
    text = f"{hit.get('title', '')} {hit.get('content', '')}"
    want = terms(f"{profile['name']} {' '.join(profile['keywords'])}") | {"alternatives", "alternative", "similar", "best", "vs"}
    bonus = 3 if norm_key(profile["name"]) and norm_key(profile["name"]) in norm_key(text) else 0
    return len(want & terms(text)) + bonus


def github_candidates(gh_hits: list[dict], target_url: str, limit: int = 6) -> list[dict]:
    """Repositories found by the GitHub engine, as candidates: name = repo name, GitHub link and description known."""
    tm = re.match(r"https?://github\.com/([^/]+)/([^/#?]+)", target_url)
    skip = (tm.group(1).lower(), tm.group(2).lower().removesuffix(".git")) if tm else None
    out: list[dict] = []
    seen: set[str] = set()
    for r in gh_hits:
        m = re.match(r"https?://github\.com/([^/]+)/([^/#?]+)/?$", r["url"])
        if not m or m.group(1).lower() in GITHUB_NON_OWNERS:
            continue
        owner, repo = m.group(1), m.group(2)
        key = norm_key(repo)
        if (skip and (owner.lower(), repo.lower()) == skip) or key in seen or not key:
            continue
        seen.add(key)
        out.append({"name": repo, "url": f"https://github.com/{owner}/{repo}", "snippet": (r.get("content") or "")[:400],
                    "mentions": 1, "engines": ["github"], "pages": []})
        if len(out) >= limit:
            break
    return out


def official_match(name: str, hit: dict, profile: dict) -> bool:
    """A search hit that is plausibly the tool's own site: the domain name is the tool's name (not a government or
    school domain), and the title or snippet is about the same kind of thing as the target."""
    host = host_of(hit["url"])
    if not host or host.rsplit(".", 1)[-1] in ("mil", "gov", "edu") or not is_official_candidate(hit["url"]):
        return False
    key, label = norm_key(name), norm_key(host.split(".")[0])
    domain_ok = label == key or (label.startswith(key) and len(label) - len(key) <= 8) or (len(label) >= 4 and key.startswith(label))
    topic = terms(" ".join(profile["keywords"]) + " " + profile["does"])
    return domain_ok and bool(topic & terms(f"{hit.get('title', '')} {hit.get('content', '')}"))


REPO_SUFFIXES = ("browser", "app", "ai", "agent", "os", "desktop")


def repo_is_the_tool(name: str, owner: str, repo: str) -> bool:
    """A repository found by search is accepted only if its name IS the tool's name (optionally plus a short suffix).
    Wrappers, plugins and look-alikes such as 'comet-mcp' or 'diagram-design' are rejected."""
    key, rk = norm_key(name), norm_key(repo)
    return bool(key) and (rk == key or any(rk == key + s for s in REPO_SUFFIXES)) and owner.lower() not in GITHUB_NON_OWNERS


def repo_fits(repo_text: str, topic: set[str], candidate_text: str) -> bool:
    """A repository with the right name must also be about the same thing: it shares a word with the target's topic,
    or at least two words with the candidate's own page (so 'strawberry' the Python library is rejected for the
    Strawberry browser, while an open-source workspace app matches its own site)."""
    words = terms(repo_text)
    return bool(topic & words) or len(terms(candidate_text) & words) >= 2


def clean_row(similar_text: str, different_text: str, target_does: str) -> tuple[str, str]:
    """Drop cells that just repeat each other or the target's own description (a common small-model slip)."""
    def same(a: str, b: str) -> bool:
        ta, tb = terms(a), terms(b)
        return bool(ta and tb) and len(ta & tb) / min(len(ta), len(tb)) >= 0.8

    if same(similar_text, different_text):
        different_text = ""
    if same(similar_text, target_does):
        similar_text = ""
    if same(different_text, target_does):
        different_text = ""
    return similar_text, different_text


def grounded(cell: str, *sources: str) -> str:
    """Return the cell only if every capitalised word in it (a product or company name) appears in the sources it was
    written from. A small model sometimes invents a name; an empty cell is better than a wrong one."""
    if not cell:
        return ""
    low = " ".join(sources).lower()
    for m in re.finditer(r"(?<![.!?]\s)(?<!^)\b([A-Z][A-Za-z0-9]{3,})\b", cell):
        if m.group(1).lower() not in low:
            return ""
    return cell


def strip_tracking(url: str) -> str:
    """Official site links from search results often carry tracking parameters (msockid, utm_*); drop the query."""
    return url.split("#")[0].split("?")[0] if "github.com" not in url else url.split("#")[0]


def brand_host(url: str, profile: dict) -> bool:
    """True for the target's own website: its marketing page is not an independent list of alternatives."""
    key = norm_key(profile["name"])
    return len(key) >= 4 and key in norm_key(host_of(url).split(".")[0])


# ---- links -------------------------------------------------------------------------------------------------------
def find_link_for_name(markdown: str, name: str, skip_host: str = "") -> str | None:
    """A markdown link on the page whose link text matches the tool name and that can be the tool's own site."""
    key = norm_key(name)
    for text, url in re.findall(r"\[([^\]]{2,80})\]\((https?://[^)\s]+)\)", markdown):
        if norm_key(text) == key or (len(key) >= 3 and norm_key(text).startswith(key) and len(text) <= len(name) + 8):
            if is_official_candidate(url, skip_host):
                return url.split("#")[0]
    return None


def github_repo_from_links(markdown: str, name: str) -> str | None:
    """First github.com/<owner>/<repo> link whose owner or repo matches the tool name."""
    key, tokens = norm_key(name), name_tokens(name)
    for m in re.finditer(r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", markdown):
        owner, repo = m.group(1), m.group(2).removesuffix(".git")
        if owner.lower() in GITHUB_NON_OWNERS or not repo:
            continue
        hay = norm_key(owner + repo)
        if (key and key in hay) or any(t in hay for t in tokens):
            return f"https://github.com/{owner}/{repo}"
    return None


def parse_row_reply(reply: str) -> tuple[str, str]:
    def field(label: str, nxt: str) -> str:
        m = re.search(rf"{label}\s*:\s*(.*?)(?=\n\s*{nxt}\s*:|\Z)", reply, re.I | re.S)
        text = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
        return "" if text in ("", "-", "NONE") else text[:300]

    return field("SIMILAR", "DIFFERENT"), field("DIFFERENT", "SIMILAR")


# ---- table output ------------------------------------------------------------------------------------------------
COLUMNS = [("name", "Name"), ("similar", "How it is similar"), ("different", "Key difference"),
           ("url", "URL"), ("github", "GitHub repo")]


def to_markdown(rows: list[dict]) -> str:
    def cell(v) -> str:
        return (str(v) if v else "-").replace("|", "/").replace("\n", " ")

    lines = ["| " + " | ".join(h for _, h in COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    lines += ["| " + " | ".join(cell(r.get(k)) for k, _ in COLUMNS) + " |" for r in rows]
    return "\n".join(lines) + "\n"


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([h for _, h in COLUMNS])
    for r in rows:
        w.writerow([r.get(k) or "" for k, _ in COLUMNS])
    return buf.getvalue()


# ---- search and page reading -------------------------------------------------------------------------------------
async def search_web(query: str, engines: list[str] | None = None, limit: int = 20) -> dict:
    """Query the local SearXNG (JSON API). Returns {results: [{url, title, content, engines}], failed: [[engine, why]]}."""
    params = {"q": query, "format": "json"}
    if engines:
        params["engines"] = ",".join(engines)
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(40, connect=5)) as c:
            r = await c.get(f"{SEARXNG_URL}/search", params=params)
    except httpx.HTTPError as e:
        raise cfg.ConfigError(f"SearXNG is not reachable at {SEARXNG_URL} ({type(e).__name__}). "
                              "Start it first (see README, 'Similar tools mode').") from e
    if r.status_code == 403:
        raise cfg.ConfigError("SearXNG refused the JSON format (HTTP 403): add 'json' to search.formats in its settings.yml.")
    r.raise_for_status()
    data = r.json()
    return {"results": [{"url": x.get("url", ""), "title": x.get("title", ""), "content": x.get("content", ""),
                         "engines": x.get("engines", [])} for x in data.get("results", [])[:limit]],
            "failed": [list(f) for f in data.get("unresponsive_engines", [])]}


async def read_page(url: str, out_root: Path) -> dict:
    """One page through the normal crawler. Returns {markdown, title, url} or {skipped: reason}."""
    from app.crawl import crawl_site  # lazy: loads Crawl4AI only when this mode runs

    res = await crawl_site(url, 1, 0, out_root=out_root)
    if not res["pages"]:
        return {"skipped": res["skipped"][0]["reason"] if res["skipped"] else "no content"}
    page = res["pages"][0]
    return {"markdown": (Path(res["out_dir"]) / page["file"]).read_text(encoding="utf-8"),
            "title": page["title"], "url": page["url"]}


def sentence_with(markdown: str, name: str, limit: int = 400) -> str:
    """The text line of a list page that mentions the tool (used when the tool's own page cannot be read)."""
    low = name.lower()
    for line in clean_text(markdown).split("\n"):
        if low in line.lower() and len(line.strip()) >= 30:
            return line.strip()[:limit]
    return ""


# ---- the pipeline ------------------------------------------------------------------------------------------------
async def run_similar(url: str, instruction: str = "", top_n: int = 10, out_root: Path | None = None,
                      search_delay: float = 3.0) -> dict:
    t0 = time.time()
    focus = instruction.strip() or "find the similar tools"
    out_root = (out_root or CRAWL_OUT_DIR) / f"similar_{time.strftime('%Y%m%d_%H%M%S')}"
    top_n = max(1, min(top_n, 10))
    result: dict = {"url": url, "instruction": focus, "rows": [], "skipped": [], "engines_used": [],
                    "engines_failed": [], "ok": False}
    failed: dict[str, str] = {}
    used: set[str] = set()

    def log(msg: str) -> None:
        print(f"[similar +{time.time() - t0:.0f}s] {msg}", flush=True)

    def note_search(res: dict) -> None:
        for f in res["failed"]:
            failed[f[0]] = f[1]
        for r in res["results"]:
            used.update(r["engines"])

    def finish() -> dict:
        result["engines_used"] = sorted(used)
        result["engines_failed"] = [{"engine": e, "reason": why} for e, why in sorted(failed.items())]
        result["seconds"] = round(time.time() - t0, 1)
        if len(result["rows"]) < 3 and not result.get("error"):
            result["error"] = f"Only {len(result['rows'])} similar tool(s) could be found; see the skipped list."
        result["ok"] = len(result["rows"]) >= 3
        if result["rows"]:
            out_root.mkdir(parents=True, exist_ok=True)
            (out_root / "similar.md").write_text(to_markdown(result["rows"]), encoding="utf-8")
            (out_root / "similar.csv").write_text(to_csv(result["rows"]), encoding="utf-8")
            result["out_dir"] = str(out_root)
        return result

    target_host = host_of(url)
    try:
        async with asyncio.timeout(SIMILAR_TOTAL_TIMEOUT_S):
            # 1. profile the target
            page = await read_page(url, out_root)
            fallback_name = host_of(url).split(".")[0].title() or "the tool"
            if "skipped" in page:
                result["skipped"].append({"url": url, "reason": f"target page: {page['skipped']}"})
                log(f"target page not readable ({page['skipped']}); profiling from the URL only")
                profile = parse_profile("", fallback_name, focus)
                if urlparse(url).path.strip("/"):
                    profile["name"] = urlparse(url).path.strip("/").split("/")[-1].replace("-", " ").title()
            else:
                try:
                    reply = await chat_once(PROFILE_PROMPT.format(title=page["title"][:120],
                                                                   chunk=clean_text(page["markdown"])[:1500]), max_tokens=120)
                except Exception as e:  # noqa: BLE001 - the page title is a usable fallback
                    log(f"profile call failed ({type(e).__name__}); using the page title")
                    reply = ""
                profile = parse_profile(reply, page["title"].split("|")[0].split(" - ")[0].strip() or fallback_name, focus)
            result["profile"] = profile
            log(f"target: {profile['name']} | {profile['does']} | keywords: {', '.join(profile['keywords'])}")

            # 2 and 3. multi-engine search
            hits: dict[str, dict] = {}
            gh_hits: list[dict] = []
            for query, engines in build_queries(profile):
                try:
                    res = await search_web(query, engines)
                except cfg.ConfigError:
                    raise
                except Exception as e:  # noqa: BLE001 - one failed query must not stop the run
                    log(f"search failed for {query!r}: {type(e).__name__}")
                    continue
                note_search(res)
                log(f"query {query!r} {engines or ''}: {len(res['results'])} results, failed {[f[0] for f in res['failed']]}")
                for r in res["results"]:
                    if engines == ["github"]:
                        gh_hits.append(r)
                        continue
                    h = hits.setdefault(r["url"], {**r, "engines": set()})
                    h["engines"] |= set(r["engines"])
                await asyncio.sleep(search_delay)
            if not hits:
                result["error"] = "Search returned no results (all engines failed or blocked). Try again later."
                return finish()

            # 4. candidate names from list pages
            scored = [(relevance(h, profile), len(h["engines"]), h) for h in hits.values()
                      if host_of(h["url"]) != target_host and not brand_host(h["url"], profile)
                      and urlparse(h["url"]).path.strip("/")
                      and not any(host_of(h["url"]).endswith(b) for b in ANTI_BOT_HOSTS)]
            scored = [t for t in scored if t[0] >= 2]
            scored.sort(key=lambda t: (-t[0], -t[1]))
            list_pages = [h for _, _, h in scored[:SIMILAR_MAX_LIST_PAGES]]
            log(f"{len(hits)} web results, {len(list_pages)} relevant list pages to read")
            per_page: list[dict] = []
            page_md: dict[str, str] = {}
            for h in list_pages:
                p = await read_page(h["url"], out_root)
                if "skipped" in p:
                    result["skipped"].append({"url": h["url"], "reason": p["skipped"]})
                    log(f"list page skipped {h['url']}: {p['skipped']}")
                    continue
                page_md[p["url"]] = p["markdown"]
                evidence = pick_evidence(p["markdown"], f"{profile['name']} alternatives similar {' '.join(profile['keywords'])}", 2400)
                try:
                    reply = await chat_once(NAMES_PROMPT.format(chunk=evidence, name=profile["name"], does=profile["does"]),
                                            max_tokens=80)
                except Exception as e:  # noqa: BLE001
                    result["skipped"].append({"url": h["url"], "reason": f"askg1 failed: {type(e).__name__}"})
                    continue
                names = extract_names(reply, evidence, profile["name"])
                per_page.append({"url": p["url"], "engines": sorted(h["engines"]), "names": names})
                log(f"{p['url']}: {names}")
            snips = "\n".join(f"{h['title']}. {h['content']}" for h in sorted(hits.values(), key=lambda h: -len(h["engines"]))
                              if not brand_host(h["url"], profile))[:2400]
            if snips:
                try:
                    reply = await chat_once(NAMES_PROMPT.format(chunk=snips, name=profile["name"], does=profile["does"]),
                                            max_tokens=80)
                    names = extract_names(reply, snips, profile["name"])
                    per_page.append({"url": "search-results", "engines": sorted({e for h in hits.values() for e in h["engines"]}),
                                     "names": names})
                    log(f"names from search snippets: {names}")
                except Exception as e:  # noqa: BLE001
                    log(f"snippet names failed: {type(e).__name__}")
            # A name seen only in search-result snippets is too weak (it let unrelated products in): a page must confirm it.
            candidates = [c for c in merge_names(per_page) if c["pages"] != ["search-results"]]
            for g in github_candidates(gh_hits, url):
                same = next((c for c in candidates if norm_key(c["name"]) == norm_key(g["name"])), None)
                if same:
                    same.update(url=g["url"], snippet=g["snippet"])
                    same["engines"] = sorted(set(same["engines"]) | {"github"})
                else:
                    candidates.append(g)
            candidates.sort(key=lambda c: -(2 * c["mentions"] + len(c["engines"])))
            candidates = candidates[:top_n]
            if not candidates:
                result["error"] = "No similar tool names were found on the pages read."
                return finish()
            log(f"{len(candidates)} candidates: {[c['name'] for c in candidates]}")

            # 5 and 6. per candidate: URL, GitHub repo, similar / different
            for c in candidates:
                name = c["name"]
                cand_url = None
                gh_known = c.get("url") if host_of(c.get("url", "")) == "github.com" else None
                snippet = c.get("snippet", "")
                for purl in c["pages"]:
                    md = page_md.get(purl, "")
                    cand_url = cand_url or find_link_for_name(md, name, host_of(purl))
                    snippet = snippet or sentence_with(md, name)
                if not cand_url and not gh_known:
                    try:
                        res = await search_web(f"{name} official site", None, 10)
                        note_search(res)
                        for r in res["results"]:
                            if official_match(name, r, profile):
                                cand_url = r["url"]
                                break
                        await asyncio.sleep(search_delay)
                    except cfg.ConfigError:
                        raise
                    except Exception as e:  # noqa: BLE001
                        log(f"official-site search failed for {name}: {type(e).__name__}")
                if cand_url:
                    snippet = snippet or next((h["content"] for h in hits.values()
                                               if host_of(h["url"]) == host_of(cand_url) and h["content"]), "")
                row = {"name": name, "similar": "", "different": "", "url": strip_tracking(cand_url) if cand_url else "",
                       "github": gh_known or "",
                       "mentions": c["mentions"], "engines": c["engines"], "source": "page"}
                text = ""
                if cand_url or gh_known:
                    p = await read_page(cand_url or gh_known, out_root)
                    if "skipped" in p:
                        result["skipped"].append({"url": cand_url or gh_known, "reason": f"{name}: {p['skipped']}"})
                        row["source"] = "snippet"
                    else:
                        text = pick_evidence(p["markdown"], f"{profile['name']} {profile['does']} {name}", 1400)
                        row["github"] = row["github"] or github_repo_from_links(p["markdown"], name) or ""
                else:
                    row["source"] = "snippet"
                if not row["github"]:
                    try:
                        res = await search_web(name, ["github"], 10)
                        note_search(res)
                        topic = terms(" ".join(profile["keywords"]) + " " + profile["does"])
                        for r in res["results"]:
                            m = re.match(r"https?://github\.com/([^/]+)/([^/#?]+)/?$", r["url"])
                            if m and repo_is_the_tool(name, m.group(1), m.group(2)) and repo_fits(
                                    f"{r['title']} {r['content']}", topic, f"{text} {snippet}"):
                                row["github"] = f"https://github.com/{m.group(1)}/{m.group(2)}"
                                break
                        await asyncio.sleep(search_delay)
                    except cfg.ConfigError:
                        raise
                    except Exception as e:  # noqa: BLE001
                        log(f"github search failed for {name}: {type(e).__name__}")
                if not row["url"] and row["github"]:
                    row["url"] = row["github"]
                if not (row["url"] or row["github"]):
                    result["skipped"].append({"url": name, "reason": f"{name}: no official site or repository found"})
                    log(f"dropped {name}: no official site or repository found")
                    continue
                for chunk in dict.fromkeys(x for x in (text, snippet) if x):
                    try:
                        reply = await chat_once(ROW_PROMPT.format(name=profile["name"], does=profile["does"], focus=focus,
                                                                  cand=name, chunk=chunk[:1500]), max_tokens=220)
                        sim, diff = clean_row(*parse_row_reply(reply), profile["does"])
                        src = f"{chunk} {name} {profile['name']} {profile['does']} {focus}"
                        row["similar"], row["different"] = grounded(sim, src), grounded(diff, src)
                    except Exception as e:  # noqa: BLE001
                        log(f"row call failed for {name}: {type(e).__name__}")
                    if row["similar"] or row["different"]:
                        break
                result["rows"].append(row)
                log(f"row {len(result['rows'])}/{len(candidates)}: {name} | url {bool(row['url'])} | github {bool(row['github'])}")
    except TimeoutError:
        result["skipped"].append({"url": url, "reason": f"time budget of {SIMILAR_TOTAL_TIMEOUT_S}s reached; partial table kept"})
        log("time budget reached; returning the rows found so far")
    return finish()
