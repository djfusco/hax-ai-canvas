"""Hosted mode: admission, canonical host redirects and configuration checks."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python/app"), str(ROOT / "python"), str(ROOT / "tests")]
import app as web
from hosted_backend import HostedBackend
from hosted_config import settings, HostedSetupError
from hosted_data import PostgresMentorStore, PostgresCredentialVault
from test_hosted_data import MockDriver


def fixture_environment():
    return {
        "HAX_HOSTED_ENABLED": "1", "HAX_AUTH_MODE": "browser-beta", "DATABASE_URL": "postgresql://fixture",
        "HAX_APP_ORIGIN": "https://mentor.example",
        "HAX_SESSION_SECRET": "test-session-" + "s" * 40,
        "HAX_BETA_ACCESS_CODE": "test-invitation-" + "b" * 40,
        "HAX_MENTOR_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    }


class FakeHostedBackend(HostedBackend):
    def __init__(self, config, directory):
        self.config = config
        self.driver = MockDriver(Path(directory) / "postgres.sqlite")
        self.store = PostgresMentorStore(config.database_url, connect=self.driver)
        self.vault = PostgresCredentialVault(config.database_url, config.encryption_key, connect=self.driver)


class HostedApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = fixture_environment()
        self.environ = patch.dict(os.environ, self.env, clear=True)
        self.environ.start()
        self.service = FakeHostedBackend(settings(), self.temp.name)
        self.old_factory = web.app.config.get("HOSTED_BACKEND_FACTORY")
        self.old_limiter = web.app.config.get("HOSTED_LOGIN_LIMITER")
        web.app.config.update(TESTING=True, HOSTED_BACKEND_FACTORY=lambda config: self.service,
                              HOSTED_LOGIN_LIMITER=lambda config, peer: True)
        self.client = web.app.test_client()
        session = self.get("/mentor/api/session").json
        self.csrf = session["csrf"]
        self.assertFalse(session["authenticated"])

    def tearDown(self):
        self.environ.stop()
        for name, previous in (("HOSTED_BACKEND_FACTORY", self.old_factory),
                               ("HOSTED_LOGIN_LIMITER", self.old_limiter)):
            if previous is None:
                web.app.config.pop(name, None)
            else:
                web.app.config[name] = previous
        self.temp.cleanup()

    def get(self, path, client=None):
        return (client or self.client).get(path, base_url="https://mentor.example")

    def request(self, path, data=None, method="POST", expected=200, headers=None):
        response = self.client.open(
            path, method=method, json=data, base_url="https://mentor.example",
            headers={"X-Mentor-CSRF": self.csrf, **(headers or {})},
        )
        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
        return response.json

    def login(self):
        return self.request("/mentor/api/login", {"code": self.env["HAX_BETA_ACCESS_CODE"]})

    def test_admission_and_removed_endpoints(self):
        self.request("/mentor/api/workspaces", {"title": "Unauthenticated"}, expected=401)
        self.request("/mentor/api/login", {"code": "wrong"}, expected=401)
        self.login()
        self.assertTrue(self.get("/mentor/api/session").json["authenticated"])
        for path in ("/setup", "/run", "/history", "/import", "/mentor/legacy", "/mentor/internal/cleanup"):
            self.assertEqual(self.get(path).status_code, 404, path)
        response = self.get("/mentor")
        self.assertIn(b"consultant.js", response.data)
        cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        workspace = self.request("/mentor/api/workspaces", {"title": "Hosted course"}, expected=201)
        self.assertEqual(self.get("/mentor/api/workspaces/" + workspace["id"]).json["title"], "Hosted course")

    def test_alternate_entry_hosts_redirect_to_canonical_without_query_or_cookie(self):
        for path in ("/", "/mentor", "/mentor/"):
            for method in ("GET", "HEAD"):
                response = self.client.open(
                    path + "?private-query=discard-me", method=method,
                    base_url="https://alternate-deployment.example")
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], "https://mentor.example/mentor")
                self.assertNotIn("Set-Cookie", response.headers)
        response = self.get("/mentor")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Location", response.headers)

    def test_alias_posts_still_reject_origin_and_never_redirect_credentials(self):
        response = self.client.post(
            "/mentor/api/login", base_url="https://alternate-deployment.example",
            json={"code": "synthetic-not-a-real-code"},
            headers={"Origin": "https://alternate-deployment.example", "X-Mentor-CSRF": self.csrf})
        self.assertEqual(response.status_code, 403)
        self.assertIn("Cross-origin", response.json["error"])
        self.assertNotIn("Location", response.headers)
        self.assertFalse(self.get("/mentor/api/session").json["authenticated"])
        # A spoofed forwarded host must not choose the redirect destination.
        response = self.client.get("/mentor", base_url="https://alternate-deployment.example",
                                   headers={"X-Forwarded-Host": "evil.example"})
        self.assertEqual(response.headers["Location"], "https://mentor.example/mentor")


class HostedConfigurationTests(unittest.TestCase):
    def test_missing_and_invalid_config_never_echoes_values(self):
        with self.assertRaises(HostedSetupError) as missing:
            settings({})
        self.assertIn("DATABASE_URL", str(missing.exception))
        env = fixture_environment()
        env["HAX_APP_ORIGIN"] = "https://user:secret@wrong.example"
        with self.assertRaises(HostedSetupError) as invalid:
            settings(env)
        self.assertNotIn("secret@", str(invalid.exception))

    def test_removed_pipeline_variables_are_not_required(self):
        env = fixture_environment()
        self.assertEqual(settings(env).origin, "https://mentor.example")
        env["HAX_BETA_ACCESS_CODE"] = env["HAX_SESSION_SECRET"]
        with self.assertRaises(HostedSetupError):
            settings(env)


if __name__ == "__main__":
    unittest.main()
