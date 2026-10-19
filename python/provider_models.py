"""Bounded model catalogs; never issue inference or transmit course content."""
import os
import re
import time
from urllib.parse import urlsplit


class ModelDiscoveryError(ValueError):
    pass


def model_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}", value):
        raise ValueError("Choose a valid model identifier.")
    return value


def validate_gateway(provider, value):
    if provider != "nebula":
        if value:
            raise ValueError("Custom endpoints are supported only for institutional Nebula gateways.")
        return
    try:
        parsed = urlsplit(value)
        allowed = {h.strip() for h in os.environ.get("HAX_ALLOWED_AI_HOSTS", "").split(",") if h.strip()}
        valid = (parsed.scheme == "https" and not parsed.username and not parsed.password
                 and parsed.port in (None, 443) and not parsed.query and not parsed.fragment
                 and (parsed.hostname.endswith(".azure-api.net") or parsed.hostname in allowed))
    except (ValueError, AttributeError):
        valid = False
    if not valid:
        raise ValueError("Use an HTTPS Azure APIM gateway or an administrator-allowlisted AI host.")


def discover_models(provider, saved, *, transport=None):
    import httpx
    key = saved["api_key"]
    if provider == "openai":
        url, headers = "https://api.openai.com/v1/models", {"Authorization": "Bearer " + key}
    elif provider in ("anthropic", "nebula"):
        base = "https://api.anthropic.com"
        if provider == "nebula":
            base = saved.get("base_url", "")
            validate_gateway(provider, base)
        base = base.rstrip("/")
        url = base + ("/models" if base.endswith("/v1") else "/v1/models")
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    elif provider == "gemini":
        url, headers = "https://generativelanguage.googleapis.com/v1beta/models", {"x-goog-api-key": key}
    else:
        raise ValueError("Unsupported AI provider.")
    result, seen, cursors = [], set(), set()
    params = {"pageSize": 100} if provider == "gemini" else {"limit": 100} if provider != "openai" else {}
    deadline = time.monotonic() + 20
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, transport=transport) as client:
            for _ in range(10):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ModelDiscoveryError("Model discovery timed out. Please retry.")
                with client.stream("GET", url, headers=headers, params=params,
                                   timeout=min(10, remaining)) as response:
                    if provider == "nebula" and response.status_code in (404, 405, 501):
                        return {"models": [], "manual_allowed": True,
                                "warning": "This gateway does not expose model discovery. Enter its deployment/model ID manually."}
                    if not response.is_success:
                        raise ModelDiscoveryError("Unable to list models. Check this key's model-list permission, gateway and provider availability.")
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > 1024 * 1024 or time.monotonic() > deadline:
                            raise ModelDiscoveryError("Provider catalog exceeded its time or size limit.")
                    import json
                    data = json.loads(content)
                rows = data.get("models" if provider == "gemini" else "data")
                if not isinstance(rows, list) or len(rows) > 1000:
                    raise ModelDiscoveryError("Provider returned an invalid or oversized catalog.")
                for item in rows:
                    if not isinstance(item, dict):
                        raise ValueError()
                    identifier = item.get("name", "") if provider == "gemini" else item.get("id", "")
                    if provider == "gemini":
                        identifier = identifier.removeprefix("models/")
                        if "generateContent" not in item.get("supportedGenerationMethods", []):
                            continue
                    identifier = model_id(identifier)
                    if key in identifier:
                        continue
                    if re.search(r"(embed|whisper|tts|image|audio|realtime|moderation|dall-e|video|sora)", identifier, re.I):
                        continue
                    if item.get("lifecycle") == "retired" or identifier in seen:
                        continue
                    seen.add(identifier)
                    label = item.get("displayName", item.get("display_name", identifier))
                    if not isinstance(label, str):
                        label = identifier
                    label = label.replace(key, "[credential removed]")
                    result.append({"id": identifier, "name": "".join(c for c in label[:160] if c.isprintable()),
                                   "compatibility": "unverified" if provider in ("openai", "nebula") else "text"})
                    if len(result) > 500:
                        raise ModelDiscoveryError("More than 500 text-model candidates were returned. Narrow the provider catalog.")
                cursor = data.get("nextPageToken") if provider == "gemini" else data.get("last_id") if data.get("has_more") else None
                if not cursor:
                    return {"models": result, "manual_allowed": provider == "nebula",
                            "warning": "Catalog listing is not a guarantee of inference permission or request compatibility."}
                if not isinstance(cursor, str) or len(cursor) > 512 or cursor in cursors:
                    raise ModelDiscoveryError("Invalid model pagination.")
                cursors.add(cursor)
                params["pageToken" if provider == "gemini" else "after_id"] = cursor
            raise ModelDiscoveryError("Provider catalog exceeded the page limit.")
    except ModelDiscoveryError:
        raise
    except Exception:
        raise ModelDiscoveryError("Model discovery failed. Check the provider configuration and retry.") from None
