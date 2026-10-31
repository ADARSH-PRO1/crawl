"""The browser extension, end to end in a real Chromium with the unpacked extension loaded (no network).

A toolbar click cannot be automated, so the test build adds <all_urls> and the scraper window is opened directly on a
fixture tab. Skips when no Chromium is available. Kept in its own module: the synchronous Playwright API leaves an event
loop running in the thread, which breaks asyncio.run() in other modules while it is active (the fixtures are module scoped).
"""
import functools
import http.server
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import scrape  # noqa: E402
from test_scrape_browser import CARDS, TABLE  # noqa: E402

spec = importlib.util.spec_from_file_location("build_extension", ROOT / "scripts" / "build_extension.py")
build_extension = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_extension)

UNICODE_PAGE = ("<html><head><meta charset=\"utf-8\"></head><body><ul>" + "".join(
    f'<li class="p"><a href="/p/{i}">Café “no. {i}”, large</a><span class="pr">£{i}.50</span></li>'
    for i in range(1, 5)) + "</ul></body></html>")
NO_LIST = "<html><body><h1>Nothing repeats here</h1><p>Just a paragraph of text.</p></body></html>"


def li(i, style=""):
    return f'<li class="p" style="{style}"><a href="/p/{i}">Item {i}</a><span class="pr">£{i}</span></li>'


def paged(n, total, link="next{}.html", prefix="next"):
    """Page n of a chain: four items and, except on the last page, a rel=next link."""
    items = "".join(li(i) for i in range(4 * (n - 1) + 1, 4 * n + 1))
    nxt = f'<p><a rel="next" href="{link.format(n + 1)}">Next</a></p>' if n < total else ""
    return f"<html><head><meta charset=\"utf-8\"></head><body><h1>Page {n}</h1><ul>{items}</ul>{nxt}</body></html>"


def hn(n):
    """Hacker News shape: each story is three table rows (story, points line, spacer); a More link leads on."""
    rows = "".join(f'<tr class="athing"><td class="rank">{i}.</td><td class="vote">&#9650;</td><td class="title"><a href="/i/{i}">Story {i}</a></td></tr>'
                   f'<tr><td colspan="2"></td><td class="subtext">{i} points by user</td></tr><tr class="spacer" style="height:5px"></tr>'
                   for i in range(4 * (n - 1) + 1, 4 * n + 1))
    more = '<a class="morelink" href="hn2.html">More</a>' if n == 1 else ""
    return f"<html><body><table><tbody>{rows}</tbody></table>{more}</body></html>"


def detail(n, js=False):
    """A product page: labelled values in a list and a table, a sidebar that must not leak into the text."""
    h1 = f"<h1 id=h></h1><script>document.getElementById('h').textContent='Widget {n} (JS)'</script>" if js else f"<h1>Widget {n}</h1>"
    return (f'<html><head><meta charset="utf-8"><title>Widget {n} | Shop</title><meta name="description" content="Description {n}"></head>'
            f"<body><nav>MENU</nav><main>{h1}<dl><dt>Colour:</dt><dd>Red {n}</dd></dl>"
            f"<table><tr><th>Weight</th><td>{n} kg</td></tr></table><p>Body text {n}.</p><aside>SIDE JUNK</aside></main></body></html>")


BLOCKED = "<html><head><title>Just a moment...</title></head><body><p>Verify you are human to continue.</p></body></html>"


def pages_html(fn):
    return "<html><body><ul id=l>" + "".join(li(i, "height:300px") for i in range(1, 5)) + f"</ul>{fn}</body></html>"


# a button that adds four items per click, twice (then removes itself)
MORE_PAGE = pages_html("""<button id=m>Load more</button><script>let n=4,c=0;m.onclick=()=>{for(let k=0;k<4;k++){n++;
l.insertAdjacentHTML('beforeend','<li class="p"><a href="/p/'+n+'">Item '+n+'</a><span class="pr">£'+n+'</span></li>')}
if(++c>=2)m.remove()}</script>""")
# the same button, but it only ever re-adds the items already shown
DUP_PAGE = pages_html("""<button id=m>Load more</button><script>m.onclick=()=>{for(let k=1;k<=4;k++)
l.insertAdjacentHTML('beforeend','<li class="p"><a href="/p/'+k+'">Item '+k+'</a><span class="pr">£'+k+'</span></li>')}</script>""")
# infinite scroll: reaching the bottom adds four items, up to twelve
SCROLL_PAGE = pages_html("""<script>let n=4;addEventListener('scroll',()=>{if(n<12&&innerHeight+scrollY>=document.body.scrollHeight-50){
for(let k=0;k<4;k++){n++;l.insertAdjacentHTML('beforeend','<li class="p" style="height:300px"><a href="/p/'+n+'">Item '+n+'</a><span class="pr">£'+n+'</span></li>')}}})</script>""")


