"""Tests for scrape mode: no askg1, no network. The in-page detector test needs a local Chromium and skips without one."""
import asyncio
import json
import time
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, scrape  # noqa: E402

TABLE = """<html><body><h1>Countries</h1><table><tbody>
<tr><td><a class="underline" href="/c/china">China</a></td><td class="pop">1,409</td></tr>
<tr><td><a class="underline" href="/c/india">India</a></td><td class="pop">1,392</td></tr>
<tr><td><a class="underline" href="/c/usa">United States</a></td><td class="pop">335</td></tr>
<tr><td><a class="underline" href="/c/brazil">Brazil</a></td><td class="pop">203</td></tr>
</tbody></table></body></html>"""

CARDS = """<html><body><nav><ul><li><a href="/a">Home</a></li><li><a href="/b">About</a></li></ul></nav>
<div class="grid">""" + "".join(
    f'<div class="card"><img src="/img/{i}.png"><h3 class="name">Item {i}</h3><span class="price">${i}.00</span>'
    f'<a class="buy" href="/buy/{i}">Buy</a></div>' for i in range(1, 7)) + "</div></body></html>"

TOO_SHORT = "<html><body><ul><li>one</li><li>two</li></ul><p>Just a paragraph.</p></body></html>"


def test_parse_result_reads_the_script_node_and_survives_garbage():
    payload = json.dumps({"ok": True, "lists": []}).replace("<", "\\u003c")
    html = f'<body><script type="application/json" id="__scrape_result">{payload}</script></body>'
    assert scrape.parse_result(html) == {"ok": True, "lists": []}
    assert scrape.parse_result("<body>no node</body>") is None
    assert scrape.parse_result('<script id="__scrape_result">{broken</script>') is None


def test_build_js_embeds_the_arguments_before_the_detector():
    js = scrape.build_js("ul > li", 25)
    assert js.startswith("window.__scrapeArgs = ")
    assert '"itemsSelector": "ul > li"' in js and '"maxItems": 25' in js and "__scrape_result" in js


def test_table_of_maps_column_ids_to_names():
    lst = {"selector": "ul > li", "count": 2, "score": 1,
           "columns": [{"id": "c0", "name": "title"}, {"id": "c1", "name": "title href"}],
           "rows": [{"c0": "A", "c1": "/a", "zz": "dropped"}, {"c0": "B", "c1": "/b"}]}
    t = scrape.table_of(lst)
    assert t["columns"] == ["title", "title href"]
    assert t["rows"] == [{"title": "A", "title href": "/a"}, {"title": "B", "title href": "/b"}]


def test_fallback_finds_table_rows_with_links_and_text():
    lists = scrape.detect_lists_html(TABLE, "https://example.com/list")
    best = lists[0]
    assert len(best["rows"]) == 4
    assert best["columns"] == ["underline href", "underline", "pop"]
    assert best["rows"][0] == {"underline href": "https://example.com/c/china", "underline": "China", "pop": "1,409"}


def test_fallback_prefers_the_card_grid_over_the_nav_list():
    lists = scrape.detect_lists_html(CARDS, "https://shop.test/")
    best = lists[0]
    assert len(best["rows"]) == 6
    assert {"name", "price", "buy href", "img src"} <= set(best["columns"])
    assert best["rows"][2]["name"] == "Item 3" and best["rows"][2]["buy href"] == "https://shop.test/buy/3"
    assert all(len(x["rows"]) != 2 for x in lists)  # the two-link nav is below the three-item minimum


def test_fallback_returns_nothing_for_short_or_empty_pages():
    assert scrape.detect_lists_html(TOO_SHORT) == []
    assert scrape.detect_lists_html("") == []


def test_to_csv_keeps_column_order_and_quotes_commas():
    out = scrape.to_csv(["a", "b"], [{"a": "x,y", "b": "1"}, {"b": "2"}])
    assert out.splitlines() == ['a,b', '"x,y",1', ",2"]


