"""Offline text-model protocol knowledge, metadata normalization and preflight.

Documented limits describe protocols, not live access, funding or answer quality.
No provider SDK, credential, HTTP client or application adapter is imported here.
"""
from __future__ import annotations

import re


class ModelCapabilityError(ValueError):
    """An authored, safe capability or request-budget diagnostic."""


REASONING_HEADROOM = 8192
AUTO_OUTPUT_CAP = 16384
PROTOCOL_MARGIN = 256
MAX_INPUT_LIMIT = 10_000_000
MAX_OUTPUT_LIMIT = 2_000_000
TOKEN_ESTIMATION = "Conservative UTF-8 byte estimate plus 256 protocol tokens; not exact tokenization."

_DEFAULT_API = {"anthropic": "messages", "nebula": "messages",
                "openai": "chat_completions", "gemini": "generate_content"}
_OUTPUT_PARAMETER = {"messages": "max_tokens", "chat_completions": "max_completion_tokens",
                     "responses": "max_output_tokens", "generate_content": "max_output_tokens"}
_SOURCES = {
    "fallback": "conservative local fallback",
    "unsupported": "local text compatibility policy",
    "anthropic": "Anthropic model documentation (2026-10-19)",
    "openai": "OpenAI model documentation (2026-10-19)",
    "gemini": "Gemini model documentation (2026-10-19)",
    "anthropic_metadata": "Anthropic model catalog",
    "nebula_metadata": "Institutional gateway model catalog",
    "gemini_metadata": "Gemini model catalog",
}
_WARNINGS = {
    "unverified": "Unverified model: conservative local ceilings are not verified provider limits or API compatibility.",
    "access": "Catalog or documented limits do not guarantee inference access, funding or answer quality.",
    "metadata": "Some advertised capability fields were invalid and were ignored.",
    "partial": "Provider metadata is partial; unspecified limits or controls use documented profiles or conservative local ceilings, not provider verification.",
    "unsupported": "This model is not supported by the text-generation workflow.",
    "deprecated": "The provider marks this model deprecated; existing access may be required.",
    "clamped": "The output allowance was clamped to the model limit or the 16,384-token application ceiling.",
    "reasoning": "The bounded output allowance includes reasoning and answer tokens; completion is not guaranteed.",
}
_FIELDS = {"version", "provider", "model", "status", "api", "input_limit",
           "context_window", "output_limit", "reasoning", "effort",
           "output_parameter", "source", "warnings"}

# Exact IDs only. Sources: https://developers.openai.com/api/docs/models/{id}
# and /guides/reasoning. Never manufacture support for an arbitrary dated suffix.
# Historical 4o snapshot limits: https://cdn.openai.com/API/docs/txt/llms-models-pricing.txt
_OPENAI = {}


def _openai(ids, context, output, *, input_limit=None, reasoning=False,
            effort=None, api="chat_completions"):
    for identifier in ids.split():
        _OPENAI[identifier] = (context, input_limit or context, output, reasoning, effort, api)


_openai("gpt-4o gpt-4o-2024-08-06 gpt-4o-2024-11-20", 128000, 16384)
_openai("gpt-4o-2024-05-13", 128000, 4096)
_openai("gpt-4o-mini gpt-4o-mini-2024-07-18", 128000, 16384)
for _name in ("gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano"):
    _openai(f"{_name} {_name}-2025-04-14", 1047576, 32768)
for _name in ("gpt-5", "gpt-5-mini", "gpt-5-nano"):
    _openai(f"{_name} {_name}-2025-08-07", 400000, 128000,
            input_limit=272000, reasoning=True, effort="medium")
for _name, _date in (("gpt-5.1", "2025-11-13"), ("gpt-5.2", "2025-12-11")):
    _openai(f"{_name} {_name}-{_date}", 400000, 128000, reasoning=True, effort="medium")
for _name, _date in (("gpt-5.4", "2026-03-05"), ("gpt-5.5", "2026-04-23")):
    _openai(f"{_name} {_name}-{_date}", 1050000, 128000, reasoning=True, effort="medium")
for _name in ("o3", "o4-mini"):
    _openai(f"{_name} {_name}-2025-04-16", 200000, 100000, reasoning=True, effort="medium")
# This profile only supports high effort: omit effort, never send medium.
_openai("gpt-5-pro gpt-5-pro-2025-10-06", 400000, 272000,
        reasoning=True, api="responses")
_openai("gpt-6-astra", 1050000, 128000, input_limit=922000, reasoning=True, effort="medium")

