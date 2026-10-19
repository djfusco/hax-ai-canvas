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



def _anthropic_request_options(provider, model, max_tokens, isolated):
    """Compatibility helper; all mentor budget knowledge lives in one planner."""
    if not isolated:
        return {"max_tokens": max_tokens}
    from model_capabilities import plan_request
    plan = plan_request(provider, model, "", "", max_tokens)
    options = {"max_tokens": plan["max_output_tokens"]}
    if plan["reasoning_effort"]:
        options["output_config"] = {"effort": plan["reasoning_effort"]}
    return options


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


def _anthropic_text(response, max_tokens=None):
    # Reasoning-capable models can return thinking/redacted blocks before text,
    # even without an explicit thinking parameter. Never read or publish them.
    stop = getattr(response, "stop_reason", None)
    if stop == "max_tokens":
        raise AIResponseError(
            f"The AI exhausted this request's {max_tokens or 'bounded'} output-token allowance "
            "before completing its answer. Thinking uses the same allowance. "
            "This is not a Canvas-file size limit. No incomplete scores were accepted "
            "and no automatic retry was made.")
    if stop == "model_context_window_exceeded":
        raise AIResponseError(
            "The AI reached its context window for this individual request. "
            "This is distinct from the output-token cap and the Canvas-file size limit. "
            "No incomplete scores were accepted and no automatic retry was made.")
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

def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _openai_chat_text(response):
    choices = _field(response, "choices")
    if not isinstance(choices, list) or not choices:
        raise AIResponseError("The selected model returned no text choices; no automatic retry was made.")
    choice = choices[0]
    reason = _field(choice, "finish_reason")
    if reason == "length":
        raise AIResponseError("The AI exhausted its output allowance before completing the answer. No incomplete assessment or automatic retry was accepted.")
    message = _field(choice, "message")
    if reason == "content_filter" or _field(message, "refusal"):
        raise AIResponseError("The AI declined or filtered this request. No assessment was fabricated or automatically retried.")
    if reason != "stop":
        raise AIResponseError("The selected model did not complete a text answer. Its API or tool-only response is incompatible with this request.")
    text = _field(message, "content")
    if not isinstance(text, str) or not text.strip():
        raise AIResponseError("The AI returned no answer text. Reasoning may have consumed the allowance; no automatic retry was made.")
    return text.strip()


def _openai_response_text(response):
    status = _field(response, "status")
    if status == "incomplete":
        raise AIResponseError("The AI response was incomplete, possibly because its reasoning and answer reached the output cap. No automatic retry was made.")
    if status != "completed":
        raise AIResponseError("The selected model did not complete its response; no assessment or automatic retry was accepted.")
    output = _field(response, "output")
    if not isinstance(output, list):
        raise AIResponseError("The model returned an invalid response format.")
    parts = []
    for entry in output:
        kind = _field(entry, "type")
        if kind == "reasoning":
            continue
        if kind != "message" or _field(entry, "role") != "assistant":
            raise AIResponseError("The model returned an unsupported tool or non-text response.")
        if _field(entry, "status") != "completed":
            raise AIResponseError("The model's answer message was incomplete.")
        content = _field(entry, "content")
        if not isinstance(content, list):
            raise AIResponseError("The model returned invalid answer content.")
        for block in content:
            if _field(block, "type") == "refusal":
                raise AIResponseError("The AI declined this request; no assessment was fabricated.")
            text = _field(block, "text")
            if _field(block, "type") != "output_text" or not isinstance(text, str):
                raise AIResponseError("The model returned unsupported answer content.")
            parts.append(text)
    result = "".join(parts).strip()
    if not result:
        raise AIResponseError("The AI returned no final answer text; no automatic retry was made.")
    return result


def _enum_name(value):
    value = getattr(value, "value", value)
    return value.rsplit(".", 1)[-1].upper() if isinstance(value, str) else None


