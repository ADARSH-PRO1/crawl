"""Streamlit front end.  Run:  python -m streamlit run app/ui.py --server.port 8501"""
import hmac
import io
import json
import os
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # `streamlit run app/ui.py` only puts app/ on sys.path

import streamlit as st  # noqa: E402

from app.config import (SIMILAR_TOTAL_TIMEOUT_S, CRAWL_OUT_DIR, CRAWL_TOTAL_TIMEOUT_S, DEFAULT_CRAWL_PAGES, MAX_CRAWL_PAGES,  # noqa: E402
                        SCRAPE_DEFAULT_ITEMS, SCRAPE_DETAIL_MAX_URLS, SCRAPE_MAX_ITEMS, SCRAPE_MAX_PAGES,
                        SCRAPE_TOTAL_TIMEOUT_S,
                        ConfigError, SETTINGS)  # noqa: F401  (importing config fails fast on forbidden keys)

RUNS_DIR = ROOT / "reports" / "runs"
UPLOAD_DIR = ROOT / "reports" / "uploads"
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
RUN_TIMEOUT_S = 300

st.set_page_config(page_title="askg1 Browser Agent", page_icon=":mag:", layout="wide")


@st.cache_resource(show_spinner="Preparing the crawler browser (first start only)...")
def ensure_browser() -> str:
    """Hosted builds have no Docker and no pre-installed browser: fetch Playwright's Chromium once per process.

    Enabled with INSTALL_BROWSER=1 (set it in the hosting platform's secrets). Idempotent and fast when present.
    """
    if os.environ.get("INSTALL_BROWSER") != "1":
        return "skipped"
    proc = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                          capture_output=True, text=True, timeout=600)
    return "ok" if proc.returncode == 0 else f"failed: {(proc.stderr or proc.stdout)[-300:]}"


def gate() -> None:
    """Fail closed: with no APP_PASSWORD the app refuses to run (it may sit behind a public tunnel)."""
    expected = os.environ.get("APP_PASSWORD", "")
    if not expected:
        try:  # hosted deployments (Streamlit Cloud) keep it in st.secrets
            expected = str(st.secrets.get("APP_PASSWORD", ""))
        except Exception:  # noqa: BLE001 - no secrets file is the normal local case
            expected = ""
    if not expected:
        st.error("APP_PASSWORD is not set (.env locally, or app secrets when hosted); refusing to start.")
        st.stop()
    if st.session_state.get("authed"):
        return
    with st.form("login"):
        pw = st.text_input("Password", type="password")
        if st.form_submit_button("Enter"):
            if hmac.compare_digest(pw.encode(), expected.encode()):
                st.session_state["authed"] = True
                st.rerun()
            st.error("Wrong password.")
    st.stop()


def scrape_args(o: dict, instruction: str = "") -> list[str]:
    """Runner arguments for scrape mode from the form's options (all optional)."""
    args = ["--max-items", str(o.get("max_items", SCRAPE_DEFAULT_ITEMS)), "--load-more", o.get("load_more", "none"),
            "--scrape-pages", str(o.get("pages", 1))]
    if o.get("load_more_selector", "").strip():
        args += ["--load-more-selector", o["load_more_selector"].strip()]
    if o.get("each_url"):
        args += ["--scrape-each-url", "--detail-max", str(o.get("detail_max", 10))]
    if o.get("template", "").strip():
        args += ["--template", o["template"].strip()]
    if o.get("save_template", "").strip():
        args += ["--save-template", o["save_template"].strip()]
    if o.get("page_details"):
        args.append("--page-details")
    if o.get("html_file"):
        args += ["--html-file", str(o["html_file"])]
    if o.get("use_instruction") and instruction.strip():
        args += ["--prompt", instruction.strip()]
    return args


def build_cmd(mode: str, url: str, prompt: str, steps: int, headless: bool, external: bool,
              max_pages: int = 20, max_depth: int = 2, summarize: bool = False,
              top_n: int = 10, scrape: dict | None = None, show_browser: bool = False) -> list[str]:
    cmd = [sys.executable, "-u", "-m", "app.runner", "--mode", mode, "--url", url]
    if mode == "scrape":
        return cmd + scrape_args(scrape or {}, prompt)
    if mode == "similar":
        return cmd + ["--prompt", prompt, "--top", str(top_n)]
    if mode == "crawl":
        cmd += ["--max-pages", str(max_pages), "--max-depth", str(max_depth)]
        if summarize:
            cmd.append("--summarize")
        if show_browser:
            cmd.append("--show-browser")
        return cmd
    cmd += ["--prompt", prompt, "--max-steps", str(steps)]
    if headless:
        cmd.append("--headless")
    if external:
        cmd.append("--allow-external")
    return cmd