def test_clamp_scrape_limits():
    assert config.clamp_scrape(None, None) == (config.SCRAPE_DEFAULT_ITEMS, config.SCRAPE_DEFAULT_PAGES)
    assert config.clamp_scrape(10**6, 10**6) == (config.SCRAPE_MAX_ITEMS, config.SCRAPE_MAX_PAGES)
    assert config.clamp_scrape(0, -3) == (config.SCRAPE_DEFAULT_ITEMS, 1)


# ---- run_scrape with a stand-in browser --------------------------------------------------------------------------
class FakeFetcher:
    pages: list = []

    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch(self, url, js="", wait_for="", session_id="", js_only=False):
        FakeFetcher.log.append((url, js_only))
        return FakeFetcher.pages.pop(0)

    async def close_session(self, session_id):
        FakeFetcher.closed = True


def page(html="", success=True, markdown="", failure=None, url="https://example.com/list"):
    return SimpleNamespace(url=url, final_url=url, success=success, html=html, markdown=markdown,
                           status_code=200 if success else 403, error_message=None, failure=failure)


def node_page(values, url="https://example.com/list", **extra):
    """A page the in-page detector has already processed: one list whose rows hold `values` in a column "title"."""
    lst = {"selector": "ul > li", "count": len(values), "score": 1, "columns": [{"id": "c0", "name": "title"}],
           "rows": [{"c0": v} for v in values]}
    node = json.dumps({"ok": True, "lists": [lst], **extra})
    return page(html=f'<script id="__scrape_result" type="application/json">{node}</script>', url=url)


@pytest.fixture
def fake_browser(monkeypatch):
    monkeypatch.setattr(scrape, "CRAWL_DELAY_SECONDS", 0)
    FakeFetcher.closed = False
    FakeFetcher.log = []
    async def public(url):
        return url

    monkeypatch.setattr(scrape, "Fetcher", FakeFetcher)
    monkeypatch.setattr(scrape, "avalidate_public_url", public)
    return FakeFetcher


def test_run_scrape_uses_the_in_page_result_when_present(fake_browser):
    lst = {"selector": "tbody > tr", "count": 2, "score": 1, "columns": [{"id": "c0", "name": "country"}],
           "rows": [{"c0": "China"}, {"c0": "India"}]}
    node = json.dumps({"ok": True, "lists": [lst]})
    fake_browser.pages = [page(html=f'<script id="__scrape_result" type="application/json">{node}</script>')]
    r = asyncio.run(scrape.run_scrape("https://example.com/list"))
    assert r["ok"] and r["method"] == "browser" and r["columns"] == ["country"] and r["items"] == 2


def test_run_scrape_falls_back_to_html_when_the_script_did_not_run(fake_browser):
    fake_browser.pages = [page(html=TABLE)]
    r = asyncio.run(scrape.run_scrape("https://example.com/list"))
    assert r["ok"] and r["method"] == "html" and r["items"] == 4


def test_run_scrape_chooses_the_requested_list_and_clamps_the_index(fake_browser):
    fake_browser.pages = [page(html=CARDS + TABLE)]
    r = asyncio.run(scrape.run_scrape("https://example.com/list", list_index=99))
    assert r["chosen"] == len(r["lists"]) - 1 and r["ok"]


def test_run_scrape_reports_blocked_pages_without_bypassing(fake_browser):
    fake_browser.pages = [page(html="<p>x</p>", markdown="Just a moment... verify you are human captcha")]
    r = asyncio.run(scrape.run_scrape("https://example.com/list"))
    assert not r.get("ok") and "blocked" in r["error"].lower()


def test_run_scrape_reports_a_page_with_no_list(fake_browser):
    fake_browser.pages = [page(html=TOO_SHORT, markdown="Just a paragraph " * 40)]
    r = asyncio.run(scrape.run_scrape("https://example.com/list"))
    assert not r.get("ok") and "No list" in r["error"]