# ---- pure helpers (no browser) ------------------------------------------------------------------------------------
def test_manifests_are_valid_for_both_browser_families(tmp_path):
    built = build_extension.build(tmp_path, make_zip=False)
    chrome = json.loads((built["chromium"] / "manifest.json").read_text(encoding="utf-8"))
    firefox = json.loads((built["firefox"] / "manifest.json").read_text(encoding="utf-8"))
    for m in (chrome, firefox):
        assert m["manifest_version"] == 3 and set(m["permissions"]) == {"activeTab", "scripting", "storage"}
        assert "host_permissions" not in m  # no blanket access in anything we ship
        assert m["optional_host_permissions"] == ["*://*/*"]  # asked per site, only for "Click next page"
    assert chrome["background"] == {"service_worker": "background.js"}
    assert firefox["background"] == {"scripts": ["background.js"]} and firefox["browser_specific_settings"]["gecko"]["id"]
    for folder in built.values():
        for icon in (16, 32, 48, 128):
            assert (folder / "icons" / f"{icon}.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_the_extension_uses_the_same_detector_as_the_crawler(tmp_path):
    built = build_extension.build(tmp_path, targets=("chromium",))
    detector = (built["chromium"] / "detect.js").read_text(encoding="utf-8")
    source = (ROOT / "app" / "scrape_detect.js").read_text(encoding="utf-8")
    assert all(line.strip() in detector for line in source.splitlines() if line.strip())  # nothing dropped or edited


def test_the_test_build_is_the_only_one_with_blanket_access(tmp_path):
    built = build_extension.build(tmp_path, targets=("chromium",), test=True)
    assert json.loads((built["chromium"] / "manifest.json").read_text(encoding="utf-8"))["host_permissions"] == ["<all_urls>"]


# ---- the extension in a real browser -------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("site")
    files = {"table.html": TABLE, "cards.html": CARDS, "unicode.html": UNICODE_PAGE, "empty.html": NO_LIST,
             "more.html": MORE_PAGE, "dup.html": DUP_PAGE, "custom.html": MORE_PAGE.replace(">Load more<", ">Give me more<"), "scroll.html": SCROLL_PAGE,
             "loopa.html": paged(1, 9, "loopb.html").replace("Item 1", "A1"),  # a -> b -> a
             "loopb.html": paged(2, 9, "loopa.html")}
    files.update({f"next{n}.html": paged(n, 3) for n in range(1, 4)})
    files["arrow1.html"] = paged(1, 3, "arrow2.html").replace(' rel="next"', "").replace(">Next<", "><span>Next</span> →<")  # quotes.toscrape: no rel
    files["arrow2.html"] = paged(2, 3, "next3.html")
    files.update({f"hn{n}.html": hn(n) for n in (1, 2)})
    files.update({f"long{n}.html": paged(n, 8, "long{}.html") for n in range(1, 9)})
    for name, html in files.items():
        (root / name).write_text(html, encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    (root / "d").mkdir()
    for n in (1, 2, 3):
        (root / "d" / f"{n}.html").write_text(detail(n, js=n == 3), encoding="utf-8")
    (root / "d" / "4.html").write_text(BLOCKED, encoding="utf-8")
    links = [f"/d/{n}.html" for n in (1, 2, 3, 4)] + [f"http://localhost:{port}/d/1.html"]  # the last is another site
    (root / "catalog.html").write_text('<html><head><meta charset="utf-8"></head><body><ul>' + "".join(
        f'<li class="p"><a href="{h}">Item {i}</a><span class="pr">£{i}</span></li>' for i, h in enumerate(links, 1)) + "</ul></body></html>",
        encoding="utf-8")
    # "localhost" is a different site from 127.0.0.1: the next link points there
    (root / "other.html").write_text(paged(1, 3, f"http://localhost:{port}/next{{}}.html"), encoding="utf-8")
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture(scope="module")
def browser(tmp_path_factory):
    sync_api = pytest.importorskip("playwright.sync_api")
    ext = build_extension.build(tmp_path_factory.mktemp("ext"), targets=("chromium",), test=True)["chromium"]
    pw = sync_api.sync_playwright().start()
    try:
        ctx = pw.chromium.launch_persistent_context(
            str(tmp_path_factory.mktemp("profile")), channel="chromium", headless=True, accept_downloads=True,
            args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"])
    except Exception as e:  # noqa: BLE001 - no Chromium here
        pw.stop()
        pytest.skip(f"no Chromium that can load extensions: {e}")
    worker = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=15000)
    ext_id = worker.url.split("/")[2]
    yield ctx, worker, ext_id
    ctx.close()
    pw.stop()


def open_scraper(browser, site_url, path, keep_storage=False):
    """Open the page, then the scraper window for that tab, like the toolbar click would.
    The extension's saved templates and settings are wiped first (every fixture page is on the same host), unless a test
    needs them to survive from an earlier window."""
    ctx, worker, ext_id = browser
    if not keep_storage:
        worker.evaluate("() => chrome.storage.local.clear()")
    page = ctx.new_page()
    page.goto(f"{site_url}/{path}")
    tab_id = worker.evaluate("async (url) => (await chrome.tabs.query({})).find((t) => t.url === url).id",
                             f"{site_url}/{path}")
    ui = ctx.new_page()
    ui.goto(f"chrome-extension://{ext_id}/ui/ui.html?tabId={tab_id}")
    return page, ui


def grid(ui):
    ui.wait_for_selector("#grid tbody tr, #message:not([hidden])")
    heads = ui.eval_on_selector_all("#grid thead input", "els => els.map(e => e.value)")
    rows = ui.eval_on_selector_all("#grid tbody tr", "trs => trs.map(tr => [...tr.querySelectorAll('td')].slice(1).map(td => td.textContent))")
    return heads, rows


def test_a_table_page_gives_the_same_columns_as_the_app(browser, site):
    page, ui = open_scraper(browser, site, "table.html")
    heads, rows = grid(ui)
    assert heads == ["underline href", "underline", "pop"] and len(rows) == 4
    assert rows[0] == [f"{site}/c/china", "China", "1,409"]
    assert ui.inner_text("#stat-items") == "4" and ui.inner_text("#stat-cols") == "3"
    page.close(); ui.close()


def test_the_extension_and_the_crawler_find_identical_tables(browser, site):
    """Same detector on the same page: the app's table_of() and the extension's table must match."""
    page, ui = open_scraper(browser, site, "cards.html")
    heads, rows = grid(ui)
    page.evaluate("() => { window.__scrapeArgs = { maxItems: 100, maxLists: 5 }; }")
    page.evaluate("() => {" + scrape.build_js("", 100) + "}")
    page.wait_for_selector("#__scrape_result", state="attached")
    expected = scrape.table_of(scrape.parse_result(page.content())["lists"][0])
    assert heads == expected["columns"]
    assert rows == [[r.get(c, "") for c in expected["columns"]] for r in expected["rows"]]
    page.close(); ui.close()


def test_hiding_and_renaming_columns_changes_the_table_and_the_exports(browser, site):
    page, ui = open_scraper(browser, site, "table.html")
    grid(ui)
    ui.fill("#grid thead th:nth-child(3) input", "Country")
    ui.press("#grid thead th:nth-child(3) input", "Tab")
    ui.click("#grid thead th:nth-child(2) .x")  # hide the link column
    heads, rows = grid(ui)
    assert heads == ["Country", "pop"] and rows[1] == ["India", "1,392"]
    assert ui.inner_text("#stat-cols") == "2" and "Hidden columns" in ui.inner_text("#hidden-cols")
    with ui.expect_download() as d:
        ui.click("#btn-csv")
    text = Path(d.value.path()).read_bytes().decode("utf-8")
    assert text.startswith("﻿Country,pop\r\nChina,\"1,409\"\r\n")  # BOM for Excel, comma cell quoted
    assert d.value.suggested_filename.startswith("scrape-127.0.0.1-") and d.value.suggested_filename.endswith(".csv")
    with ui.expect_download() as d:
        ui.click("#btn-json")
    assert json.loads(Path(d.value.path()).read_text(encoding="utf-8"))[0] == {"Country": "China", "pop": "1,409"}
    ui.click("#hidden-cols button")  # Show all
    assert grid(ui)[0] == ["underline href", "Country", "pop"]  # the typed name stays on its column
    page.close(); ui.close()


def test_unicode_survives_the_csv_and_the_json(browser, site):
    page, ui = open_scraper(browser, site, "unicode.html")
    heads, rows = grid(ui)
    assert "Café “no. 1”, large" in rows[0] and "£1.50" in rows[0]
    with ui.expect_download() as d:
        ui.click("#btn-csv")
    csv_text = Path(d.value.path()).read_bytes().decode("utf-8-sig")
    assert '"Café “no. 1”, large"' in csv_text and "£1.50" in csv_text
    page.close(); ui.close()


def test_copy_puts_tab_separated_text_on_the_clipboard(browser, site):
    ctx, _worker, _id = browser
    ctx.grant_permissions(["clipboard-read", "clipboard-write"])
    page, ui = open_scraper(browser, site, "table.html")
    grid(ui)
    ui.click("#btn-copy")
    ui.wait_for_function("() => document.getElementById('btn-copy').textContent === 'Copied'")
    text = ui.evaluate("() => navigator.clipboard.readText()")
    lines = text.replace("\r\n", "\n").split("\n")  # Windows turns line breaks into CRLF on the clipboard
    assert lines[0] == "underline href\tunderline\tpop" and lines[1].endswith("\tChina\t1,409") and len(lines) == 5
    page.close(); ui.close()


def test_a_page_without_a_list_says_so_and_disables_export(browser, site):
    page, ui = open_scraper(browser, site, "empty.html")
    ui.wait_for_selector("#message:not([hidden])")
    assert "No list of repeated items" in ui.inner_text("#message")
    assert ui.is_disabled("#btn-csv") and ui.is_disabled("#btn-copy") and ui.is_disabled("#btn-json")
    page.close(); ui.close()


def test_an_unreadable_tab_gives_a_plain_message(browser, site):
    ctx, _worker, ext_id = browser
    ui = ctx.new_page()
    ui.goto(f"chrome-extension://{ext_id}/ui/ui.html?tabId=999999")  # no such tab
    ui.wait_for_selector("#message:not([hidden])")
    assert "cannot be read" in ui.inner_text("#message")
    ui.goto(f"chrome-extension://{ext_id}/ui/ui.html")  # opened without the toolbar click
    ui.wait_for_selector("#message:not([hidden])")
    assert "toolbar icon" in ui.inner_text("#message")
    ui.close()


def test_page_text_is_shown_as_text_never_as_html():
    """The window builds every cell with textContent; page content must never reach an HTML sink."""
    source = (ROOT / "extension" / "src" / "ui" / "ui.js").read_text(encoding="utf-8")
    assert "innerHTML" not in source and "insertAdjacentHTML" not in source and "outerHTML" not in source
    assert "eval(" not in source and "new Function" not in source


# ---- export helpers on their own (Node, skipped when it is not installed) -----------------------------------------
def run_node(script: str, module: str = "csv") -> str:
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    code = f'import * as m from "{(ROOT / "extension" / "src" / "lib" / f"{module}.js").as_uri()}"; {script}'
    done = subprocess.run([node, "--input-type=module", "-"], input=code, capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0, done.stderr  # code goes through stdin: quotes in -e arguments break on Windows
    return done.stdout


def test_csv_quotes_only_what_needs_quotes():
    out = run_node(r'console.log(JSON.stringify(m.toCsv(["a","b"], [{a:"x,y", b:"say \"hi\""}, {a:"line\nbreak", b:null}, {a:"plain", b:0}])));')
    assert json.loads(out) == 'a,b\r\n"x,y","say ""hi"""\r\n"line\nbreak",\r\nplain,0\r\n'


def test_tsv_never_breaks_a_row_and_unique_names_fix_blanks_and_repeats():
    assert json.loads(run_node(r'console.log(JSON.stringify(m.toTsv(["a"], [{a:"x\ty\nz"}])));')) == "a\nx y z"
    assert json.loads(run_node('console.log(JSON.stringify(m.uniqueNames(["a", "a", "", "  ", "a"])));')) == ["a", "a 2", "column 3", "column 4", "a 3"]


def test_file_names_are_safe_and_dated():
    name = run_node('console.log(m.fileName("https://www.shop.example/a?x=1", "csv", new Date("2026-10-30T10:00:00Z")));').strip()
    assert name == "scrape-shop.example-2026-10-30.csv"
    assert run_node('console.log(m.fileName("not a url", "json", new Date("2026-10-30T10:00:00Z")));').strip() == "scrape-page-2026-10-30.json"


# ---- load more, in the browser ------------------------------------------------------------------------------------
def load_more(ui, action, pages=5, selector=""):
    """Choose the action, press the button and wait until it says why it stopped. Returns the status line."""
    ui.wait_for_selector("#grid tbody tr")
    ui.select_option("#action", action)
    ui.fill("#pages", str(pages))
    if selector:
        ui.fill("#action-selector", selector)
    ui.click("#btn-load")
    try:
        ui.wait_for_function("() => document.getElementById('load-status').textContent.startsWith('Stopped')", timeout=90000)
    except Exception:  # noqa: BLE001 - say what the window showed instead of only timing out
        raise AssertionError(f"never stopped; message={ui.inner_text('#message')!r} status={ui.inner_text('#load-status')!r}") from None
    return ui.inner_text("#load-status")


def items(ui):
    return [c for r in grid(ui)[1] for c in r if c.startswith(("Item ", "A1"))]


def test_scroll_down_loads_an_infinite_list_without_repeats(browser, site):
    page, ui = open_scraper(browser, site, "scroll.html")
    status = load_more(ui, "scroll")
    assert items(ui) == [f"Item {i}" for i in range(1, 13)]
    assert ui.inner_text("#stat-pages") == "3" and ui.inner_text("#stat-items") == "12"
    assert "added no new rows" in status and "12 rows from 3 pages" in status
    page.close(); ui.close()


def test_click_load_more_stops_when_the_button_is_gone(browser, site):
    page, ui = open_scraper(browser, site, "more.html")
    status = load_more(ui, "more")
    assert items(ui) == [f"Item {i}" for i in range(1, 13)] and "nothing more to load" in status
    page.close(); ui.close()


def test_a_button_that_only_repeats_rows_adds_nothing(browser, site):
    page, ui = open_scraper(browser, site, "dup.html")
    status = load_more(ui, "more")
    assert items(ui) == [f"Item {i}" for i in range(1, 5)] and "added no new rows" in status
    assert ui.inner_text("#stat-pages") == "1"
    page.close(); ui.close()


def test_a_custom_selector_finds_a_button_the_wording_rules_miss(browser, site):
    page, ui = open_scraper(browser, site, "custom.html")
    assert "nothing more to load" in load_more(ui, "more")
    assert len(items(ui)) == 4  # "Give me more" is not a recognised label
    ui.click("#btn-rescan")
    load_more(ui, "more", selector="#m")
    assert len(items(ui)) == 12
    page.close(); ui.close()


def test_next_page_links_are_followed_in_the_users_tab(browser, site):
    page, ui = open_scraper(browser, site, "next1.html")
    status = load_more(ui, "next")
    assert items(ui) == [f"Item {i}" for i in range(1, 13)] and "nothing more to load" in status
    assert ui.inner_text("#stat-pages") == "3"
    assert page.url.endswith("/next3.html") and "stays on the last page" in status
    page.close(); ui.close()


def test_next_with_an_arrow_in_its_label_is_recognised(browser, site):
    page, ui = open_scraper(browser, site, "arrow1.html")
    load_more(ui, "next", pages=2)
    assert len(items(ui)) == 8
    page.close(); ui.close()


def test_later_pages_keep_only_the_real_rows_not_spacers_and_sub_rows(browser, site):
    page, ui = open_scraper(browser, site, "hn1.html")
    ui.wait_for_selector("#grid tbody tr")
    assert ui.inner_text("#stat-items") == "4"
    load_more(ui, "more", pages=2)
    assert ui.inner_text("#stat-items") == "8"
    assert [c for r in grid(ui)[1] for c in r if c.startswith("Story ")] == [f"Story {i}" for i in range(1, 9)]
    page.close(); ui.close()


def test_the_page_limit_is_respected(browser, site):
    page, ui = open_scraper(browser, site, "next1.html")
    status = load_more(ui, "next", pages=2)
    assert len(items(ui)) == 8 and "page limit" in status
    page.close(); ui.close()


def test_a_next_link_to_another_site_is_not_followed(browser, site):
    page, ui = open_scraper(browser, site, "other.html")
    status = load_more(ui, "next")
    assert len(items(ui)) == 4 and "another site" in status
    assert "localhost" in ui.inner_text("#message") and page.url.endswith("/other.html")  # the tab never left
    page.close(); ui.close()


def test_pages_that_link_back_to_each_other_do_not_loop(browser, site):
    page, ui = open_scraper(browser, site, "loopa.html")
    status = load_more(ui, "next", pages=20)
    assert len(items(ui)) == 8 and "added no new rows" in status
    page.close(); ui.close()


def test_stop_keeps_the_rows_found_so_far(browser, site):
    page, ui = open_scraper(browser, site, "long1.html")
    ui.wait_for_selector("#grid tbody tr")
    ui.select_option("#action", "next")
    ui.fill("#pages", "20")
    ui.click("#btn-load")
    ui.wait_for_function("() => document.getElementById('stat-pages').textContent === '2'", timeout=60000)
    ui.click("#btn-stop")
    ui.wait_for_function("() => document.getElementById('load-status').textContent.startsWith('Stopped')", timeout=60000)
    assert "Stopped by you" in ui.inner_text("#load-status")
    assert 8 <= len(items(ui)) < 32 and not ui.is_disabled("#btn-csv") and ui.is_hidden("#btn-stop")
    page.close(); ui.close()


def test_the_action_box_starts_on_none_and_needs_a_choice(browser, site):
    page, ui = open_scraper(browser, site, "table.html")
    grid(ui)
    assert ui.input_value("#action") == "none" and ui.is_disabled("#btn-load") and ui.is_hidden("#action-selector")
    ui.select_option("#action", "next")
    assert not ui.is_disabled("#btn-load") and ui.is_visible("#action-selector")
    page.close(); ui.close()


# ---- load more, the logic on its own (Node) ------------------------------------------------------------------------
def test_sites_and_permission_patterns():
    out = json.loads(run_node(r'''console.log(JSON.stringify([
      m.sameSite("https://www.a.com/x", "https://a.com/y"), m.sameSite("https://a.com", "https://b.com"),
      m.sameSite("nope", "nope"), m.isWebUrl("javascript:alert(1)"), m.isWebUrl("https://a.com/p"),
      m.originPatterns("https://www.shop.example:8080/list?x=1"), m.originPatterns("http://127.0.0.1:8000/a"),
      m.originPatterns("http://localhost/a")]));''', "loadmore"))
    assert out == [True, False, False, False, True, ["*://shop.example/*", "*://www.shop.example/*"],
                   ["*://127.0.0.1/*"], ["*://localhost/*"]]


def test_columns_are_matched_by_name_not_by_the_numbering_of_each_page():
    out = json.loads(run_node(r'''
      const table = { columns: [{id:"c0",name:"title"},{id:"c1",name:"price"}], rows: [{c0:"A",c1:"1"}] };
      const seen = new Set(table.rows.map(m.rowKey));
      const page = { columns: [{id:"c0",name:"price"},{id:"c1",name:"title"},{id:"c2",name:"stock"}],
                     rows: [{c0:"1",c1:"A",c2:""}, {c0:"2",c1:"B",c2:"yes"}] };
      const added = m.mergeTable(table, page, seen);
      console.log(JSON.stringify({added, cols: table.columns.map(c=>c.name+":"+c.id), rows: table.rows}));''', "loadmore"))
    assert out["added"] == 1 and out["cols"] == ["title:c0", "price:c1", "stock:m2"]
    assert out["rows"][1] == {"c0": "B", "c1": "2", "m2": "yes"}  # the repeat of "A" was dropped


LOOP = r'''
  const mk = (n) => ({ ok: true, lists: [{ columns: [{id:"c0",name:"t"}], rows: Array.from({length: 4}, (_, i) => ({c0: "r" + (n * 4 + i)})) }], acted: true });
  let n = 0;
  const base = { table: { columns: [{id:"c0",name:"t"}], rows: [{c0:"r0"},{c0:"r1"},{c0:"r2"},{c0:"r3"}] },
                 pageUrl: "https://a.com/1", read: async () => mk(++n), navigate: async (u) => u, delayMs: 0 };
'''


def test_the_row_cap_and_the_page_cap_stop_the_loop():
    out = json.loads(run_node(LOOP + r'''
      const a = await m.runLoadMore({ ...base, maxRows: 5, maxPages: 20 });
      const trimmed = base.table.rows.length;  // cut back to exactly the cap
      n = 0; base.table.rows.length = 4;
      const b = await m.runLoadMore({ ...base, maxPages: 3 });
      console.log(JSON.stringify([a.reason, trimmed, b.reason, b.pages, base.table.rows.length]));''', "loadmore"))
    assert out == ["row-cap", 5, "page-cap", 3, 12]


def test_stop_and_the_time_limit_end_the_loop_cleanly():
    out = json.loads(run_node(LOOP + r'''
      const ctl = new AbortController();
      const a = await m.runLoadMore({ ...base, signal: ctl.signal, maxPages: 20, onProgress: () => ctl.abort() });
      let t = 0;
      const b = await m.runLoadMore({ ...base, table: { columns: [{id:"c0",name:"t"}], rows: [] }, maxPages: 20, totalMs: 10, now: () => (t += 6) });
      console.log(JSON.stringify([a.reason, a.pages, b.reason]));''', "loadmore"))
    assert out == ["stopped", 2, "time"]


def test_a_next_link_off_site_or_a_redirect_off_site_is_never_followed():
    out = json.loads(run_node(r'''
      const table = () => ({ columns: [{id:"c0",name:"t"}], rows: [{c0:"r0"}] });
      let loaded = [];
      const off = await m.runLoadMore({ table: table(), pageUrl: "https://a.com/1", delayMs: 0, maxPages: 5,
        read: async () => ({ ok: true, lists: [{columns: [{id:"c0",name:"t"}], rows: [{c0:"x"}]}], nextHref: "https://evil.example/2" }),
        navigate: async (u) => { loaded.push(u); return u; } });
      const redirect = await m.runLoadMore({ table: table(), pageUrl: "https://a.com/1", delayMs: 0, maxPages: 5,
        read: async () => ({ ok: true, lists: [{columns: [{id:"c0",name:"t"}], rows: [{c0:"x"}]}], nextHref: "https://a.com/2" }),
        navigate: async () => "https://elsewhere.example/login" });
      const scheme = await m.runLoadMore({ table: table(), pageUrl: "https://a.com/1", delayMs: 0, maxPages: 5,
        read: async () => ({ ok: true, lists: [], nextHref: "javascript:alert(1)" }), navigate: async (u) => { loaded.push(u); return u; } });
      console.log(JSON.stringify([off.reason, redirect.reason, scheme.reason, loaded]));''', "loadmore"))
    assert out == ["other-site", "other-site", "other-site", []]


# ---- scrape each URL and page details -------------------------------------------------------------------------------
def scrape_each(ui, max_urls=10):
    ui.wait_for_selector("#grid tbody tr")
    ui.fill("#max-urls", str(max_urls))
    ui.click("#btn-details")
    try:
        ui.wait_for_function("() => /^(Done|Stopped)/.test(document.getElementById('load-status').textContent)", timeout=90000)
    except Exception:  # noqa: BLE001
        raise AssertionError(f"never finished; message={ui.inner_text('#message')!r} status={ui.inner_text('#load-status')!r}") from None
    return ui.inner_text("#load-status")


def by_item(ui):
    heads, rows = grid(ui)
    return heads, {next(c for c in r if c.startswith("Item ")): dict(zip(heads, r)) for r in rows}


def test_scrape_each_url_adds_the_details_of_every_same_site_page(browser, site):
    ctx, _w, _id = browser
    page, ui = open_scraper(browser, site, "catalog.html")
    ui.wait_for_selector("#grid tbody tr")
    assert ui.input_value("#url-col") and "href" in ui.eval_on_selector("#url-col option:checked", "o => o.textContent")
    tabs_before = len(ctx.pages)
    status = scrape_each(ui)
    heads, rows = by_item(ui)
    assert {"page title", "page heading", "page description", "page colour", "page weight", "page text"} <= set(heads)
    one = rows["Item 1"]
    assert one["page title"] == "Widget 1 | Shop" and one["page heading"] == "Widget 1" and one["page description"] == "Description 1"
    assert one["page colour"] == "Red 1" and one["page weight"] == "1 kg" and "Body text 1." in one["page text"]
    assert "SIDE JUNK" not in one["page text"] and "MENU" not in one["page text"]
    assert rows["Item 3"]["page heading"] == "Widget 3 (JS)"  # a page that fills itself in after loading
    assert rows["Item 4"]["page title"] == "" and rows["Item 5"]["page title"] == ""  # bot check and other site: left alone
    assert "Details for 3 of 4 pages" in status
    note = ui.inner_text("#message")
    assert "bot check" in note and "another site" in note
    assert len(ctx.pages) == tabs_before  # every background tab was closed again
    assert page.url.endswith("/catalog.html")
    with ui.expect_download() as d:
        ui.click("#btn-csv")
    assert "page heading" in Path(d.value.path()).read_bytes().decode("utf-8-sig").splitlines()[0]
    page.close(); ui.close()


def test_the_url_limit_is_respected_and_reported(browser, site):
    page, ui = open_scraper(browser, site, "catalog.html")
    status = scrape_each(ui, max_urls=2)
    assert "Details for 2 of 2 pages" in status and "left out" in status
    heads, rows = by_item(ui)
    assert rows["Item 2"]["page heading"] == "Widget 2" and rows["Item 3"].get("page heading", "") == ""
    page.close(); ui.close()


def test_page_details_shows_the_page_itself_as_one_row(browser, site):
    page, ui = open_scraper(browser, site, "d/1.html")
    ui.wait_for_selector("#message:not([hidden]), #grid tbody tr")
    ui.click("#btn-page")
    ui.wait_for_function("() => document.querySelector('#grid thead input') && document.querySelector('#grid thead input').value === 'page title'")
    heads, rows = grid(ui)
    row = dict(zip(heads, rows[0]))
    assert len(rows) == 1 and row["page weight"] == "1 kg" and row["page heading"] == "Widget 1"
    assert ui.is_hidden("#detailbar") and not ui.is_disabled("#btn-csv")
    page.close(); ui.close()


# ---- scrape each URL, the logic on its own (Node) ------------------------------------------------------------------
DETAIL_ROWS = r'''
  const cols = [{id:"c0",name:"title"},{id:"c1",name:"title href"}];
  const mk = () => ({ columns: cols.map(c => ({...c})), rows: [1,2,3,4].map(i => ({c0:"t"+i, c1:"https://a.com/p/"+i}))
    .concat([{c0:"x", c1:"https://b.com/p/9"}, {c0:"dup", c1:"https://a.com/p/1#frag"}]) });
'''


def test_the_link_column_is_found_and_other_sites_and_the_cap_are_left_out():
    out = json.loads(run_node(DETAIL_ROWS + r'''
      const t = mk();
      const col = m.detailUrlColumn(t.columns, t.rows);
      const r = m.collectTargets(t.rows, col, "https://www.a.com/list", 3);
      console.log(JSON.stringify([col, [...r.targets.keys()], r.targets.get("https://a.com/p/1").length, r.skipped.map(s=>s.url), r.overCap,
        m.detailUrlColumn([{id:"c0",name:"title"}], [{c0:"no link"}]), m.urlColumns(t.columns, t.rows).map(c=>c.id)]));''', "details"))
    assert out == ["c1", ["https://a.com/p/1", "https://a.com/p/2", "https://a.com/p/3"], 2, ["https://b.com/p/9"], 1, "", ["c1"]]


def test_details_run_two_at_a_time_and_report_blocked_and_failed_pages():
    out = json.loads(run_node(DETAIL_ROWS + r'''
      const t = mk(); let open = 0, peak = 0;
      const fetchDetails = async (u) => {
        open++; peak = Math.max(peak, open); await new Promise(r => setTimeout(r, 20)); open--;
        if (u.endsWith("/2")) return { blocked: true, details: {} };
        if (u.endsWith("/3")) throw new Error("the page took too long to load");
        return { blocked: false, details: { "page title": "T " + u.slice(-1), ["page n" + u.slice(-1)]: "v" } };
      };
      const r = await m.runDetails({ table: t, urlColumn: "c1", pageUrl: "https://a.com/", fetchDetails, delayMs: 0 });
      console.log(JSON.stringify({ peak, done: r.done, total: r.total, reason: r.reason, skipped: r.skipped.map(s => s.reason),
        cols: t.columns.map(c => c.id), r0: t.rows[0], r1: t.rows[1], dup: t.rows[5]["d:page title"] }));''', "details"))
    assert out["peak"] == 2 and out["done"] == 2 and out["total"] == 4 and out["reason"] == "done"
    assert any("bot check" in x for x in out["skipped"]) and any("too long" in x for x in out["skipped"])
    assert out["cols"] == ["c0", "c1", "d:page title", "d:page n1", "d:page n4"]
    assert out["r0"]["d:page title"] == "T 1" and "d:page title" not in out["r1"] and out["dup"] == "T 1"  # same page, both rows


def test_details_stop_when_asked_and_when_time_runs_out():
    out = json.loads(run_node(DETAIL_ROWS + r'''
      const ctl = new AbortController(); let calls = 0;
      const a = await m.runDetails({ table: mk(), urlColumn: "c1", pageUrl: "https://a.com/", delayMs: 0, concurrency: 1, signal: ctl.signal,
        fetchDetails: async () => { calls++; ctl.abort(); return { details: { k: "v" } }; } });
      let t = 0;
      const b = await m.runDetails({ table: mk(), urlColumn: "c1", pageUrl: "https://a.com/", delayMs: 0, totalMs: 10, now: () => (t += 6),
        fetchDetails: async () => ({ details: { k: "v" } }) });
      console.log(JSON.stringify([a.reason, calls, a.done, b.reason]));''', "details"))
    assert out == ["stopped", 1, 1, "time"]


# ---- templates and settings -----------------------------------------------------------------------------------------
def save_shop_template(ui, name="Shop"):
    """On the catalog page: rename the second column, hide the first, choose Scroll down, save as a template."""
    heads, _rows = grid(ui)
    ui.fill("#grid thead th:nth-child(3) input", "Product")
    ui.press("#grid thead th:nth-child(3) input", "Tab")
    ui.click("#grid thead th:nth-child(2) .x")
    ui.select_option("#action", "scroll")
    ui.fill("#tpl-name", name)
    ui.click("#btn-tpl-save")
    ui.wait_for_selector("#message:not([hidden])")
    return heads


def test_a_saved_template_is_applied_when_the_site_is_opened_again(browser, site):
    page, ui = open_scraper(browser, site, "catalog.html")
    heads = save_shop_template(ui)
    assert 'Template "Shop" saved' in ui.inner_text("#message") and ui.input_value("#tpl-select") == "Shop"
    ui.close()
    ui = browser[0].new_page()
    ui.goto(f"chrome-extension://{browser[2]}/ui/ui.html?tabId={tab_id_of(browser, page)}")
    heads2, rows = grid(ui)
    assert heads2 == ["Product"] + heads[2:] and len(rows) == 5  # first column hidden, second renamed, nothing else touched
    assert 'Template "Shop" applied' in ui.inner_text("#message")
    assert ui.input_value("#tpl-select") == "Shop" and ui.input_value("#action") == "scroll" and not ui.is_disabled("#btn-tpl-delete")
    ui.select_option("#tpl-select", "")  # None: the detected list again
    assert grid(ui)[0] == heads
    page.close(); ui.close()


def tab_id_of(browser, page):
    return browser[1].evaluate("async (url) => (await chrome.tabs.query({})).find((t) => t.url === url).id", page.url)


def test_a_template_that_no_longer_matches_falls_back_to_detection_with_a_note(browser, site):
    ctx, worker, _id = browser
    worker.evaluate("""() => chrome.storage.local.set({templates: {"127.0.0.1": {Old: {itemsSelector: "div.gone > li", loadMore: {action: "none", selector: ""}, columns: null, savedAt: "2026-01-01 00:00:00"}}}})""")
    page, ui = open_scraper(browser, site, "catalog.html", keep_storage=True)
    heads, rows = grid(ui)
    assert len(rows) == 5 and "matches nothing" in ui.inner_text("#message")
    page.close(); ui.close()


def test_a_broken_selector_in_a_template_does_not_break_the_window(browser, site):
    worker = browser[1]
    worker.evaluate("""() => chrome.storage.local.set({templates: {"127.0.0.1": {Bad: {itemsSelector: "div[[[", loadMore: {action: "none", selector: ""}, columns: null, savedAt: ""}}}})""")
    page, ui = open_scraper(browser, site, "catalog.html", keep_storage=True)
    assert len(grid(ui)[1]) == 5 and "matches nothing" in ui.inner_text("#message")
    page.close(); ui.close()


def test_deleting_a_template_removes_it_for_good(browser, site):
    page, ui = open_scraper(browser, site, "catalog.html")
    save_shop_template(ui)
    ui.once("dialog", lambda d: d.accept())
    ui.click("#btn-tpl-delete")
    ui.wait_for_function("() => document.getElementById('tpl-select').value === '' && document.getElementById('btn-tpl-delete').disabled")
    assert browser[1].evaluate("async () => JSON.stringify((await chrome.storage.local.get('templates')).templates)") == "{}"
    assert len(grid(ui)[0]) > 2  # all columns are back
    page.close(); ui.close()


def test_export_holds_selectors_only_and_import_brings_it_back(browser, site, tmp_path):
    page, ui = open_scraper(browser, site, "catalog.html")
    save_shop_template(ui)
    with ui.expect_download() as d:
        ui.click("#btn-tpl-export")
    text = Path(d.value.path()).read_text(encoding="utf-8")
    data = json.loads(text)
    tpl = data["127.0.0.1"]["Shop"]
    assert tpl["itemsSelector"] and tpl["loadMore"] == {"action": "scroll", "selector": ""} and tpl["columns"][0]["as"] == "Product"
    assert "Item 1" not in text and "£" not in text  # no page content
    assert d.value.suggested_filename.startswith("bizfy-templates-")
    browser[1].evaluate("() => chrome.storage.local.clear()")
    bad = tmp_path / "mixed.json"
    bad.write_text(json.dumps({"127.0.0.1": {"Shop": tpl, "Empty": {"itemsSelector": "  "}}, "x y": {"A": tpl}, "ok.example": [1]}), encoding="utf-8")
    ui.set_input_files("#tpl-file", str(bad))
    ui.wait_for_function("() => document.getElementById('message').textContent.startsWith('Imported')")
    assert "Imported 1 template" in ui.inner_text("#message") and "3 entries were not valid" in ui.inner_text("#message")
    assert ui.eval_on_selector_all("#tpl-select option", "o => o.map(x => x.value)") == ["", "Shop"]
    junk = tmp_path / "junk.json"
    junk.write_text("not json at all", encoding="utf-8")
    ui.set_input_files("#tpl-file", str(junk))
    ui.wait_for_function("() => document.getElementById('message').textContent.includes('not valid JSON')")
    page.close(); ui.close()


def test_the_numbers_you_set_are_remembered(browser, site):
    page, ui = open_scraper(browser, site, "catalog.html")
    grid(ui)
    ui.select_option("#action", "scroll")  # the Pages box is only live once an action is chosen
    ui.fill("#pages", "7"); ui.press("#pages", "Tab")
    ui.fill("#max-urls", "3"); ui.press("#max-urls", "Tab")
    ui.close()
    ui = browser[0].new_page()
    ui.goto(f"chrome-extension://{browser[2]}/ui/ui.html?tabId={tab_id_of(browser, page)}")
    grid(ui)
    assert ui.input_value("#pages") == "7" and ui.input_value("#max-urls") == "3"
    page.close(); ui.close()


# ---- templates, the logic on its own (Node), and the file format shared with the app -------------------------------------
def test_template_names_hosts_order_and_validation():
    out = json.loads(run_node(r'''
      const s = {};
      m.putTemplate(s, "https://www.shop.example/a", "  Prices <b>!  ", m.makeTemplate({ itemsSelector: "ul > li", loadMore: { action: "next", selector: "a.n" } }, new Date(2026, 0, 2, 3, 4, 5)));
      m.putTemplate(s, "https://shop.example/b", "Newer", m.makeTemplate({ itemsSelector: "ol > li", loadMore: { action: "bogus", selector: "x" }, columns: [] }, new Date(2026, 5, 2, 3, 4, 5)));
      let err = ""; try { m.makeTemplate({ itemsSelector: "" }); } catch (e) { err = e.message; }
      let err2 = ""; try { m.putTemplate(s, "not a url", "x", {}); } catch (e) { err2 = e.message; }
      console.log(JSON.stringify({ host: m.templateHost("https://WWW.Shop.Example:8080/x"), names: m.templatesFor(s, "https://shop.example/").map(t => t.name),
        saved: s["shop.example"]["Prices b"], newer: s["shop.example"]["Newer"].loadMore, cols: s["shop.example"]["Newer"].columns, err, err2,
        clean: m.cleanName("Ünï cödé 日本 <script>"), removed: Object.keys(m.removeTemplate(s, "shop.example", "Newer")["shop.example"]) }));''', "templates"))
    assert out["host"] == "shop.example" and out["names"] == ["Newer", "Prices b"]
    assert out["saved"] == {"itemsSelector": "ul > li", "loadMore": {"action": "next", "selector": "a.n"}, "columns": None, "savedAt": "2026-01-02 03:04:05"}
    assert out["newer"] == {"action": "none", "selector": ""} and out["cols"] is None  # unknown action, no columns
    assert "selector" in out["err"] and "name" in out["err2"] and out["clean"] == "Ünï cödé 日本 script" and out["removed"] == ["Prices b"]


def test_import_drops_everything_that_is_not_a_well_formed_template():
    out = json.loads(run_node(r'''
      const cols = Array.from({length: 60}, (_, i) => ({ name: "c" + i }));
      const r = m.sanitizeStore({
        "shop.example": { Good: { itemsSelector: "ul > li", loadMore: { action: "scroll", selector: 5 }, columns: cols, evil: "<img onerror=x>", savedAt: "2026-01-01 00:00:00" },
                          NoSel: { itemsSelector: "" }, Num: { itemsSelector: 7 }, "": { itemsSelector: "a" }, Arr: [] },
        "bad host!": { A: { itemsSelector: "a" } }, "list.example": [], "Up.Example": { Ok: { itemsSelector: "p" } } });
      console.log(JSON.stringify({ kept: r.kept, dropped: r.dropped, hosts: Object.keys(r.store), good: r.store["shop.example"].Good,
        ok: r.store["up.example"].Ok, notObj: m.sanitizeStore([1]).kept, str: m.sanitizeStore("x").dropped }));''', "templates"))
    assert out["kept"] == 2 and out["dropped"] == 6 and out["hosts"] == ["shop.example", "up.example"]
    good = out["good"]
    assert "evil" not in good and good["loadMore"] == {"action": "scroll", "selector": ""} and len(good["columns"]) == 40
    assert out["ok"]["loadMore"] == {"action": "none", "selector": ""} and out["notObj"] == 0 and out["str"] == 1


def test_a_template_picks_hides_renames_and_orders_columns():
    out = json.loads(run_node(r'''
      const list = { columns: [{id:"c0",name:"a href"},{id:"c1",name:"title"},{id:"c2",name:"price"},{id:"c3",name:"junk"}] };
      const r = m.applyToList(list, { columns: [{name:"price", as:"Cost"}, {name:"title", as:"title"}, {name:"gone", as:"x"}, {name:"price", as:"dup"}] });
      const none = m.applyToList({ columns: [{id:"c0",name:"a"}] }, { columns: [{name:"zzz", as:"zzz"}] });
      console.log(JSON.stringify({ order: list.columns.map(c => c.id), hidden: [...r.hidden], names: [...r.names], matched: r.matched, none: [none.matched, [...none.hidden]] }));''', "templates"))
    assert out["order"] == ["c2", "c1", "c0", "c3"] and out["hidden"] == ["c0", "c3"] and out["names"] == [["c2", "Cost"]]
    assert out["matched"] is True and out["none"] == [False, []]  # a template that names nothing here changes nothing


def test_templates_are_exchangeable_with_the_app(tmp_path):
    """A file the app wrote is accepted as it is, and what the extension writes has exactly the app's keys."""
    path = tmp_path / "scrape_templates.json"
    scrape.save_template("https://www.books.toscrape.com/", "Books", "ol > li", {"action": "next", "selector": "li.next a"},
                         [{"name": "a href", "as": "Link"}, {"name": "price_color", "as": "Price"}], path=path)
    scrape.save_template("https://quotes.toscrape.com/", "All", "div.quote", None, None, path=path)
    app_file = json.loads(path.read_text(encoding="utf-8"))
    out = json.loads(run_node("const r = m.sanitizeStore(" + json.dumps(app_file) + r''');
      const made = m.makeTemplate({ itemsSelector: "ul > li", loadMore: { action: "none", selector: "" }, columns: [{ name: "a", as: "a" }] });
      console.log(JSON.stringify({ kept: r.kept, dropped: r.dropped, same: JSON.stringify(r.store) === JSON.stringify(''' + json.dumps(app_file) + r'''), keys: Object.keys(made).sort() }));''', "templates"))
    assert out["kept"] == 2 and out["dropped"] == 0 and out["same"] is True
    assert out["keys"] == sorted(app_file["books.toscrape.com"]["Books"].keys())


# ---- packaging: what a store reviewer would check ---------------------------------------------------------------------
def test_the_store_zips_are_complete_and_clean(tmp_path):
    import re
    import zipfile

    built = build_extension.build(tmp_path, make_zip=True)
    version = json.loads((built["chromium"] / "manifest.json").read_text(encoding="utf-8"))["version"]
    for target in ("chromium", "firefox"):
        archive = tmp_path / f"bizfy-scraper-{target}-{version}.zip"
        with zipfile.ZipFile(archive) as z:
            names = z.namelist()
            manifest = json.loads(z.read("manifest.json"))
        assert "manifest.json" in names and not any(n.startswith(("dist/", "chromium/", "firefox/")) for n in names)  # files at the root
        assert not any(n.endswith((".map", ".py", ".md")) or "test" in n.lower() for n in names)
        assert "detect.js" in names and "pagedetails.js" in names and "ui/ui.html" in names and "icons/128.png" in names
        assert "test build" not in manifest["name"] and "host_permissions" not in manifest and len(manifest["name"]) <= 75
        assert len(manifest["description"]) <= 132  # the Chrome Web Store limit
        assert manifest["content_security_policy"]["extension_pages"] == "script-src 'self'; object-src 'self'"
        assert re.fullmatch(r"\d+(\.\d+){0,3}", manifest["version"])
        listed = [manifest["action"]["default_icon"], manifest["icons"]]
        listed = [p for icons in listed for p in icons.values()] + ["background.js"]
        assert all(p in names for p in listed)  # every file the manifest names is in the zip


def test_the_shipped_code_makes_no_network_calls_and_loads_no_remote_code(tmp_path):
    import re

    folder = build_extension.build(tmp_path, targets=("chromium",))["chromium"]
    banned = re.compile(r"\bfetch\s*\(|XMLHttpRequest|WebSocket|sendBeacon|importScripts|\beval\s*\(|new\s+Function|EventSource|"
                        r"\.innerHTML\s*=|insertAdjacentHTML|document\.write|<script[^>]+src=[\"']?https?:", re.I)
    for path in folder.rglob("*"):
        if path.suffix in (".js", ".html", ".css", ".json"):
            hits = banned.findall(path.read_text(encoding="utf-8"))
            # detect.js and pagedetails.js build no markup; the one allowed use of insertAdjacentHTML is none at all
            assert not hits, f"{path.name}: {hits}"
    assert "http-equiv" not in (folder / "ui" / "ui.html").read_text(encoding="utf-8")


def test_the_version_script_sets_bumps_and_refuses_bad_versions(tmp_path):
    spec = importlib.util.spec_from_file_location("bump", ROOT / "scripts" / "bump_extension_version.py")
    bump = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bump)
    copy = tmp_path / "manifest.base.json"
    original = (ROOT / "extension" / "src" / "manifest.base.json").read_text(encoding="utf-8")
    copy.write_text(original, encoding="utf-8")
    assert [bump.bumped("0.1.9", p) for p in ("patch", "minor", "major")] == ["0.1.10", "0.2.0", "1.0.0"]
    bump.write_version("1.2.3", copy)
    assert bump.read_version(copy) == "1.2.3"
    assert copy.read_text(encoding="utf-8").replace("1.2.3", bump.read_version(ROOT / "extension" / "src" / "manifest.base.json")) == original
    for bad in ("v1", "1.2.3.4.5", "1..2", "x", "", "1.2.-3"):
        with pytest.raises(ValueError):
            bump.write_version(bad, copy)


def test_the_window_works_on_a_narrow_screen(browser, site):
    page, ui = open_scraper(browser, site, "catalog.html")
    grid(ui)
    ui.set_viewport_size({"width": 400, "height": 800})  # like a phone
    assert ui.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")  # nothing pushes the page sideways
    assert ui.is_visible("#btn-csv") and ui.is_visible("#grid")
    page.close(); ui.close()
