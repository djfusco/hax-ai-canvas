"""OpenRouter provider: capabilities, request shape, discovery and PKCE connect.

Real SDK and httpx with synthetic transports; never live network access.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "python/app")]
from ai_client import AIClient, AIResponseError
from model_capabilities import ModelCapabilityError, plan_request, resolve_capabilities, validate_capabilities
from provider_models import discover_models

CATALOG_ENTRY = {
    "id": "anthropic/claude-sonnet-4.5", "name": "Anthropic: Claude Sonnet 4.5",
    "context_length": 1000000,
    "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
    "top_provider": {"context_length": 1000000, "max_completion_tokens": 64000},
    "pricing": {"prompt": "0.000003", "completion": "0.000015"},
    "supported_parameters": ["max_tokens", "temperature", "structured_outputs", "response_format", "reasoning"],
    "description": "x" * 2000,
}


def guard():
    return patch.object(socket, "create_connection", side_effect=AssertionError("Live network prohibited"))


class CapabilityTests(unittest.TestCase):
    def test_catalog_metadata(self):
        profile = resolve_capabilities("openrouter", CATALOG_ENTRY["id"], CATALOG_ENTRY)
        self.assertEqual(profile["status"], "provider_metadata")
        self.assertEqual((profile["input_limit"], profile["output_limit"]), (1000000, 64000))
        self.assertTrue(profile["structured_outputs"])
        self.assertTrue(profile["reasoning"])
        self.assertEqual((profile["api"], profile["output_parameter"]), ("openrouter_chat", "max_tokens"))
        self.assertEqual(validate_capabilities("openrouter", CATALOG_ENTRY["id"], profile), profile)
        plan = plan_request("openrouter", CATALOG_ENTRY["id"], "s", "p", 3500, profile)
        self.assertEqual(plan["max_output_tokens"], 3500 + 8192)

    def test_unknown_and_non_text_models(self):
        fallback = resolve_capabilities("openrouter", "someone/new-model")
        self.assertEqual((fallback["status"], fallback["input_limit"], fallback["structured_outputs"]),
                         ("unverified", 32768, False))
        image = dict(CATALOG_ENTRY, id="x/image-gen", architecture={"input_modalities": ["text"],
                                                                     "output_modalities": ["image"]})
        self.assertEqual(resolve_capabilities("openrouter", "x/image-gen", image)["status"], "unsupported")
        bad = dict(CATALOG_ENTRY, context_length=-5, top_provider={"max_completion_tokens": "lots"})
        self.assertIn("Some advertised capability fields were invalid and were ignored.",
                      resolve_capabilities("openrouter", CATALOG_ENTRY["id"], bad)["warnings"])
        profile = resolve_capabilities("openrouter", CATALOG_ENTRY["id"], CATALOG_ENTRY)
        with self.assertRaises(ModelCapabilityError):
            validate_capabilities("openrouter", CATALOG_ENTRY["id"], dict(profile, api="chat_completions"))


class ClientTests(unittest.TestCase):
    def client(self, response):
        requests = []
        real = httpx.Client

        class Fixture(real):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(lambda r: (requests.append(r), httpx.Response(200, json=response))[1])
                super().__init__(*args, **kwargs)

        profile = resolve_capabilities("openrouter", CATALOG_ENTRY["id"], CATALOG_ENTRY)
        with patch("httpx.Client", Fixture), guard():
            client = AIClient("openrouter", model=CATALOG_ENTRY["id"], api_key="sk-or-fixture",
                              isolated_credentials=True, capabilities=profile)
        self.addCleanup(client.close)
        return client, requests

    @staticmethod
    def chat(reason="stop", content="{\"reply\": \"ok\"}"):
        return {"id": "gen-1", "object": "chat.completion", "created": 1, "model": CATALOG_ENTRY["id"],
                "choices": [{"index": 0, "finish_reason": reason,
                             "message": {"role": "assistant", "content": content}}]}

    def test_structured_request_shape(self):
        client, requests = self.client(self.chat())
        schema = {"type": "object", "properties": {"reply": {"type": "string"}}, "required": ["reply"],
                  "additionalProperties": False}
        with guard():
            self.assertEqual(client.call_structured("system text", "prompt text", schema, max_tokens=3500),
                             '{"reply": "ok"}')
        request = requests[0]
        self.assertEqual(str(request.url), "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(request.headers["authorization"], "Bearer sk-or-fixture")
        body = json.loads(request.content)
        self.assertEqual(body["model"], CATALOG_ENTRY["id"])
        self.assertEqual(body["max_tokens"], 3500 + 8192)
        self.assertNotIn("store", body)
        self.assertEqual(body["messages"][0], {"role": "system", "content": "system text"})
        self.assertEqual(body["response_format"]["json_schema"]["schema"], schema)

    def test_truncated_answer_is_an_error(self):
        client, _ = self.client(self.chat(reason="length"))
        with guard(), self.assertRaises(AIResponseError):
            client.call("s", "p", max_tokens=100)

    def test_requires_isolated_credentials(self):
        with self.assertRaises(ValueError):
            AIClient("openrouter", model="a/b", api_key="k")


class DiscoveryTests(unittest.TestCase):
    def test_lists_text_models_with_pricing(self):
        catalog = {"data": [
            CATALOG_ENTRY,
            dict(CATALOG_ENTRY, id="x/image-gen", name="Image",
                 architecture={"input_modalities": ["text"], "output_modalities": ["image"]}),
            dict(CATALOG_ENTRY, id="openai/text-embedding-3", name="Embed"),
        ] + [dict(CATALOG_ENTRY, id=f"vendor/model-{n}") for n in range(300)]}
        seen = []

        def reply(request):
            seen.append(request)
            return httpx.Response(200, json=catalog)
        with guard():
            result = discover_models("openrouter", {"api_key": "sk-or-fixture"}, transport=httpx.MockTransport(reply))
        ids = [m["id"] for m in result["models"]]
        self.assertIn(CATALOG_ENTRY["id"], ids)
        self.assertNotIn("x/image-gen", ids)
        self.assertNotIn("openai/text-embedding-3", ids)
        first = result["models"][0]
        self.assertEqual(first["name"], "Anthropic: Claude Sonnet 4.5")
        self.assertEqual(first["pricing"], {"input_per_million": 3.0, "output_per_million": 15.0})
        self.assertTrue(first["capabilities"]["structured_outputs"])
        self.assertEqual(str(seen[0].url), "https://openrouter.ai/api/v1/models")


class ConnectFlowTests(unittest.TestCase):
    def setUp(self):
        import app as web
        self.web = web
        self.temp = tempfile.TemporaryDirectory()
        self.old_root = web.app.config.get("MENTOR_DATA_ROOT")
        web.app.config.update(TESTING=True, MENTOR_DATA_ROOT=Path(self.temp.name))
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.client = web.app.test_client()
        self.csrf = self.client.get("/mentor/api/session").json["csrf"]

    def tearDown(self):
        self.environment.stop()
        if self.old_root is None:
            self.web.app.config.pop("MENTOR_DATA_ROOT", None)
        else:
            self.web.app.config["MENTOR_DATA_ROOT"] = self.old_root
        self.temp.cleanup()

    def start(self):
        response = self.client.post("/mentor/api/openrouter/start", headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(response.status_code, 200)
        return parse_qs(urlsplit(response.json["url"]).query)

    def exchange(self, code="authcode1234", expected=200, issued=None, status=200):
        posted = []
        real = httpx.Client

        class Fixture(real):
            def __init__(self, *args, **kwargs):
                def reply(request):
                    posted.append(request)
                    return httpx.Response(status, json=issued if issued is not None else {"key": "sk-or-user"})
                kwargs["transport"] = httpx.MockTransport(reply)
                super().__init__(*args, **kwargs)
        with patch("httpx.Client", Fixture), guard():
            response = self.client.post("/mentor/api/openrouter/exchange", json={"code": code},
                                        headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
        return posted

    def test_pkce_round_trip(self):
        query = self.start()
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertTrue(query["callback_url"][0].endswith("/mentor/openrouter/callback"))
        posted = self.exchange()
        body = json.loads(posted[0].content)
        self.assertEqual(str(posted[0].url), "https://openrouter.ai/api/v1/auth/keys")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(body["code_verifier"].encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(challenge, query["code_challenge"][0])
        credentials = self.client.get("/mentor/api/session").json["credentials"]
        self.assertEqual([c["provider"] for c in credentials], ["openrouter"])

    def test_exchange_requires_a_started_flow_and_a_key(self):
        self.exchange(expected=400)  # no PKCE cookie: never started in this browser
        self.start()
        self.exchange(code="bad code!", expected=400)
        self.exchange(expected=400, issued={"error": "invalid"}, status=403)
        self.assertEqual(self.client.get("/mentor/api/session").json["credentials"], [])

    def test_callback_does_not_touch_the_identity_cookie(self):
        fresh = self.web.app.test_client()
        response = fresh.get("/mentor/openrouter/callback?code=abc12345")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("mentor_browser", response.headers.get("Set-Cookie", ""))
        self.assertIn(b"openrouter-callback.js", response.data)


if __name__ == "__main__":
    unittest.main()