def test_run_scrape_reports_load_failures_and_refuses_off_limits_redirects(fake_browser):
    fake_browser.pages = [page(success=False, failure="robots_disallowed")]
    assert asyncio.run(scrape.run_scrape("https://example.com/list"))["error"] == "robots_disallowed"
    fake_browser.pages = [page(html=TABLE, url="http://127.0.0.1/admin")]
    assert "redirect_blocked" in asyncio.run(scrape.run_scrape("https://example.com/list"))["error"]


# ---- load more: next page, load-more button, scroll ---------------------------------------------------------------
def scrape_more(action, **kw):
    return asyncio.run(scrape.run_scrape("https://example.com/list", max_pages=kw.pop("max_pages", 5),
                                         load_more_action=action, **kw))


def test_next_link_pages_are_followed_and_merged(fake_browser):
    fake_browser.pages = [node_page(["a", "b"], nextHref="https://example.com/p2"),
                          node_page(["c", "d"], url="https://example.com/p2", nextHref="https://example.com/p3"),
                          node_page(["e"], url="https://example.com/p3")]
    r = scrape_more("next")
    assert [x["title"] for x in r["rows"]] == list("abcde") and r["pages"] == 3 and fake_browser.closed


def test_load_more_button_rounds_reuse_the_same_page(fake_browser):
    fake_browser.pages = [node_page(["a", "b"], acted=True), node_page(["a", "b", "c", "d"], acted=True),
                          node_page(["a", "b", "c", "d"], acted=True)]
    r = scrape_more("more")
    assert [x["title"] for x in r["rows"]] == list("abcd")  # a repeat of the same rows ends the loop
    assert r["pages"] == 2 and [js_only for _url, js_only in fake_browser.log] == [False, True, True]


def test_next_link_to_another_site_is_not_followed(fake_browser):
    for target in ("https://other.example.org/p2", "http://127.0.0.1/admin"):
        fake_browser.pages = [node_page(["a", "b", "c"], nextHref=target)]
        r = scrape_more("next")
        assert r["items"] == 3 and r["pages"] == 1 and "another site" in r["skipped"][0]["reason"]


def test_next_link_that_fails_the_public_address_check_is_not_followed(fake_browser, monkeypatch):
    async def refuse(url):
        if "private" in url:
            raise config.UrlError("not a public address")
        return url

    monkeypatch.setattr(scrape, "avalidate_public_url", refuse)
    fake_browser.pages = [node_page(["a", "b", "c"], nextHref="https://example.com/private")]
    r = scrape_more("next")
    assert r["pages"] == 1 and "public address" in r["skipped"][0]["reason"]


def test_a_repeated_next_link_does_not_loop(fake_browser):
    fake_browser.pages = [node_page(["a"], nextHref="https://example.com/list"), node_page(["b"])]
    assert scrape_more("next")["pages"] == 1  # the link points at the page we are already on


def test_no_button_means_no_extra_pages(fake_browser):
    fake_browser.pages = [node_page(["a", "b", "c"], acted=False)]
    r = scrape_more("next")
    assert r["items"] == 3 and r["pages"] == 1 and "skipped" in r and not r["skipped"]


def test_page_and_row_caps_are_respected(fake_browser):
    fake_browser.pages = [node_page(["a"], acted=True), node_page(["a", "b"], acted=True),
                          node_page(["a", "b", "c"], acted=True), node_page(["a", "b", "c", "d"], acted=True)]
    assert scrape_more("scroll", max_pages=3)["pages"] == 3
    fake_browser.pages = [node_page(["a"], acted=True), node_page(["a", "b", "c", "d"], acted=True)]
    assert scrape_more("scroll", max_items=2)["items"] == 2


def test_load_more_is_ignored_when_only_the_html_fallback_ran(fake_browser):
    fake_browser.pages = [page(html=TABLE)]
    r = scrape_more("next")
    assert r["method"] == "html" and r["items"] == 4 and r["pages"] == 1


