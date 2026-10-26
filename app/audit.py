"""Deterministic page audit. Runs entirely outside the agent loop: zero LLM context tokens.

The optional askg1 summary is a single call with a hard ~500-token prompt budget.
"""
import asyncio
import json
import time
from urllib.parse import urljoin, urldefrag, urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import avalidate_public_url, estimate_tokens
from app.llm import chat_once

UA = "askg1-audit/0.1 (+internal prototype)"
MAX_BYTES = 2_000_000
SECURITY_HEADERS = [
    "content-security-policy", "strict-transport-security", "x-frame-options",
    "x-content-type-options", "referrer-policy", "permissions-policy",
]
SUMMARY_PROMPT_TOKEN_BUDGET = 500


async def _guard_request(request: httpx.Request) -> None:
    """Runs for the first request and every redirect hop: no pivoting to internal hosts."""
    await avalidate_public_url(str(request.url))


async def _fetch(client: httpx.AsyncClient, url: str) -> tuple[httpx.Response, str, float]:
    t0 = time.perf_counter()
    async with client.stream("GET", url) as r:
        chunks, size = [], 0
        async for chunk in r.aiter_bytes():
            chunks.append(chunk)
            size += len(chunk)
            if size >= MAX_BYTES:
                break
        elapsed = time.perf_counter() - t0
        body = b"".join(chunks).decode(r.encoding or "utf-8", errors="replace")
        return r, body, elapsed


async def _check_link(client: httpx.AsyncClient, sem: asyncio.Semaphore, link: str) -> dict:
    async with sem:
        try:
            r = await client.head(link)
            if r.status_code in (403, 405, 501):
                async with client.stream("GET", link) as g:
                    return {"url": link, "status": g.status_code}
            return {"url": link, "status": r.status_code}
        except Exception as e:  # noqa: BLE001 - any failure means the link is not verifiably OK
            return {"url": link, "status": None, "error": f"{type(e).__name__}: {str(e)[:80]}"}


def _heading_order_problems(soup: BeautifulSoup) -> list[str]:
    levels = [int(h.name[1]) for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])]
    return [f"h{a} followed by h{b}" for a, b in zip(levels, levels[1:]) if b > a + 1][:5]


async def run_audit(url: str, max_links: int = 15) -> dict:
    out: dict = {"url": url}
    async with httpx.AsyncClient(
        follow_redirects=True, max_redirects=5, timeout=15.0, trust_env=False,
        headers={"User-Agent": UA}, event_hooks={"request": [_guard_request]},
    ) as client:
        try:
            r, html, elapsed = await _fetch(client, url)
        except Exception as e:  # noqa: BLE001
            return {**out, "ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}

        soup = BeautifulSoup(html, "html.parser")
        host = urlparse(str(r.url)).netloc
        hdrs = {k.lower(): v for k, v in r.headers.items()}

        title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
        meta_desc = soup.find("meta", attrs={"name": "description"})
        h1s = [h.get_text(" ", strip=True)[:120] for h in soup.find_all("h1")]
        imgs = soup.find_all("img")
        no_alt = [i.get("src", "")[:100] for i in imgs if i.get("alt") is None]

        links: list[str] = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            absolute = urldefrag(urljoin(str(r.url), href))[0]
            if urlparse(absolute).scheme in ("http", "https") and absolute not in links:
                links.append(absolute)
        internal = [x for x in links if urlparse(x).netloc == host]
        to_check = (internal + [x for x in links if x not in internal])[:max_links]
        sem = asyncio.Semaphore(5)
        checked = await asyncio.gather(*(_check_link(client, sem, x) for x in to_check))
        broken = [c for c in checked if c["status"] is None or c["status"] >= 400]

    missing_headers = [h for h in SECURITY_HEADERS if h not in hdrs]
    issues: list[str] = []
    if r.status_code >= 400:
        issues.append(f"HTTP status {r.status_code}")
    if not title:
        issues.append("missing <title>")
    if not meta_desc or not (meta_desc.get("content") or "").strip():
        issues.append("missing meta description")
    if len(h1s) != 1:
        issues.append(f"expected exactly 1 <h1>, found {len(h1s)}")
    if not soup.html or not soup.html.get("lang"):
        issues.append("missing <html lang>")
    if no_alt:
        issues.append(f"{len(no_alt)} image(s) without alt attribute")
    if broken:
        issues.append(f"{len(broken)} broken/unreachable link(s) of {len(to_check)} checked")
    if missing_headers:
        issues.append(f"missing security headers: {', '.join(missing_headers)}")
    issues += [f"heading level skipped: {p}" for p in _heading_order_problems(soup)]

    return {
        **out,
        "ok": True,
        "final_url": str(r.url),
        "http_status": r.status_code,
        "redirects": len(r.history),
        "response_seconds": round(elapsed, 2),
        "content_type": hdrs.get("content-type", ""),
        "title": title,
        "meta_description": (meta_desc.get("content") if meta_desc else "") or "",
        "h1": h1s,
        "images_total": len(imgs),
        "images_without_alt": no_alt[:10],
        "links_total": len(links),
        "links_checked": len(checked),
        "broken_links": broken[:10],
        "security_headers_present": [h for h in SECURITY_HEADERS if h in hdrs],
        "security_headers_missing": missing_headers,
        "issues": issues,
    }


async def summarize_audit(audit: dict) -> str:
    """One-shot askg1 summary. Facts are trimmed so the whole prompt stays under ~500 tokens."""
    instruction = "Summarize this web page audit in 3 short bullet points: worst problems first. No invented facts.\n\n"
    facts = {
        "status": audit.get("http_status"), "title": audit.get("title"), "h1_count": len(audit.get("h1", [])),
        "issues": audit.get("issues", []),
    }
    facts_text = json.dumps(facts, ensure_ascii=False)
    room_chars = (SUMMARY_PROMPT_TOKEN_BUDGET - estimate_tokens(instruction)) * 4
    prompt = instruction + facts_text[:room_chars]
    return await chat_once(prompt, max_tokens=200)
