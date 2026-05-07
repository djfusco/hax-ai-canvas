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

logger = logging.getLogger(__name__)

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
    ) -> None:
        self.provider = provider.lower()
        self.model    = model or os.getenv(
            f"{provider.upper()}_MODEL",
            _DEFAULT_MODELS.get(self.provider, "claude-haiku-4-5"),
        )
        self._client: object = None
        self._init(api_key, base_url)

    # ── initialisation ────────────────────────────────────────────────────────

    def _init(self, api_key: Optional[str], base_url: Optional[str]) -> None:
        p = self.provider

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
            self._client = Anthropic(**kwargs)
            via = f"NebulaOne/{self.model}" if p == "nebula" else f"Anthropic/{self.model}"
            print(f"  AI : {via}")

        elif p == "openai":
            if not HAS_OPENAI:
                raise ImportError("openai package missing — pip install openai")
            key = api_key or os.getenv("OPENAI_API_KEY", "")
            self._client = OpenAI(api_key=key)
            print(f"  AI : OpenAI/{self.model}")

        elif p == "gemini":
            if not HAS_GEMINI:
                raise ImportError(
                    "google-generativeai package missing — pip install google-generativeai"
                )
            key = api_key or os.getenv("GEMINI_API_KEY", "")
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
                if attempt < 2:
                    wait = 2 ** attempt          # 1 s, then 2 s
                    logger.warning(
                        "LLM attempt %d/3 failed: %s — retrying in %ds",
                        attempt + 1, exc, wait,
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
            return r.content[0].text.strip()

        if p == "openai":
            r = self._client.chat.completions.create(  # type: ignore[union-attr]
                model=self.model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": prompt},
                ],
                temperature=0.5,
                timeout=effective_timeout,
            )
            return r.choices[0].message.content.strip()

        if p == "gemini":
            # Gemini uses a single combined prompt
            combined = f"{system}\n\n{prompt}" if system else prompt
            r = self._client.models.generate_content(  # type: ignore[union-attr]
                model=self.model,
                contents=combined,
            )
            return r.text.strip()

        raise ValueError(f"Unknown provider: {p!r}")


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