def _gemini_text(response):
    blocked = _enum_name(_field(_field(response, "prompt_feedback"), "block_reason"))
    if blocked and blocked not in ("BLOCK_REASON_UNSPECIFIED", "UNSPECIFIED"):
        raise AIResponseError("The AI blocked this request. No assessment was fabricated or automatically retried.")
    candidates = _field(response, "candidates")
    if not isinstance(candidates, list) or len(candidates) != 1:
        raise AIResponseError("The AI returned no single completed text answer.")
    candidate = candidates[0]
    reason = _enum_name(_field(candidate, "finish_reason"))
    if reason == "MAX_TOKENS":
        raise AIResponseError("The AI exhausted its thinking/answer allowance before completing the response. No automatic retry was made.")
    if reason != "STOP":
        raise AIResponseError("The AI response was blocked, incomplete or incompatible; no assessment was fabricated.")
    content = _field(_field(candidate, "content"), "parts")
    if not isinstance(content, list):
        raise AIResponseError("The AI returned an invalid text response.")
    parts = []
    for block in content:
        if _field(block, "thought") is True:
            continue
        if _field(block, "function_call") is not None:
            raise AIResponseError("The AI returned an unsupported tool call instead of a final text answer.")
        text = _field(block, "text")
        if text is not None:
            if not isinstance(text, str):
                raise AIResponseError("The AI returned invalid answer text.")
            parts.append(text)
    result = "".join(parts).strip()
    if not result:
        raise AIResponseError("The AI returned only thoughts or no answer text; no automatic retry was made.")
    return result


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
        capabilities: Optional[dict] = None,
    ) -> None:
        self.provider = provider.lower()
        self.isolated_credentials = isolated_credentials
        self.model    = model or (_DEFAULT_MODELS.get(self.provider) if isolated_credentials else os.getenv(
            f"{provider.upper()}_MODEL",
            _DEFAULT_MODELS.get(self.provider, "claude-haiku-4-5"),
        ))
        self._client: object = None
        self.capabilities = None
        if isolated_credentials:
            from model_capabilities import resolve_capabilities, validate_capabilities
            self.capabilities = (validate_capabilities(self.provider, self.model, capabilities)
                                 if capabilities is not None
                                 else resolve_capabilities(self.provider, self.model))
        self._init(api_key, base_url)

    def preflight(self, system, prompt, max_tokens=4096):
        """Offline admission only: never truncates text or sends provider data."""
        from model_capabilities import plan_request
        return plan_request(self.provider, self.model, system, prompt, max_tokens, self.capabilities)

    def input_budget(self, max_tokens=4096):
        return self.preflight("", "", max_tokens)["available_input_tokens"]

    def fits(self, system, prompt, max_tokens=4096):
        from model_capabilities import ModelCapabilityError
        try:
            self.preflight(system, prompt, max_tokens)
            return True
        except ModelCapabilityError:
            return False

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
        plan = self.preflight(system, prompt, max_tokens) if self.isolated_credentials else None
        output = plan["max_output_tokens"] if plan else max_tokens
        effective_timeout = timeout or max(60, 30 + output // 100)

        if p in ("anthropic", "nebula"):
            options = {"max_tokens": output}
            if plan and plan["reasoning_effort"]:
                options["extra_body"] = {"output_config": {"effort": plan["reasoning_effort"]}}
            r = self._client.messages.create(  # type: ignore[union-attr]
                model=self.model,
                **options,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                timeout=effective_timeout,
            )
            return _anthropic_text(r, output)

        if p == "openai":
            if plan and plan["api"] == "responses":
                options = {"model": self.model, "instructions": system, "input": prompt,
                           "max_output_tokens": output, "store": False, "timeout": effective_timeout}
                if plan["reasoning_effort"]:
                    options["reasoning"] = {"effort": plan["reasoning_effort"]}
                return _openai_response_text(self._client.responses.create(**options))
            token_options = ({plan["output_parameter"]: output, "store": False} if plan
                             else {"max_tokens": max_tokens, "temperature": 0.5})
            if plan and plan["reasoning_effort"]:
                token_options["reasoning_effort"] = plan["reasoning_effort"]
            r = self._client.chat.completions.create(  # type: ignore[union-attr]
                model=self.model,
                **token_options,
                messages=[
                    {"role": "developer" if plan and plan["reasoning"] else "system", "content": system},
                    {"role": "user",   "content": prompt},
                ],
                timeout=effective_timeout,
            )
            return _openai_chat_text(r) if plan else r.choices[0].message.content.strip()

        if p == "gemini":
            # Gemini uses a single combined prompt
            combined = f"{system}\n\n{prompt}" if system else prompt
            r = self._client.models.generate_content(  # type: ignore[union-attr]
                model=self.model,
                contents=combined,
                **({"config": {"max_output_tokens": output,
                                "http_options": {"timeout": effective_timeout * 1000}}} if plan else {}),
            )
            return _gemini_text(r) if plan else r.text.strip()

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
