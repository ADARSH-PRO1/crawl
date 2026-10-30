"""Offline tests for similar-tools mode: no askg1, no SearXNG, no browser."""
import asyncio
import csv
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, similar  # noqa: E402


def test_pick_evidence_prefers_matching_lines_and_strips_markup():
    md = ("<!-- source: x -->\n\n" + "Intro line that says nothing useful about anything here.\n" * 3
          + "[Comet](https://c.com) is a Chromium based agentic browser from Perplexity for research.\n"
          + "Unrelated footer text about cookies and privacy policy terms of service.\n")
    out = similar.pick_evidence(md, "agentic browser Chromium Comet", max_chars=120)
    assert "Comet is a Chromium" in out and "footer" not in out and "https://" not in out and "<!--" not in out


def test_pick_evidence_falls_back_to_page_start():
    md = "Totally different words appear on this page, nothing matches the query at all."
    assert similar.pick_evidence(md, "zzzz qqqq") == md


def test_parse_profile_reads_fields_and_falls_back():
    p = similar.parse_profile("NAME: BrowserOS\nDOES: Open-source agentic browser\nKEYWORDS: browser, agent, privacy",
                              "Fallback", "find the similar tools")
    assert p == {"name": "BrowserOS", "does": "Open-source agentic browser", "keywords": ["browser", "agent", "privacy"]}
    q = similar.parse_profile("garbage", "Fallback", "find open source editors")
    assert q["name"] == "Fallback" and "editors" in q["keywords"]


def test_build_queries_include_a_github_engine_query():
    qs = similar.build_queries({"name": "BrowserOS", "does": "", "keywords": ["agentic", "browser"]})
    assert ("alternatives to BrowserOS", None) in qs
    assert ("agentic browser", ["github"]) in qs and len(qs) == 5


def test_extract_names_only_keeps_names_present_in_the_text():
    evidence = "Alternatives include Perplexity Comet, Dia and Fellou. BrowserOS is the baseline."
    reply = "Perplexity Comet, Dia, Fellou, Imaginary Browser, BrowserOS, browser"
    assert similar.extract_names(reply, evidence, "BrowserOS") == ["Perplexity Comet", "Dia", "Fellou"]


def test_extract_names_none_and_bullets():
    assert similar.extract_names("NONE", "x", "T") == []
    assert similar.extract_names("- Alpha\n- Beta tool", "Alpha and Beta tool here", "T") == ["Alpha", "Beta tool"]


def test_merge_names_ranks_by_pages_then_engines():
    per_page = [{"url": "https://a", "engines": ["bing"], "names": ["Comet", "Dia"]},
                {"url": "https://b", "engines": ["brave", "yahoo"], "names": ["comet", "Fellou"]},
                {"url": "https://c", "engines": ["bing"], "names": ["Comet"]}]
    ranked = similar.merge_names(per_page)
    assert [r["name"] for r in ranked] == ["Comet", "Fellou", "Dia"]
    assert ranked[0]["mentions"] == 3 and ranked[0]["engines"] == ["bing", "brave", "yahoo"]


def test_find_link_for_name_skips_aggregators_and_other_names():
    md = ("[Dia](https://alternativeto.net/software/dia/) and [Dia Browser](https://diabrowser.com/) "
          "and [Fellou](https://fellou.ai/#top) and [Other](https://other.com)")
    assert similar.find_link_for_name(md, "Dia") == "https://diabrowser.com/"
    assert similar.find_link_for_name(md, "Fellou") == "https://fellou.ai/"
    assert similar.find_link_for_name(md, "Missing") is None


def test_github_repo_from_links_needs_owner_and_repo_and_a_name_match():
    md = ("[org](https://github.com/features/actions) [x](https://github.com/browseros-ai) "
          "[repo](https://github.com/browseros-ai/BrowserOS.git) [o](https://github.com/other/thing)")
    assert similar.github_repo_from_links(md, "BrowserOS") == "https://github.com/browseros-ai/BrowserOS"
    assert similar.github_repo_from_links(md, "Nothing Matching") is None


def test_parse_row_reply():
    assert similar.parse_row_reply("SIMILAR: Also an agentic browser.\nDIFFERENT: Closed source.") == (
        "Also an agentic browser.", "Closed source.")
    assert similar.parse_row_reply("SIMILAR: -\nDIFFERENT: -") == ("", "")
    assert similar.parse_row_reply("no labels at all") == ("", "")


def test_table_renderers():
    rows = [{"name": "A|B", "similar": "s", "different": "", "url": "https://a.com", "github": ""}]
    md = similar.to_markdown(rows)
    assert md.splitlines()[0] == "| Name | How it is similar | Key difference | URL | GitHub repo |"
    assert "A/B" in md and "| - |" in md
    parsed = list(csv.reader(io.StringIO(similar.to_csv(rows))))
    assert parsed[0][0] == "Name" and parsed[1][3] == "https://a.com"


