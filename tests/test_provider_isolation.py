import os
from pathlib import Path
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from ai_client import AIClient
from mentor_credentials import private_key


class ProviderIsolationTests(unittest.TestCase):
    def test_direct_providers_pin_origins_and_strip_global_auth_headers(self):
        for provider, factory_name, canonical in (
            ("anthropic", "Anthropic", "https://api.anthropic.com"),
            ("openai", "OpenAI", "https://api.openai.com/v1"),
        ):
            with self.subTest(provider=provider), patch.dict(os.environ, {
                "ANTHROPIC_BASE_URL": "https://wrong.example",
                "OPENAI_BASE_URL": "https://wrong.example",
                "ANTHROPIC_AUTH_TOKEN": "unrelated-global-test-token",
            }), patch("ai_client." + factory_name) as factory:
                client = AIClient(provider, api_key="explicit-unit-key", isolated_credentials=True)
                options = factory.call_args.kwargs
                self.assertEqual(options["base_url"], canonical)
                if provider == "anthropic":
                    self.assertEqual(options["auth_token"], "")
                transport = options["http_client"]
                try:
                    request = httpx.Request("POST", canonical + "/messages", headers={
                        "authorization": "Bearer unrelated-global-test-token",
                        "x-api-key": "wrong-key", "x-private-header": "must-not-leak",
                        "content-type": "application/json",
                    })
                    transport.event_hooks["request"][0](request)
                    self.assertNotIn("x-private-header", request.headers)
                    self.assertNotIn("unrelated-global-test-token", str(request.headers))
                    key_header = "x-api-key" if provider == "anthropic" else "authorization"
                    self.assertIn("explicit-unit-key", request.headers[key_header])
                    with self.assertRaises(ValueError):
                        transport.event_hooks["request"][0](httpx.Request(
                            "POST", "https://wrong.example/messages"))
                finally:
                    transport.close()
                client.close()

    def test_nebula_requires_explicit_gateway_and_sends_only_selected_key(self):
        with patch("ai_client.Anthropic") as factory:
            client = AIClient("nebula", api_key="institutional-unit-key",
                              base_url="https://example.azure-api.net/anthropic",
                              isolated_credentials=True)
            self.assertEqual(factory.call_args.kwargs["base_url"],
                             "https://example.azure-api.net/anthropic")
            transport = factory.call_args.kwargs["http_client"]
            try:
                request = httpx.Request("POST", "https://example.azure-api.net/anthropic/v1/messages",
                                        headers={"authorization": "Bearer unrelated"})
                transport.event_hooks["request"][0](request)
                self.assertNotIn("authorization", request.headers)
                self.assertEqual(request.headers["x-api-key"], "institutional-unit-key")
            finally:
                transport.close()
                client.close()

    def test_atomic_private_key_bootstrap(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "keys" / ".key"
            with ThreadPoolExecutor(max_workers=12) as pool:
                keys = list(pool.map(lambda _: private_key(path), range(40)))
            self.assertEqual(len(set(keys)), 1)
            self.assertEqual(len(keys[0]), 44)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
