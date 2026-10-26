"""Site crawler (PDF Step 7, Crawl4AI): pages -> clean Markdown files + index.json.

No LLM is used here, so it is unaffected by askg1's 4096-token window. Politeness rules from the PDF:
robots.txt is honoured, the crawl stays on the start host, rates are low, and pages that block us
(CAPTCHA, 401/403/429, robots) are reported and skipped, never bypassed.
"""
import asyncio
import json
import os
import re
import time
from contextlib import aclosing
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx

import app.config as cfg
from app.config import (CRAWL_DELAY_SECONDS, CRAWL_MAX_PAGE_CHARS, CRAWL_OUT_DIR, CRAWL_PAGE_TIMEOUT_MS,
                        CRAWL_TOTAL_TIMEOUT_S, UrlError, avalidate_public_url, clamp_crawl, validate_public_url)

from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig  # noqa: E402
from crawl4ai.deep_crawling import (BFSDeepCrawlStrategy, ContentTypeFilter, DomainFilter,  # noqa: E402
                                          FilterChain)

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


def page_title(result, markdown: str) -> str:
    title = ((result.metadata or {}).get("title") or "").strip()
    if title:
        return title
    m = re.search(r"^#\s+(.+)$", markdown, re.M)
    return m.group(1).strip() if m else result.url


def markdown_of(result) -> str:
    md = result.markdown
    return (getattr(md, "raw_markdown", None) or str(md or "")).strip()


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


async def _docker_results(url: str, pages_cap: int, depth: int, host: str):
    """Deep crawl through the container's /crawl/stream (NDJSON, one result per page as it completes)."""
    payload = {
        "urls": [url],
        "browser_config": {"type": "BrowserConfig", "params": {"headless": True}},
        "crawler_config": {"type": "CrawlerRunConfig", "params": {
            "cache_mode": "bypass", "check_robots_txt": True, "stream": True,
            "excluded_tags": ["nav", "footer", "aside", "script", "style"],
            "remove_overlay_elements": True, "page_timeout": CRAWL_PAGE_TIMEOUT_MS,
            "deep_crawl_strategy": {"type": "BFSDeepCrawlStrategy", "params": {
                # +1: the container returns nothing at all for max_pages=1; our loop still stops at pages_cap.
                "max_depth": depth, "max_pages": pages_cap + 1, "include_external": False,
                # DomainFilter keeps the crawl on the start host; without it sibling subdomains are followed.
                "filter_chain": {"type": "FilterChain", "params": {"filters": [
                    {"type": "DomainFilter", "params": {"allowed_domains": [host]}},
                    {"type": "ContentTypeFilter", "params": {"allowed_types": ["text/html"]}},
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


async def _local_results(url: str, pages_cap: int, depth: int, host: str, delay: float):
    """Deep crawl with the in-process Crawl4AI library (fallback when no container is running)."""
    run_cfg = CrawlerRunConfig(
        deep_crawl_strategy=BFSDeepCrawlStrategy(
            max_depth=depth,
            max_pages=pages_cap,
            include_external=False,
            filter_chain=FilterChain([DomainFilter(allowed_domains=[host]),
                                      ContentTypeFilter(allowed_types=["text/html"])]),
        ),
        check_robots_txt=True,            # PDF 7b: respect site rules
        stream=True,                      # results arrive page by page so the UI can show progress
        cache_mode=CacheMode.BYPASS,
        page_timeout=CRAWL_PAGE_TIMEOUT_MS,
        mean_delay=delay,
        semaphore_count=2,                # low concurrency: keep crawl rates low
        excluded_tags=["nav", "footer", "aside", "script", "style"],  # PDF 7e: drop navigation and footers
        remove_overlay_elements=True,
        verbose=VERBOSE,
    )
    browser_cfg = BrowserConfig(headless=True, user_agent=UA, verbose=VERBOSE)  # no stealth: we do not evade blocks
    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        async for result in await crawler.arun(url=url, config=run_cfg):
            yield result


async def crawl_site(url: str, max_pages: int | None = None, max_depth: int | None = None,
                     out_root: Path | None = None, delay: float = CRAWL_DELAY_SECONDS) -> dict:
    """Crawl `url` (same host only). Returns a summary dict; files land in <out_dir>/docs_md/."""
    t0 = time.time()
    pages_cap, depth = clamp_crawl(max_pages, max_depth)
    await avalidate_public_url(url)
    host = urlparse(url).hostname

    out_dir = (out_root or CRAWL_OUT_DIR) / f"{time.strftime('%Y%m%d_%H%M%S')}_{re.sub(r'[^a-z0-9.-]', '', host.lower())}"
    md_dir = out_dir / "docs_md"
    md_dir.mkdir(parents=True, exist_ok=True)

    backend = await choose_backend()
    print(f"[crawl] backend: {backend}" + (f" ({CRAWL4AI_URL})" if backend == "docker" else " (in-process library)"),
          flush=True)

    pages: list[dict] = []
    skipped: list[dict] = []
    used: set[str] = set()
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
            md = markdown_of(result)
            ctype = ((result.response_headers or {}).get("content-type") or "text/html").lower()
            if "html" not in ctype:
                skipped.append({"url": result.url, "reason": f"non_html ({ctype.split(';')[0]})"})
            elif not md:
                skipped.append({"url": result.url, "reason": "empty_page"})
            elif looks_blocked(md):
                skipped.append({"url": result.url, "reason": "blocked_by_site (CAPTCHA / bot wall detected; not bypassed)"})
            else:
                md = md[:CRAWL_MAX_PAGE_CHARS]
                fname = f"{slugify(result.url, used)}.md"
                (md_dir / fname).write_text(f"<!-- source: {result.url} -->\n\n{md}\n", encoding="utf-8")
                pages.append({"url": result.url, "title": page_title(result, md), "file": f"docs_md/{fname}",
                              "words": len(md.split()), "depth": (result.metadata or {}).get("depth", 0)})
                log(f"{seen}/{pages_cap} ok {result.url} ({pages[-1]['words']} words)")
                return
        log(f"{seen}/{pages_cap} skipped {result.url}: {skipped[-1]['reason']}")

    timed_out = False
    results = (_docker_results(url, pages_cap, depth, host) if backend == "docker"
               else _local_results(url, pages_cap, depth, host, delay))
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

    (out_dir / "index.json").write_text(json.dumps(pages, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "skipped.json").write_text(json.dumps(skipped, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "start_url": url, "host": host, "out_dir": str(out_dir), "pages": pages, "skipped": skipped,
        "pages_ok": len(pages), "pages_skipped": len(skipped), "max_pages": pages_cap, "max_depth": depth,
        "crawl_seconds": round(time.time() - t0, 1), "timed_out": timed_out, "backend": backend, "ok": bool(pages),
    }
