"""Local identity-only account tests; no real email, database or cloud calls."""

import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "python/app")]
import app as web
from hosted_accounts import COOKIE_NAMES, NeonAuth
from local_accounts import LOCAL_SESSION_COOKIE, LocalAccounts
from mentor_credentials import CredentialVault
from mentor_store import MentorStore, WorkspaceNotFound

AUTH_URL = "https://ep-local-fixture.neonauth.us-east-1.aws.neon.tech/neondb/auth"


class LocalAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {"NEON_AUTH_BASE_URL": AUTH_URL}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.revoked = set()
        self.requests = []

        def upstream(request):
            self.requests.append(request)
            self.assertEqual(request.headers["origin"], "http://localhost")
            self.assertNotIn("mentor_browser", request.headers.get("cookie", ""))
            if request.url.path.endswith("send-verification-otp"):
                return httpx.Response(200, json={"success": True})
            if request.url.path.endswith("sign-in/email-otp"):
                value = json.loads(request.content)
                if value["otp"] != "123456":
                    return httpx.Response(400, json={"message": "must-not-be-disclosed"})
                person = value["email"].split("@")[0]
                self.revoked.discard(person)
                return httpx.Response(200, json={"user": {"id": person}}, headers={
                    "set-cookie": COOKIE_NAMES[0] + "=local-fixture-" + person +
                    "; Max-Age=3600; Secure; HttpOnly; Partitioned"})
            person = request.headers.get("cookie", "").split("local-fixture-")[-1]
            if request.url.path.endswith("sign-out"):
                self.revoked.add(person)
                return httpx.Response(200, json={"success": True})
            if person not in ("alice", "bob") or person in self.revoked:
                return httpx.Response(200, content=b"null")
            return httpx.Response(200, json={
                "user": {"id": person, "email": person + "@example.edu", "emailVerified": True},
                "session": {"userId": person, "expiresAt": "2099-01-01T00:00:00Z"}})

        self.transport = httpx.MockTransport(upstream)
        updates = {"TESTING": True, "MENTOR_DATA_ROOT": self.root,
                   "LOCAL_AUTH_FACTORY": lambda url, origin: NeonAuth(url, origin, self.transport),
                   "HOSTED_BACKEND_FACTORY": lambda _: self.fail("Local auth initialized hosted storage")}
        old = {key: web.app.config.get(key) for key in updates}
        web.app.config.update(updates)
        def restore():
            for key, value in old.items():
                if value is None:
                    web.app.config.pop(key, None)
                else:
                    web.app.config[key] = value
        self.addCleanup(restore)
        self.client = web.app.test_client()

    def session(self, client=None):
        return (client or self.client).get("/mentor/api/session").json

    def post(self, suffix, data, client=None, expected=200):
        client = client or self.client
        response = client.post("/mentor/api/" + suffix, json=data,
                               headers={"X-Mentor-CSRF": self.session(client)["csrf"]})
        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
        return response

    def sign_in(self, person="alice", client=None, claim=False):
        return self.post("auth/verify-code", {"email": person + "@example.edu",
                                            "code": "123456", "claim_browser": claim}, client)

    def test_identity_only_sign_in_with_two_local_courses(self):
        self.assertTrue(self.session()["account_required"])
        self.assertFalse(self.session()["invite_required"])
        self.post("workspaces", {"title": "Blocked"}, expected=401)
        self.post("auth/send-code", {"email": "alice@example.edu"})
        response = self.sign_in()
        cookies = response.headers.getlist("Set-Cookie")
        self.assertTrue(any(LOCAL_SESSION_COOKIE in header and "HttpOnly" in header for header in cookies))
        self.assertFalse(any(COOKIE_NAMES[0] in header for header in cookies))
        first = self.post("workspaces", {"title": "First course"}, expected=201).json
        second = self.post("workspaces", {"title": "Second course"}, expected=201).json
        self.assertNotEqual(first["id"], second["id"])
        browser = web.app.test_client()
        self.sign_in(client=browser)
        resumed = browser.get("/mentor/api/workspaces").json
        self.assertEqual({item["id"] for item in resumed}, {first["id"], second["id"]})
        self.post("credentials", {"provider": "openai", "api_key": "private-local-provider-fixture"})
        for request in self.requests:
            self.assertNotIn("First course", request.content.decode())
            self.assertNotIn("private-local-provider-fixture", request.content.decode())
        with sqlite3.connect(self.root / "accounts.sqlite") as db:
            sealed = db.execute("SELECT sealed FROM local_sessions").fetchall()
            self.assertTrue(sealed)
            self.assertFalse(any(b"local-fixture-alice" in bytes(row[0]) for row in sealed))

    def test_switching_accounts_does_not_expose_other_courses_or_keys(self):
        self.sign_in()
        course = self.post("workspaces", {"title": "Alice private"}, expected=201).json
        self.post("credentials", {"provider": "openai", "api_key": "alice-local-fixture"})
        self.post("auth/logout", {})
        self.assertFalse(self.session()["authenticated"])
        self.sign_in("bob")
        self.assertEqual(self.client.get("/mentor/api/workspaces").json, [])
        self.assertEqual(self.session()["credentials"], [])
        self.assertEqual(self.client.get("/mentor/api/workspaces/" + course["id"]).status_code, 404)
        self.post("auth/logout", {})
        self.sign_in()
        self.assertEqual(self.client.get("/mentor/api/workspaces/" + course["id"]).status_code, 200)

    def test_local_browser_claim_is_atomic_and_keeps_keys_local(self):
        accounts = LocalAccounts(self.root)
        legacy = "c" * 64
        store = MentorStore(self.root / "workspaces.sqlite")
        course = store.create(legacy, "Browser legacy")
        CredentialVault(self.root).save(legacy, "openai", {"api_key": "claim-fixture"})
        owner = accounts.resolve(AUTH_URL, {"subject": "alice"}, legacy_owner=legacy)
        self.assertEqual(store.get(owner, course["id"])["title"], "Browser legacy")
        self.assertEqual(CredentialVault(self.root).get(owner, "openai")["api_key"], "claim-fixture")
        with self.assertRaises(WorkspaceNotFound):
            store.get(legacy, course["id"])
        self.assertEqual(LocalAccounts(self.root).resolve(AUTH_URL, {"subject": "alice"}), owner)

    def test_csrf_host_and_revocation_are_enforced(self):
        response = self.client.post("/mentor/api/auth/send-code", json={"email": "alice@example.edu"})
        self.assertEqual(response.status_code, 403)
        response = self.client.get("/mentor/api/session", headers={"Host": "rebinding.example"})
        self.assertEqual(response.status_code, 403)
        self.sign_in()
        self.revoked.add("alice")
        self.assertFalse(self.session()["authenticated"])
        self.post("workspaces", {"title": "Revoked"}, expected=401)

    def test_public_setup_requires_no_cloud_storage_configuration(self):
        os.environ.pop("NEON_AUTH_BASE_URL")
        self.assertTrue(self.session()["local_auth_configurable"])
        self.post("local-auth/config", {"auth_url": "https://evil.example/auth"}, expected=400)
        self.post("local-auth/config", {"auth_url": AUTH_URL})
        self.assertTrue(self.session()["account_required"])
        self.sign_in()
        self.assertTrue(self.session()["local_accounts"])

    def test_forced_local_mode_ignores_hosted_execution_flags(self):
        with patch.dict(os.environ, {"HAX_EXECUTION_MODE": "local", "VERCEL": "1",
                                    "HAX_HOSTED_ENABLED": "1"}):
            self.assertTrue(self.session()["local_mode"])
            self.assertFalse(self.session()["hosted_ready"])


if __name__ == "__main__":
    unittest.main()
