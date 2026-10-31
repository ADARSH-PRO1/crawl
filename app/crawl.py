"""Site crawler (PDF Step 7, Crawl4AI): pages -> clean Markdown files + index.json.

No LLM is used here, so it is unaffected by askg1's 4096-token window. Politeness rules from the PDF:
robots.txt is honoured, the crawl stays on the start host, rates are low, and pages that block us
(CAPTCHA, 401/403/429, robots) are reported and skipped, never bypassed.
"""
import asyncio
import fnmatch
import json
import os
import re
import time
from contextlib import aclosing
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urldefrag, urljoin, urlparse

import httpx

import app.config as cfg
from app.config import (CRAWL_DELAY_SECONDS, CRAWL_MAX_PAGE_CHARS, CRAWL_OUT_DIR, CRAWL_PAGE_TIMEOUT_MS,
                        CRAWL_TOTAL_TIMEOUT_S, UrlError, avalidate_public_url, clamp_crawl, validate_public_url)

from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig  # noqa: E402

UA = "askg1-crawler/0.1 (internal prototype; respects robots.txt)"
VERBOSE = os.environ.get("CRAWL_VERBOSE") == "1"  # per-URL fetch/scrape timing from Crawl4AI, for diagnosing stalls
BLOCK_STATUS = {401, 403, 429, 451}
BLOCK_PHRASES = ("captcha", "verify you are human", "are you a robot", "just a moment", "attention required",
                 "access denied", "unusual traffic", "enable javascript and cookies")


def slugify(url: str, used: set[str]) -> str:
    p = urlparse(url)
    base = re.sub(r"[^a-zA-Z0-9]+", "-", f"{p.netloc}{p.path}").strip("-").lower()[:80] or "index"
    slug, n = base, 2
    while slug in used:
        slug, n = f"{base}-{n}", n + 1
    used.add(slug)
    return slug


def classify_failure(result) -> str:
    err = (result.error_message or "").lower()
    status = result.status_code
    if "robots" in err:
        return "robots_disallowed"
    if "anti-bot" in err or "blocked by" in err:
        return "blocked_by_site (anti-bot detected; not bypassed)"
    if status in BLOCK_STATUS:
        return f"blocked_by_site (HTTP {status})"
    if "timeout" in err:
        return "timeout"
    return f"failed (HTTP {status or '?'}): {(result.error_message or 'unknown error')[:120]}"


def looks_blocked(markdown: str) -> bool:
    low = markdown.lower()
    return len(markdown.split()) < 80 and any(p in low for p in BLOCK_PHRASES)


def looks_login(markdown: str) -> bool:
    """A short page that is mostly a sign-in form: the crawler was bounced to a login screen."""
    low = markdown.lower()
    return len(markdown.split()) < 120 and "password" in low and any(p in low for p in ("log in", "login", "sign in"))


def distinct_titles(pages: list[dict], md_dir: Path) -> None:
    """App UIs often share one <title> on every route. When a title repeats across pages, use the page's first
    heading instead, falling back to the URL path, so index.json entries can be told apart."""
    counts: dict[str, int] = {}
    for pg in pages:
        counts[pg["title"]] = counts.get(pg["title"], 0) + 1
    for pg in pages:
        if counts[pg["title"]] < 2:
            continue
        text = (md_dir.parent / pg["file"]).read_text(encoding="utf-8")
        m = re.search(r"^#{1,3}\s+(.+)$", text, re.M)
        path = urlparse(pg["url"])
        pg["title"] = (m.group(1).strip() if m
                       else (path.path.strip("/") or "/") + (f"?{path.query}" if path.query else ""))


def page_title(result, markdown: str) -> str:
    title = ((result.metadata or {}).get("title") or "").strip()
    if title:
        return title
    m = re.search(r"^#\s+(.+)$", markdown, re.M)
    return re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", m.group(1)).strip() if m else result.url  # drop link markup


UI_BOILERPLATE = {"Assistant", "Responses are generated using AI and may contain mistakes.",  # docs chat widget
                  "Was this page helpful?", "YesNo"}  # repeated feedback footer
