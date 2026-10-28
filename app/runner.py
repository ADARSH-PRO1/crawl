"""CLI entry:  python -u -m app.runner --mode crawl|browse --url URL [--max-pages N] [--prompt ...]

Logs stream to stdout/stderr; the final line is always  RESULT_JSON:<json>.
"""
import app.config as cfg  # noqa: F401  (must come first: enforces telemetry-off and the askg1-only rule)

import argparse
import asyncio
import json
import sys
import time
from urllib.parse import urlparse

from app import tasks
from app.config import SETTINGS, UrlError, avalidate_public_url, clamp_steps

OVERFLOW_MARKERS = ("exceeds the available context", "context size", "context length", "maximum context")


def emit(result: dict) -> None:
    print("RESULT_JSON:" + json.dumps(result, default=str, ensure_ascii=False), flush=True)


async def run_agent(args, result: dict) -> None:
    # Imported lazily so crawl mode never loads browser-use.
    from browser_use import Agent, BrowserProfile
    from browser_use.tools.service import Tools
    from app.llm import get_llm

    llm = get_llm()
    peak = {"prompt": 0, "completion": 0, "calls": 0}
    orig = llm.ainvoke

    async def tracked(*a, **k):  # record real prompt size per call so the report can prove the budget
        r = await orig(*a, **k)
        u = getattr(r, "usage", None)
        if u:
            peak["prompt"] = max(peak["prompt"], u.prompt_tokens or 0)
            peak["completion"] += u.completion_tokens or 0
        peak["calls"] += 1
        print(f"[llm] call {peak['calls']}: prompt_tokens={getattr(u, 'prompt_tokens', '?')}", flush=True)
        return r

    object.__setattr__(llm, "ainvoke", tracked)

    all_actions = list(Tools().registry.registry.actions)
    tools = Tools(exclude_actions=[a for a in all_actions if a not in tasks.ALLOWED_ACTIONS])

    host = urlparse(args.url).hostname
    profile = BrowserProfile(
        headless=args.headless,
        allowed_domains=None if args.allow_external else [host, f"*.{host}"],
        window_size={"width": 1024, "height": 700},
    )

    agent = Agent(
        task=tasks.browse_task(args.url, args.prompt),
        llm=llm,
        tools=tools,
        browser_profile=profile,
        override_system_message=tasks.SYSTEM_PROMPT,
        # --- context-budget controls (askg1 window = 4096 tokens) ---
        use_vision=False,
        flash_mode=True,                 # drops thinking/eval/next_goal fields from the reply schema
        use_thinking=False,
        enable_planning=False,           # no extra planning prompts
        use_judge=False,                 # no post-run LLM judge call
        message_compaction=False,        # compaction would trigger extra LLM summarization calls
        max_history_items=6,             # keep only the last few steps in the prompt
        max_clickable_elements_length=SETTINGS.dom_chars,  # hard cap on DOM text per step
        include_attributes=["href"],     # [] would fall back to the (larger) default attribute set
        max_actions_per_step=2,
        max_failures=2,
        llm_timeout=120,
        step_timeout=120,
        generate_gif=False,
        enable_signal_handler=False,
    )

    hist = await agent.run(max_steps=args.max_steps)
    raw = hist.final_result()
    errors = [e for e in hist.errors() if e]

    result.update(
        raw=raw, steps=hist.number_of_steps(), done=hist.is_done(), errors=errors[:5],
        peak_prompt_tokens=peak["prompt"], llm_calls=peak["calls"], completion_tokens=peak["completion"],
        context_limit=SETTINGS.context_tokens,
        context_overflow=any(m in e.lower() for e in errors for m in OVERFLOW_MARKERS),
    )
    if result["context_overflow"]:
        result["hint"] = ("Prompt exceeded askg1's 4096-token window. Use a simpler page/task, lower "
                          "ASKG1_DOM_CHARS, or ask the lead to raise the server context size.")

    # The agent can call done(success=False) to give up; that must not be reported as a successful run.
    result["ok"] = bool(raw) and result["done"] and hist.is_successful() is not False


async def run_crawl(args, result: dict) -> None:
    # Lazy imports: Crawl4AI is only loaded for crawl mode. The crawl itself makes no LLM calls.
    from app.crawl import crawl_site

    crawl = await crawl_site(args.url, args.max_pages, args.max_depth)
    result.update(crawl)
    if args.summarize and not SETTINGS.askg1_enabled:
        result["summary_note"] = "askg1 summaries skipped: askg1 is not configured in this deployment."
    elif args.summarize and crawl["pages"]:
        from app.extract import summarize_pages
        result["summaries"] = await summarize_pages(crawl["out_dir"], crawl["pages"])
    if not crawl["pages"]:
        reasons = sorted({s["reason"] for s in crawl["skipped"]})
        result["error"] = "No pages were crawled: " + "; ".join(reasons)[:300] if reasons else "No pages were crawled."


async def run(args) -> dict:
    t0 = time.time()
    result: dict = {"mode": args.mode, "url": args.url, "ok": False}
    try:
        await avalidate_public_url(args.url)
        args.max_steps = clamp_steps(args.max_steps)
        if args.mode == "crawl":
            await run_crawl(args, result)
        else:
            if not SETTINGS.askg1_enabled:
                raise cfg.ConfigError("The browser agent needs askg1, which is not configured in this deployment.")
            await run_agent(args, result)
    except (UrlError, cfg.ConfigError) as e:
        result["error"] = str(e)
    except Exception as e:  # noqa: BLE001 - always return a RESULT_JSON line to the UI
        result["error"] = f"{type(e).__name__}: {str(e)[:300]}"
        if any(m in str(e).lower() for m in OVERFLOW_MARKERS):
            result["context_overflow"] = True
    result["seconds"] = round(time.time() - t0, 1)
    return result


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="python -u -m app.runner")
    p.add_argument("--mode", choices=["crawl", "browse"], required=True)
    p.add_argument("--max-pages", type=int, default=None, help="crawl: page cap (default 20, max 100)")
    p.add_argument("--max-depth", type=int, default=None, help="crawl: link depth (default 2, max 3)")
    p.add_argument("--summarize", action="store_true", help="crawl: askg1 summaries for the first few pages (slow)")
    p.add_argument("--url", required=True)
    p.add_argument("--prompt", default="", help="browse: instruction for the agent (may ask for JSON)")
    p.add_argument("--max-steps", type=int, default=None, help="default 15, capped at 30")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--allow-external", action="store_true", help="let the agent leave the start domain")
    return p.parse_args(argv)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    emit(asyncio.run(run(parse_args())))
