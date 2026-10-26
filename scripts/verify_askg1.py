"""Block B: askg1 environment verification and capability probe.

Run from the project root:  python scripts/verify_askg1.py [--out reports/verify_askg1.md]
Exit code: 0 = no FAIL, 1 = at least one FAIL, 2 = compliance violation (fail fast).
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_KEYS = [
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
    "GROQ_API_KEY", "OPENROUTER_API_KEY", "MOONSHOT_API_KEY", "KIMI_API_KEY",
    "MISTRAL_API_KEY", "COHERE_API_KEY", "TOGETHER_API_KEY", "DEEPSEEK_API_KEY",
    "AZURE_OPENAI_API_KEY", "OLLAMA_HOST",
]
TARGET_CTX_TOKENS = 20_000
PROBE_TOKENS = [4_000, 16_000, 22_000]
TIMEOUT = httpx.Timeout(180.0, connect=10.0)

results: list[tuple[str, str, str]] = []


def record(name: str, status: str, details: str) -> None:
    results.append((name, status, details.replace("|", "/").replace("\n", " ")))
    print(f"[{status}] {name}: {details}", file=sys.stderr, flush=True)


def print_table() -> None:
    print("\n| Test | Status | Latency / Details |")
    print("|---|---|---|")
    for name, status, details in results:
        print(f"| {name} | {status} | {details} |")


def finish() -> None:
    print_table()
    sys.exit(1 if any(s == "FAIL" for _, s, _ in results) else 0)


def compliance_check() -> None:
    env_file = ROOT / ".env"
    bad = [k for k in FORBIDDEN_KEYS if os.environ.get(k)]
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key = line.split("=", 1)[0].strip().removeprefix("export ").strip()
            if key in FORBIDDEN_KEYS and key not in bad:
                bad.append(f"{key} (in .env)")
    if bad:
        record("Compliance: askg1-only", "FAIL", f"third-party vars present: {', '.join(bad)}")
        print_table()
        sys.exit(2)
    record("Compliance: askg1-only", "PASS", "no third-party AI provider vars in environment or .env")

    tel = os.environ.get("ANONYMIZED_TELEMETRY", "").strip().lower()
    if tel == "false":
        record("Telemetry disabled", "PASS", "ANONYMIZED_TELEMETRY=false")
    else:
        record("Telemetry disabled", "FAIL", f"ANONYMIZED_TELEMETRY={tel!r}, expected 'false'")
        print_table()
        sys.exit(2)


def chat(client: httpx.Client, base: str, payload: dict) -> tuple[httpx.Response, float]:
    t0 = time.perf_counter()
    r = client.post(f"{base}/chat/completions", json=payload)
    return r, time.perf_counter() - t0


def err_text(r: httpx.Response) -> str:
    return f"HTTP {r.status_code}: {r.text[:200]}"


def check_models(client, base, model) -> None:
    t0 = time.perf_counter()
    try:
        r = client.get(f"{base}/models")
    except httpx.HTTPError as e:
        record("Reachability GET /models", "FAIL", f"{type(e).__name__}: {e}")
        finish()
    dt = time.perf_counter() - t0
    if r.status_code != 200:
        record("Reachability GET /models", "FAIL", err_text(r))
        finish()
    try:
        data = r.json().get("data", [])
    except ValueError:
        data = []
    ids = [m.get("id") for m in data]
    record("Reachability GET /models", "PASS", f"{dt * 1000:.0f} ms, {len(ids)} model(s)")
    if model in ids:
        meta = next(m for m in data if m.get("id") == model)
        ctx = next((meta[k] for k in ("max_model_len", "context_length", "context_window") if k in meta), None)
        record("Model available", "PASS", f"{model} listed" + (f", advertised context={ctx}" if ctx else ""))
    else:
        record("Model available", "FAIL", f"{model!r} not in {ids[:10]}")
        finish()


def check_completion(client, base, model) -> None:
    payload = {"model": model, "temperature": 0, "max_tokens": 16,
               "messages": [{"role": "user", "content": "Reply with OK"}]}
    try:
        r, dt = chat(client, base, payload)
    except httpx.HTTPError as e:
        record("Basic completion", "FAIL", f"{type(e).__name__}: {e}")
        return
    if r.status_code != 200:
        record("Basic completion", "FAIL", err_text(r))
        return
    msg = r.json()["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    status = "PASS" if "ok" in content.lower() else "WARN"
    record("Basic completion", status, f"{dt:.2f} s, reply={content[:40]!r}")


TOOLS = [{
    "type": "function",
    "function": {
        "name": "click_element",
        "description": "Click an interactive element on the current page by its index.",
        "parameters": {
            "type": "object",
            "properties": {"index": {"type": "integer", "description": "Element index from the page state"}},
            "required": ["index"],
        },
    },
}, {
    "type": "function",
    "function": {
        "name": "go_to_url",
        "description": "Navigate the browser to a URL.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    },
}]


def check_tools(client, base, model) -> None:
    payload = {
        "model": model, "temperature": 0, "tools": TOOLS, "tool_choice": "auto",
        "messages": [
            {"role": "system", "content": "You control a browser. Always act by calling a tool."},
            {"role": "user", "content": "Page elements: [3] <a>Login</a> [7] <a>Pricing</a>. Click the Pricing link."},
        ],
    }
    try:
        r, dt = chat(client, base, payload)
    except httpx.HTTPError as e:
        record("Tool calling (tool_choice=auto)", "FAIL", f"{type(e).__name__}: {e}")
        return
    if r.status_code != 200:
        record("Tool calling (tool_choice=auto)", "FAIL", err_text(r))
        return
    msg = r.json()["choices"][0]["message"]
    calls = msg.get("tool_calls") or []
    if not calls:
        record("Tool calling (tool_choice=auto)", "FAIL",
               f"{dt:.2f} s, no tool_calls; content={(msg.get('content') or '')[:80]!r}")
        return
    fn = calls[0]["function"]
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except ValueError:
        record("Tool calling (tool_choice=auto)", "WARN", f"{dt:.2f} s, arguments not valid JSON: {fn.get('arguments')!r}")
        return
    ok = fn.get("name") == "click_element" and args.get("index") == 7
    record("Tool calling (tool_choice=auto)", "PASS" if ok else "WARN",
           f"{dt:.2f} s, {fn.get('name')}({args})" + ("" if ok else " (expected click_element(index=7))"))


def check_json_schema(client, base, model) -> None:
    payload = {
        "model": model, "temperature": 0, "max_tokens": 100,
        "messages": [{"role": "user", "content": "Give a title 'Test' and number 1."}],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "out", "strict": True,
            "schema": {"type": "object", "properties": {"title": {"type": "string"}, "n": {"type": "integer"}},
                       "required": ["title", "n"], "additionalProperties": False}}},
    }
    try:
        r, dt = chat(client, base, payload)
    except httpx.HTTPError as e:
        record("Structured output (json_schema)", "WARN", f"{type(e).__name__}: {e}")
        return
    if r.status_code != 200:
        record("Structured output (json_schema)", "WARN",
               f"unsupported ({err_text(r)}); set dont_force_structured_output=True in app/llm.py")
        return
    try:
        json.loads(r.json()["choices"][0]["message"]["content"])
        record("Structured output (json_schema)", "PASS", f"{dt:.2f} s, valid JSON")
    except (ValueError, KeyError, TypeError):
        record("Structured output (json_schema)", "WARN",
               "accepted but output not valid JSON; set dont_force_structured_output=True")


def build_filler(approx_tokens: int, needle: str) -> str:
    # ~4 chars per token heuristic; real size is read back from usage.prompt_tokens.
    lines = [f"Note {i:05d}: the archive entry discusses routine page layout and navigation."
             for i in range(max(1, approx_tokens * 4 // 75))]
    return f"SECRET_CODE={needle}\n" + "\n".join(lines)


def check_context(client, base, model) -> None:
    max_ok = 0
    for target in PROBE_TOKENS:
        needle = f"K{target}Z"
        prompt = build_filler(target, needle) + "\n\nWhat is the value of SECRET_CODE? Answer with the value only."
        payload = {"model": model, "temperature": 0, "max_tokens": 32,
                   "messages": [{"role": "user", "content": prompt}]}
        try:
            r, dt = chat(client, base, payload)
        except httpx.HTTPError as e:
            record(f"Context probe ~{target // 1000}k", "FAIL", f"{type(e).__name__}: {e}")
            break
        if r.status_code != 200:
            record(f"Context probe ~{target // 1000}k", "FAIL", err_text(r))
            break
        body = r.json()
        used = (body.get("usage") or {}).get("prompt_tokens", 0)
        answer = (body["choices"][0]["message"].get("content") or "")
        found = needle in answer
        record(f"Context probe ~{target // 1000}k", "PASS" if found else "WARN",
               f"{dt:.1f} s, prompt_tokens={used}, needle {'recalled' if found else 'NOT recalled'}")
        if found:
            max_ok = max(max_ok, used)
    if max_ok >= TARGET_CTX_TOKENS:
        record("Context window >= 20k", "PASS", f"recalled needle at {max_ok} prompt tokens")
    elif max_ok:
        record("Context window >= 20k", "FAIL", f"largest verified prompt = {max_ok} tokens (< 20000)")
    else:
        record("Context window >= 20k", "FAIL", "no probe succeeded")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="also write the Markdown table to this file")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    load_dotenv(ROOT / ".env")
    compliance_check()

    base = os.environ["ASKG1_BASE_URL"].rstrip("/")
    model = os.environ["ASKG1_MODEL"]
    headers = {"Authorization": f"Bearer {os.environ.get('ASKG1_API_KEY', 'local-key')}"}
    print(f"# askg1 verification: {base} model={model} ({time.strftime('%Y-%m-%d %H:%M')})", file=sys.stderr)

    # trust_env=False: never route internal askg1 traffic through a system proxy.
    with httpx.Client(headers=headers, timeout=TIMEOUT, trust_env=False) as client:
        check_models(client, base, model)
        check_completion(client, base, model)
        check_tools(client, base, model)
        check_json_schema(client, base, model)
        check_context(client, base, model)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = ["| Test | Status | Latency / Details |", "|---|---|---|"]
        lines += [f"| {n} | {s} | {d} |" for n, s, d in results]
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    finish()


if __name__ == "__main__":
    main()