COPY_PAGE_RE = re.compile(r"^(Copy page)+$")  # "Copy page" buttons render as repeated text


def clean_markdown(md: str) -> str:
    """Drop what is not page content: embedded framework JSON blobs (one huge line, e.g. Mintlify/Next.js data),
    docs chat-widget text and 'Copy page' buttons. PDF 7e asks for clean Markdown."""
    kept = []
    for line in md.split("\n"):
        s = line.strip()
        if len(s) > 2000 and s[0] in "{[" and s.count('":') >= 20:
            continue
        if s in UI_BOILERPLATE or COPY_PAGE_RE.match(s):
            continue
        kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def markdown_of(result) -> str:
    md = result.markdown
    return (getattr(md, "raw_markdown", None) or str(md or "")).strip()


EXCLUDED_TAGS = ["nav", "footer", "aside", "script", "style"]  # PDF 7e: drop navigation and footers
# A logged-in app keeps its routes in nav/aside sidebars; dropping those tags would also drop the links to follow.
SESSION_EXCLUDED_TAGS = ["footer", "script", "style"]
# With a session the crawler acts as the user: never follow links that end it, start OAuth flows or hit API endpoints.
UNSAFE_URL_GLOBS = ["*logout*", "*log-out*", "*signout*", "*sign-out*", "*/auth/*", "*/api/*"]
LOGIN_PATHS = ("/login", "/signin", "/sign-in", "/users/sign_in", "/accounts/login", "/user/login")
_FIELD_SKIP = ":not([type=password]):not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=submit]):not([type=button])"


def _host_matches(domain: str, host: str) -> bool:
    d = domain.lstrip(".").lower()
    return host == d or host.endswith("." + d)


async def login_session(start_url: str, username: str, password: str, headless: bool = True) -> tuple[dict, str]:
    """Log in with a plain username + password form using the caller's own credentials.

    Returns (Playwright storage state limited to the start host, URL to start crawling from). Looks for a password
    field on the start URL, then on common login paths of the same host. Anything else (SSO, 2FA, CAPTCHA) is
    reported as an error, never worked around. The password is only typed into that host's form; it is never logged.
    """
    from playwright.async_api import async_playwright

    host = urlparse(start_url).hostname
    candidates = [start_url] + [f"{urlparse(start_url).scheme}://{urlparse(start_url).netloc}{p}" for p in LOGIN_PATHS]
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless, slow_mo=0 if headless else 400)
        try:
            ctx = await browser.new_context(user_agent=UA)
            page = await ctx.new_page()
            page.set_default_timeout(CRAWL_PAGE_TIMEOUT_MS)
            found = False
            for i, cand in enumerate(candidates):
                try:
                    await page.goto(cand, wait_until="domcontentloaded")
                    await page.wait_for_timeout(1000)
                except Exception:  # noqa: BLE001 - a missing login path is normal; try the next one
                    continue
                if urlparse(page.url).hostname != host:
                    continue
                if await page.locator("input[type=password]:visible").count():
                    found, on_start = True, i == 0
                    break
            if not found:
                raise cfg.ConfigError("Login failed: no username/password form was found on this site (SSO, 2FA or a "
                                      "custom login flow is not supported).")
            form = page.locator("form:has(input[type=password]:visible)").first
            user_field = form.locator(f"input:visible{_FIELD_SKIP}").first
            if not await user_field.count():
                raise cfg.ConfigError("Login failed: the form has no username field the crawler can fill.")
            await user_field.fill(username)
            await form.locator("input[type=password]:visible").first.fill(password)
            submit = form.locator("button[type=submit], input[type=submit], button:not([type])").first
            if await submit.count():
                await submit.click()
            else:
                await form.locator("input[type=password]:visible").first.press("Enter")
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:  # noqa: BLE001 - some pages never go idle; the checks below decide
                pass
            await page.wait_for_timeout(1500)
            if urlparse(page.url).hostname != host:
                raise cfg.ConfigError("Login failed: the site sent the login to another host (SSO is not supported).")
            if await page.locator("input[type=password]:visible").count():
                raise cfg.ConfigError("Login failed: the site still shows the sign-in form (wrong username or password, "
                                      "or a CAPTCHA / 2FA step).")
            landing = page.url
            state = await ctx.storage_state()
            if not headless:
                await page.wait_for_timeout(2500)  # let the user see that the login worked
        finally:
            await browser.close()
    state["cookies"] = [c for c in state.get("cookies", []) if _host_matches(str(c.get("domain", "")), host)]
    state["origins"] = [o for o in state.get("origins", []) if urlparse(str(o.get("origin", ""))).hostname == host]
    # If the login form was the start page, crawl from where the login landed; otherwise from the URL the user gave.
    return state, (landing if on_start else start_url)