def test_a_failed_follow_up_page_keeps_the_rows_already_read(fake_browser):
    fake_browser.pages = [node_page(["a", "b"], nextHref="https://example.com/p2"),
                          page(success=False, failure="blocked_by_site (HTTP 403)")]
    r = scrape_more("next")
    assert r["ok"] and r["items"] == 2 and "403" in r["skipped"][0]["reason"]


# ---- scrape each URL: details of every linked page ---------------------------------------------------------------
DETAIL_HTML = """<html><head><title>Blue Mug | Shop</title><meta name="description" content="A sturdy mug">
<meta property="og:image" content="https://example.com/mug.png"></head><body><nav>Home Cart</nav><main><h1>Blue Mug</h1>
<dl><dt>Brand:</dt><dd>Acme</dd><dt>Stock</dt><dd>12 left</dd></dl>
<table><tr><th>Color</th><td>Blue</td></tr><tr><td>no</td><td>label</td></tr></table>
<p>Holds <b>350 ml</b> of coffee.</p></main><footer>Contact us</footer></body></html>"""


def test_page_details_reads_meta_pairs_and_main_text():
    d = scrape.page_details(DETAIL_HTML)
    assert d["page title"] == "Blue Mug | Shop" and d["page heading"] == "Blue Mug"
    assert d["page description"] == "A sturdy mug" and d["page image"] == "https://example.com/mug.png"
    assert d["page brand"] == "Acme" and d["page stock"] == "12 left" and d["page color"] == "Blue"
    assert "350 ml" in d["page text"] and "Contact us" not in d["page text"] and "Home Cart" not in d["page text"]
    assert scrape.page_details("") == {}


def test_detail_url_column_picks_the_first_link_column():
    rows = [{"name": "A", "name href": "https://example.com/a", "img src": "https://example.com/a.png"},
            {"name": "B", "name href": "https://example.com/b", "img src": "https://example.com/b.png"}]
    assert scrape.detail_url_column(["name", "name href", "img src"], rows) == "name href"
    assert scrape.detail_url_column(["name"], rows) == ""


def link_rows(n):
    return [{"title": f"T{i}", "title href": f"https://example.com/item/{i}"} for i in range(1, n + 1)]


def scrape_details_for(rows, **kw):
    out = {"skipped": []}
    columns = ["title", "title href"]

    async def go():
        async with FakeFetcher() as f:
            return await scrape.scrape_details(f, out, rows, columns, "title href", "https://example.com/list",
                                               kw.get("cap", 10), time.time())

    return asyncio.run(go()), out, columns


def test_scrape_details_adds_columns_and_respects_the_cap(fake_browser):
    rows = link_rows(5)
    fake_browser.pages = [page(html=DETAIL_HTML, url=r["title href"]) for r in rows[:3]]
    done, out, columns = scrape_details_for(rows, cap=3)
    assert done == 3 and rows[0]["page title"] == "Blue Mug | Shop" and "page title" not in rows[4]
    assert {"page title", "page brand", "page text"} <= set(columns) and not out["skipped"]


def test_scrape_details_skips_other_sites_failed_pages_and_bad_redirects(fake_browser):
    rows = link_rows(3) + [{"title": "X", "title href": "https://elsewhere.org/x"}]
    fake_browser.pages = [page(success=False, failure="blocked_by_site (HTTP 403)"),
                          page(html=DETAIL_HTML, url="http://127.0.0.1/secret"), page(html=DETAIL_HTML)]
    done, out, _ = scrape_details_for(rows)
    reasons = sorted(s["reason"] for s in out["skipped"])
    assert done == 1 and len(reasons) == 3
    assert any("another site" in r for r in reasons) and any("403" in r for r in reasons) and any("redirect_blocked" in r for r in reasons)


