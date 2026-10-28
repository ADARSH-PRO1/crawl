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

from app.config import (CRAWL_OUT_DIR, CRAWL_TOTAL_TIMEOUT_S, DEFAULT_CRAWL_PAGES, MAX_CRAWL_PAGES,  # noqa: E402
                        ConfigError, SETTINGS)  # noqa: F401  (importing config fails fast on forbidden keys)

RUNS_DIR = ROOT / "reports" / "runs"
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


def build_cmd(mode: str, url: str, prompt: str, steps: int, headless: bool, external: bool,
              max_pages: int = 20, max_depth: int = 2, summarize: bool = False) -> list[str]:
    cmd = [sys.executable, "-u", "-m", "app.runner", "--mode", mode, "--url", url]
    if mode == "crawl":
        cmd += ["--max-pages", str(max_pages), "--max-depth", str(max_depth)]
        if summarize:
            cmd.append("--summarize")
        return cmd
    cmd += ["--prompt", prompt, "--max-steps", str(steps)]
    if headless:
        cmd.append("--headless")
    if external:
        cmd.append("--allow-external")
    return cmd


def run_subprocess(cmd: list[str], log_box, timeout_s: int = RUN_TIMEOUT_S) -> tuple[dict | None, list[str]]:
    """Stream the runner's output live; returns (RESULT_JSON payload, log lines)."""
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
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
                continue
            lines.append(line)
            log_box.code("\n".join(lines[-60:]), language="text")
            if time.time() > deadline:
                proc.kill()
                lines.append(f"[ui] killed after {timeout_s}s timeout")
                break
    finally:
        proc.stdout.close()
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
        st.warning("Some pages are behind a login. The crawler does not log in, so those pages were skipped.")
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
st.title("askg1 Site Crawler & Browser Agent" if AI else "Site Crawler")
if AI:
    st.caption(f"Model: {SETTINGS.model} | context window: {SETTINGS.context_tokens} tokens | all inference via askg1")
else:
    st.caption("Crawl-only deployment: askg1 is not reachable from here, so AI summaries and the browser agent "
               "are switched off. Crawling uses no AI.")
if browser_status.startswith("failed"):
    st.error(f"Browser setup failed, crawling will not work: {browser_status}")

prompt, headless, steps, external, summarize = "Tell me the main heading of the page.", True, 8, False, False
with st.form("task"):
    modes = ["crawl", "browse"] if AI else ["crawl"]
    mode = st.radio("Mode", modes, horizontal=True,
                    help="crawl = whole site to Markdown files (no LLM), "
                         "browse = single-page browser agent (slow, optional; ask for JSON in the instruction)")
    url = st.text_input("Target URL", "https://docs.browser-use.com")
    st.caption("Crawl options (crawl mode)")
    k1, k2, k3 = st.columns(3)
    max_pages = k1.slider("Max pages", 1, MAX_CRAWL_PAGES, min(DEFAULT_CRAWL_PAGES, MAX_CRAWL_PAGES))
    max_depth = k2.slider("Link depth", 0, 3, 2)
    if AI:
        summarize = k3.toggle("askg1 summaries of first pages (slow)", value=False)
        st.caption("Agent options (browse mode)")
        prompt = st.text_area("Instruction", prompt, height=80)
        c1, c2, c3 = st.columns(3)
        headless = c1.toggle("Headless browser", value=True)
        steps = c2.slider("Max steps", 1, 30, 15)
        external = c3.toggle("Allow leaving the start domain", value=False)
    submitted = st.form_submit_button("Run", type="primary")

if submitted:
    if not url.strip():
        st.warning("Enter a URL.")
        st.stop()
    cmd = build_cmd(mode, url.strip(), prompt, steps, headless, external, max_pages, max_depth, summarize)
    timeout_s = (CRAWL_TOTAL_TIMEOUT_S + 120 + (360 if summarize else 0)) if mode == "crawl" else RUN_TIMEOUT_S
    with st.expander("Live log", expanded=True):
        log_box = st.empty()
        with st.spinner("Running..."):
            result, lines = run_subprocess(cmd, log_box, timeout_s)
    request = {"mode": mode, "url": url, "prompt": prompt, "max_steps": steps,
               "headless": headless, "allow_external": external, "max_pages": max_pages,
               "max_depth": max_depth, "summarize": summarize}
    if result is None:
        st.error("The runner exited without a result. See the log above.")
        save_run(mode, request, None, lines)
    else:
        path = save_run(mode, request, result, lines)
        show_result(result)
        st.download_button("Download run JSON", json.dumps(result, indent=2, ensure_ascii=False),
                           file_name=path.name, mime="application/json")
        st.caption(f"Saved to {path.relative_to(ROOT)}")
