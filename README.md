# askg1 Site Crawler (Crawl4AI) + optional Browser Use agent

Follows the internal guide "Local Private AI Browser Setup (askg1 + BrowserOS)". **askg1 is the only AI model used.**
Main job: crawl a site into clean Markdown files plus `index.json` (guide Step 7). Crawling itself uses no LLM.

## Setup (uv only, Python 3.11+)
```powershell
uv venv --python 3.12
.venv\Scripts\activate
uv pip install -e ".[dev]"          # or: uv pip install crawl4ai browser-use streamlit httpx beautifulsoup4 python-dotenv pytest
crawl4ai-setup ; crawl4ai-doctor     # guide 7a
uvx browser-use install              # only for the optional agent modes
copy .env.example .env               # then fill in ASKG1_* and a password
```
`.env` holds only `ASKG1_*` for AI. Any `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY` ... makes the app refuse to start.

## Verify askg1 first (guide Step 1)
```powershell
python scripts/verify_askg1.py --out reports/verify_askg1.md
```
Checks compliance, telemetry, `/models`, chat, tool calling, structured output and the real context window.

## Crawl4AI Docker service (guide 7d, recommended)
```powershell
docker run -d -p 127.0.0.1:11235:11235 --shm-size=1g `
  -e CRAWL4AI_API_TOKEN=<same value as in .env> -e CRAWL4AI_ALLOW_DEEP_CRAWL=true `
  --name crawl4ai unclecode/crawl4ai:latest
```
Differences from the guide's command, and why:
- `CRAWL4AI_API_TOKEN`: the current image listens only on the container's loopback unless a token is set, so the guide's exact command is unreachable from the host. This is a service auth token, not an AI key.
- `CRAWL4AI_ALLOW_DEEP_CRAWL=true`: the image rejects `deep_crawl_strategy` in API requests by default.
- `127.0.0.1:` prefix: keeps the port off the network.
No AI provider keys are passed to the container. With `CRAWL_BACKEND=auto` (default) the app uses the container when it answers
and otherwise falls back to the in-process library (about 10x slower on Windows in our tests, see the report).

## Run
```powershell
# CLI (always ends with a RESULT_JSON:<json> line)
python -u -m app.runner --mode crawl --url https://docs.browser-use.com --max-pages 10 --max-depth 2
python -u -m app.runner --mode crawl --url https://example.com --max-pages 1 --max-depth 0 --summarize
python -u -m app.runner --mode browse --url https://news.ycombinator.com --prompt "Return the title, url, points and comments of the top story as JSON" --headless
# UI
python -m streamlit run app/ui.py --server.port 8501
```
Crawl output: `reports/crawls/<timestamp>_<host>/docs_md/*.md`, `index.json` (url, title, file), `skipped.json` (reason per skipped URL).

## Modes
| Mode | Engine | askg1 use |
|---|---|---|
| `crawl` (main) | Crawl4AI (Docker service or library) | none; optional 2-sentence summaries of the first 5 pages |
| `browse` | Browser Use agent (guide Step 6); ask for JSON in the instruction if needed | every step (slow; tuned for a 4096-token window) |

## Rules enforced in code
Same-host crawl only, `robots.txt` honoured, page and depth caps (default 20 pages / depth 2, max 100 / 3), 2 concurrent pages,
overall time budget, pages behind CAPTCHA / 401 / 403 / 429 are reported and skipped (never bypassed), private and non-public
addresses (loopback, RFC1918, link-local, CGNAT) are refused as targets, UI is password-gated and refuses to start without one.

## Known constraints
- askg1 here has a **4096-token window** (the guide assumes 20000+). Verified by `scripts/verify_askg1.py`.
- askg1 degenerates under Browser Use's large `json_schema` output mode, so the agent uses unconstrained output with a compact action sheet.
- Each askg1 call takes seconds to about a minute, so the LLM-based modes are slow.

## Tests
`python -m pytest tests -q` (offline: URL guard, limits, parsing, crawl classification).