CRAWL4AI_URL = os.environ.get("CRAWL4AI_URL", "http://127.0.0.1:11235").rstrip("/")


async def choose_backend() -> str:
    """'docker' = the Crawl4AI service from PDF Step 7d (preferred), 'local' = in-process library.

    CRAWL_BACKEND=auto (default) uses the container when its /health answers. Measured on a 8-page docs crawl:
    ~25 s through the container vs 225-480 s locally on Windows (a multi-level stall in the in-process path).
    """
    pref = os.environ.get("CRAWL_BACKEND", "auto").lower()
    if pref == "local":
        return "local"
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=3) as c:
            if (await c.get(f"{CRAWL4AI_URL}/health")).status_code == 200:
                return "docker"
    except httpx.HTTPError:
        pass
    if pref == "docker":
        raise cfg.ConfigError(f"CRAWL_BACKEND=docker but no Crawl4AI service answers at {CRAWL4AI_URL}")
    return "local"


def _from_api(d: dict) -> SimpleNamespace:
    """Adapt a Crawl4AI service JSON result to the attribute shape the local library returns."""
    md = d.get("markdown")
    raw = md.get("raw_markdown") if isinstance(md, dict) else md
    return SimpleNamespace(
        url=d.get("url", ""), redirected_url=d.get("redirected_url"), success=bool(d.get("success")),
        status_code=d.get("status_code"), error_message=d.get("error_message"),
        markdown=SimpleNamespace(raw_markdown=raw or ""), metadata=d.get("metadata") or {},
        response_headers={str(k).lower(): v for k, v in (d.get("response_headers") or {}).items()},
    )


