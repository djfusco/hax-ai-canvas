"""Publish to HAX Lite (connect and approve) against a fake HAX Lite that follows the contract."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import zipfile

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "python/app"), str(ROOT / "tests")]
import app as web
import hax_lite_publish as hl
from test_slim_intake import slim_package

LITE = "https://lite.test"
PASS = "hlc_fixture-pass-0123456789"


class FakeHaxLite:
    """Discovery, approval, token exchange (PKCE), sites and imports, as in the contract."""

    def __init__(self, connect=True, max_bytes=10 * 1048576, statuses=("running", "complete"), report=None):
        self.connect, self.max_bytes, self.statuses = connect, max_bytes, list(statuses)
        self.report = report or {"pagesImported": 12, "filesImported": 0, "skipped": [], "warnings": []}
        self.grants, self.requests, self.uploads = {}, [], []

    def approve(self, authorize_url):
        """The person presses Allow: returns the callback URL HAX Lite redirects to."""
        q = {k: v[0] for k, v in parse_qs(urlsplit(authorize_url).query).items()}
        assert q["code_challenge_method"] == "S256"
        code = "code-" + q["state"][-6:]
        self.grants[code] = (q["code_challenge"], q["redirect_uri"])
        return f"{q['redirect_uri']}?code={code}&state={q['state']}"

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if path == "/.well-known/hax-lite-publish":
            return httpx.Response(200, json={
                "service": "hax-lite-publish", "version": 1,
                "doors": {"upload": True, "token": True, "connect": self.connect}, "maxBytes": self.max_bytes,
                "formats": ["application/zip", "application/gzip"],
                "endpoints": {"authorize": f"{LITE}/publish/authorize", "token": f"{LITE}/api/publish/v1/token",
                              "sites": f"{LITE}/api/publish/v1/sites", "imports": f"{LITE}/api/publish/v1/imports/{{id}}"}})
        if path == "/api/publish/v1/token":
            body = json.loads(request.content)
            challenge, redirect_uri = self.grants.pop(body.get("code"), (None, None))
            proof = base64.urlsafe_b64encode(hashlib.sha256(body["code_verifier"].encode()).digest()).rstrip(b"=").decode()
            if challenge != proof or redirect_uri != body.get("redirect_uri"):
                return httpx.Response(400, json={"error": "This approval code is not valid, has expired or was already used"})
            return httpx.Response(200, json={"access_token": PASS, "token_type": "Bearer", "expires_in": 600})
        if request.headers.get("authorization") != "Bearer " + PASS:
            return httpx.Response(401, json={"error": "A publish token is required"})
        if path == "/api/publish/v1/sites":
            slug = request.url.params.get("slug")
            if slug == "taken":
                return httpx.Response(409, json={"error": "The path taken is already in use"})
            self.uploads.append(request)
            return httpx.Response(202, json={"import": {"id": "imp-1", "status": "pending", "door": "connect",
                                                        "report": {"pages": 12, "files": 0, "skipped": [], "warnings": []}},
                                             "site": {"id": "s1", "slug": slug, "title": request.url.params.get("title") or "From site.json",
                                                      "status": "pending", "url": f"{LITE}/{slug}/"}})
        if path == "/api/publish/v1/imports/imp-1":
            status = self.statuses.pop(0) if self.statuses else "complete"
            return httpx.Response(200, json={"import": {"id": "imp-1", "status": status, "report": self.report},
                                             "site": {"slug": "ist-210", "title": "IST 210", "url": f"{LITE}/ist-210/"}})
        return httpx.Response(404, json={"error": "Not found"})


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old = {k: web.app.config.get(k) for k in ("MENTOR_DATA_ROOT", "HAX_LITE_TRANSPORT", "HAX_LITE_SLEEP")}
        self.addCleanup(lambda: [web.app.config.pop(k, None) if v is None else web.app.config.__setitem__(k, v)
                                 for k, v in self.old.items()])
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.lite = FakeHaxLite()
        web.app.config.update(TESTING=True, MENTOR_DATA_ROOT=Path(self.temp.name),
                              HAX_LITE_TRANSPORT=httpx.MockTransport(lambda r: self.lite(r)),
                              HAX_LITE_SLEEP=lambda seconds: None)
        self.client = web.app.test_client()
        self.csrf = self.client.get("/mentor/api/session").json["csrf"]
        workspace = self.call("POST", "/mentor/api/workspaces", {"title": "Bio"}, expected=201)
        self.base = f"/mentor/api/workspaces/{workspace['id']}"
        self.client.post(self.base + "/intake/export", data={
            "package": (io.BytesIO(slim_package()), "s.zip"), "revision": str(workspace["revision"])},
            content_type="multipart/form-data", headers={"X-Mentor-CSRF": self.csrf})

    def call(self, method, path, data=None, expected=200):
        response = self.client.open(path, method=method, json=data, headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
        return response.json

    def start(self, slug="ist-210", title="IST 210", address=LITE, expected=200):
        return self.call("POST", self.base + "/hax-lite/start", {"address": address, "slug": slug, "title": title},
                         expected=expected)

    def finish(self, callback_url, expected=200):
        q = {k: v[0] for k, v in parse_qs(urlsplit(callback_url).query).items()}
        return self.call("POST", "/mentor/api/hax-lite/finish", {"code": q.get("code"), "state": q.get("state"),
                                                                  "error": q.get("error")}, expected=expected)

    def test_connect_approve_publish_and_wait(self):
        started = self.start()
        url = urlsplit(started["authorize_url"])
        self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}", f"{LITE}/publish/authorize")
        q = parse_qs(url.query)
        self.assertEqual(q["redirect_uri"], ["http://localhost/mentor/hax-lite/callback"])
        self.assertTrue(8 <= len(q["state"][0]) <= 200)
        # The callback page is reachable without the app's cookies (cross-site redirect).
        self.assertEqual(web.app.test_client().get("/mentor/hax-lite/callback?code=x&state=y").status_code, 200)
        result = self.finish(self.lite.approve(started["authorize_url"]))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["site"]["url"], f"{LITE}/ist-210/")
        self.assertEqual(result["report"]["pages"], 12)
        upload = self.lite.uploads[0]
        self.assertEqual(upload.headers["content-type"], "application/zip")
        self.assertTrue(8 <= len(upload.headers["idempotency-key"]) <= 100)
        self.assertEqual((upload.url.params["slug"], upload.url.params["title"]), ("ist-210", "IST 210"))
        self.assertIn("site.json", zipfile.ZipFile(io.BytesIO(upload.content)).namelist(), "the course-site ZIP")
        self.assertTrue(all("origin" not in r.headers for r in self.lite.requests), "server calls send no Origin")
        polls = [r for r in self.lite.requests if "/imports/" in r.url.path]
        self.assertEqual(len(polls), 2)
        # The pass is never returned, stored or reused.
        self.assertNotIn(PASS, json.dumps(result))
        for db in Path(self.temp.name).glob("workspaces.sqlite"):
            with sqlite3.connect(db) as conn:
                for (state,) in conn.execute("SELECT state FROM mentor_workspaces"):
                    self.assertNotIn(PASS, state)
                    self.assertNotIn("hax_lite_pending", state, "the approval is used once")
        self.finish(self.lite.approve(started["authorize_url"]), expected=400)

    def test_partial_report_is_shown(self):
        self.lite.report = {"pagesImported": 10, "filesImported": 0, "error": "Two pages were too large",
                            "skipped": ["pages/big/index.html (over 2 MiB)"], "warnings": ["Files were left out"]}
        self.lite.statuses = ["partial"]
        result = self.finish(self.lite.approve(self.start()["authorize_url"]))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["report"]["error"], "Two pages were too large")
        self.assertEqual(result["report"]["skipped"], ["pages/big/index.html (over 2 MiB)"])
        self.assertEqual(result["report"]["warnings"], ["Files were left out"])

    def test_refusals_and_hax_lite_errors(self):
        started = self.start()
        denied = f"http://localhost/mentor/hax-lite/callback?error=access_denied&state={parse_qs(urlsplit(started['authorize_url']).query)['state'][0]}"
        self.assertIn("chose not to allow", self.finish(denied, expected=400)["error"])
        self.assertIn("does not match", self.finish("http://localhost/x?code=c&state=bogus.statevalue", expected=400)["error"])
        # HAX Lite's own error text reaches the person.
        error = self.finish(self.lite.approve(self.start(slug="taken")["authorize_url"]), expected=502)["error"]
        self.assertEqual(error, "The path taken is already in use")
        # Discovery and input checks stop before anything is sent.
        self.assertIn("lowercase letter", self.start(slug="IST 210", expected=400)["error"])
        self.assertIn("https://", self.start(address="http://lite.test", expected=400)["error"])
        self.lite.connect = False
        self.assertIn("connect and approve", self.start(expected=400)["error"])
        self.lite.connect, self.lite.max_bytes = True, 100
        self.assertIn("larger than", self.start(expected=400)["error"])

    def test_address_and_endpoint_rules(self):
        self.assertEqual(hl.normalize_address("lite.example.edu/"), "https://lite.example.edu")
        self.assertEqual(hl.normalize_address("http://localhost:3000"), "http://localhost:3000")
        for bad in ("http://lite.example.edu", "https://lite.example.edu/path", "ftp://x.edu", "https://u:p@x.edu"):
            with self.subTest(bad=bad), self.assertRaises(hl.PublishError):
                hl.normalize_address(bad)
        with self.assertRaises(hl.PublishError):
            hl.check_public("http://localhost:3000", allow_local=False)

        def elsewhere(request):
            return httpx.Response(200, json={"service": "hax-lite-publish", "doors": {"connect": True}, "maxBytes": 10,
                                             "formats": ["application/zip"], "endpoints": {
                                                 "authorize": f"{LITE}/a", "token": "https://evil.test/t",
                                                 "sites": f"{LITE}/s", "imports": f"{LITE}/i/{{id}}"}})
        with self.assertRaisesRegex(hl.PublishError, "different website"):
            hl.discover(LITE, httpx.MockTransport(elsewhere))
        with self.assertRaisesRegex(hl.PublishError, "not switched on"):
            hl.discover(LITE, httpx.MockTransport(lambda r: httpx.Response(404, json={"error": "nope"})))
        verifier, challenge = hl.pkce_pair()
        self.assertTrue(43 <= len(verifier) <= 128)
        self.assertEqual(challenge, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode())


if __name__ == "__main__":
    unittest.main()
