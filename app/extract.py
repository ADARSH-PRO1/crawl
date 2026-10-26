"""Optional askg1 extraction on crawled pages (PDF 7c). Off by default: the crawl itself needs no LLM.

Each call sends one small chunk of one page, so the prompt stays well inside askg1's 4096-token window.
askg1 takes ~50 s per call, so the number of pages summarized is capped.
"""
import re
from pathlib import Path

from app.config import SETTINGS, estimate_tokens
from app.llm import chat_once

CHUNK_CHARS = 1200
MAX_SUMMARY_PAGES = 5
INSTRUCTION = ("Summarize this web page in 2 short sentences. Use only the text below; "
               "do not add facts.\n\nTitle: {title}\n\n{chunk}")


def first_chunk(markdown: str, max_chars: int = CHUNK_CHARS) -> str:
    """First max_chars of readable text: drops the source comment, link targets and images."""
    text = re.sub(r"<!--.*?-->", "", markdown, flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:max_chars]


async def summarize_pages(out_dir: str | Path, pages: list[dict], limit: int = MAX_SUMMARY_PAGES) -> list[dict]:
    """Returns [{url, title, summary | error, prompt_tokens_est}] for the first `limit` pages."""
    results: list[dict] = []
    for page in pages[:limit]:
        md = (Path(out_dir) / page["file"]).read_text(encoding="utf-8")
        prompt = INSTRUCTION.format(title=page["title"][:120], chunk=first_chunk(md))
        entry = {"url": page["url"], "title": page["title"], "prompt_tokens_est": estimate_tokens(prompt)}
        try:
            entry["summary"] = await chat_once(prompt, max_tokens=150)
        except Exception as e:  # noqa: BLE001 - one failed summary must not abort the rest
            entry["error"] = f"{type(e).__name__}: {str(e)[:150]}"
        print(f"[summary] {page['url']}: {'ok' if 'summary' in entry else entry['error']}", flush=True)
        results.append(entry)
    return results
