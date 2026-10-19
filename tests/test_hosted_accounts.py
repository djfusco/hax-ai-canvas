"""Synthetic Neon HTTP and recording-Postgres tests; never send real email."""
from concurrent.futures import ThreadPoolExecutor
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
from hosted_accounts import (NeonAuth, PostgresAccounts, COOKIE_NAMES,
                             AccountAuthError, validate_auth_url, session_cookies,
                             parse_session_updates, new_session_cookies)
from hosted_config import settings, HostedSetupError
from mentor_store import WorkspaceConflict, WorkspaceNotFound
from test_hosted_integration import fixture_environment, FakeHostedBackend

AUTH_URL = "https://ep-fixture.neonauth.us-east-1.aws.neon.tech/neondb/auth"


def identity(email="alice@example.edu"):
    return {"user": {"id": email.split("@")[0], "email": email, "emailVerified": True},
            "session": {"userId": email.split("@")[0], "expiresAt": "2099-01-01T00:00:00Z",
                        "token": "never-return-upstream-token"}}


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = fixture_environment()
        self.env.update(HAX_AUTH_MODE="neon", NEON_AUTH_BASE_URL=AUTH_URL)
        self.environment = patch.dict(os.environ, self.env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.service = FakeHostedBackend(settings(), self.temp.name)
        self.service.accounts = PostgresAccounts(settings().database_url, self.service.driver)
        self.requests = []
        self.revoked = set()

        def upstream(request):
            self.requests.append(request)
            self.assertEqual(request.headers["origin"], "https://mentor.example")
            self.assertEqual(request.headers["x-neon-auth-middleware"], "true")
            self.assertNotIn("mentor_browser", request.headers.get("cookie", ""))
            if request.url.path.endswith("send-verification-otp"):
                return httpx.Response(200, json={"success": True})
            if request.url.path.endswith("sign-in/email-otp"):
                data = json.loads(request.content)
                if data["otp"] != "123456":
                    return httpx.Response(400, json={"message": "secret-upstream-detail"})
                name = data["email"].split("@")[0]
                self.revoked.discard(name)
                return httpx.Response(200, json={"token": "never-return-upstream-token",
                                                "user": identity(data["email"])["user"]}, headers={
                    "set-cookie": COOKIE_NAMES[0] + "=token-" + name +
                    "; Path=/; Domain=upstream.neon.tech; Max-Age=3600; Secure; HttpOnly; SameSite=None; Partitioned"})
            if request.url.path.endswith("sign-out"):
                name = request.headers.get("cookie", "").split("token-")[-1]
                self.revoked.add(name)
                return httpx.Response(200, json={"success": True})
            if request.url.path.endswith("get-session"):
                self.assertEqual(request.url.params["disableCookieCache"], "true")
                name = request.headers.get("cookie", "").split("token-")[-1]
                if name not in ("alice", "bob") or name in self.revoked:
                    return httpx.Response(200, content=b"null")
                return httpx.Response(200, json=identity(name + "@example.edu"))
            self.fail("Unexpected auth route.")

        self.transport = httpx.MockTransport(upstream)
        updates = {"TESTING": True, "HOSTED_BACKEND_FACTORY": lambda config: self.service,
                   "HOSTED_LOGIN_LIMITER": lambda config, peer: True,
                   "HOSTED_AUTH_FACTORY": lambda url, origin: NeonAuth(url, origin, self.transport)}
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
        return (client or self.client).get("/mentor/api/session", base_url="https://mentor.example").json

    def post(self, path, body, client=None, expected=200):
        client = client or self.client
        response = client.post("/mentor/api/" + path, json=body, base_url="https://mentor.example",
                               headers={"X-Mentor-CSRF": self.session(client)["csrf"]})
        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
        self.assertNotIn("never-return-upstream-token", response.get_data(as_text=True))
        return response

    def sign_in(self, email="alice@example.edu", client=None, invite=True, claim=False):
        if invite:
            self.post("login", {"code": self.env["HAX_BETA_ACCESS_CODE"]}, client)
        return self.post("auth/verify-code", {"email": email, "code": "123456",
                                             "claim_browser": claim}, client)

    def test_invitation_alone_does_not_authorize_account_work(self):
        self.post("login", {"code": self.env["HAX_BETA_ACCESS_CODE"]})
        self.assertTrue(self.session()["account_required"])
        self.post("workspaces", {"title": "Denied"}, expected=401)

    def test_email_code_is_verified_and_no_token_is_returned(self):
        self.post("auth/send-code", {"email": "alice@example.edu"})
        self.post("auth/verify-code", {"email": "alice@example.edu", "code": "999999"}, expected=400)
        before = self.session()["csrf"]
        response = self.sign_in()
        session = self.session()
        self.assertTrue(session["authenticated"])
        self.assertEqual(session["account"]["email"], "alice@example.edu")
        self.assertNotEqual(before, session["csrf"])
        cookie = next(h for h in response.headers.getlist("Set-Cookie") if COOKIE_NAMES[0] in h)
        for flag in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/mentor"):
            self.assertIn(flag, cookie)
        self.assertNotIn("Domain=", cookie)
        self.assertNotIn("Partitioned", cookie)

    def test_same_account_across_browsers_different_accounts_are_isolated(self):
        self.sign_in()
        workspace = self.post("workspaces", {"title": "Alice course"}, expected=201).json
        second = web.app.test_client()
        self.sign_in(client=second, invite=False)
        path = "/mentor/api/workspaces/" + workspace["id"]
        self.assertEqual(second.get(path, base_url="https://mentor.example").status_code, 200)
        bob = web.app.test_client()
        self.sign_in("bob@example.edu", bob)
        for suffix in ("", "/inventory", "/download", "/preview/index.html"):
            self.assertEqual(bob.get(path + suffix, base_url="https://mentor.example").status_code, 404)
        self.post("credentials", {"provider": "openai", "api_key": "alice-fixture-key"})
        self.assertEqual(self.session(bob)["credentials"], [])
        self.assertEqual(bob.get("/mentor/api/providers/openai/models",
                                base_url="https://mentor.example").status_code, 400)
        self.assertEqual(bob.get(path + "/uploads", base_url="https://mentor.example").status_code, 405)

    def test_new_account_cannot_skip_invitation(self):
        self.post("auth/verify-code", {"email": "bob@example.edu", "code": "123456"}, expected=403)
        self.assertFalse(self.session()["authenticated"])

    def test_token_response_without_a_cookie_never_authenticates(self):
        seen = []
        def upstream(request):
            seen.append(request.url.path)
            return httpx.Response(200, json={"token": "never-return-upstream-token", "user": identity()["user"]})
        web.app.config["HOSTED_AUTH_FACTORY"] = lambda url, origin: NeonAuth(
            url, origin, httpx.MockTransport(upstream))
        self.post("auth/verify-code", {"email": "alice@example.edu", "code": "123456"}, expected=503)
        self.assertEqual(len(seen), 1)
        self.assertFalse(self.session()["authenticated"])

    def test_logout_and_upstream_revocation_remove_access(self):
        self.sign_in()
        self.post("auth/logout", {})
        self.assertFalse(self.session()["authenticated"])
        self.post("workspaces", {"title": "Denied"}, expected=401)
        self.sign_in()
        self.revoked.add("alice")
        self.post("workspaces", {"title": "Revoked"}, expected=401)

    def test_csrf_and_foreign_origin_rejected_before_sending_email(self):
        before = len(self.requests)
        for headers in ({}, {"X-Mentor-CSRF": self.session()["csrf"], "Origin": "https://evil.example"}):
            response = self.client.post("/mentor/api/auth/send-code", json={"email": "alice@example.edu"},
                                        headers=headers, base_url="https://mentor.example")
            self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self.requests), before)

    def test_throttling_blocks_auth_requests(self):
        web.app.config["HOSTED_LOGIN_LIMITER"] = lambda config, peer: False
        self.post("auth/send-code", {"email": "alice@example.edu"}, expected=429)
        self.assertEqual(self.requests, [])

    def test_claim_moves_private_records_without_merging_other_accounts(self):
        legacy = "c" * 64
        old = self.service.store.create(legacy, "Legacy work")
        self.service.vault.save(legacy, "openai", {"api_key": "legacy-fixture-key"})
        verified = {"subject": "alice", "email": "alice@example.edu"}
        owner = self.service.accounts.resolve(AUTH_URL, verified, admit=True, legacy_owner=legacy)
        self.assertNotEqual(owner, legacy)
        self.assertEqual(self.service.store.get(owner, old["id"])["title"], "Legacy work")
        with self.assertRaises(WorkspaceNotFound):
            self.service.store.get(legacy, old["id"])
        self.assertEqual(self.service.vault.get(owner, "openai")["api_key"], "legacy-fixture-key")
        with self.assertRaises(WorkspaceConflict):
            self.service.accounts.resolve(AUTH_URL, {"subject": "bob"}, admit=True, legacy_owner=legacy)

    def test_claim_preserves_prepared_and_raw_paths_and_owner_scoped_upload_status(self):
        legacy = "f" * 64
        work = self.service.store.create(legacy, "Prepared legacy")
        record = self.service.uploads.create(legacy, work["id"], 1, "course.imscc", 1)
        self.service.uploads.completed(record["id"], {
            "pathname": record["pathname"], "size": 1, "etag": "fixture",
            "url": "https://fixture.private.blob.vercel-storage.com/" + record["pathname"]})
        record = self.service.uploads.claim(record["id"])
        prepared = {"pathname": f"prepared/{legacy}/{work['id']}/{record['id']}/attempt-1/course.haxcourse",
                    "sha256": "a" * 64, "size": 1}
        self.service.uploads.prepared(record["id"], prepared, attempt=1)
        self.service.uploads.attach(record["id"], {
            "source_type": "prepared", "course_id": "imscc-" + record["id"],
            "prepared_blob": prepared["pathname"], "sha256": prepared["sha256"], "size": 1}, attempt=1)
        owner = self.service.accounts.resolve(AUTH_URL, {"subject": "alice"}, admit=True, legacy_owner=legacy)
        saved = self.service.uploads.get(owner, record["id"])
        self.assertEqual(saved["pathname"], record["pathname"])
        self.assertEqual(saved["prepared"]["pathname"], prepared["pathname"])
        self.assertEqual(self.service.store.get(owner, work["id"])["source"]["prepared_blob"], prepared["pathname"])
        with self.assertRaises(WorkspaceNotFound):
            self.service.uploads.get(legacy, record["id"])

    def test_claim_refuses_running_jobs_and_rolls_back(self):
        legacy = "d" * 64
        old = self.service.store.create(legacy, "Busy work")
        self.service.store.begin_interview(legacy, old["id"], 1)
        with self.assertRaises(WorkspaceConflict):
            self.service.accounts.resolve(AUTH_URL, {"subject": "alice"}, admit=True, legacy_owner=legacy)
        self.assertEqual(self.service.store.get(legacy, old["id"])["title"], "Busy work")

    def test_competing_legacy_claims_have_only_one_winner(self):
        legacy = "e" * 64
        self.service.store.create(legacy, "Legacy")
        def claim(name):
            try:
                return self.service.accounts.resolve(AUTH_URL, {"subject": name}, admit=True, legacy_owner=legacy)
            except WorkspaceConflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ("alice", "bob")))
        self.assertEqual(sum(value is not None for value in results), 1)

    def test_default_hosted_auth_fails_closed_without_neon_url(self):
        env = fixture_environment()
        env.pop("HAX_AUTH_MODE")
        with self.assertRaises(HostedSetupError):
            settings(env)

    def test_expired_or_unverified_session_is_signed_out(self):
        for changes in ("expired", "unverified", "wrong-user"):
            data = identity()
            if changes == "expired":
                data["session"]["expiresAt"] = "2000-01-01T00:00:00Z"
            elif changes == "unverified":
                data["user"]["emailVerified"] = False
            else:
                data["session"]["userId"] = "different-user"
            auth = NeonAuth(AUTH_URL, "https://mentor.example",
                            httpx.MockTransport(lambda request: httpx.Response(200, json=data)))
            account, cookies = auth.session(COOKIE_NAMES[0] + "=fixture")
            self.assertIsNone(account)
            self.assertTrue(all(age == 0 for _, _, age in cookies))

    def test_auth_url_and_forwarded_cookie_are_constrained(self):
        for url in ("http://localhost/auth", "https://evil.example/db/auth",
                    AUTH_URL + "?redirect=evil", AUTH_URL.replace("https://", "https://user@")):
            with self.assertRaises(ValueError):
                validate_auth_url(url)
        self.assertEqual(session_cookies({"mentor_browser": "unrelated",
                                         COOKIE_NAMES[0]: "token-alice"}),
                         COOKIE_NAMES[0] + "=token-alice")

    def test_auth_transport_sanitizes_failures_and_bounds_responses(self):
        for status, body in ((302, b""), (500, b"secret-upstream-detail"), (200, b"x" * 131073)):
            auth = NeonAuth(AUTH_URL, "https://mentor.example",
                            httpx.MockTransport(lambda request: httpx.Response(status, content=body)))
            with self.assertRaises(AccountAuthError) as error:
                auth.call("send", data={"email": "alice@example.edu", "type": "sign-in"})
            self.assertNotIn("secret-upstream-detail", str(error.exception))