def test_relevance_prefers_pages_about_alternatives_to_the_target():
    profile = {"name": "BrowserOS", "does": "", "keywords": ["agentic", "browser"]}
    good = {"title": "10 best BrowserOS alternatives", "content": "agentic browsers like Comet"}
    junk = {"title": "Alternative | Definition", "content": "a dictionary entry about choices"}
    assert similar.relevance(good, profile) >= 2 > similar.relevance(junk, profile)


def test_github_candidates_skip_the_target_and_non_repos():
    hits = [{"url": "https://github.com/browseros-ai/BrowserOS", "content": "the target"},
            {"url": "https://github.com/browser-use/browser-use", "content": "Make websites accessible for AI agents"},
            {"url": "https://github.com/topics/agent", "content": "topic page"},
            {"url": "https://github.com/browser-use/browser-use/", "content": "duplicate"},
            {"url": "https://github.com/skyvern-ai/skyvern/issues/1", "content": "an issue, not a repo"}]
    out = similar.github_candidates(hits, "https://github.com/browseros-ai/BrowserOS")
    assert [c["name"] for c in out] == ["browser-use"]
    assert out[0]["url"] == "https://github.com/browser-use/browser-use" and out[0]["engines"] == ["github"]


def test_official_match_rejects_look_alike_domains():
    profile = {"name": "T", "does": "agentic AI browser", "keywords": ["browser", "agentic"]}
    ok = {"url": "https://www.diabrowser.com/", "title": "Dia: the AI browser", "content": "an AI browser"}
    gov = {"url": "https://www.dia.mil/", "title": "Defense Intelligence Agency", "content": "intelligence for defense"}
    other = {"url": "https://example.org/dia", "title": "Dia browser", "content": "agentic browser"}
    assert similar.official_match("Dia", ok, profile)
    assert not similar.official_match("Dia", gov, profile)
    assert not similar.official_match("Dia", other, profile)  # domain is not the tool's name
    assert similar.official_match("Perplexity Comet", {"url": "https://www.perplexity.ai/comet", "title": "Comet browser",
                                                       "content": "AI browser"}, profile)


def test_repo_is_the_tool_rejects_wrappers_and_look_alikes():
    assert similar.repo_is_the_tool("Skyvern", "Skyvern-AI", "skyvern")
    assert similar.repo_is_the_tool("Fellou", "x", "fellou-browser")
    assert not similar.repo_is_the_tool("Comet", "hanzili", "comet-mcp")
    assert not similar.repo_is_the_tool("Dia", "c", "diagram-design")
    assert not similar.repo_is_the_tool("Dia", "topics", "dia")


def test_clean_row_drops_repeats_and_copies_of_the_target_description():
    does = "An open-source agentic browser that runs tasks inside the page"
    assert similar.clean_row("Also an agentic browser.", "Also an agentic browser!", does) == ("Also an agentic browser.", "")
    assert similar.clean_row("open-source agentic browser that runs tasks inside the page", "Closed source.", does) == ("", "Closed source.")
    assert similar.clean_row("Cloud based.", "Needs an account.", does) == ("Cloud based.", "Needs an account.")


def test_grounded_drops_cells_with_invented_names():
    assert similar.grounded("Runs its own model, but BrowserOS uses the model from Bizfy.", "runs its own model", "BrowserOS") == ""
    assert similar.grounded("Both run agents. Skyvern uses vision.", "skyvern uses vision", "BrowserOS") == "Both run agents. Skyvern uses vision."
    assert similar.grounded("", "x") == ""


def test_strip_tracking_keeps_github_query_free_and_cleans_site_urls():
    assert similar.strip_tracking("https://www.opera.com/download?msockid=2a33#x") == "https://www.opera.com/download"
    assert similar.strip_tracking("https://github.com/a/b#readme") == "https://github.com/a/b"


def test_repo_fits_uses_target_topic_or_the_candidates_own_text():
    topic = {"browser", "agentic"}
    assert similar.repo_fits("An agentic browser", topic, "")
    assert similar.repo_fits("Next-gen knowledge base with docs and whiteboards", topic,
                             "all-in-one workspace: docs, whiteboards and a knowledge base")
    assert not similar.repo_fits("A Python GraphQL library", topic, "AI companion that works inside the browser")


def test_image_and_file_links_are_never_an_official_site():
    assert not similar.is_official_candidate("https://static.example.com/uploads/logo.png")
    assert similar.is_official_candidate("https://docmost.com/")
    assert similar.find_link_for_name("[Docmost](https://cdn.example.com/docmost-logo.png) [Docmost](https://docmost.com/)",
                                      "Docmost") == "https://docmost.com/"


def test_asset_hosts_are_not_official_sites():
    for u in ("https://avatars.githubusercontent.com/u/86002201", "https://repository-images.githubusercontent.com/1/2",
              "https://www.google.com/s2/favicons?domain=x.com", "https://d1234.cloudfront.net/logo"):
        assert not similar.is_official_candidate(u), u


def test_brand_host_is_the_targets_own_site():
    profile = {"name": "BrowserOS", "does": "", "keywords": []}
    assert similar.brand_host("https://www.browseros.com/open-source/", profile)
    assert not similar.brand_host("https://www.firecrawl.dev/blog/best", profile)


