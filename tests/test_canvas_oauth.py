"""Institutional Canvas connection: OAuth flow, server-side collector, import."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "python/app"), str(ROOT / "tests")]
import canvas_collect as cc
import canvas_oauth as co

FIXTURE = json.loads((ROOT / "tests/fixtures/extension_bundle.json").read_text())
CANVAS = "https://canvas.state.edu"
TOKEN = "canvas-access-token-fixture"
INSTITUTIONS = json.dumps([{"id": "state-u", "name": "State University", "base_url": CANVAS,
                            "client_id": "1700001", "client_secret_env": "CANVAS_SECRET_STATE"}])


class FakeCanvas:
    """Canvas API and OAuth endpoints from the fixture course, with extra fields to drop."""

    def __init__(self, **overrides):
        c = "/api/v1/courses/101"
        self.routes = {
            "/api/v1/courses": [{"id": 101, "name": "Biology 101: Cells", "course_code": "BIO-101",
                                 "term": {"name": "Fall 2026"}, "total_students": 30}],
            c: {**FIXTURE["course"], "students": [{"name": "Student One"}]},
            c + "/modules": FIXTURE["modules"],
            c + "/pages": [{k: v for k, v in p.items() if k != "body"} for p in FIXTURE["pages"]],
            c + "/pages/welcome": FIXTURE["pages"][0],
            c + "/assignments": [{**a, "needs_grading_count": 7} for a in FIXTURE["assignments"]],
            c + "/assignment_groups": FIXTURE["assignment_groups"],
            c + "/quizzes": [{k: v for k, v in q.items() if k != "questions"} for q in FIXTURE["quizzes"]],
            c + "/quizzes/31/questions": [{**q, "answers": [{"weight": 100}]} for q in FIXTURE["quizzes"][0]["questions"]],
            "/api/quiz/v1/courses/101/quizzes/205/items": FIXTURE["new_quizzes"][0]["items"],
            c + "/discussion_topics": FIXTURE["discussion_topics"],
            c + "/outcome_group_links": [{"outcome": o} for o in FIXTURE["outcomes"]],
            c + "/files": [{**f, "url": "https://files/secret"} for f in FIXTURE["files"]],
            c + "/late_policy": {"late_policy": FIXTURE["late_policy"]},
        }
        self.routes.update(overrides)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path == "/login/oauth2/token":
            if request.method == "DELETE":
                return httpx.Response(200, json={})
            form = parse_qs(request.content.decode())
            if form.get("code") == ["good-code"] and form.get("client_secret") == ["secret-value"]:
                return httpx.Response(200, json={"access_token": TOKEN, "token_type": "Bearer"})
            return httpx.Response(400, json={"error": "invalid_grant"})
        if request.headers.get("authorization") != "Bearer " + TOKEN:
            return httpx.Response(401, json={"errors": []})
        route = self.routes.get(request.url.path)
        if route is None:
            return httpx.Response(404, json={})
        if callable(route):
            return route(request)
        return httpx.Response(200, text="while(1);" + json.dumps(route))


class CollectorTests(unittest.TestCase):
    def client(self, fake):
        client = cc.CanvasClient(CANVAS, TOKEN, transport=httpx.MockTransport(fake))
        self.addCleanup(client.close)
        return client

    def test_collects_and_imports_like_the_extension(self):
        from canvas_api_import import import_api_bundle
        fake = FakeCanvas()
        bundle = cc.collect(self.client(fake), "101")
        for request in fake.requests:
            self.assertEqual(request.method, "GET")
            self.assertTrue(any(p.match(request.url.path) for p in cc.ALLOWED), request.url.path)
        text = json.dumps(bundle)
        for forbidden in ("Student One", "needs_grading_count", "STUDENT REPLY", "secret", '"answers"'):
            self.assertNotIn(forbidden, text)
        model = import_api_bundle(bundle, source="canvas")
        self.assertEqual(model["provenance"]["source"], "canvas")
        counts = model["provenance"]["counts"]
        self.assertEqual((counts["assignments"], counts["quizzes"], counts["discussions"]), (3, 2, 1))
        self.assertTrue(model["course"]["weighted_groups"])

    def test_refuses_requests_outside_course_content(self):
        for link in ('<https://canvas.state.edu/api/v1/users/self>; rel="next"',
                     '<https://evil.example/api/v1/courses/101/assignments?page=2>; rel="next"'):
            fake = FakeCanvas(**{"/api/v1/courses/101/assignments":
                                 lambda r, link=link: httpx.Response(200, json=FIXTURE["assignments"], headers={"Link": link})})
            with self.assertRaisesRegex(cc.CollectError, "Refusing"):
                cc.collect(self.client(fake), "101")
            self.assertFalse(any("users" in str(r.url) or "evil" in str(r.url) for r in fake.requests))

    def test_partial_failures_and_access_denied(self):
        fake = FakeCanvas(**{"/api/v1/courses/101/quizzes/31/questions": lambda r: httpx.Response(403, json={})})
        bundle = cc.collect(self.client(fake), "101")
        self.assertIn("(403)", bundle["warnings"][0])
        fake = FakeCanvas(**{"/api/v1/courses/101": lambda r: httpx.Response(403, json={})})
        with self.assertRaisesRegex(cc.CollectError, "teach it"):
            cc.collect(self.client(fake), "101")
        with self.assertRaisesRegex(cc.CollectError, "Choose a course"):
            cc.collect(self.client(FakeCanvas()), "../users")
        with self.assertRaisesRegex(cc.CollectError, "HTTPS"):
            cc.CanvasClient("http://canvas.state.edu", TOKEN)

    def test_course_list(self):
        self.assertEqual(cc.list_courses(self.client(FakeCanvas())),
                         [{"id": 101, "name": "Biology 101: Cells", "code": "BIO-101", "term": "Fall 2026"}])


class ConfigTests(unittest.TestCase):
    def test_configuration(self):
        env = {"HAX_CANVAS_INSTITUTIONS": INSTITUTIONS, "CANVAS_SECRET_STATE": "s"}
        self.assertEqual(co.institutions(env)["state-u"]["client_secret"], "s")
        self.assertEqual(co.institutions({}), {})
        bad = [({"HAX_CANVAS_INSTITUTIONS": "{"}, "valid JSON"),
               ({"HAX_CANVAS_INSTITUTIONS": INSTITUTIONS}, "client secret"),
               ({"HAX_CANVAS_INSTITUTIONS": INSTITUTIONS.replace("https://", "http://"), "CANVAS_SECRET_STATE": "s"}, "HTTPS"),
               ({"HAX_CANVAS_INSTITUTIONS": INSTITUTIONS.replace(CANVAS, CANVAS + "/x"), "CANVAS_SECRET_STATE": "s"}, "origin")]
        for environ, message in bad:
            with self.subTest(message=message), self.assertRaisesRegex(co.CanvasConfigError, message):
                co.institutions(environ)

    def test_sealed_token(self):
        sealed = co.seal_token(b"k" * 32, "alice", "state-u", "tok")
        self.assertEqual(co.open_token(b"k" * 32, "alice", sealed), ("state-u", "tok"))
        self.assertIsNone(co.open_token(b"k" * 32, "mallory", sealed))
        self.assertIsNone(co.open_token(b"x" * 32, "alice", sealed))
        self.assertNotIn("tok", sealed)


class OAuthFlowTests(unittest.TestCase):
    def setUp(self):
        import app as web
        self.web = web
        self.temp = tempfile.TemporaryDirectory()
        self.old = {k: web.app.config.get(k) for k in ("MENTOR_DATA_ROOT", "CANVAS_TRANSPORT")}
        self.fake = FakeCanvas()
        web.app.config.update(TESTING=True, MENTOR_DATA_ROOT=Path(self.temp.name),
                              CANVAS_TRANSPORT=httpx.MockTransport(self.fake))
        self.environment = patch.dict(os.environ, {"HAX_CANVAS_INSTITUTIONS": INSTITUTIONS,
                                                   "CANVAS_SECRET_STATE": "secret-value"}, clear=True)
        self.environment.start()
        self.client = web.app.test_client()
        self.csrf = self.client.get("/mentor/api/session").json["csrf"]

    def tearDown(self):
        self.environment.stop()
        for key, value in self.old.items():
            if value is None:
                self.web.app.config.pop(key, None)
            else:
                self.web.app.config[key] = value
        self.temp.cleanup()

    def call(self, method, path, data=None, expected=200, client=None):
        response = (client or self.client).open(path, method=method, json=data, headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
        return response.json

    def connect(self):
        url = self.call("POST", "/mentor/api/canvas/connect", {"institution": "state-u"})["url"]
        query = parse_qs(urlsplit(url).query)
        self.assertTrue(url.startswith(CANVAS + "/login/oauth2/auth?"))
        self.assertEqual(query["client_id"], ["1700001"])
        self.assertTrue(query["redirect_uri"][0].endswith("/mentor/canvas/callback"))
        return query["state"][0]

    def test_connect_choose_and_import(self):
        listed = self.call("GET", "/mentor/api/canvas/institutions")
        self.assertEqual((listed["institutions"], listed["connected"]),
                         ([{"id": "state-u", "name": "State University"}], None))
        self.call("GET", "/mentor/api/canvas/courses", expected=400)  # not connected
        state = self.connect()
        self.call("POST", "/mentor/api/canvas/exchange", {"code": "good-code", "state": state})
        self.assertEqual(self.call("GET", "/mentor/api/canvas/institutions")["connected"], "state-u")
        courses = self.call("GET", "/mentor/api/canvas/courses")["courses"]
        self.assertEqual(courses[0]["name"], "Biology 101: Cells")
        workspace = self.call("POST", "/mentor/api/workspaces", {"title": "Bio"}, expected=201)
        result = self.call("POST", f"/mentor/api/workspaces/{workspace['id']}/intake/canvas",
                           {"course_id": 101, "revision": workspace["revision"]})
        self.assertEqual(result["course"]["source"], "canvas")
        self.assertEqual(result["course"]["counts"]["assignments"], 3)
        self.call("POST", "/mentor/api/canvas/disconnect")
        self.assertTrue(any(r.method == "DELETE" for r in self.fake.requests), "token revoked")
        self.assertIsNone(self.call("GET", "/mentor/api/canvas/institutions")["connected"])

    def test_exchange_is_bound_to_this_browser_and_state(self):
        self.call("POST", "/mentor/api/canvas/exchange", {"code": "good-code", "state": "x"}, expected=400)
        state = self.connect()
        self.call("POST", "/mentor/api/canvas/exchange", {"code": "good-code", "state": state + "x"}, expected=400)
        self.call("POST", "/mentor/api/canvas/exchange", {"code": "bad-code", "state": state}, expected=400)
        other = self.web.app.test_client()
        other_csrf = other.get("/mentor/api/session").json["csrf"]
        response = other.post("/mentor/api/canvas/exchange", json={"code": "good-code", "state": state},
                              headers={"X-Mentor-CSRF": other_csrf})
        self.assertEqual(response.status_code, 400)
        self.call("POST", "/mentor/api/canvas/connect", {"institution": "nope"}, expected=400)

    def test_callback_page_does_not_touch_identity(self):
        response = self.web.app.test_client().get("/mentor/canvas/callback?code=abc&state=xyz")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("mentor_browser", response.headers.get("Set-Cookie", ""))


if __name__ == "__main__":
    unittest.main()
