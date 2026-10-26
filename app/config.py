"""Central settings. Import this module BEFORE browser_use so telemetry is forced off first."""
import asyncio
import ipaddress
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")  # before the constants below, which read the environment


def _askg1_configured() -> bool:
    """False on a deployment that cannot reach askg1 (e.g. Streamlit Cloud): crawl-only mode."""
    return bool(os.environ.get("ASKG1_BASE_URL") and os.environ.get("ASKG1_MODEL")) \
        and os.environ.get("ASKG1_DISABLED") != "1"


FORBIDDEN_KEYS = [
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
    "GROQ_API_KEY", "OPENROUTER_API_KEY", "MOONSHOT_API_KEY", "KIMI_API_KEY",
    "MISTRAL_API_KEY", "COHERE_API_KEY", "TOGETHER_API_KEY", "DEEPSEEK_API_KEY",
    "AZURE_OPENAI_API_KEY", "OLLAMA_HOST",
]

# askg1 is served with a hard 4096-token window (verified in Block B). Override only if the lead raises it.
DEFAULT_CONTEXT_TOKENS = 4096
DEFAULT_MAX_STEPS = 8
MAX_STEPS_CAP = 10

# Crawl limits (PDF: "keep crawl rates low"). The head may enter any site, so caps are hard, not advisory.
# Without askg1 we are on a small shared host (little RAM), so the default cap is lower; override with CRAWL_MAX_PAGES_CAP.
MAX_CRAWL_PAGES = int(os.environ.get("CRAWL_MAX_PAGES_CAP", 100 if _askg1_configured() else 15))
DEFAULT_CRAWL_PAGES = min(20, MAX_CRAWL_PAGES)
DEFAULT_CRAWL_DEPTH = 2
MAX_CRAWL_DEPTH = 3
CRAWL_DELAY_SECONDS = 1.0
CRAWL_PAGE_TIMEOUT_MS = 30_000
CRAWL_MAX_PAGE_CHARS = 400_000
CRAWL_TOTAL_TIMEOUT_S = 480
CRAWL_OUT_DIR = ROOT / "reports" / "crawls"


class ConfigError(RuntimeError):
    pass


class UrlError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    base_url: str
    model: str
    api_key: str
    context_tokens: int
    max_output_tokens: int   # reserved for the reply; counts against the context window
    dom_chars: int           # cap on page-state text sent to the model per step
    askg1_enabled: bool = True  # False = crawl-only deployment: summaries, audit verdict and agent are unavailable


def _forbidden_present(env_file: Path | None = ROOT / ".env") -> list[str]:
    bad = [k for k in FORBIDDEN_KEYS if os.environ.get(k)]
    if env_file and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key = line.split("=", 1)[0].strip().removeprefix("export ").strip()
            if key in FORBIDDEN_KEYS and key not in bad:
                bad.append(f"{key} (in .env)")
    return bad


def load_settings(env_file: Path | None = ROOT / ".env") -> Settings:
    if env_file:
        load_dotenv(env_file)

    bad = _forbidden_present(env_file)
    if bad:
        raise ConfigError(f"Third-party AI provider vars are forbidden (askg1 only): {', '.join(bad)}")

    tel = os.environ.get("ANONYMIZED_TELEMETRY")
    if tel is not None and tel.strip().lower() != "false":
        raise ConfigError(f"ANONYMIZED_TELEMETRY must be 'false', got {tel!r}")
    os.environ["ANONYMIZED_TELEMETRY"] = "false"
    os.environ.setdefault("CRAWL4AI_NO_CLOUD_NOTICE", "1")  # hides Crawl4AI's cloud banner; it makes no calls

    ctx = int(os.environ.get("ASKG1_CONTEXT_TOKENS", DEFAULT_CONTEXT_TOKENS))
    if not _askg1_configured():
        # Crawl-only deployment: no askg1 endpoint is available, so no AI feature may run.
        return Settings(base_url="", model="", api_key="", context_tokens=ctx, max_output_tokens=0,
                        dom_chars=0, askg1_enabled=False)

    base = os.environ["ASKG1_BASE_URL"].rstrip("/")
    if not base.endswith("/v1"):
        raise ConfigError(f"ASKG1_BASE_URL must end in /v1, got {base!r}")

    return Settings(
        base_url=base,
        model=os.environ["ASKG1_MODEL"],
        api_key=os.environ.get("ASKG1_API_KEY", "local-key"),
        context_tokens=ctx,
        max_output_tokens=int(os.environ.get("ASKG1_MAX_OUTPUT_TOKENS", max(1024, ctx // 4))),
        dom_chars=int(os.environ.get("ASKG1_DOM_CHARS", max(1200, ctx // 2))),
    )


SETTINGS = load_settings()


def clamp_steps(n: int | None) -> int:
    return max(1, min(n or DEFAULT_MAX_STEPS, MAX_STEPS_CAP))


def clamp_crawl(pages: int | None, depth: int | None) -> tuple[int, int]:
    return (max(1, min(pages or DEFAULT_CRAWL_PAGES, MAX_CRAWL_PAGES)),
            max(0, min(DEFAULT_CRAWL_DEPTH if depth is None else depth, MAX_CRAWL_DEPTH)))


def estimate_tokens(text: str) -> int:
    return len(text) // 4 + 1


def validate_public_url(url: str) -> str:
    """SSRF guard: http(s) only, and the host must resolve to public addresses.

    Needed because the UI may be exposed through a tunnel. Set ALLOW_PRIVATE_URLS=1 to test intranet pages.
    """
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise UrlError(f"Only http(s) URLs are allowed: {url!r}")
    if os.environ.get("ALLOW_PRIVATE_URLS") == "1":
        return url
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except socket.gaierror as e:
        raise UrlError(f"Cannot resolve {p.hostname!r}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:  # loopback, RFC1918, link-local, CGNAT (100.64/10, used by VPNs)
            raise UrlError(f"Blocked non-public address {ip} for host {p.hostname!r}")
    return url


async def avalidate_public_url(url: str) -> str:
    return await asyncio.to_thread(validate_public_url, url)
