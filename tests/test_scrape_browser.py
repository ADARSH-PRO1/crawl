"""In-page detector tests: they run scrape_detect.js in a local Chromium (no network) and skip without one.

Kept apart from test_scrape.py because the synchronous Playwright API leaves an event loop running in the thread,
which would break the asyncio.run() calls of the other tests."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import scrape  # noqa: E402

TABLE = '<html><body><h1>Countries</h1><table><tbody>\n<tr><td><a class="underline" href="/c/china">China</a></td><td class="pop">1,409</td></tr>\n<tr><td><a class="underline" href="/c/india">India</a></td><td class="pop">1,392</td></tr>\n<tr><td><a class="underline" href="/c/usa">United States</a></td><td class="pop">335</td></tr>\n<tr><td><a class="underline" href="/c/brazil">Brazil</a></td><td class="pop">203</td></tr>\n</tbody></table></body></html>'
CARDS = '<html><body><nav><ul><li><a href="/a">Home</a></li><li><a href="/b">About</a></li></ul></nav>\n<div class="grid"><div class="card"><img src="/img/1.png"><h3 class="name">Item 1</h3><span class="price">$1.00</span><a class="buy" href="/buy/1">Buy</a></div><div class="card"><img src="/img/2.png"><h3 class="name">Item 2</h3><span class="price">$2.00</span><a class="buy" href="/buy/2">Buy</a></div><div class="card"><img src="/img/3.png"><h3 class="name">Item 3</h3><span class="price">$3.00</span><a class="buy" href="/buy/3">Buy</a></div><div class="card"><img src="/img/4.png"><h3 class="name">Item 4</h3><span class="price">$4.00</span><a class="buy" href="/buy/4">Buy</a></div><div class="card"><img src="/img/5.png"><h3 class="name">Item 5</h3><span class="price">$5.00</span><a class="buy" href="/buy/5">Buy</a></div><div class="card"><img src="/img/6.png"><h3 class="name">Item 6</h3><span class="price">$6.00</span><a class="buy" href="/buy/6">Buy</a></div></div></body></html>'

# ---- the in-page detector itself, in a real (local) Chromium -----------------------------------------------------
@pytest.fixture(scope="module")
def chromium_page():
    sync_api = pytest.importorskip("playwright.sync_api")
    pw = sync_api.sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=True)
    except Exception as e:  # noqa: BLE001 - no browser installed on this machine
        pw.stop()
        pytest.skip(f"no local Chromium: {e}")
    pg = browser.new_page()
    yield pg
    browser.close()
    pw.stop()


def run_detector(pg, html, items_selector=""):
    pg.set_content(html)
    pg.evaluate("() => {" + scrape.build_js(items_selector, 100) + "}")
    pg.wait_for_selector("#__scrape_result", state="attached")
    return scrape.parse_result(pg.content())


def test_detector_in_browser_matches_the_extension_columns(chromium_page):
    data = run_detector(chromium_page, TABLE)
    best = scrape.table_of(data["lists"][0])
    assert best["columns"] == ["underline href", "underline", "pop"] and len(best["rows"]) == 4


def test_detector_in_browser_ignores_nav_and_hidden_items(chromium_page):
    hidden = CARDS.replace('<div class="card">', '<div class="card" style="display:none">', 1)
    data = run_detector(chromium_page, hidden)
    best = scrape.table_of(data["lists"][0])
    assert len(best["rows"]) == 5 and best["rows"][0]["name"] == "Item 2"


def test_detector_in_browser_can_reuse_a_saved_selector(chromium_page):
    data = run_detector(chromium_page, CARDS, items_selector="div.grid > div.card")
    assert len(data["lists"]) == 1 and len(data["lists"][0]["rows"]) == 6
    missing = run_detector(chromium_page, CARDS, items_selector="ul.nope > li")
    assert missing["lists"] == []




LOAD_MORE_PAGE = """<ul id="l"><li class="i"><a href="/1">One</a></li><li class="i"><a href="/2">Two</a></li>
<li class="i"><a href="/3">Three</a></li></ul>%s"""


def run_action(pg, html, action, selector=""):
    pg.set_content(html)
    pg.evaluate("() => {" + scrape.build_js("", 100, action, selector) + "}")
    pg.wait_for_selector("#__scrape_result", state="attached")
    return scrape.parse_result(pg.content())


def test_detector_clicks_a_load_more_button_and_reports_it(chromium_page):
    html = LOAD_MORE_PAGE % ('<button id="m">Load more</button><script>document.getElementById("m").onclick=()=>'
                             '{document.getElementById("l").insertAdjacentHTML("beforeend",'
                             '"<li class=i><a href=/4>Four</a></li>")}</script>')
    data = run_action(chromium_page, html, "more")
    assert data["acted"] is True and len(data["lists"][0]["rows"]) == 3  # rows are read before the click
    assert chromium_page.locator("#l li").count() == 4


def test_detector_reports_a_next_link_without_following_it(chromium_page):
    data = run_action(chromium_page, LOAD_MORE_PAGE % '<a rel="next" href="/page2">Next</a>', "next")
    assert data["acted"] is False and data["nextHref"].endswith("/page2")


def test_detector_skips_a_disabled_next_button_and_finds_a_custom_selector(chromium_page):
    assert run_action(chromium_page, LOAD_MORE_PAGE % '<button disabled>Next</button>', "next")["acted"] is False
    custom = LOAD_MORE_PAGE % "<div id=go role=button onclick=\"document.title='clicked'\">Go on</div>"
    assert run_action(chromium_page, custom, "next", "#go")["acted"] is True and chromium_page.title() == "clicked"
