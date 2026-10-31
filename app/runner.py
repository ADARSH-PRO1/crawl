"""CLI entry:  python -u -m app.runner --mode crawl|browse|similar|scrape --url URL [--max-pages N] [--prompt ...]

Logs stream to stdout/stderr; the final line is always  RESULT_JSON:<json>.
"""
import app.config as cfg  # noqa: F401  (must come first: enforces telemetry-off and the askg1-only rule)

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
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
    # Optional login with the user's own credentials, done here in code: the model never sees the password. The agent's
    # browser then starts already signed in (it is still told never to log in or submit forms itself).
    user, pw = os.environ.pop("CRAWL_USERNAME", ""), os.environ.pop("CRAWL_PASSWORD", "")
    session, start_url = None, args.url
    if user and pw:
        from app.crawl import login_session
        session, start_url = await login_session(args.url, user, pw, headless=args.headless)
        print("[browse] logged in; the agent starts with the session", flush=True)
    result["authenticated"] = session is not None
    profile = BrowserProfile(
        headless=args.headless,
        allowed_domains=None if args.allow_external else [host, f"*.{host}"],
        window_size={"width": 1024, "height": 700},
        **({"storage_state": session} if session else {}),
    )

    agent = Agent(
        task=tasks.browse_task(start_url, args.prompt),
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
        max_actions_per_step=1,
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

    # If no real page ever loaded (tab stayed on about:blank), the site refused the automated browser. The model
    # only saw an empty DOM, so whatever it answered is not about the site. Report that instead (never bypassed).
    if not any(u and not u.startswith("about:") for u in hist.urls()):
        result.update(ok=False, raw=None, error="The site did not load in the browser: it most likely blocks "
                      "automated access (bot firewall). The agent does not bypass blocks.",
                      hint="Try another site, or use crawl mode on pages that allow crawlers.")


async def run_crawl(args, result: dict) -> None:
    # Lazy imports: Crawl4AI is only loaded for crawl mode. The crawl itself makes no LLM calls.
    from app.crawl import crawl_site

    # Credentials come from the environment, never from argv (argv is visible in process lists and saved reports).
    user, pw = os.environ.pop("CRAWL_USERNAME", ""), os.environ.pop("CRAWL_PASSWORD", "")
    crawl = await crawl_site(args.url, args.max_pages, args.max_depth, credentials=(user, pw) if user and pw else None,
                             show_browser=args.show_browser)
    result.update(crawl)
    if args.summarize and not SETTINGS.askg1_enabled:
        result["summary_note"] = "askg1 summaries skipped: askg1 is not configured in this deployment."
    elif args.summarize and crawl["pages"]:
        from app.extract import summarize_pages
        result["summaries"] = await summarize_pages(crawl["out_dir"], crawl["pages"])
    if not crawl["pages"]:
        reasons = sorted({s["reason"] for s in crawl["skipped"]})
        result["error"] = "No pages were crawled: " + "; ".join(reasons)[:300] if reasons else "No pages were crawled."


async def run_similar_mode(args, result: dict) -> None:
    from app.similar import run_similar  # lazy: only similar mode needs it

    if not SETTINGS.askg1_enabled:
        raise cfg.ConfigError("Similar-tools mode needs askg1, which is not configured in this deployment.")
    await avalidate_public_url(args.url)
    result.update(await run_similar(args.url, args.prompt, args.top))


async def run_scrape_mode(args, result: dict) -> None:
    from app.scrape import MAX_HTML_BYTES, run_page_details, run_scrape, run_scrape_html  # lazy; no LLM calls

    if args.html_file:  # a page saved by the user: nothing is fetched
        path = Path(args.html_file)
        if not path.is_file() or path.stat().st_size > MAX_HTML_BYTES:
            raise cfg.ConfigError(f"--html-file must be an existing file of at most {MAX_HTML_BYTES // 1_000_000} MB.")
        result.update(await run_scrape_html(path.read_text(encoding="utf-8", errors="replace"), args.url,
                                            args.list_index, args.max_items, args.page_details))
        return
    await avalidate_public_url(args.url)
    if args.page_details:
        result.update(await run_page_details(args.url))
        return
    result.update(await run_scrape(args.url, args.list_index, args.max_items, args.scrape_pages,
                                   load_more_action=args.load_more, action_selector=args.load_more_selector,
                                   each_url=args.scrape_each_url, detail_column=args.detail_column,
                                   detail_max=args.detail_max, template=args.template, save_as=args.save_template,
                                   instruction=args.prompt))


async def run(args) -> dict:
    t0 = time.time()
    result: dict = {"mode": args.mode, "url": args.url, "ok": False}
    try:
        args.max_steps = clamp_steps(args.max_steps)
        if not args.url and not (args.mode == "scrape" and args.html_file):
            raise cfg.ConfigError("--url is required.")
        if args.mode == "similar":
            await run_similar_mode(args, result)
        elif args.mode == "scrape":
            await run_scrape_mode(args, result)
        elif args.mode == "crawl":
            await avalidate_public_url(args.url)
            await run_crawl(args, result)
        else:
            await avalidate_public_url(args.url)
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
    p.add_argument("--mode", choices=["crawl", "browse", "similar", "scrape"], required=True)
    p.add_argument("--list-index", type=int, default=0, help="scrape: which detected list to return (0 = best)")
    p.add_argument("--max-items", type=int, default=None, help="scrape: row cap (default 200, max 2000)")
    p.add_argument("--load-more", choices=["none", "scroll", "next", "more"], default="none",
                   help="scrape: how to reach more rows: scroll (infinite scroll), next (next-page link or button), more (load-more button)")
    p.add_argument("--load-more-selector", default="", help="scrape: CSS selector of the next / load-more button (default: found automatically)")
    p.add_argument("--scrape-each-url", action="store_true", help="scrape: also read the page behind each row's link (same site)")
    p.add_argument("--detail-column", default="", help="scrape: the link column to follow (default: the first column of links)")
    p.add_argument("--detail-max", type=int, default=None, help="scrape: how many linked pages to read (default and max 30)")
    p.add_argument("--page-details", action="store_true", help="scrape: one row with the details of this single page (title, text, label/value pairs)")
    p.add_argument("--html-file", default="", help="scrape: scrape this saved HTML file instead of fetching the URL (the URL, if given, only resolves relative links)")
    p.add_argument("--template", default="", help="scrape: reuse the saved template of this name for the site")
    p.add_argument("--save-template", default="", help="scrape: save the chosen list under this name for the site")
    p.add_argument("--scrape-pages", type=int, default=None, help="scrape: how many pages or scroll rounds (default 1)")
    p.add_argument("--max-pages", type=int, default=None, help="crawl: page cap (default 20, max 100)")
    p.add_argument("--max-depth", type=int, default=None, help="crawl: link depth (default 2, max 3)")
    p.add_argument("--show-browser", action="store_true", help="crawl: open a visible browser window (local backend) to watch the crawl")
    p.add_argument("--summarize", action="store_true", help="crawl: askg1 summaries for the first few pages (slow)")
    p.add_argument("--url", default="", help="the start URL (crawl, browse) or the tool's page (similar)")
    p.add_argument("--top", type=int, default=10, help="similar: how many similar tools to list (1 to 10)")
    p.add_argument("--prompt", default="", help="browse: instruction for the agent; similar: what to find (default: the similar tools); scrape: optional, askg1 uses it to pick the list")
    p.add_argument("--max-steps", type=int, default=None, help="default 15, capped at 30")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--allow-external", action="store_true", help="let the agent leave the start domain")
    return p.parse_args(argv)


async def main() -> None:
    result = await run(parse_args())
    emit(result)
    if "app.crawl" in sys.modules:  # a "show browser" crawl keeps its window open after the result is out
        await sys.modules["app.crawl"].hold_browser()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