# https://platform.claude.com/docs/en/models/overview and /build-with-claude/effort
# Earlier generation overview: /en/about-claude/models/overview.
# Individual legacy specs: /en/models/{opus-4-6,opus-4-7,opus-4-8,
# opus-4-5,sonnet-4-6,sonnet-4-5,fable-5}/overview.
# No invented dated snapshots for dateless pinned (4.6+) model IDs.
_ANTHROPIC = {
    "claude-opus-5-5": (1000000, 128000, True, "medium"),
    "claude-sonnet-5-5": (1000000, 128000, True, "medium"),
    "claude-haiku-5-5": (1000000, 128000, True, "medium"),
    "claude-fable-5-1": (1000000, 128000, True, "medium"),
    "claude-opus-5": (1000000, 128000, True, "medium"),
    "claude-sonnet-5": (1000000, 128000, True, "medium"),
    "claude-fable-5": (1000000, 128000, True, "medium"),
    "claude-mythos-5": (1000000, 128000, True, "medium"),
    "claude-opus-4-8": (1000000, 128000, True, "medium"),
    "claude-opus-4-7": (1000000, 128000, True, "medium"),
    "claude-opus-4-6": (1000000, 128000, True, "medium"),
    "claude-sonnet-4-6": (1000000, 128000, True, "medium"),
    "claude-opus-4-5": (200000, 64000, False, "medium"),
    "claude-opus-4-5-20251101": (200000, 64000, False, "medium"),
    "claude-sonnet-4-5": (200000, 64000, False, None),
    "claude-sonnet-4-5-20250929": (200000, 64000, False, None),
    # Optional extended thinking is not activated by this planner. Do not
    # manufacture a thinking budget merely because the model supports one.
    "claude-haiku-4-5": (200000, 64000, False, None),
    "claude-haiku-4-5-20251001": (200000, 64000, False, None),
}
# https://ai.google.dev/gemini-api/docs/models/{id}; separate input/output limits.
_GEMINI = {name: (1048576, 65536, True) for name in (
    "gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5-flash-lite")}


def _identity(provider, model):
    if not isinstance(provider, str) or provider not in _DEFAULT_API:
        raise ModelCapabilityError("Unsupported AI provider.")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}", model):
        raise ModelCapabilityError("Choose a valid model identifier.")


def _integer(value, maximum):
    return type(value) is int and 1 <= value <= maximum


def _unsupported(provider, model):
    # Gateway deployment names are opaque, including names resembling direct models.
    if provider == "nebula":
        return False
    if re.search(r"(embed|whisper|tts|image|audio|realtime|moderation|dall-e|video|sora|veo|imagen|lyria)", model, re.I):
        return True
    return model in {"babbage-002", "davinci-002", "text-davinci-003", "text-davinci-002",
                     "gpt-3.5-turbo-instruct", "claude-2", "claude-2.0", "claude-2.1",
                     "claude-instant-1.2", "claude-3-opus-20240229",
                     "claude-3-sonnet-20240229", "claude-3-haiku-20240307",
                     "claude-3-5-sonnet-20240620", "claude-3-5-sonnet-20241022",
                     "claude-3-7-sonnet-20250219", "claude-3-7-sonnet-latest",
                     "claude-3-5-haiku-20241022", "claude-3-5-haiku-latest",
                     "claude-opus-4-1-20250805", "claude-opus-4-1",
                     "claude-opus-4-20250514", "claude-opus-4",
                     "claude-sonnet-4-20250514", "claude-sonnet-4",
                     "gemini-1.0-pro", "gemini-1.5-pro", "gemini-1.5-flash"}


def _base(provider, model):
    api = _DEFAULT_API[provider]
    profile = {"version": 1, "provider": provider, "model": model,
               "status": "unverified", "api": api, "input_limit": 32768,
               "context_window": 32768, "output_limit": 4096, "reasoning": False,
               "effort": None, "output_parameter": _OUTPUT_PARAMETER[api],
               "source": _SOURCES["fallback"],
               "warnings": [_WARNINGS["unverified"], _WARNINGS["access"]]}
    if provider == "openai" and model in _OPENAI:
        context, limit, output, reasoning, effort, api = _OPENAI[model]
        profile.update(context_window=context, input_limit=limit, output_limit=output,
                       reasoning=reasoning, effort=effort, api=api,
                       output_parameter=_OUTPUT_PARAMETER[api], status="documented")
    elif provider == "anthropic" and model in _ANTHROPIC:
        limit, output, reasoning, effort = _ANTHROPIC[model]
        profile.update(context_window=limit, input_limit=limit, output_limit=output,
                       reasoning=reasoning, effort=effort, status="documented")
    elif provider == "gemini" and model in _GEMINI:
        limit, output, reasoning = _GEMINI[model]
        profile.update(context_window=None, input_limit=limit, output_limit=output,
                       reasoning=reasoning, status="documented")
    if profile["status"] == "documented":
        profile.update(source=_SOURCES[provider], warnings=[_WARNINGS["access"]])
        if provider == "anthropic" and model in ("claude-sonnet-4-5", "claude-sonnet-4-5-20250929"):
            profile["warnings"].append(_WARNINGS["deprecated"])
    if _unsupported(provider, model):
        profile.update(status="unsupported", api=None, source=_SOURCES["unsupported"],
                       warnings=[_WARNINGS["unsupported"]], reasoning=False, effort=None)
    return profile