async def _docker_results(url: str, pages_cap: int, depth: int, host: str, session: dict | None = None):
    """Deep crawl through the container's /crawl/stream (NDJSON, one result per page as it completes)."""
    payload = {
        "urls": [url],
        # The service rejects a storage_state field on untrusted requests, so it is sent only for a session crawl.
        "browser_config": {"type": "BrowserConfig",
                           "params": {"headless": True, **({"storage_state": session} if session else {})}},
        "crawler_config": {"type": "CrawlerRunConfig", "params": {
            "cache_mode": "bypass", "check_robots_txt": True, "stream": True,
            "excluded_tags": SESSION_EXCLUDED_TAGS if session else EXCLUDED_TAGS,
            "remove_overlay_elements": True, "page_timeout": CRAWL_PAGE_TIMEOUT_MS,
            "deep_crawl_strategy": {"type": "BFSDeepCrawlStrategy", "params": {
                # +1: the container returns nothing at all for max_pages=1; our loop still stops at pages_cap.
                "max_depth": depth, "max_pages": pages_cap + 1, "include_external": False,
                # DomainFilter keeps the crawl on the start host; without it sibling subdomains are followed.
                "filter_chain": {"type": "FilterChain", "params": {"filters": [
                    {"type": "DomainFilter", "params": {"allowed_domains": [host]}},
                    {"type": "ContentTypeFilter", "params": {"allowed_types": ["text/html"]}},
                    *([{"type": "URLPatternFilter", "params": {"patterns": UNSAFE_URL_GLOBS, "use_glob": True,
                                                                 "reverse": True}}] if session else []),
                ]}}}},
        }},
    }
    token = os.environ.get("CRAWL4AI_API_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    timeout = httpx.Timeout(CRAWL_TOTAL_TIMEOUT_S + 30, connect=10)
    async with httpx.AsyncClient(trust_env=False, timeout=timeout, headers=headers) as c:
        async with c.stream("POST", f"{CRAWL4AI_URL}/crawl/stream", json=payload) as r:
            if r.status_code != 200:
                body = (await r.aread()).decode(errors="replace")[:300]
                raise RuntimeError(f"Crawl4AI service returned HTTP {r.status_code}: {body}")
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                d = json.loads(line)
                if "url" in d:
                    yield _from_api(d)


PAGE_HARD_TIMEOUT_S = CRAWL_PAGE_TIMEOUT_MS / 1000 + 15
HOLD_BROWSER_S = 300   # a visible browser stays open this long after the crawl unless the user closes it first
_HELD = None           # the visible crawler kept open after a "show browser" crawl


async def hold_browser(max_seconds: int = HOLD_BROWSER_S) -> None:
    """Keep the visible browser open for inspection until the user closes the window (or max_seconds), then clean up."""
    crawler = _HELD
    if crawler is None:
        return
    try:
        print(f"[crawl] browser stays open so you can look at it; close the window to finish (auto-closes in {max_seconds}s)",
              flush=True)
    except (OSError, ValueError):
        pass  # the UI stopped reading our output; the window still stays open
    bm = crawler.crawler_strategy.browser_manager
    t0 = time.time()
    try:
        while time.time() - t0 < max_seconds:
            b = getattr(bm, "browser", None)
            if b is not None and not b.is_connected():
                break
            await asyncio.sleep(1)
    finally:
        try:
            await crawler.close()
        except Exception:  # noqa: BLE001 - the user may already have closed the window
            pass
SKIP_EXT = (".pdf", ".zip", ".gz", ".rar", ".7z", ".exe", ".dmg", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
            ".css", ".js", ".json", ".xml", ".mp3", ".mp4", ".avi", ".mov", ".woff", ".woff2", ".ttf", ".csv", ".xlsx", ".docx")


def _norm_url(u: str) -> str:
    """One key per page: no fragment, no trailing slash (so 'site.com' and 'site.com/' are the same page)."""
    u, _ = urldefrag(u.strip())
    return u.rstrip("/") if urlparse(u).path in ("", "/") else u


def _wanted_link(u: str, host: str, session: bool) -> bool:
    pu = urlparse(u)
    if pu.scheme not in ("http", "https") or pu.hostname != host or pu.path.lower().endswith(SKIP_EXT):
        return False
    return not (session and any(fnmatch.fnmatch(u.lower(), g) for g in UNSAFE_URL_GLOBS))


async def _local_results(url: str, pages_cap: int, depth: int, host: str, delay: float, session: dict | None = None,
                         headless: bool = True):
    """Breadth-first crawl on the in-process Crawl4AI library (fallback when no container is running).

    Our own BFS instead of the library's deep-crawl strategy: that one can hang forever on a single page (the page
    timeout does not always fire), freezing the whole crawl. Here every page has a hard timeout, a stuck page is
    reported as `timeout` and skipped, and the crawl goes on. One page at a time when the browser is visible.
    """
    watch = {} if headless else {"session_id": "watch", "delay_before_return_html": 1.5}  # one stable tab, a pause per page
    run_cfg = CrawlerRunConfig(
        check_robots_txt=True,            # PDF 7b: respect site rules
        cache_mode=CacheMode.BYPASS,
        page_timeout=CRAWL_PAGE_TIMEOUT_MS,
        excluded_tags=SESSION_EXCLUDED_TAGS if session else EXCLUDED_TAGS,
        remove_overlay_elements=True,
        verbose=VERBOSE,
        **watch,
    )
    browser_cfg = BrowserConfig(headless=headless, user_agent=UA, verbose=VERBOSE, storage_state=session)  # no stealth: we do not evade blocks
    sem = asyncio.Semaphore(2 if headless else 1)  # low concurrency: keep crawl rates low

    global _HELD
    crawler = AsyncWebCrawler(config=browser_cfg)
    await crawler.start()
    try:
        async def fetch(u: str, d: int):
            async with sem:
                await asyncio.sleep(delay)  # PDF: keep crawl rates low
                try:
                    r = await asyncio.wait_for(crawler.arun(url=u, config=run_cfg), PAGE_HARD_TIMEOUT_S)
                except (asyncio.TimeoutError, TimeoutError):
                    r = SimpleNamespace(url=u, redirected_url=None, success=False, status_code=None,
                                        error_message="timeout: the page did not finish loading", links={},
                                        markdown=SimpleNamespace(raw_markdown=""), response_headers={})
                except Exception as e:  # noqa: BLE001 - one bad page must not end the crawl
                    r = SimpleNamespace(url=u, redirected_url=None, success=False, status_code=None,
                                        error_message=f"{type(e).__name__}: {str(e)[:100]}", links={},
                                        markdown=SimpleNamespace(raw_markdown=""), response_headers={})
                r.metadata = {**(getattr(r, "metadata", None) or {}), "depth": d}
                return r

        visited = {_norm_url(url)}
        level = [url]
        yielded = 0
        for d in range(depth + 1):
            if not level or yielded >= pages_cap:
                break
            level = level[:pages_cap - yielded]
            tasks = [asyncio.create_task(fetch(u, d)) for u in level]
            nxt: list[str] = []
            try:
                for fut in asyncio.as_completed(tasks):
                    r = await fut
                    yielded += 1
                    yield r
                    if d < depth and r.success:
                        links = (getattr(r, "links", None) or {}).get("internal", [])
                        for lk in links:
                            href = _norm_url(urljoin(r.url, lk.get("href", "") if isinstance(lk, dict) else str(lk)))
                            if href and href not in visited and _wanted_link(href, host, bool(session)):
                                visited.add(href)
                                nxt.append(href)
            finally:
                for t in tasks:
                    t.cancel()
            level = nxt
    finally:
        if headless:
            await crawler.close()
        else:
            _HELD = crawler  # a visible browser stays open after the crawl; hold_browser() closes it later


async def crawl_site(url: str, max_pages: int | None = None, max_depth: int | None = None,
                     out_root: Path | None = None, delay: float = CRAWL_DELAY_SECONDS,
                     credentials: tuple[str, str] | None = None, show_browser: bool = False) -> dict:
    """Crawl `url` (same host only). Returns a summary dict; files land in <out_dir>/docs_md/.

    `credentials` = (username, password) of the caller's own account on that site: the crawler logs in once through a
    plain username/password form and crawls with that session. Without credentials nothing is logged into.
    """
    t0 = time.time()
    pages_cap, depth = clamp_crawl(max_pages, max_depth)
    await avalidate_public_url(url)
    host = urlparse(url).hostname
    session = None
    if credentials and credentials[0] and credentials[1]:
        session, url = await login_session(url, *credentials, headless=not show_browser)  # raises ConfigError with a clear reason on failure

    out_dir = (out_root or CRAWL_OUT_DIR) / f"{time.strftime('%Y%m%d_%H%M%S')}_{re.sub(r'[^a-z0-9.-]', '', host.lower())}"
    md_dir = out_dir / "docs_md"
    md_dir.mkdir(parents=True, exist_ok=True)

    # The Crawl4AI container refuses cookies / storage_state / headers from API clients, so a logged-in crawl runs on
    # the in-process library; crawls without credentials keep using the container when it is up.
    backend = "local" if (session or show_browser) else await choose_backend()  # a visible browser cannot live in the container
    print(f"[crawl] backend: {backend}" + (f" ({CRAWL4AI_URL})" if backend == "docker" else " (in-process library)"),
          flush=True)

    pages: list[dict] = []
    skipped: list[dict] = []
    used: set[str] = set()
    saved_urls: set[str] = set()  # real (post-redirect) addresses already saved, to skip redirect duplicates
    seen = 0

    def log(msg: str) -> None:
        print(f"[crawl +{time.time() - t0:.0f}s] {msg}", flush=True)

    def process(result) -> None:
        final_url = result.redirected_url or result.url
        # Redirects must not leave the start host or reach non-public addresses.
        try:
            if urlparse(final_url).hostname != host:
                raise UrlError(f"redirected off-domain to {urlparse(final_url).hostname}")
            validate_public_url(final_url)
        except UrlError as e:
            skipped.append({"url": result.url, "reason": f"redirect_blocked: {e}"})
            log(f"{seen}/{pages_cap} skipped {result.url}: redirect blocked")
            return

        if not result.success:
            skipped.append({"url": result.url, "reason": classify_failure(result)})
        else:
            md = clean_markdown(markdown_of(result))
            ctype = ((result.response_headers or {}).get("content-type") or "text/html").lower()
            redirected = final_url.rstrip("/") != result.url.rstrip("/")
            # After a redirect the headers are those of the redirect hop (often text/plain), not of the page.
            if "html" not in ctype and not (redirected and md):
                skipped.append({"url": result.url, "reason": f"non_html ({ctype.split(';')[0]})"})
            elif (final_url if redirected else result.url) in saved_urls:
                skipped.append({"url": result.url, "reason": f"duplicate of {final_url}"})
            elif not md:
                skipped.append({"url": result.url, "reason": "empty_page"})
            elif looks_login(md):
                why = "login did not reach this page" if session else "no credentials supplied"
                skipped.append({"url": result.url, "reason": f"login_required ({why}); page is a sign-in form"})
            elif looks_blocked(md):
                skipped.append({"url": result.url, "reason": "blocked_by_site (CAPTCHA / bot wall detected; not bypassed)"})
            else:
                md = md[:CRAWL_MAX_PAGE_CHARS]
                page_url = final_url if redirected else result.url  # index the page's real address
                saved_urls.add(page_url)
                fname = f"{slugify(page_url, used)}.md"
                (md_dir / fname).write_text(f"<!-- source: {page_url} -->\n\n{md}\n", encoding="utf-8")
                pages.append({"url": page_url, "title": page_title(result, md), "file": f"docs_md/{fname}",
                              "words": len(md.split()), "depth": (result.metadata or {}).get("depth", 0)})
                log(f"{seen}/{pages_cap} ok {page_url} ({pages[-1]['words']} words)")
                return
        log(f"{seen}/{pages_cap} skipped {result.url}: {skipped[-1]['reason']}")

    timed_out = False
    log("logged in; crawling with the session" if session else "no credentials (public pages only)")
    results = (_docker_results(url, pages_cap, depth, host, session) if backend == "docker"
               else _local_results(url, pages_cap, depth, host, delay, session, headless=not show_browser))
    try:
        # Overall budget: any site can stall (slow servers, endless redirects); return what we have.
        async with asyncio.timeout(CRAWL_TOTAL_TIMEOUT_S), aclosing(results) as stream:
            async for result in stream:
                seen += 1
                process(result)
                if seen >= pages_cap:
                    break
    except TimeoutError:
        timed_out = True
        skipped.append({"url": url, "reason": f"crawl_time_budget_exceeded ({CRAWL_TOTAL_TIMEOUT_S}s); partial results kept"})
        log(f"time budget of {CRAWL_TOTAL_TIMEOUT_S}s reached; keeping {len(pages)} page(s)")

    distinct_titles(pages, md_dir)
    (out_dir / "index.json").write_text(json.dumps(pages, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "skipped.json").write_text(json.dumps(skipped, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "start_url": url, "host": host, "out_dir": str(out_dir), "pages": pages, "skipped": skipped,
        "pages_ok": len(pages), "pages_skipped": len(skipped), "max_pages": pages_cap, "max_depth": depth,
        "crawl_seconds": round(time.time() - t0, 1), "timed_out": timed_out, "backend": backend, "ok": bool(pages),
        "authenticated": session is not None,
    }