def test_run_scrape_each_url_end_to_end(fake_browser):
    rows = [{"c0": f"T{i}", "c1": f"https://example.com/item/{i}"} for i in range(1, 4)]
    lst = {"selector": "ul > li", "count": 3, "score": 1, "columns": [{"id": "c0", "name": "title"}, {"id": "c1", "name": "title href"}], "rows": rows}
    node = json.dumps({"ok": True, "lists": [lst]})
    fake_browser.pages = [page(html=f'<script id="__scrape_result" type="application/json">{node}</script>')] + \
                         [page(html=DETAIL_HTML) for _ in range(3)]
    r = asyncio.run(scrape.run_scrape("https://example.com/list", each_url=True, detail_max=2))
    assert r["details"] == {"column": "title href", "fetched": 2, "limit": 2}
    assert "page brand" in r["columns"] and r["rows"][0]["page brand"] == "Acme" and "page brand" not in r["rows"][2]


# ---- templates --------------------------------------------------------------------------------------------------
@pytest.fixture
def tpl_file(tmp_path, monkeypatch):
    path = tmp_path / "templates.json"
    monkeypatch.setattr(scrape, "SCRAPE_TEMPLATES_FILE", path)
    return path


def test_template_save_load_round_trip_by_host(tpl_file):
    assert scrape.get_template("https://www.shop.test/a", "main") is None
    scrape.save_template("https://www.shop.test/a", "main", "ul > li", {"action": "next", "selector": ""},
                         [{"name": "title", "as": "Name"}])
    tpl = scrape.get_template("https://shop.test/other/page?x=1", "main")  # same host, www and path do not matter
    assert tpl["itemsSelector"] == "ul > li" and tpl["loadMore"]["action"] == "next" and tpl["columns"][0]["as"] == "Name"
    assert scrape.get_template("https://other.test/", "main") is None
    scrape.save_template("https://shop.test/", "second", "div > p")
    assert set(scrape.load_templates()["shop.test"]) == {"main", "second"}
    assert not list(tpl_file.parent.glob("*.tmp"))


def test_template_needs_a_name_and_a_selector_and_survives_a_corrupt_file(tpl_file):
    with pytest.raises(ValueError):
        scrape.save_template("https://shop.test/", "   ", "ul > li")
    with pytest.raises(ValueError):
        scrape.save_template("https://shop.test/", "ok", "")
    tpl_file.write_text("{not json", encoding="utf-8")
    assert scrape.load_templates() == {}
    scrape.save_template("https://shop.test/", "ok", "ul > li")  # a corrupt file is replaced, not fatal
    assert scrape.get_template("https://shop.test/", "ok")


def test_apply_template_columns_renames_drops_and_fills_gaps():
    tpl = {"columns": [{"name": "title", "as": "Name"}, {"name": "gone", "as": "Gone"}, {"name": "price", "as": ""}]}
    cols, rows = scrape.apply_template_columns(["title", "price", "extra"], [{"title": "A", "price": "1", "extra": "x"}], tpl)
    assert cols == ["Name", "price"] and rows == [{"Name": "A", "price": "1"}]
    assert scrape.apply_template_columns(["a"], [{"a": "1"}], {"columns": None}) == (["a"], [{"a": "1"}])
    assert scrape.apply_template_columns(["a"], [{"a": "1"}], {"columns": [{"name": "zz", "as": "q"}]}) == (["a"], [{"a": "1"}])


def test_run_scrape_uses_a_template_selector_and_its_columns(fake_browser, tpl_file):
    scrape.save_template("https://example.com/list", "t", "ul > li", {"action": "none", "selector": ""},
                         [{"name": "title", "as": "Name"}])
    fake_browser.pages = [node_page(["a", "b", "c"])]
    r = asyncio.run(scrape.run_scrape("https://example.com/list", template="t"))
    assert r["ok"] and r["template"] == "t" and r["columns"] == ["Name"] and r["rows"][0] == {"Name": "a"}
    assert len(fake_browser.log) == 1  # one load: the selector worked, no second detection


