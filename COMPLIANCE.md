# PDF compliance checklist

Source of truth: "Local Private AI Browser Setup (askg1 + BrowserOS)" (Oct 26, 2026).
Status: OK = enforced/verified in this repo, DEV = documented deviation, YOU = must be done/verified on the machine.

## askg1-only rule
| PDF item | Status | Evidence |
|---|---|---|
| Only `ASKG1_*` vars in `.env`; no other provider keys | OK | `app/config.py` `FORBIDDEN_KEYS` raises `ConfigError`; `scripts/verify_askg1.py` compliance check (PASS 2026-10-27) |
| Every script passes `base_url=ASKG1_BASE_URL` | OK | `app/llm.py` (`get_llm`, `chat_once`), `trust_env=False` |
| Telemetry off | OK | `ANONYMIZED_TELEMETRY=false` enforced in `load_settings`; verify script PASS |
| `.env` out of git | OK | `.gitignore` |
| BrowserOS: askg1 is the only provider (Step 3) | YOU | BrowserOS settings, outside the repo |
| MCP-connected agents use askg1 (Step 4) | YOU | Do not connect Cursor/Claude Code/Codex without lead approval |

## Steps
| Step | Status | Notes |
|---|---|---|
| 1 askg1 reachable (`/models`, chat, tool calling, 20k+) | OK / DEV | `verify_askg1.py` 2026-10-27 18:41 against `100.64.0.2:8080`, model `g1-chat`: /models, completion, tool calling and structured output PASS; 4k context PASS. **FAIL: 16k probe (HTTP 500, server context is 4096) and the 20k check.** Real window is 4096, not 20,000 (see DEV below). Lead should know. |
| 2 BrowserOS install | OK | BrowserOS neo installed 2026-10-27 from browseros.com, fresh profile (no Chrome import). MCP endpoint is `http://127.0.0.1:9010/mcp` on this build (PDF says 9200). |
| 3 BrowserOS provider = askg1 | BLOCKED | Not possible in this build: `chrome://browseros/settings` gives ERR_INVALID_URL, `chrome://settings/browseros-ai` falls back to generic browser settings, and searching settings for "provider" / "browseros" finds no AI provider setting. neo has only "Ask for human help" and "tab group cleanup tip". Needs lead decision (older BrowserOS build with the OpenAI Compatible card?). |
| 4 MCP agents | OK | Setup auto-connected Claude Code, Codex, Cursor and VS Code; all four were disconnected (0 of 4 connected). PDF: only connect agents configured for askg1. |
| 5 BrowserOS first tests | BLOCKED | Needs Step 3. Same tasks were run through our own `browse` mode and did not pass (see Step 6 note: 4096-token model emits invalid actions). |
| 6 Browser Use agent | OK | `ChatOpenAI` + askg1 base URL, `temperature=0`, `use_vision=False`, step cap. Default 15 steps (matches PDF), cap 30 |
| 7a-b Crawl4AI, robots.txt | OK | `check_robots_txt=True`, same-host only, low concurrency |
| 7c Extraction with askg1 | DEV | Own chunked summarizer (`app/extract.py`) instead of `LLMExtractionStrategy`; askg1 only, fits 4096 |
| 7d Docker service | OK | `unclecode/crawl4ai:latest`, auto-detected via `/health`, token set |
| 7e Crawl job output | OK | `docs_md/*.md` + `index.json` (url, title, file), nav and footers removed (default) |
| uv only | OK | `.venv` was created by uv 0.12.10 |

## Deviations
- **Context window 4096 vs PDF 20,000+.** The PDF says askg1's real values win. The agent is built for 4096: compact system prompt, six actions only, capped page text and output. Latency is not a concern, so the step limit is raised (default 15, cap 30) to finish tasks in small steps.
- **Structured output disabled** in `app/llm.py`: askg1 degenerates under JSON-schema decoding (verified earlier). The reply format is taught by the prompt in `app/tasks.py`.
- **Authenticated crawling is not in the PDF.** Off by default. Only with `ALLOW_SESSION=1`, local use, TEST accounts only. It is refused otherwise. Never enable it on a hosted deployment (PDF: no login bypassing, test accounts only, keep data local). When on, nav/aside are kept so sidebar links can be followed, and links containing logout/signout/`/auth/`/`/api/` are skipped.
- **Hosted build.** The Streamlit Cloud build keeps crawled pages on a third party's server. The PDF says keep data local; get lead approval before using it for anything non-public.
- The 2026-10-27 crawl of the SendSafe app used an Admin session before this gate existed. Confirm with the lead that it was a test account.