class SessionCookieParsingTests(unittest.TestCase):
    def test_modern_flags_do_not_hide_allowlisted_session_cookie(self):
        for name in COOKIE_NAMES:
            for flags in ("Partitioned", "partitioned", "Priority=High; Partitioned; FutureFlag"):
                updates = parse_session_updates([
                    name + "=synthetic-token; Path=/; Secure; HttpOnly; Max-Age=3600; " + flags])
                self.assertEqual(updates, [(name, "synthetic-token", 3600)])
                self.assertEqual(new_session_cookies(updates), name + "=synthetic-token")

    def test_cookie_names_are_exact_and_unrelated_headers_are_not_forwarded(self):
        headers = ["analytics_session_token_ref=unrelated; Partitioned",
                   "__Secure-neon-auth.local.session_data=untrusted-cache; Partitioned",
                   "Priority=High", COOKIE_NAMES[0] + "=synthetic-token; Priority=High; Partitioned"]
        self.assertEqual(parse_session_updates(headers), [(COOKIE_NAMES[0], "synthetic-token", None)])

    def test_max_age_and_expires_are_preserved_with_max_age_precedence(self):
        from datetime import datetime, timezone
        from email.utils import format_datetime
        now = 1800000000
        expires = format_datetime(datetime.fromtimestamp(now + 600, timezone.utc), usegmt=True)
        prefix = COOKIE_NAMES[0] + "=synthetic-token; Partitioned; Expires=" + expires
        self.assertEqual(parse_session_updates([prefix], now=now)[0][2], 600)
        self.assertEqual(parse_session_updates([prefix + "; Max-Age=20"], now=now)[0][2], 20)
        self.assertEqual(parse_session_updates([prefix + "; Max-Age=-1"], now=now)[0][2], 0)
        self.assertEqual(parse_session_updates([prefix], now=now + 700)[0][2], 0)

    def test_last_deletion_wins_and_values_are_not_confused_with_attributes(self):
        name = COOKIE_NAMES[0]
        updates = parse_session_updates([name + "=token-max-age=0; Max-Age=3600; Partitioned"])
        self.assertEqual(updates[0][2], 3600)
        self.assertTrue(new_session_cookies(updates))
        updates += parse_session_updates([name + "=; Max-Age=0; Partitioned"])
        self.assertEqual(new_session_cookies(updates), "")

    def test_signed_base64_and_percent_encoded_values_remain_unchanged(self):
        for token in ("synthetic+signed/value==", "synthetic%2Bvalue%2Fsignature%3D"):
            updates = parse_session_updates([COOKIE_NAMES[0] + "=" + token + "; Partitioned"])
            self.assertEqual(new_session_cookies(updates), COOKIE_NAMES[0] + "=" + token)

    def test_malformed_or_oversized_values_and_header_injection_are_rejected(self):
        for token in ("bad token", "bad,token", "bad;token", "bad\\token", "bad\rtoken", "x" * 4097):
            self.assertEqual(session_cookies({COOKIE_NAMES[0]: token}), "")
        self.assertEqual(parse_session_updates([COOKIE_NAMES[0] + "=token\r\nInjected: value"]), [])
        self.assertEqual(parse_session_updates(["not=a-cookie; " + COOKIE_NAMES[0] + "=token"]), [])