def test_run_scrape_falls_back_to_detection_when_the_saved_selector_matches_nothing(fake_browser, tpl_file):
    scrape.save_template("https://example.com/list", "t", "ul.gone > li")
    empty = json.dumps({"ok": True, "lists": []})
    fake_browser.pages = [page(html=f'<script id="__scrape_result" type="application/json">{empty}</script>'),
                          node_page(["a", "b", "c"])]
    r = asyncio.run(scrape.run_scrape("https://example.com/list", template="t"))
    assert r["ok"] and "matched nothing" in r["template_note"] and "template" not in r and r["items"] == 3


def test_run_scrape_reports_an_unknown_template_and_saves_a_new_one(fake_browser, tpl_file):
    r = asyncio.run(scrape.run_scrape("https://example.com/list", template="missing"))
    assert not r.get("ok") and "No template named 'missing'" in r["error"]
    fake_browser.pages = [node_page(["a", "b", "c"])]
    r = asyncio.run(scrape.run_scrape("https://example.com/list", save_as="mine"))
    assert r["saved_template"] == "mine" and scrape.get_template("https://example.com/", "mine")["itemsSelector"] == "ul > li"
    fake_browser.pages = [page(html=TABLE)]  # html fallback: its selector is a guess, so nothing is saved
    r = asyncio.run(scrape.run_scrape("https://example.com/list", save_as="guess"))
    assert "saved_template" not in r and scrape.get_template("https://example.com/", "guess") is None


# ---- optional askg1 list choice -----------------------------------------------------------------------------------
TWO_LISTS = [{"columns": ["name", "price"], "rows": [{"name": "Mug", "price": "$5"}] * 4},
             {"columns": ["link"], "rows": [{"link": "Home"}] * 3}]


def test_pick_prompt_is_compact_and_numbers_the_lists():
    wide = [{"columns": [f"c{i}" for i in range(30)], "rows": [{f"c{i}": "x" * 500 for i in range(30)}] * 3}] * 5
    prompt = scrape.pick_prompt(wide, "the products " * 100)
    assert prompt.count("\n") < 20 and "5. 3 rows" in prompt and len(prompt) < 3000 and len(prompt) // 4 < 1000
    assert "1. 4 rows; columns: name, price; first row: Mug | $5" in scrape.pick_prompt(TWO_LISTS, "products")


@pytest.mark.parametrize("reply,expected", [("2", 1), ("List 1.", 0), ("I think 2 is best", 1), ("7", None),
                                            ("0", None), ("none", None), ("", None)])
def test_pick_list_accepts_only_a_valid_list_number(reply, expected):
    async def chat(prompt, max_tokens=8):
        return reply

    assert asyncio.run(scrape.pick_list(TWO_LISTS, "products", chat)) == expected


def test_pick_list_survives_an_unavailable_model():
    async def broken(prompt, max_tokens=8):
        raise RuntimeError("askg1 is down")

    assert asyncio.run(scrape.pick_list(TWO_LISTS, "products", broken)) is None


def two_list_page():
    def lst(name, vals):
        return {"selector": name, "count": len(vals), "score": 1, "columns": [{"id": "c0", "name": "title"}],
                "rows": [{"c0": v} for v in vals]}

    node = json.dumps({"ok": True, "lists": [lst("nav > li", ["Home", "About", "Help"]), lst("ul > li", ["Mug", "Cup", "Jug"])]})
    return page(html=f'<script id="__scrape_result" type="application/json">{node}</script>')


def test_run_scrape_lets_the_model_choose_between_lists(fake_browser):
    async def chat(prompt, max_tokens=8):
        return "2"

    fake_browser.pages = [two_list_page()]
    r = asyncio.run(scrape.run_scrape("https://example.com/list", instruction="the products", chat=chat))
    assert r["chosen"] == 1 and r["picked_by"] == "askg1" and r["rows"][0]["title"] == "Mug"


