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
python -u -m app.runner --mode similar --url https://github.com/browseros-ai/BrowserOS --prompt "find the similar tools" --top 10
python -u -m app.runner --mode scrape --url https://news.ycombinator.com --load-more next --scrape-pages 3
python -u -m app.runner --mode browse --url https://news.ycombinator.com --prompt "Return the title, url, points and comments of the top story as JSON" --headless
# UI
python -m streamlit run app/ui.py --server.port 8501
```
Crawl output: `reports/crawls/<timestamp>_<host>/docs_md/*.md`, `index.json` (url, title, file), `skipped.json` (reason per skipped URL).

## Modes
| Mode | Engine | askg1 use |
|---|---|---|
| `crawl` (main) | Crawl4AI (Docker service or library) | none; optional 2-sentence summaries of the first 5 pages |
| `scrape` | Crawl4AI page + in-page list detector (`app/scrape_detect.js`) | none (optional: askg1 picks one of several lists) |
| `similar` | local SearXNG search (bing, yahoo, brave, github) + Crawl4AI page reads | about 16 small askg1 calls: profile, names from list pages, one per tool |
| `browse` | Browser Use agent (guide Step 6); ask for JSON in the instruction if needed | every step (slow; tuned for a 4096-token window) |

## Similar tools mode (a tool's page in, a table of similar tools out)
Not in the PDF (it has no search step), so it is a documented deviation: see `COMPLIANCE.md`. askg1 is still the only AI;
SearXNG is a search proxy, not an AI service. Give any page (a GitHub repo or a product site) and an instruction such as
"find the similar tools"; the result is a table of up to 10 tools, open source and closed source: name, how it is similar,
key difference, URL and GitHub repo (empty for closed-source tools).
Pipeline: the page is read and askg1 names the tool -> several searches (bing, yahoo, brave, plus the GitHub repository
search) -> the best list pages are read through the normal crawler (robots.txt honoured, blocked or login pages skipped,
never bypassed) and askg1 lists the tool names they mention -> a name is kept only if it appears in the page text and is
confirmed by a page -> for each tool the code finds its official site and GitHub repo from page links and search results
(a repo is accepted only if its name is the tool's name) -> one small askg1 call per tool writes the two comparison cells,
which are emptied if they repeat each other or contain a name that is not in the sources. URLs and repos are never written by
the model. A run takes about 3 to 7 minutes (askg1 is slow) and is local only: it needs SearXNG and askg1, and the hosted
build hides it.
```powershell
$env:SEARXNG_SECRET = -join ((48..57)+(97..102) | Get-Random -Count 64 | % {[char]$_})
docker run -d --name searxng -p 127.0.0.1:8888:8080 `
  -v "${PWD}\searxng\settings.yml:/etc/searxng/settings.yml:ro" -e SEARXNG_SECRET=$env:SEARXNG_SECRET searxng/searxng
curl "http://127.0.0.1:8888/search?q=test&format=json"      # must return JSON, not HTTP 403
```
`searxng/settings.yml` enables the JSON format and keeps the engines that answered from the development machine on
2026-10-28: bing, yahoo, brave (rate-limits quick bursts) and github. Google and Qwant answered "access denied" and
DuckDuckGo showed a CAPTCHA; those blocks are reported and never bypassed (PDF page 12). More engines or an official search
API need the lead's approval. Only search query text leaves the machine. Results are shallower than a large cloud model's.

## Scrape mode (a listing page in, a table out; no AI)
Not in the PDF, so it is a documented deviation: see `COMPLIANCE.md`. Modeled on the Easy Scraper Chrome extension, whose list detection
was read from its source (v1.4.1). A script runs inside the rendered page: every element with 3 or more similar children is a candidate
list (similar = same position of the first link, same classes, or same shape); candidates are scored by row count, area, fields per row
and visibility (and penalised when their rows differ a lot in height, which separates a real list from a page-layout wrapper); every
link, image and text node of a row becomes a column. The best list is returned first and the others can be picked.
```powershell
python -u -m app.runner --mode scrape --url https://books.toscrape.com                          # the best list, CSV/JSON in the UI
python -u -m app.runner --mode scrape --url <url> --load-more next --scrape-pages 5               # follow Next links / buttons
python -u -m app.runner --mode scrape --url <url> --load-more scroll --scrape-pages 10           # infinite scroll
python -u -m app.runner --mode scrape --url <url> --load-more more --load-more-selector "button.more"
python -u -m app.runner --mode scrape --url <url> --scrape-each-url --detail-max 10              # also read each row's linked page
python -u -m app.runner --mode scrape --url <url> --save-template shop                           # then: --template shop
```
- **Load more:** `next` clicks a Next button or follows a Next link (found by `rel=next`, label or aria-label, or `--load-more-selector`),
  `more` clicks a Load more button, `scroll` scrolls to the bottom. Each round adds only rows not seen before; it stops when nothing new
  appears, at `--scrape-pages` (max 20), at `--max-items` (max 2000) or at the time budget. Next links must be public and on the same site.
- **Scrape each URL:** reads the page behind each row's first link column (same site, max 30 pages, 2 at a time) and adds `page title`,
  `page heading`, `page description`, `page image`, `page text` and every label/value pair of the page (`dl`, two-cell table rows) as columns.
- **Templates:** `reports/scrape_templates.json` keeps, per site, the list selector, the load-more setting and the kept or renamed columns.
  `--template NAME` reuses it and detects again if the selector no longer matches.
- **askg1 (optional):** with `--prompt "the products"` and several lists on the page, askg1 gets one line per list (columns and first row,
  under 700 tokens) and answers with a list number. It never writes data.
- Scrape mode always uses the in-process Crawl4AI library, even when the Docker service is running: the service rejects any request that carries `js_code` (a security rule with no setting to turn it off), and the detector is a script. Crawl mode is unaffected.
- **Columns:** every link (`href`, and `title` when set), image (`src`, and `alt`) and text of a row. Full names that a page shortens
  on screen ("A Light in the ...") come from the `title` or `alt` attribute; a column that repeats an earlier one in every row is dropped.
- **Page details (`--page-details`):** one row for a single page: title, heading, description, image, text and every label/value
  pair (`dl`, two-cell table rows). Easy Scraper's "Scrape Page Details" tab.
- **Login-only pages (`--html-file page.html`, or the file upload in the UI):** save the page from your own logged-in browser
  (Save page as, or View source) and scrape the file. Nothing is fetched and no login is used, so no rule is bent. The URL, if given,
  only makes relative links absolute. Combine with `--page-details` for one row. Next-page, each-URL and templates need live pages.
- Copy to clipboard in the UI gives tab-separated text that pastes straight into Excel or Google Sheets.
- Without the browser detector (script blocked or failed) a BeautifulSoup version on the raw HTML is used, without layout, and nothing is
  scrolled or clicked.
- Limits: it runs in a separate crawler, not in your logged-in browser tab, so login-only or bot-protected pages are reported, not bypassed.
  Rows whose data sits in a sibling element (Hacker News keeps points and comments in the next table row) are not merged.

## Browser extension, bizfy Scraper (phases 1-5 of `reports/EXTENSION_PLAN.md`)
Easy Scraper's flow inside the user's own browser tab: click the toolbar icon on any page, get its main list as a table, hide or
rename columns, copy, download CSV or JSON. It reads the page the user is already looking at, so login-only and bot-protected pages
work without an HTML upload, and nothing is sent anywhere. One source tree builds two packages: **Chromium** (Chrome, Edge, Brave,
Opera, Vivaldi, Arc) and **Firefox** (140+, Android 142+). Safari comes last. The list detector is the same file the crawler uses
(`app/scrape_detect.js`, copied into the build as `detect.js`), so the app and the extension find the same lists.
```powershell
python scripts/build_extension.py            # dist/chromium and dist/firefox
python scripts/build_extension.py --zip      # also store-ready zips in dist/
npx web-ext lint --source-dir dist/firefox   # Firefox manifest check (0 errors, 0 warnings)
```
Install for a try-out: Chrome or Edge, `chrome://extensions` > Developer mode > Load unpacked > `dist/chromium`; Firefox,
`about:debugging` > This Firefox > Load Temporary Add-on > `dist/firefox/manifest.json`. Permissions: `activeTab`, `scripting`,
`storage` only, plus an optional per-site access request that appears only when "Click next page" is used.
"Action to load more items" (None / Scroll down / Click next page / Click load more, optional CSS selector, Pages 1-20) ports the app's
`load_more()` loop: duplicate rows are dropped, it stops when nothing new arrives or at 2000 rows / 20 pages / 7 minutes, waits 1 s
between steps, and never follows a link to another site (a redirect off the site also stops it). The tab stays on the last page it opened.
**Scrape each URL** opens the linked pages of the list (same site only, max 30 unique, 2 at a time, 1 s apart) in background tabs of
your own browser, so JavaScript pages and login-only pages work, adds title / heading / description / image / labelled values / text start as
columns and closes each tab again; a page that looks like a login wall or bot check is reported and skipped, never worked around. **Page details**
shows the same facts for the page itself as one row.
**Templates:** "Save as template" remembers the chosen list's selector, the load-more choice and the kept / renamed columns for the site (selectors
and names only, never page content) in the browser's own storage; the newest one is applied when you open that site again, and "None" detects the
list afresh. If a saved selector matches nothing any more the list is detected again and a note says so. Export / Import use the app's own
`scrape_templates.json` format, so files move both ways; an imported file is validated entry by entry. The Pages and Max URLs numbers are remembered.
**Packaging (phase 5):** `python scripts/build_extension.py --zip` writes the Chromium zip (Chrome and Edge) and the Firefox zip; tests check that a zip
has the manifest at its root, no test-only permission, a strict extension-pages CSP, and that the shipped code makes no network calls and loads no
remote code. `python scripts/bump_extension_version.py 1.0.0` sets the version in one place. `python scripts/make_store_assets.py` makes the store
screenshots and tile from local sample pages. Store text, permission justifications and the privacy policy are in `extension/store/LISTING.md` and
`extension/PRIVACY.md`; `extension/MANUAL_TEST.md` is the checklist for what a script cannot test. **Safari (phase 6)** is written up in
`extension/SAFARI.md` but not built: it needs a Mac with Xcode and an Apple Developer account.
Still to do by hand: the real icon artwork, a permanent Firefox add-on id, the contact email in the privacy policy, and the manual checks.
`tests/test_extension.py` loads the unpacked extension into Chromium and checks it end to end; Firefox gets the lint check and a
manual run (Firefox is not installed on the development machine).

## Rules enforced in code
Same-host crawl only, `robots.txt` honoured, page and depth caps (default 20 pages / depth 2, max 100 / 3), 2 concurrent pages,
overall time budget, pages behind CAPTCHA / 401 / 403 / 429 are reported and skipped (never bypassed), private and non-public
addresses (loopback, RFC1918, link-local, CGNAT) are refused as targets, UI is password-gated and refuses to start without one.

## Known constraints
- askg1 here has a **4096-token window** (the guide assumes 20000+). Verified by `scripts/verify_askg1.py`.
- askg1 degenerates under Browser Use's large `json_schema` output mode, so the agent uses unconstrained output with a compact action sheet.
- Each askg1 call takes seconds to about a minute, so the LLM-based modes are slow.

## Tests
`python -m pytest tests -q` (offline: URL guard, limits, parsing, crawl classification, scrape mode; the in-page detector tests in `tests/test_scrape_browser.py` need a local Chromium and skip without one).