def _supported(value):
    return value.get("supported") if isinstance(value, dict) and type(value.get("supported")) is bool else None


def resolve_capabilities(provider, model, metadata=None):
    """Normalize raw catalog metadata with exact documented fallback profiles.

    Unknown upstream fields (including URLs, sources and warnings) are discarded.
    Invalid individual metadata fields never enlarge a documented/fallback limit.
    """
    _identity(provider, model)
    profile = _base(provider, model)
    if metadata is None:
        return profile
    if not isinstance(metadata, dict):
        profile["warnings"].append(_WARNINGS["metadata"])
        return profile
    identifier = metadata.get("name" if provider == "gemini" else "id")
    if identifier is not None and identifier != ("models/" + model if provider == "gemini" else model):
        profile["warnings"].append(_WARNINGS["metadata"])
        return profile
    if profile["status"] == "unsupported":
        return profile
    if metadata.get("lifecycle") == "retired":
        profile.update(status="unsupported", api=None, reasoning=False, effort=None,
                       output_parameter=_OUTPUT_PARAMETER[_DEFAULT_API[provider]],
                       source=_SOURCES["unsupported"], warnings=[_WARNINGS["unsupported"]])
        return profile
    if metadata.get("lifecycle") == "deprecated":
        profile["warnings"].append(_WARNINGS["deprecated"])
    changed, invalid, valid_limits = False, False, 0
    keys = (("inputTokenLimit", "input_limit", MAX_INPUT_LIMIT),
            ("outputTokenLimit", "output_limit", MAX_OUTPUT_LIMIT)) if provider == "gemini" else (
                ("max_input_tokens", "input_limit", MAX_INPUT_LIMIT),
                ("max_tokens", "output_limit", MAX_OUTPUT_LIMIT)) if provider in ("anthropic", "nebula") else ()
    for raw, normalized, maximum in keys:
        if raw not in metadata or metadata[raw] is None:
            continue
        if _integer(metadata[raw], maximum):
            profile[normalized] = metadata[raw]
            changed = True
            valid_limits += 1
            if normalized == "input_limit":
                # Anthropic describes this as input context-window size. Reserve
                # output from the same window, conservatively, including gateways.
                profile["context_window"] = None if provider == "gemini" else metadata[raw]
        else:
            invalid = True
    if provider == "gemini":
        methods = metadata.get("supportedGenerationMethods")
        if methods is not None:
            if isinstance(methods, list) and all(isinstance(x, str) for x in methods):
                if "generateContent" not in methods:
                    profile.update(status="unsupported", api=None, reasoning=False, effort=None,
                                   source=_SOURCES["unsupported"], warnings=[_WARNINGS["unsupported"]])
                    return profile
            else:
                invalid = True
        if "thinking" in metadata:
            if type(metadata["thinking"]) is bool:
                profile["reasoning"] = metadata["thinking"]
                changed = True
            else:
                invalid = True
    elif provider in ("anthropic", "nebula"):
        caps = metadata.get("capabilities")
        if isinstance(caps, dict):
            thinking = _supported(caps.get("thinking"))
            if thinking is not None:
                profile["reasoning"] = thinking
                changed = True
            effort = caps.get("effort")
            if isinstance(effort, dict):
                medium = _supported(effort.get("medium"))
                # A false overall support flag overrides a contradictory level.
                if _supported(effort) is False:
                    medium = False
                if medium is not None:
                    profile["effort"] = "medium" if medium else None
                    changed = True
    if changed:
        profile.update(status="provider_metadata", source=_SOURCES[provider + "_metadata"])
        if valid_limits < 2:
            profile["warnings"].append(_WARNINGS["partial"])
    if invalid:
        profile["warnings"].append(_WARNINGS["metadata"])
    return profile


