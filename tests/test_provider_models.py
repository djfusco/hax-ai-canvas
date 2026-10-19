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
from mentor_store import WorkspaceConflict
from hosted_jobs import execute_mentor
from hosted_config import settings
from test_hosted_integration import fixture_environment, FakeHostedBackend


class CatalogTests(unittest.TestCase):
    def test_openai_mentor_parameters_do_not_change_legacy_converter_contract(self):
        from ai_client import AIClient
        from types import SimpleNamespace
        for isolated in (False, True):
            with patch("ai_client.OpenAI") as factory:
                factory.return_value.chat.completions.create.return_value = SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="Fixture answer"))])
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
        self.request("/workspaces/" + work["id"] + "/chat",
                     {"message": "Hello", "provider": "openai"}, status=400)
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

    def test_queued_model_does_not_change_with_preference(self):
        owner = "a" * 64
        self.service.vault.save(owner, "openai", {"api_key": "fixture-key", "model": "model-a"})
        saved = self.service.vault.get(owner, "openai")
        work = self.service.store.create(owner, "Fixture")
        work = self.service.store.begin_interview(owner, work["id"], work["revision"])
        job = work["active_job"]["id"]
        self.service.store.pin_provider(owner, work["id"], job, "openai", "model-a", saved["version"])
        with self.assertRaises(WorkspaceConflict):
            self.service.store.pin_provider(owner, work["id"], job, "openai", "model-b", saved["version"])
        self.service.store.add_message(owner, work["id"], "user", "Goals", "interview")
        self.service.vault.update_preferences(owner, "openai", saved["version"], {"model": "model-b"})
        with patch("ai_client.AIClient") as factory:
            factory.return_value.call.return_value = '{"reply":"What are your goals?"}'
            outcome = execute_mentor(owner, work["id"], job, "interview", "openai", self.service)
            self.assertEqual(outcome["status"], "completed")
            self.assertEqual(factory.call_args.kwargs["model"], "model-a")