def _patch(monkeypatch, search_results, pages, replies):
    """search_results: dict query-or-'*' -> search_web result; pages: url -> read_page dict; replies: askg1 call order."""
    it = iter(replies)
    prompts: list[str] = []

    async def fake_chat(prompt, system=None, max_tokens=200):
        prompts.append(prompt)
        return next(it)

    async def fake_search(query, engines=None, limit=20):
        return search_results.get(query) or search_results.get("*") or {"results": [], "failed": []}

    async def fake_read(url, out_root):
        return pages.get(url, {"skipped": "no content"})

    monkeypatch.setattr(similar, "chat_once", fake_chat)
    monkeypatch.setattr(similar, "search_web", fake_search)
    monkeypatch.setattr(similar, "read_page", fake_read)
    return prompts


def run(url, tmp_path, **kw):
    return asyncio.run(similar.run_similar(url, "find the similar tools", out_root=tmp_path, search_delay=0, **kw))


def test_run_similar_builds_a_table_with_links_from_the_pages(monkeypatch, tmp_path):
    hit = {"url": "https://list.example/best", "title": "Best alternatives to Target", "content": "agentic browser", "engines": ["bing", "yahoo"]}
    pages = {
        "https://tool.dev/": {"markdown": "# Target\nAn open source agentic browser.", "title": "Target - home", "url": "https://tool.dev/"},
        "https://list.example/best": {"markdown": "Top picks: [Comet](https://comet.ai) and [Dia](https://dia.io) are both agentic browsers you can use today.\n",
                                      "title": "Best", "url": "https://list.example/best"},
        "https://comet.ai": {"markdown": "Comet is a browser. Source: https://github.com/pplx/comet-browser", "title": "Comet", "url": "https://comet.ai"},
        "https://dia.io": {"markdown": "Dia is a closed source browser from a company.", "title": "Dia", "url": "https://dia.io"},
    }
    replies = ["NAME: Target\nDOES: agentic browser\nKEYWORDS: agentic, browser",
               "Comet, Dia, Invented", "NONE",
               "SIMILAR: Agentic browser.\nDIFFERENT: Closed.", "SIMILAR: Also agentic.\nDIFFERENT: No repo."]
    _patch(monkeypatch, {"*": {"results": [hit], "failed": [["duckduckgo", "CAPTCHA"]]}}, pages, replies)
    r = run("https://tool.dev/", tmp_path)
    assert [x["name"] for x in r["rows"]] == ["Comet", "Dia"]
    comet, dia = r["rows"]
    assert comet["url"] == "https://comet.ai" and comet["github"] == "https://github.com/pplx/comet-browser"
    assert dia["github"] == "" and dia["similar"] == "Also agentic."
    assert "Invented" not in [x["name"] for x in r["rows"]]
    assert r["engines_used"] == ["bing", "yahoo"] and r["engines_failed"] == [{"engine": "duckduckgo", "reason": "CAPTCHA"}]
    assert r["ok"] is False and "Only 2" in r["error"]


def test_run_similar_no_search_results_is_a_clear_error(monkeypatch, tmp_path):
    pages = {"https://tool.dev/": {"markdown": "# Target\nText", "title": "Target", "url": "https://tool.dev/"}}
    _patch(monkeypatch, {}, pages, ["NAME: Target\nDOES: x\nKEYWORDS: a, b"])
    r = run("https://tool.dev/", tmp_path)
    assert not r["ok"] and "no results" in r["error"] and r["rows"] == []


def test_run_similar_blocked_target_page_still_profiles_from_the_url(monkeypatch, tmp_path):
    _patch(monkeypatch, {}, {}, [])
    r = run("https://www.acme.com/products/widget-pro", tmp_path)
    assert r["profile"]["name"] == "Widget Pro"
    assert r["skipped"][0]["reason"].startswith("target page:")


def test_run_similar_top_n_is_capped(monkeypatch, tmp_path):
    names = [f"Tool{i}" for i in range(12)]
    hit = {"url": "https://list.example/all", "title": "All alternatives to T", "content": "a b tools", "engines": ["bing"]}
    text = ("Many tools exist: " + ", ".join(f"[{n}](https://{n.lower()}.example/)" for n in names)
            + " are all agentic browsers worth a look today.\n")
    pages = {"https://tool.dev/": {"markdown": "# T\nx", "title": "T", "url": "https://tool.dev/"},
             "https://list.example/all": {"markdown": text, "title": "All", "url": "https://list.example/all"}}
    replies = ["NAME: T\nDOES: x\nKEYWORDS: a, b", ", ".join(names), "NONE"] + ["SIMILAR: s\nDIFFERENT: d"] * 12
    _patch(monkeypatch, {"*": {"results": [hit], "failed": []}}, pages, replies)
    r = run("https://tool.dev/", tmp_path, top_n=4)
    assert len(r["rows"]) == 4 and r["ok"]


def test_search_web_explains_a_missing_searxng(monkeypatch):
    monkeypatch.setattr(similar, "SEARXNG_URL", "http://127.0.0.1:1")  # nothing listens on port 1
    with pytest.raises(config.ConfigError, match="SearXNG is not reachable"):
        asyncio.run(similar.search_web("x"))
