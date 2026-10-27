"""Offline unit tests: no askg1, no network, no browser."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, tasks  # noqa: E402
from app.crawl import _from_api, classify_failure, looks_blocked, slugify  # noqa: E402
from app.extract import first_chunk  # noqa: E402


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/", "http://localhost/", "http://10.0.0.5/", "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data", "http://100.100.100.100:8080/v1",  # CGNAT range (VPN/Tailscale)
    "file:///etc/passwd", "ftp://example.com/", "javascript:alert(1)", "http:///nohost",
])
def test_private_and_odd_urls_blocked(url):
    with pytest.raises(config.UrlError):
        config.validate_public_url(url)


def test_private_urls_allowed_with_explicit_override(monkeypatch):
    monkeypatch.setenv("ALLOW_PRIVATE_URLS", "1")
    assert config.validate_public_url("http://10.0.0.5/") == "http://10.0.0.5/"


def test_clamp_crawl_limits():
    assert config.clamp_crawl(None, None) == (config.DEFAULT_CRAWL_PAGES, config.DEFAULT_CRAWL_DEPTH)
    assert config.clamp_crawl(10_000, 99) == (config.MAX_CRAWL_PAGES, config.MAX_CRAWL_DEPTH)
    assert config.clamp_crawl(0, -5) == (config.DEFAULT_CRAWL_PAGES, 0)  # 0 pages falls back to default


def test_clamp_steps():
    assert config.clamp_steps(None) == config.DEFAULT_MAX_STEPS
    assert config.clamp_steps(99) == config.MAX_STEPS_CAP


def test_settings_never_exceed_context_budget():
    s = config.SETTINGS
    assert s.max_output_tokens < s.context_tokens
    assert config.estimate_tokens("x" * s.dom_chars) + s.max_output_tokens < s.context_tokens


def test_crawl_only_when_askg1_not_configured(monkeypatch):
    monkeypatch.delenv("ASKG1_BASE_URL", raising=False)
    s = config.load_settings(env_file=None)
    assert s.askg1_enabled is False and s.base_url == "" and s.max_output_tokens == 0


def test_askg1_can_be_force_disabled(monkeypatch):
    monkeypatch.setenv("ASKG1_BASE_URL", "http://example.invalid/v1")
    monkeypatch.setenv("ASKG1_MODEL", "m")
    monkeypatch.setenv("ASKG1_DISABLED", "1")
    assert config.load_settings(env_file=None).askg1_enabled is False
    monkeypatch.delenv("ASKG1_DISABLED")
    assert config.load_settings(env_file=None).askg1_enabled is True


def test_base_url_must_end_in_v1(monkeypatch):
    monkeypatch.setenv("ASKG1_BASE_URL", "http://example.invalid/api")
    monkeypatch.setenv("ASKG1_MODEL", "m")
    monkeypatch.delenv("ASKG1_DISABLED", raising=False)
    with pytest.raises(config.ConfigError):
        config.load_settings(env_file=None)


def test_forbidden_provider_keys_are_listed():
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY"):
        assert k in config.FORBIDDEN_KEYS


def test_slugify_unique_and_safe():
    used: set[str] = set()
    a = slugify("https://docs.example.com/a/b?x=1", used)
    b = slugify("https://docs.example.com/a/b?x=2", used)
    assert a != b and all(c.isalnum() or c == "-" for c in a + b)


def test_looks_blocked():
    assert looks_blocked("Just a moment... Verify you are human")
    assert not looks_blocked("word " * 200 + "captcha")  # long real page mentioning captcha is fine


def test_classify_failure():
    R = lambda **k: _from_api({"url": "u", **k})  # noqa: E731
    assert classify_failure(R(error_message="Access denied by robots.txt", status_code=403)) == "robots_disallowed"
    assert classify_failure(R(status_code=429)).startswith("blocked_by_site")
    assert classify_failure(R(error_message="Blocked by anti-bot protection: Structural", status_code=200)).startswith("blocked_by_site")
    assert classify_failure(R(error_message="Timeout 30000ms", status_code=None)) == "timeout"


def test_from_api_adapter_lowercases_headers_and_reads_markdown():
    r = _from_api({"url": "u", "success": True, "markdown": {"raw_markdown": "# T"},
                   "response_headers": {"Content-Type": "text/html"}})
    assert r.markdown.raw_markdown == "# T" and r.response_headers["content-type"] == "text/html"


def test_browse_task_allows_json_in_instruction_and_has_no_scrape_leftovers():
    t = tasks.browse_task("https://x.test", "Return the title as JSON")
    assert "https://x.test" in t and "Return the title as JSON" in t and "JSON" in t
    assert not hasattr(tasks, "scrape_task") and not hasattr(tasks, "validate")


def test_first_chunk_strips_links_and_respects_limit():
    md = "<!-- source: x -->\n\n# Title\n\nSee [docs](https://x.y/z) and ![img](a.png)\n" + "word " * 1000
    chunk = first_chunk(md, 300)
    assert len(chunk) <= 300 and "https://" not in chunk and "<!--" not in chunk and "docs" in chunk


def test_system_prompt_is_small_and_lists_only_allowed_actions():
    assert config.estimate_tokens(tasks.SYSTEM_PROMPT) < 400
    assert "extract" not in tasks.ALLOWED_ACTIONS
    json.dumps(sorted(tasks.ALLOWED_ACTIONS))


def test_looks_login():
    from app.crawl import looks_login
    assert looks_login("# Log in\nEmail*\nPassword*\nLog in")
    assert not looks_login("word " * 300 + " password log in")  # long docs page mentioning passwords
    assert not looks_login("# Find people\nName contains")


def test_load_session_scopes_to_host(monkeypatch):
    from app.crawl import load_session
    monkeypatch.setattr("app.config.ALLOW_SESSION", True)
    monkeypatch.delenv("CRAWL_STORAGE_STATE_FILE", raising=False)
    monkeypatch.setenv("CRAWL_STORAGE_STATE", json.dumps({
        "cookies": [{"name": "a", "value": "1", "domain": "app.example.com", "path": "/"},
                    {"name": "b", "value": "2", "domain": "evil.com", "path": "/"}],
        "origins": [{"origin": "https://app.example.com", "localStorage": []},
                    {"origin": "https://other.com", "localStorage": []}]}))
    monkeypatch.setenv("CRAWL_COOKIES", "sid=xyz; theme=dark")
    s = load_session("app.example.com")
    assert [c["name"] for c in s["cookies"]] == ["a", "sid", "theme"]
    assert [o["origin"] for o in s["origins"]] == ["https://app.example.com"]


def test_load_session_none_when_unset(monkeypatch):
    from app.crawl import load_session
    for k in ("CRAWL_STORAGE_STATE", "CRAWL_STORAGE_STATE_FILE", "CRAWL_COOKIES"):
        monkeypatch.delenv(k, raising=False)
    assert load_session("x.com") is None


def test_unsafe_url_filter():
    from crawl4ai.deep_crawling import URLPatternFilter
    from app.crawl import UNSAFE_URL_GLOBS
    f = URLPatternFilter(UNSAFE_URL_GLOBS, use_glob=True, reverse=True)
    for u in ("https://a.com/logout", "https://a.com/auth/google", "https://a.com/api/samples/people", "https://a.com/sign-out"):
        assert not f.apply(u), u
    for u in ("https://a.com/inbox?folder=activity", "https://a.com/sequences/new", "https://a.com/blog/catalog"):
        assert f.apply(u), u


def test_distinct_titles(tmp_path):
    from app.crawl import distinct_titles
    d = tmp_path / "docs_md"
    d.mkdir()
    (d / "a.md").write_text("<!-- x -->\n\n# Home page\ntext", encoding="utf-8")
    (d / "b.md").write_text("no heading here", encoding="utf-8")
    (d / "c.md").write_text("# Solo", encoding="utf-8")
    pages = [{"url": "https://a.com/home", "title": "App", "file": "docs_md/a.md"},
             {"url": "https://a.com/tasks", "title": "App", "file": "docs_md/b.md"},
             {"url": "https://a.com/x", "title": "Unique", "file": "docs_md/c.md"}]
    distinct_titles(pages, d)
    assert [p["title"] for p in pages] == ["Home page", "tasks", "Unique"]


def test_session_refused_unless_enabled(monkeypatch):
    from app.crawl import load_session
    monkeypatch.setattr("app.config.ALLOW_SESSION", False)
    monkeypatch.setenv("CRAWL_COOKIES", "sid=xyz")
    with pytest.raises(config.ConfigError):
        load_session("x.com")


def test_clean_markdown_drops_json_blob_and_widget_text():
    from app.crawl import clean_markdown
    blob = '{"docsConfig":' + ('"a":"b",' * 400) + '}'
    md = f"Assistant\nResponses are generated using AI and may contain mistakes.\n# Title\nCopy pageCopy page\nBody text.\n\n\n\n{blob}\n"
    out = clean_markdown(md)
    assert out == "# Title\nBody text."
    assert clean_markdown("Use `{...}` in code\n" + '{"x":1}') == 'Use `{...}` in code\n{"x":1}'  # short JSON kept
