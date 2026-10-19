"""
ai_client.py — Shared LLM client for the Canvas-to-HAX pipeline.

Wraps Anthropic (with optional NebulaOne Foundry), OpenAI, and Gemini
with automatic retry / exponential back-off and a single consistent
call interface used by both the evaluation and transform phases.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

class AIResponseError(ValueError):
    """Safe, authored diagnostics for a completed but unusable AI response."""


def _retryable(exc):
    import httpx
    if isinstance(exc, (AIResponseError, AttributeError, TypeError, ValueError, IndexError)):
        return False
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "code", None)
    if type(status) is int:
        return status in (408, 409, 429) or 500 <= status <= 599
    return (isinstance(exc, (httpx.TransportError, TimeoutError, ConnectionError))
            or type(exc).__name__ in ("APIConnectionError", "APITimeoutError"))


def _anthropic_text(response):
    # Reasoning-capable models can return thinking/redacted blocks before text,
    # even without an explicit thinking parameter. Never read or publish them.
    stop = getattr(response, "stop_reason", None)
    if stop in ("max_tokens", "model_context_window_exceeded"):
        raise AIResponseError(
            "The AI reached its output or context limit before finishing. Thinking uses "
            "the same token allowance. Choose a less reasoning-intensive model or a "
            "smaller review scope; this incomplete response was not retried.")
    if stop == "refusal":
        raise AIResponseError("The AI declined this request. Review the task or choose another model; no automatic retry was made.")
    if stop not in ("end_turn", "stop_sequence"):
        raise AIResponseError("The AI did not return a completed text answer. Choose a compatible text model; no automatic retry was made.")
    content = getattr(response, "content", None)
    if not isinstance(content, list):
        raise AIResponseError("The AI returned an invalid response format; no automatic retry was made.")
    parts = []
    for block in content:
        kind = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
        if kind == "text":
            text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
            if not isinstance(text, str):
                raise AIResponseError("The AI returned invalid answer text; no automatic retry was made.")
            parts.append(text)
    text = "".join(parts).strip()
    if not text:
        raise AIResponseError(
            "The AI returned no answer text (it may have returned only thinking blocks). "
            "Choose another text model or retry a smaller scope; no automatic retry was made.")
    return text

# ── optional provider imports ─────────────────────────────────────────────────
try:
    from anthropic import Anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

try:
    from google import genai as _genai
    HAS_GEMINI = True
except ImportError:
    HAS_GEMINI = False


# ── default model per provider ────────────────────────────────────────────────
_DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-haiku-4-5",
    "nebula":    "claude-haiku-4-5",
    "openai":    "gpt-4o-mini",
    "gemini":    "gemini-2.5-flash",
}


class AIClient:
    """
    Unified LLM wrapper with 3-attempt retry and exponential back-off.

    Supported providers
    ───────────────────
    • anthropic — direct Anthropic API  (ANTHROPIC_API_KEY)
    • nebula    — NebulaOne/Azure Foundry (NEBULA_API_KEY + NEBULA_BASE_URL)
    • openai    — OpenAI API             (OPENAI_API_KEY)
    • gemini    — Google Gemini          (GEMINI_API_KEY)

    Usage
    ─────
    client = AIClient(provider="nebula")
    text   = client.call(system="You are ...", prompt="Evaluate this ...", max_tokens=1024)
    """

    def __init__(
        self,
        provider: str,
        model:    Optional[str] = None,
        api_key:  Optional[str] = None,
        base_url: Optional[str] = None,
        isolated_credentials: bool = False,
    ) -> None:
        self.provider = provider.lower()
        self.isolated_credentials = isolated_credentials
        self.model    = model or (_DEFAULT_MODELS.get(self.provider) if isolated_credentials else os.getenv(
            f"{provider.upper()}_MODEL",
            _DEFAULT_MODELS.get(self.provider, "claude-haiku-4-5"),
        ))
        self._client: object = None
        self._init(api_key, base_url)

    # ── initialisation ────────────────────────────────────────────────────────

    def _init(self, api_key: Optional[str], base_url: Optional[str]) -> None:
        p = self.provider
        isolated_options = {}
        if self.isolated_credentials:
            if not api_key:
                raise ValueError("Explicit provider credentials are required.")
            import httpx
            origins = {
                "anthropic": "https://api.anthropic.com",
                "openai": "https://api.openai.com/v1",
                "gemini": "https://generativelanguage.googleapis.com",
            }
            base_url = base_url if p == "nebula" else origins.get(p)
            if not base_url:
                raise ValueError("An explicit institutional gateway is required.")
            target = urlsplit(base_url)

            def isolate(request):
                if (request.url.scheme != "https" or request.url.host != target.hostname
                        or request.url.port not in (None, 443)):
                    raise ValueError("Unexpected provider endpoint.")
                allowed = {
                    "host", "content-type", "content-length", "accept", "accept-encoding",
                    "connection", "user-agent", "anthropic-version", "anthropic-beta",
                    "x-goog-api-client",
                }
                for header in list(request.headers):
                    if header.lower() not in allowed:
                        del request.headers[header]
                if p in ("anthropic", "nebula"):
                    request.headers["x-api-key"] = api_key
                elif p == "openai":
                    request.headers["authorization"] = "Bearer " + api_key
                else:
                    request.headers["x-goog-api-key"] = api_key

            isolated_options = {
                "base_url": base_url,
                "http_client": httpx.Client(trust_env=False, follow_redirects=False,
                                            event_hooks={"request": [isolate]}),
            }

        if p in ("anthropic", "nebula"):
            if not HAS_ANTHROPIC:
                raise ImportError("anthropic package missing — pip install anthropic")
            env_key = "NEBULA_API_KEY" if p == "nebula" else "ANTHROPIC_API_KEY"
            env_url = "NEBULA_BASE_URL" if p == "nebula" else None
            key = api_key or os.getenv(env_key, "")
            url = base_url or (os.getenv(env_url) if env_url else None)
            kwargs: dict = {"api_key": key}
            if url:
                kwargs["base_url"] = url
            if self.isolated_credentials:
                kwargs.update(isolated_options, auth_token="", max_retries=0)
            self._client = Anthropic(**kwargs)
            via = f"NebulaOne/{self.model}" if p == "nebula" else f"Anthropic/{self.model}"
            print(f"  AI : {via}")

        elif p == "openai":
            if not HAS_OPENAI:
                raise ImportError("openai package missing — pip install openai")
            key = api_key or os.getenv("OPENAI_API_KEY", "")
            self._client = OpenAI(api_key=key, **isolated_options,
                                  **({"max_retries": 0} if self.isolated_credentials else {}))
            print(f"  AI : OpenAI/{self.model}")

        elif p == "gemini":
            if not HAS_GEMINI:
                raise ImportError(
                    "google-genai package missing — pip install google-genai"
                )
            key = api_key or os.getenv("GEMINI_API_KEY", "")
            if self.isolated_credentials:
                transport = isolated_options.pop("http_client")
                self._client = _genai.Client(
                    api_key=key, vertexai=False,
                    http_options={"base_url": base_url, "httpx_client": transport},
                )
            else:
                self._client = _genai.Client(api_key=key)
            print(f"  AI : Gemini/{self.model}")

        else:
            raise ValueError(
                f"Unknown provider {p!r}. Choose from: anthropic, nebula, openai, gemini"
            )

    # ── public API ────────────────────────────────────────────────────────────

    def call(
        self,
        system:     str,
        prompt:     str,
        max_tokens: int            = 4096,
        timeout:    Optional[int]  = None,
    ) -> str:
        """
        Call the LLM.  Retries up to 3 times with exponential back-off
        (1 s, 2 s) before re-raising the last exception.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                return self._call_once(system, prompt, max_tokens, timeout)
            except Exception as exc:
                last_exc = exc
                if not _retryable(exc):
                    logger.warning("LLM request failed (%s); not retrying a non-transient failure",
                                   type(exc).__name__)
                    raise
                if attempt < 2:
                    wait = 2 ** attempt          # 1 s, then 2 s
                    logger.warning(
                        "LLM attempt %d/3 failed (%s) — retrying in %ds",
                        attempt + 1, type(exc).__name__, wait,
                    )
                    time.sleep(wait)
        raise last_exc  # type: ignore[misc]

    # ── internal dispatch ─────────────────────────────────────────────────────

    def _call_once(
        self,
        system:     str,
        prompt:     str,
        max_tokens: int,
        timeout:    Optional[int],
    ) -> str:
        p = self.provider
        # Scale timeout with max_tokens if not explicitly supplied
        effective_timeout = timeout or max(60, 30 + max_tokens // 100)

        if p in ("anthropic", "nebula"):
            r = self._client.messages.create(  # type: ignore[union-attr]
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                timeout=effective_timeout,
            )
            return _anthropic_text(r)

        if p == "openai":
            token_options = ({"max_completion_tokens": max_tokens} if self.isolated_credentials
                             else {"max_tokens": max_tokens, "temperature": 0.5})
            r = self._client.chat.completions.create(  # type: ignore[union-attr]
                model=self.model,
                **token_options,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": prompt},
                ],
                timeout=effective_timeout,
            )
            return r.choices[0].message.content.strip()

        if p == "gemini":
            # Gemini uses a single combined prompt
            combined = f"{system}\n\n{prompt}" if system else prompt
            r = self._client.models.generate_content(  # type: ignore[union-attr]
                model=self.model,
                contents=combined,
                **({"config": {"max_output_tokens": max_tokens}} if self.isolated_credentials else {}),
            )
            return r.text.strip()

        raise ValueError(f"Unknown provider: {p!r}")

    def close(self):
        """Release per-action SDK transports when used by the mentor."""
        if self._client is not None and hasattr(self._client, "close"):
            self._client.close()


# ── factory helper ────────────────────────────────────────────────────────────

def build_ai_client(
    model_provider: str,
    model:          Optional[str] = None,
) -> AIClient:
    """
    Build an AIClient from environment variables for the given provider.
    This is the primary entry point for all pipeline stages.

    Model resolution order
    ──────────────────────
    1. `model` argument
    2. <PROVIDER>_MODEL env var   (e.g. NEBULA_MODEL, GEMINI_MODEL)
    3. Built-in default per provider
    """
    return AIClient(provider=model_provider, model=model)