def validate_capabilities(provider, model, profile):
    """Validate a server-owned normalized snapshot; never accepts raw metadata.

    This validates shape and safe finite values, not origin/authenticity. HTTP
    callers must never be allowed to supply the server's capability snapshot.
    """
    _identity(provider, model)
    error = "Invalid saved model capabilities. Refresh the provider's model catalog."
    if not isinstance(profile, dict) or set(profile) != _FIELDS:
        raise ModelCapabilityError(error)
    if (type(profile["version"]) is not int or profile["version"] != 1
            or profile["provider"] != provider or profile["model"] != model):
        raise ModelCapabilityError(error)
    if profile["status"] not in ("documented", "provider_metadata", "unverified", "unsupported"):
        raise ModelCapabilityError(error)
    if not _integer(profile["input_limit"], MAX_INPUT_LIMIT) or not _integer(profile["output_limit"], MAX_OUTPUT_LIMIT):
        raise ModelCapabilityError(error)
    context = profile["context_window"]
    if context is not None and (not _integer(context, MAX_INPUT_LIMIT) or profile["input_limit"] > context):
        raise ModelCapabilityError(error)
    api = profile["api"]
    if api is not None and not isinstance(api, str):
        raise ModelCapabilityError(error)
    allowed = ("chat_completions", "responses") if provider == "openai" else (_DEFAULT_API[provider],)
    if profile["status"] == "unsupported":
        if api is not None:
            raise ModelCapabilityError(error)
    elif api not in allowed:
        raise ModelCapabilityError(error)
    parameter = _OUTPUT_PARAMETER.get(api, _OUTPUT_PARAMETER[_DEFAULT_API[provider]])
    if profile["output_parameter"] != parameter or type(profile["reasoning"]) is not bool:
        raise ModelCapabilityError(error)
    if profile["effort"] not in (None, "medium") or (provider == "gemini" and profile["effort"] is not None):
        raise ModelCapabilityError(error)
    if not isinstance(profile["source"], str) or profile["source"] not in _SOURCES.values():
        raise ModelCapabilityError(error)
    warnings = profile["warnings"]
    if (not isinstance(warnings, list) or len(warnings) > len(_WARNINGS)
            or any(not isinstance(w, str) or w not in _WARNINGS.values() for w in warnings)):
        raise ModelCapabilityError(error)
    return {**profile, "warnings": list(warnings)}


def estimate_input_tokens(system, prompt):
    """Conservative byte upper estimate with fixed protocol room, not a tokenizer."""
    if not isinstance(system, str) or not isinstance(prompt, str):
        raise ModelCapabilityError("System and prompt must be text.")
    try:
        return len(system.encode("utf-8")) + len(prompt.encode("utf-8")) + PROTOCOL_MARGIN
    except UnicodeEncodeError:
        raise ModelCapabilityError("System and prompt must contain valid Unicode text.") from None


def plan_request(provider, model, system, prompt, requested_output, capabilities=None):
    """Plan one bounded request without truncation, network calls or retry policy."""
    profile = (resolve_capabilities(provider, model) if capabilities is None
               else validate_capabilities(provider, model, capabilities))
    if profile["status"] == "unsupported" or profile["api"] is None:
        raise ModelCapabilityError("This model is unavailable for text generation. Select a compatible text model.")
    if not _integer(requested_output, MAX_OUTPUT_LIMIT):
        raise ModelCapabilityError("The requested output allowance must be a positive integer within the supported limit.")
    estimate = estimate_input_tokens(system, prompt)
    warnings = list(profile["warnings"])
    output = min(requested_output, profile["output_limit"], AUTO_OUTPUT_CAP)
    if profile["reasoning"]:
        output = min(output + REASONING_HEADROOM, profile["output_limit"], AUTO_OUTPUT_CAP)
        warnings.append(_WARNINGS["reasoning"])
    if output < requested_output:
        warnings.append(_WARNINGS["clamped"])
    # Never quietly squeeze the requested answer/reasoning allowance to fit an
    # oversized input. Fitters can reduce structured context explicitly instead.
    available = profile["input_limit"]
    if profile["context_window"] is not None:
        available = min(available, profile["context_window"] - output)
    if output < min(requested_output, 256) or available < PROTOCOL_MARGIN or estimate > available:
        raise ModelCapabilityError(
            "This request exceeds the selected model's bounded input/context budget. "
            "Reduce the request scope or choose a model with verified larger limits; no AI request was sent.")
    return {"capabilities": profile, "input_estimate": estimate,
            "max_output_tokens": output, "available_input_tokens": available,
            "reasoning_effort": profile["effort"], "reasoning": profile["reasoning"],
            "api": profile["api"], "output_parameter": profile["output_parameter"],
            "warnings": warnings, "token_estimation": TOKEN_ESTIMATION}