def login_fields(key: str) -> tuple[str, str]:
    """Optional site login (the user's own account). Used by crawl and browse; nothing is saved."""
    with st.expander("Site login (optional)"):
        st.caption("Only if you have your own account on this site (plain username + password form). The app logs in "
                   "once and then works with that session. Leave empty to use public pages only. Credentials go only to the "
                   "target site (never to the AI model), are not saved and are not written to the run report.")
        l1, l2 = st.columns(2)
        return (l1.text_input("Username / email", "", key=f"{key}_user"),
                l2.text_input("Password", "", type="password", key=f"{key}_pass"))


def run_subprocess(cmd: list[str], log_box, timeout_s: int = RUN_TIMEOUT_S,
                   extra_env: dict | None = None, detach: bool = False) -> tuple[dict | None, list[str]]:
    """Stream the runner's output live; returns (RESULT_JSON payload, log lines)."""
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", **(extra_env or {})}
    proc = subprocess.Popen(
        cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    lines: list[str] = []
    result = None
    deadline = time.time() + timeout_s
    try:
        for raw in proc.stdout:
            line = ANSI.sub("", raw.rstrip())
            if line.startswith("RESULT_JSON:"):
                try:
                    result = json.loads(line[len("RESULT_JSON:"):])
                except ValueError:
                    lines.append("[ui] could not parse RESULT_JSON")
                if detach and result is not None:
                    return result, lines  # leave the runner alive: it keeps the visible browser window open
                continue
            lines.append(line)
            log_box.code("\n".join(lines[-60:]), language="text")
            if time.time() > deadline:
                proc.kill()
                lines.append(f"[ui] killed after {timeout_s}s timeout")
                break
    finally:
        proc.stdout.close()
        if not (detach and result is not None):  # a detached runner keeps running: it holds the visible browser open
            proc.wait()
    return result, lines


def save_run(mode: str, cmd_args: dict, result: dict | None, lines: list[str]) -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNS_DIR / f"{time.strftime('%Y%m%d_%H%M%S')}_{mode}.json"
    path.write_text(
        json.dumps({"request": cmd_args, "result": result, "log_tail": lines[-80:]}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def crawl_zip(result: dict) -> bytes | None:
    """Zip of docs_md/ + index.json + skipped.json. Refuses paths outside the crawl output folder."""
    out = Path(result.get("out_dir", "")).resolve()
    if not out.is_dir() or CRAWL_OUT_DIR.resolve() not in out.parents:
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(out).as_posix())
    return buf.getvalue()


def show_crawl(result: dict) -> None:
    pages, skipped = result.get("pages", []), result.get("skipped", [])
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Pages saved", len(pages))
    m2.metric("Skipped", len(skipped))
    m3.metric("Crawl time (s)", result.get("crawl_seconds", "-"))
    m4.metric("Words", sum(p.get("words", 0) for p in pages))
    if result.get("timed_out"):
        st.warning("The crawl hit its time budget; partial results are shown.")
    if any(str(sk.get("reason", "")).startswith("login_required") for sk in skipped):
        st.warning("Some pages are behind a login and were skipped. Enter your own username and password under "
                   "'Site login' to crawl them." if not result.get("authenticated")
                   else "Some pages still showed a sign-in form even with the login, so they were skipped.")
    if pages:
        st.subheader("Crawled pages (index.json)")
        st.dataframe([{k: p[k] for k in ("url", "title", "file", "words", "depth")} for p in pages],
                     width="stretch", hide_index=True)
    if skipped:
        st.subheader("Skipped / blocked")
        st.dataframe(skipped, width="stretch", hide_index=True)
    if result.get("summaries"):
        st.subheader("askg1 summaries")
        for s in result["summaries"]:
            st.markdown(f"**{s['title'] or s['url']}**  \n{s.get('summary') or 'Error: ' + s.get('error', 'unknown')}")
    data = crawl_zip(result)
    if data:
        st.download_button("Download crawl (zip: Markdown files + index.json)", data,
                           file_name=Path(result["out_dir"]).name + ".zip", mime="application/zip")


def show_similar(result: dict) -> None:
    from app.similar import COLUMNS, to_csv, to_markdown

    profile = result.get("profile") or {}
    if profile:
        st.caption(f"Target: **{profile.get('name')}**: {profile.get('does') or '-'}")
    used = ", ".join(result.get("engines_used") or []) or "-"
    bad = ", ".join(f"{f['engine']} ({f['reason']})" for f in result.get("engines_failed", [])) or "none"
    st.caption(f"Search engines that answered: {used} | failed or blocked: {bad} | {result.get('seconds', '-')} s")
    rows = result.get("rows", [])
    if rows:
        st.subheader(f"Similar tools ({len(rows)})")
        st.dataframe([{h: r.get(k) or "" for k, h in COLUMNS} for r in rows], width="stretch", hide_index=True,
                     column_config={"URL": st.column_config.LinkColumn("URL"),
                                    "GitHub repo": st.column_config.LinkColumn("GitHub repo")})
        c1, c2 = st.columns(2)
        c1.download_button("Download table (CSV)", to_csv(rows), file_name="similar_tools.csv", mime="text/csv")
        c2.download_button("Download table (Markdown)", to_markdown(rows), file_name="similar_tools.md",
                           mime="text/markdown")
    if result.get("skipped"):
        with st.expander(f"Skipped pages ({len(result['skipped'])})"):
            st.dataframe(result["skipped"], width="stretch", hide_index=True)


def show_scrape(result: dict) -> None:
    from app.scrape import to_csv

    lists = result.get("lists") or []
    if not lists:
        return
    labels = [f"List {i + 1}: {len(x['rows'])} rows, {len(x['columns'])} columns ({x['selector'][-60:]})"
              for i, x in enumerate(lists)]
    pick = st.selectbox("Detected lists (best first)", range(len(lists)), index=result.get("chosen", 0),
                        format_func=lambda i: labels[i])
    chosen = lists[pick]
    st.caption(f"Found with the {result.get('method')} detector | backend: {result.get('backend')} | "
               f"{result.get('pages', 1)} page(s) | {result.get('seconds', '-')} s"
               + (" | list chosen by askg1" if result.get("picked_by") else "")
               + (f" | template: {result['template']}" if result.get("template") else "")
               + (f" | saved template: {result['saved_template']}" if result.get("saved_template") else ""))
    if result.get("template_note"):
        st.warning(result["template_note"])
    if result.get("details"):
        d = result["details"]
        st.caption(f"Scrape each URL: read {d['fetched']} linked page(s) from the '{d['column']}' column (limit {d['limit']}).")
    keep = st.multiselect("Columns to keep", chosen["columns"], default=chosen["columns"])
    with st.expander("Rename columns"):
        names = {c: st.text_input(c, c, key=f"rename_{pick}_{c}") or c for c in keep}
    columns = [names[c] for c in keep]
    rows = [{names[c]: r.get(c, "") for c in keep} for r in chosen["rows"]]
    st.subheader(f"Scraped table ({len(rows)} rows)")
    st.dataframe(rows, width="stretch", hide_index=True)
    with st.expander("Copy to clipboard (tab separated: pastes straight into Excel or Google Sheets)"):
        cell = lambda v: str(v).replace("\t", " ").replace("\n", " ")  # noqa: E731
        st.code("\n".join(["\t".join(columns)] + ["\t".join(cell(r.get(c, "")) for c in columns) for r in rows]),
                language="text")
    c1, c2 = st.columns(2)
    c1.download_button("Download CSV", to_csv(columns, rows), file_name="scrape.csv", mime="text/csv")
    c2.download_button("Download JSON", json.dumps(rows, indent=2, ensure_ascii=False), file_name="scrape.json",
                       mime="application/json")
    if result.get("method") == "browser":
        with st.expander("Save these choices as a template for this site"):
            name = st.text_input("Template name", key="tpl_name")
            if st.button("Save template") and name.strip():
                from app.scrape import save_template
                try:
                    save_template(result["url"], name, chosen["selector"], result.get("load_more"),
                                  [{"name": c, "as": names[c]} for c in keep])
                    st.success(f"Saved template {name.strip()!r}. Use it by name next time.")
                except ValueError as e:
                    st.error(str(e))
    if result.get("skipped"):
        with st.expander(f"Skipped ({len(result['skipped'])})"):
            st.dataframe(result["skipped"], width="stretch", hide_index=True)


def show_result(result: dict) -> None:
    mode = result.get("mode")
    if result.get("ok"):
        st.success(f"Finished in {result.get('seconds')} s")
    else:
        st.error(result.get("error") or "Run did not complete successfully.")
        if result.get("hint"):
            st.info(result["hint"])

    if mode == "crawl":
        show_crawl(result)
        return
    if mode == "similar":
        show_similar(result)
        return
    if mode == "scrape":
        show_scrape(result)
        return

    left, right = st.columns(2)
    with left:
        st.subheader("Result")
        st.write(result.get("raw") or "_No answer returned._")
    with right:
        st.subheader("Run metrics")
        keys = ("steps", "llm_calls", "peak_prompt_tokens", "context_limit", "completion_tokens",
                "context_overflow", "errors", "seconds")
        st.json({k: result[k] for k in keys if k in result})


# ---- page -------------------------------------------------------------------------------------
gate()
AI = SETTINGS.askg1_enabled
browser_status = ensure_browser()
header = st.container()  # filled below, once the mode is known: every mode is its own page with its own name
if browser_status.startswith("failed"):
    st.error(f"Browser setup failed, crawling will not work: {browser_status}")

prompt, headless, steps, external, summarize = "Tell me the main heading of the page.", True, 8, False, False
top_n = 10
login_user = login_pass = ""
show_browser = False
max_pages, max_depth = DEFAULT_CRAWL_PAGES, 2
scrape_opts: dict = {}
modes = ["crawl", "scrape", "browse", "similar"] if AI else ["crawl", "scrape"]
# The mode choice sits outside the form so that changing it redraws the page: only the chosen mode's options are shown.
mode = st.radio("Mode", modes, horizontal=True, key="mode",
                help="crawl = whole site to Markdown files (no LLM), "
                     "scrape = turn a listing page into a table (CSV / JSON), no LLM, "
                     "browse = single-page browser agent (slow, optional; ask for JSON in the instruction), "
                     "similar = give a tool's page, get a table of similar tools found by web search (needs local SearXNG)")
MODE_PAGES = {
    "crawl": ("Site Crawler", "Crawls a whole site into clean Markdown files plus index.json. Same site only, robots.txt honoured. No AI."),
    "scrape": ("Web Scraper", "Turns a listing page into a table you can download as CSV or JSON. No AI. Blocked and login pages are "
                              "reported, never bypassed."),
    "browse": ("Browser Agent", "An askg1 agent opens the page and follows your instruction one step at a time. Slow, best effort."),
    "similar": ("Similar Tools Finder", "Give a tool's page and get a table of similar tools found by web search."),
}
with header:
    st.title(MODE_PAGES[mode][0])
    st.caption(MODE_PAGES[mode][1])
    if AI:
        st.caption(f"Model: {SETTINGS.model} | context window: {SETTINGS.context_tokens} tokens | all inference via askg1")
    else:
        st.caption("askg1 is not reachable from this deployment, so the browser agent, similar-tools finder and AI "
                   "summaries are switched off.")
DEFAULT_URLS = {"crawl": "https://docs.browser-use.com", "scrape": "https://books.toscrape.com",
                "browse": "https://news.ycombinator.com", "similar": "https://github.com/browseros-ai/BrowserOS"}
upload = None
with st.form("task"):
    if mode == "scrape":
        url = st.text_input("Target URL", DEFAULT_URLS[mode], key="url_scrape",
                            help="Leave it empty when you upload a saved HTML file below")
        scrape_opts["max_items"] = st.slider("Max rows", 10, SCRAPE_MAX_ITEMS, SCRAPE_DEFAULT_ITEMS)
        s1, s2, s3 = st.columns(3)
        scrape_opts["load_more"] = s1.selectbox("Load more rows by", ["none", "scroll", "next", "more"],
                                                help="scroll = infinite scroll, next = next-page link or button, more = load-more button")
        scrape_opts["pages"] = s2.slider("Max pages / scrolls", 1, SCRAPE_MAX_PAGES, 5)
        scrape_opts["load_more_selector"] = s3.text_input("Button CSS selector (optional)", "",
                                                          help="Leave empty to find the Next / Load more button automatically")
        d1, d2 = st.columns(2)
        scrape_opts["each_url"] = d1.toggle("Scrape each URL (read the page behind every row's link)", value=False,
                                            help="Same site only, at most 30 pages, 2 at a time")
        scrape_opts["detail_max"] = d2.slider("Linked pages to read", 1, SCRAPE_DETAIL_MAX_URLS, 10)
        scrape_opts["page_details"] = st.toggle("Page details only (one row: title, text, label/value pairs of this single page)",
                                                value=False)
        upload = st.file_uploader("Or scrape a page you saved yourself (HTML file, e.g. a login-only page saved from your browser)",
                                  type=["html", "htm"], help="Nothing is fetched and no login is used. The Target URL, if filled, only makes relative links absolute.")
        t1, t2 = st.columns(2)
        scrape_opts["template"] = t1.text_input("Use saved template (name)", "",
                                                help="Reuses the list, load-more setting and columns saved for this site")
        scrape_opts["save_template"] = t2.text_input("Save this list as template (name)", "")
        if AI:
            prompt = st.text_input("Which list do you want? (optional)", "",
                                   help="Only matters when the page has several lists: askg1 reads one line per list and picks one, it never writes data")
            scrape_opts["use_instruction"] = bool(prompt.strip())
    elif mode == "crawl":
        url = st.text_input("Target URL", DEFAULT_URLS[mode], key="url_crawl")
        k1, k2, k3 = st.columns(3)
        max_pages = k1.slider("Max pages", 1, MAX_CRAWL_PAGES, min(DEFAULT_CRAWL_PAGES, MAX_CRAWL_PAGES))
        max_depth = k2.slider("Link depth", 0, 3, 2)
        if AI:
            summarize = k3.toggle("askg1 summaries of first pages (slow)", value=False)
        show_browser = st.toggle("Show browser window (watch the crawl; slower, runs on this computer only)", value=False,
                                 help="Opens a visible Chrome and visits one page at a time so you can see which pages are opened. Uses the in-process crawler instead of the Docker service.")
        login_user, login_pass = login_fields("crawl")
    elif mode == "browse":
        url = st.text_input("Target URL", DEFAULT_URLS[mode], key="url_browse")
        prompt = st.text_area("Instruction", prompt, height=80)
        c1, c2, c3 = st.columns(3)
        headless = c1.toggle("Headless browser", value=True)
        steps = c2.slider("Max steps", 1, 30, 15)
        external = c3.toggle("Allow leaving the start domain", value=False)
        login_user, login_pass = login_fields("browse")
    else:  # similar
        url = st.text_input("Target URL", DEFAULT_URLS[mode], key="url_similar")
        prompt = st.text_area("Instruction", "find the similar tools", height=80)
        top_n = st.slider("How many similar tools", 3, 10, 10)
    submitted = st.form_submit_button("Run", type="primary")

if submitted:
    if not url.strip() and not (mode == "scrape" and upload is not None):
        st.warning("Enter a URL.")
        st.stop()
    st.session_state.pop("scrape_result", None)
    if mode == "scrape" and upload is not None:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        saved = UPLOAD_DIR / f"{time.strftime('%Y%m%d_%H%M%S')}.html"
        saved.write_bytes(upload.getvalue()[:5_000_000])
        scrape_opts["html_file"] = str(saved)
    cmd = build_cmd(mode, url.strip(), prompt, steps, headless, external, max_pages, max_depth, summarize, top_n,
                    scrape_opts, show_browser)
    timeout_s = {"crawl": CRAWL_TOTAL_TIMEOUT_S + 120 + (360 if summarize else 0),
                 "similar": SIMILAR_TOTAL_TIMEOUT_S + 60,
                 "scrape": SCRAPE_TOTAL_TIMEOUT_S + 60}.get(mode, RUN_TIMEOUT_S)
    creds = {"CRAWL_USERNAME": login_user.strip(), "CRAWL_PASSWORD": login_pass} if mode in ("crawl", "browse") and login_user.strip() and login_pass else None
    with st.expander("Live log", expanded=True):
        log_box = st.empty()
        with st.spinner("Running..."):
            result, lines = run_subprocess(cmd, log_box, timeout_s, creds, detach=bool(mode == "crawl" and show_browser))
    request = {"mode": mode, "url": url, "prompt": prompt, "max_steps": steps,
               "headless": headless, "allow_external": external, "max_pages": max_pages,
               "max_depth": max_depth, "summarize": summarize, "top_n": top_n, "scrape": scrape_opts,
               "login": bool(creds)}
    if result is None:
        st.error("The runner exited without a result. See the log above.")
        save_run(mode, request, None, lines)
    else:
        path = save_run(mode, request, result, lines)
        if mode == "scrape":  # its list picker and column choices rerun the page, so keep the result across reruns
            st.session_state["scrape_result"] = result
        else:
            show_result(result)
        st.download_button("Download run JSON", json.dumps(result, indent=2, ensure_ascii=False),
                           file_name=path.name, mime="application/json")
        st.caption(f"Saved to {path.relative_to(ROOT)}")

if st.session_state.get("scrape_result"):
    show_result(st.session_state["scrape_result"])