def test_an_explicit_list_index_or_a_missing_instruction_skips_the_model(fake_browser):
    async def chat(prompt, max_tokens=8):
        raise AssertionError("the model must not be called")

    fake_browser.pages = [two_list_page()]
    r = asyncio.run(scrape.run_scrape("https://example.com/list", list_index=1, instruction="the products", chat=chat))
    assert r["chosen"] == 1 and "picked_by" not in r
    fake_browser.pages = [two_list_page()]
    r = asyncio.run(scrape.run_scrape("https://example.com/list", chat=chat))
    assert r["chosen"] == 0 and "picked_by" not in r


# ---- title / alt columns, duplicate columns, page details, saved HTML ---------------------------------------------
SHORTENED = """<html><body><ol>""" + "".join(
    f'<li class="b"><a href="/p/{i}"><img src="/i/{i}.jpg" alt="Full name {i}"></a>'
    f'<h3><a href="/p/{i}" title="Full name {i}">Full na...</a></h3><p class="pr">£{i}.00</p></li>' for i in range(1, 5)
) + "</ol></body></html>"


def test_fallback_reads_full_names_from_title_and_alt_and_drops_repeated_columns():
    best = scrape.detect_lists_html(SHORTENED, "https://shop.test/")[0]
    assert "Full name 2" in best["rows"][1].values() and "Full na..." in best["rows"][1].values()
    hrefs = [c for c in best["columns"] if c.endswith("href")]
    assert len(hrefs) == 1  # the image link and the title link point to the same page: one column
    titles = [c for c in best["columns"] if c.endswith("title") or c.endswith("alt")]
    assert len(titles) == 1  # the title attribute and the alt text hold the same full name: one column


def test_run_page_details_returns_one_row(fake_browser):
    fake_browser.pages = [page(html=DETAIL_HTML)]
    r = asyncio.run(scrape.run_page_details("https://example.com/item/1"))
    assert r["ok"] and r["items"] == 1 and r["rows"][0]["page brand"] == "Acme" and r["columns"][0] == "page title"


def test_run_page_details_reports_blocked_and_empty_pages(fake_browser):
    fake_browser.pages = [page(html="", markdown="Just a moment... captcha verify you are human")]
    assert "blocked" in asyncio.run(scrape.run_page_details("https://example.com/x"))["error"].lower()
    fake_browser.pages = [page(html="<html></html>", markdown="")]
    assert "No details" in asyncio.run(scrape.run_page_details("https://example.com/x"))["error"]


def test_run_scrape_html_makes_no_request_for_a_url_and_checks_its_inputs(fake_browser):
    node = json.dumps({"ok": True, "lists": [{"selector": "ul > li", "count": 3, "score": 1,
                                              "columns": [{"id": "c0", "name": "t"}], "rows": [{"c0": "a"}, {"c0": "b"}, {"c0": "c"}]}]})
    fake_browser.pages = [page(html=f'<script id="__scrape_result" type="application/json">{node}</script>')]
    r = asyncio.run(scrape.run_scrape_html("<ul><li>a</li></ul>", "https://shop.test/"))
    assert r["ok"] and r["source"] == "html" and r["items"] == 3
    url, _ = fake_browser.log[0]
    assert url.startswith("raw:<base href=\"https://shop.test/\">")
    assert "base URL" in asyncio.run(scrape.run_scrape_html("<p>x</p>", "javascript:alert(1)"))["error"]
    assert "larger" in asyncio.run(scrape.run_scrape_html("x" * (scrape.MAX_HTML_BYTES + 1)))["error"]


def test_run_scrape_html_page_details_and_fallback_without_the_detector(fake_browser):
    r = asyncio.run(scrape.run_scrape_html(DETAIL_HTML, details_only=True))
    assert r["ok"] and r["rows"][0]["page heading"] == "Blue Mug"
    fake_browser.pages = [page(html="<html></html>")]  # detector did not answer: BeautifulSoup version on the text
    r = asyncio.run(scrape.run_scrape_html(TABLE))
    assert r["ok"] and r["method"] == "html" and r["items"] == 4
