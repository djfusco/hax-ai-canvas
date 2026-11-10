import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "python/app"), str(ROOT / "tests")]
import app as web
from provider_models import discover_models, ModelDiscoveryError
from hosted_config import settings
from test_hosted_integration import fixture_environment, FakeHostedBackend


class CatalogTests(unittest.TestCase):
    def test_openai_mentor_parameters_do_not_change_legacy_converter_contract(self):
        from ai_client import AIClient
        from types import SimpleNamespace
        for isolated in (False, True):
            with patch("ai_client.OpenAI") as factory:
                factory.return_value.chat.completions.create.return_value = SimpleNamespace(
                    choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="Fixture answer"))])
                client = AIClient("openai", model="future-text", api_key="fixture-key",
                                  isolated_credentials=isolated)
                try:
                    self.assertEqual(client.call("System", "Prompt", max_tokens=123), "Fixture answer")
                    options = factory.return_value.chat.completions.create.call_args.kwargs
                    self.assertIn("max_completion_tokens" if isolated else "max_tokens", options)
                    self.assertEqual("temperature" in options, not isolated)
                finally:
                    if isolated:
                        factory.call_args.kwargs["http_client"].close()
                    client.close()

    def test_direct_provider_auth_headers_and_no_course_or_environment_credentials(self):
        for provider, header, catalog in (
            ("openai", "authorization", {"data": [{"id": "future-text"}, {"id": "text-embedding-test"}]}),
            ("anthropic", "x-api-key", {"data": [{"id": "future-text", "display_name": "Future"}]}),
            ("gemini", "x-goog-api-key", {"models": [
                {"name": "models/future-text", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/embedding-test", "supportedGenerationMethods": ["embedContent"]}]}),
        ):
            def upstream(request):
                self.assertEqual(request.method, "GET")
                self.assertEqual(request.content, b"")
                self.assertIn("fixture-key", request.headers[header])
                self.assertNotIn("unrelated", str(request.headers))
                self.assertNotIn("fixture-key", str(request.url))
                return httpx.Response(200, json=catalog)
            with self.subTest(provider=provider), patch.dict(os.environ, {
                    "OPENAI_BASE_URL": "https://evil.example", "ANTHROPIC_AUTH_TOKEN": "unrelated"}):
                data = discover_models(provider, {"api_key": "fixture-key"},
                                       transport=httpx.MockTransport(upstream))
                self.assertEqual([m["id"] for m in data["models"]], ["future-text"])
                self.assertNotIn("fixture-key", json.dumps(data))

    def test_paginated_catalog_uses_only_fixed_provider_url(self):
        requests = []
        def upstream(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(200, json={"data": [{"id": "first-text"}],
                                                "has_more": True, "last_id": "cursor-1"})
            self.assertEqual(request.url.params["after_id"], "cursor-1")
            self.assertEqual(request.url.host, "api.anthropic.com")
            return httpx.Response(200, json={"data": [{"id": "second-text"}], "has_more": False})
        data = discover_models("anthropic", {"api_key": "fixture-key"},
                               transport=httpx.MockTransport(upstream))
        self.assertEqual(len(data["models"]), 2)

    def test_catalog_preserves_normalized_limits_not_upstream_urls_or_diagnostics(self):
        catalogs = (
            ("anthropic", {"data": [{"id": "future-text", "max_input_tokens": 100000,
                "max_tokens": 12000, "capabilities": {"thinking": {"supported": True},
                    "effort": {"medium": {"supported": True}}},
                "endpoint": "https://evil.example", "source": "fixture-key",
                "warnings": ["fixture-key"], "lifecycle": "active"}]}, 100000, 12000),
            ("gemini", {"models": [{"name": "models/future-text", "inputTokenLimit": 64000,
                "outputTokenLimit": 9000, "thinking": True,
                "supportedGenerationMethods": ["generateContent"],
                "endpoint": "https://evil.example"}]}, 64000, 9000),
        )
        for provider, catalog, limit, output in catalogs:
            calls = []
            def upstream(request):
                calls.append(request)
                return httpx.Response(200, json=catalog)
            with self.subTest(provider=provider):
                result = discover_models(provider, {"api_key": "fixture-key"},
                                         transport=httpx.MockTransport(upstream))
                profile = result["models"][0]["capabilities"]
                self.assertEqual(profile["status"], "provider_metadata")
                self.assertEqual(profile["input_limit"], limit)
                self.assertEqual(profile["output_limit"], output)
                self.assertEqual(profile["provider"], provider)
                self.assertTrue(profile["reasoning"])
                self.assertEqual(len(calls), 1)
                self.assertNotIn("evil.example", json.dumps(result))
                self.assertNotIn("fixture-key", json.dumps(result))

    def test_catalog_bad_limits_do_not_become_trusted_or_lose_valid_fields(self):
        catalog = {"data": [{"id": "future-text", "max_input_tokens": True, "max_tokens": 9000},
                            {"id": "another-text", "max_input_tokens": -1, "max_tokens": "100000"}]}
        result = discover_models("anthropic", {"api_key": "fixture-key"},
                                 transport=httpx.MockTransport(lambda r: httpx.Response(200, json=catalog)))
        valid_output, invalid = (m["capabilities"] for m in result["models"])
        self.assertEqual(valid_output["input_limit"], 32768)
        self.assertEqual(valid_output["output_limit"], 9000)
        self.assertEqual(invalid["status"], "unverified")
        self.assertEqual(invalid["output_limit"], 4096)

    def test_gateway_unsupported_is_manual_not_a_fabricated_catalog(self):
        def upstream(request):
            self.assertEqual(request.url.host, "example.azure-api.net")
            self.assertEqual(request.url.path, "/anthropic/v1/models")
            return httpx.Response(404)
        data = discover_models("nebula", {"api_key": "fixture-key",
                               "base_url": "https://example.azure-api.net/anthropic"},
                               transport=httpx.MockTransport(upstream))
        self.assertTrue(data["manual_allowed"])
        self.assertEqual(data["models"], [])

    def test_gateway_deployment_names_are_opaque_not_nontext_filters(self):
        catalog = {"data": [{"id": "course-image-reviewer", "max_input_tokens": 64000, "max_tokens": 8000},
                            {"id": "audio-discussion-feedback"}]}
        result = discover_models("nebula", {"api_key": "fixture-key",
                                 "base_url": "https://example.azure-api.net/anthropic"},
                                 transport=httpx.MockTransport(lambda r: httpx.Response(200, json=catalog)))
        self.assertEqual(len(result["models"]), 2)
        self.assertEqual(result["models"][0]["capabilities"]["status"], "provider_metadata")
        self.assertEqual(result["models"][1]["capabilities"]["status"], "unverified")

    def test_failures_do_not_follow_redirects_or_echo_upstream_secrets(self):
        for status, body in ((302, b""), (401, b"fixture-key"), (200, b"x" * (1024 * 1024 + 1))):
            calls = []
            def upstream(request):
                calls.append(request)
                return httpx.Response(status, content=body, headers={"location": "https://evil.example"})
            with self.subTest(status=status), self.assertRaises(ModelDiscoveryError) as failure:
                discover_models("openai", {"api_key": "fixture-key"},
                                transport=httpx.MockTransport(upstream))
            self.assertEqual(len(calls), 1)
            self.assertNotIn("fixture-key", str(failure.exception))

    def test_repeated_cursor_is_rejected(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
            "data": [{"id": "future-text"}], "has_more": True, "last_id": "same"}))
        with self.assertRaises(ModelDiscoveryError):
            discover_models("anthropic", {"api_key": "fixture-key"}, transport=transport)


class ModelApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict(os.environ, fixture_environment(), clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.service = FakeHostedBackend(settings(), self.temp.name)
        self.old = {key: web.app.config.get(key) for key in
                    ("HOSTED_BACKEND_FACTORY", "HOSTED_LOGIN_LIMITER", "MODEL_DISCOVERY")}
        web.app.config.update(HOSTED_BACKEND_FACTORY=lambda config: self.service,
                              HOSTED_LOGIN_LIMITER=lambda config, peer: True,
                              MODEL_DISCOVERY=lambda provider, saved: {
                                  "models": [{"id": "future-text", "name": "Future"}],
                                  "warning": "", "manual_allowed": False})
        self.addCleanup(self.restore)
        self.client = web.app.test_client()
        self.csrf = self.client.get("/mentor/api/session", base_url="https://mentor.example").json["csrf"]
        self.request("/login", {"code": os.environ["HAX_BETA_ACCESS_CODE"]})
        self.request("/credentials", {"provider": "openai", "api_key": "fixture-key"})

    def restore(self):
        for key, value in self.old.items():
            if value is None:
                web.app.config.pop(key, None)
            else:
                web.app.config[key] = value

    def request(self, path, data=None, method="POST", status=200):
        result = self.client.open("/mentor/api" + path, method=method, json=data,
                                 base_url="https://mentor.example", headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(result.status_code, status, result.get_data(as_text=True))
        self.assertNotIn("fixture-key", result.get_data(as_text=True))
        return result.json

    def test_discovery_selection_restore_and_no_silent_default(self):
        info = self.request("/providers/openai/models", method="GET")
        self.assertEqual(info["models"], [])
        work = self.request("/workspaces", {"title": "Fixture"}, status=201)
        refused = self.request("/workspaces/" + work["id"] + "/course-map/align", {"provider": "openai"}, status=400)
        self.assertIn("model", refused["error"])
        info = self.request("/providers/openai/models/refresh", {})
        self.request("/providers/openai/model",
                     {"model": "future-text", "version": info["version"]}, method="PUT")
        restored = self.request("/providers/openai/models", method="GET")
        self.assertEqual(restored["selected"], "future-text")

    def test_new_credentials_invalidate_catalog_and_fence_old_selection(self):
        info = self.request("/providers/openai/models/refresh", {})
        self.request("/credentials", {"provider": "openai", "api_key": "replacement-key"})
        fresh = self.request("/providers/openai/models", method="GET")
        self.assertEqual(fresh["models"], [])
        self.assertNotEqual(info["version"], fresh["version"])
        self.request("/providers/openai/models/refresh", {})
        self.request("/providers/openai/model", {"model": "future-text", "version": info["version"]},
                     method="PUT", status=409)
        self.request("/credentials/openai", method="DELETE")
        self.request("/providers/openai/models", method="GET", status=400)

    def test_slow_catalog_cannot_restore_replaced_key(self):
        def discover(provider, saved):
            # The fixture owner is recoverable from the browser's signed token, not from user input.
            from itsdangerous import URLSafeTimedSerializer
            import hashlib
            cookie = self.client.get_cookie("mentor_browser", domain="mentor.example").value
            token = URLSafeTimedSerializer(os.environ["HAX_SESSION_SECRET"],
                        salt="faculty-mentor-browser").loads(cookie)["token"]
            owner = hashlib.sha256(token.encode()).hexdigest()
            self.service.vault.save(owner, provider, {"api_key": "replacement-key"})
            return {"models": [{"id": "future-text", "name": "Future"}]}
        web.app.config["MODEL_DISCOVERY"] = discover
        self.request("/providers/openai/models/refresh", {}, status=409)
        self.assertEqual(self.request("/providers/openai/models", method="GET")["models"], [])

    def test_selected_capabilities_are_server_owned_and_legacy_models_are_explicitly_unverified(self):
        info = self.request("/providers/openai/models/refresh", {})
        selected = self.request("/providers/openai/model", {
            "model": "future-text", "version": info["version"],
            "capabilities": {"input_limit": 999999999, "api": "https://untrusted.example"}},
            method="PUT")
        profile = selected["selected_capabilities"]
        self.assertEqual(profile["status"], "unverified")
        self.assertEqual(profile["input_limit"], 32768)
        self.assertEqual(profile["api"], "chat_completions")
        restored = self.request("/providers/openai/models", method="GET")
        self.assertEqual(restored["selected_capabilities"], profile)
        self.assertNotIn("untrusted.example", json.dumps(restored))

    def test_known_unsupported_model_is_rejected_before_sdk_construction(self):
        web.app.config["MODEL_DISCOVERY"] = lambda provider, saved: {
            "models": [{"id": "davinci-002", "name": "Legacy completion-only"}]}
        info = self.request("/providers/openai/models/refresh", {})
        with patch("ai_client.AIClient") as factory:
            self.request("/providers/openai/model", {
                "model": "davinci-002", "version": info["version"]}, method="PUT", status=400)
            factory.assert_not_called()
