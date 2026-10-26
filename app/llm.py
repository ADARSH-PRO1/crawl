"""askg1 clients: the browser-use ChatOpenAI and a tiny direct one-shot helper."""
import httpx

from app.config import SETTINGS, ConfigError, estimate_tokens


def _require_askg1() -> None:
    if not SETTINGS.askg1_enabled:
        raise ConfigError("askg1 is not configured in this deployment (crawl-only mode).")


def get_llm():
    """ChatOpenAI pointed at askg1.

    browser-use drives ChatOpenAI through response_format=json_schema (verified PASS in Block B),
    not through the tools API. Settings are tuned for a 4096-token window.
    """
    _require_askg1()
    from browser_use import ChatOpenAI  # lazy: crawl-only deployments do not install browser-use

    return ChatOpenAI(
        model=SETTINGS.model,
        base_url=SETTINGS.base_url,
        api_key=SETTINGS.api_key,
        temperature=0,
        frequency_penalty=None,           # library default is 0.3; not needed and may be rejected
        reasoning_effort=None,            # askg1 is not an OpenAI reasoning model
        max_completion_tokens=SETTINGS.max_output_tokens,  # reply budget; default 4096 would eat the whole window
        max_retries=1,
        timeout=120,
        # Browser-use must not read proxy env vars for the internal endpoint.
        http_client=httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(120, connect=10)),
        # askg1 degenerates under json_schema-constrained decoding (runs to the token cap with
        # finish_reason=length). Without response_format it answers in ~50 tokens, so the reply format
        # is taught by the compact action sheet in tasks.SYSTEM_PROMPT instead of the full schema.
        dont_force_structured_output=True,
    )


async def chat_once(prompt: str, system: str | None = None, max_tokens: int = 200) -> str:
    """Single direct askg1 call (summaries, JSON repair). Refuses prompts that cannot fit the window."""
    _require_askg1()
    est = estimate_tokens(prompt) + estimate_tokens(system or "") + max_tokens
    if est > SETTINGS.context_tokens:
        raise ValueError(f"Prompt budget exceeded: ~{est} > {SETTINGS.context_tokens} tokens")
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(120, connect=10)) as c:
        r = await c.post(
            f"{SETTINGS.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {SETTINGS.api_key}"},
            json={"model": SETTINGS.model, "temperature": 0, "max_tokens": max_tokens, "messages": messages},
        )
    r.raise_for_status()
    return (r.json()["choices"][0]["message"].get("content") or "").strip()
