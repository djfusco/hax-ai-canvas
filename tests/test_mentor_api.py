import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "python/app"), str(ROOT / "tests")]
import app as web
from mentor_credentials import CredentialVault
from mentor_service import generate_draft, validate_manual_draft
from test_local_import import cartridge_bytes


class MentorApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_root = web.app.config.get("MENTOR_DATA_ROOT")
        web.app.config.update(TESTING=True, MENTOR_DATA_ROOT=self.root)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.client = web.app.test_client()
        response = self.client.get("/mentor/api/session")
        self.csrf = response.json["csrf"]
        self.assertIn("HttpOnly", response.headers.get("Set-Cookie", ""))
        self.assertIn("SameSite=Strict", response.headers.get("Set-Cookie", ""))

    def tearDown(self):
        self.environment.stop()
        if self.old_root is None:
            web.app.config.pop("MENTOR_DATA_ROOT", None)
        else:
            web.app.config["MENTOR_DATA_ROOT"] = self.old_root
        self.temp.cleanup()

    def request(self, path, method="POST", data=None, expected=200):
        response = self.client.open(path, method=method, json=data,
                                    headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
        return response.json

    def create(self):
        self.workspace = self.request("/mentor/api/workspaces",
                                      data={"title": "Faculty Design Studio"}, expected=201)
        self.base = "/mentor/api/workspaces/" + self.workspace["id"]
        return self.workspace

    def fetch(self):
        self.workspace = self.client.get(self.base).json
        return self.workspace

    def chat(self, kind="interview", provider="guided"):
        self.workspace = self.request(self.base + "/chat", data={
            "message": "Students should investigate local evidence; preserve course policies.",
            "kind": kind, "provider": provider,
        })
        return self.workspace

    def wait_idle(self):
        for _ in range(100):
            workspace = self.fetch()
            if not workspace["active_job"]:
                return workspace
            time.sleep(0.02)
        self.fail("The test job did not finish")

    def draft(self):
        self.chat()
        self.workspace = self.request(self.base + "/approve-direction",
                                      data={"revision": self.workspace["revision"]})
        self.request(self.base + "/analyze", data={
            "revision": self.workspace["revision"], "provider": "guided",
        }, expected=202)
        result = self.wait_idle()
        self.assertEqual(result["stage"], "draft")
        return result

    def test_synchronous_chat_exposes_only_safe_authored_errors(self):
        from ai_client import AIResponseError
        from mentor_assessment import AssessmentError
        from model_capabilities import ModelCapabilityError
        self.request("/mentor/api/credentials",
                     data={"provider": "openai", "api_key": "fixture-key"})
        for error in (ModelCapabilityError("Choose a larger-context model: fixture-key"),
                      AIResponseError("The answer exhausted its output allowance."),
                      AssessmentError("Assessment evidence did not validate."),
                      RuntimeError("private-upstream-fixture-key")):
            with self.subTest(error=type(error).__name__):
                self.create()
                with patch("mentor_service.interview", side_effect=error):
                    result = self.request(self.base + "/chat", data={
                        "message": "Teaching goals", "provider": "guided",
                    }, expected=502)
                state = self.fetch()
                self.assertIsNone(state["active_job"])
                message = result["error"]
                self.assertEqual(state["jobs"][-1]["error"], message)
                self.assertNotIn("fixture-key", message)
                self.assertNotIn("private-upstream", message)
                if not isinstance(error, RuntimeError):
                    self.assertEqual(message, str(error).replace("fixture-key", "[credential removed]"))
                else:
                    self.assertIn("Your feedback was saved", message)

    def test_blank_course_full_guided_workflow(self):
        self.create()
        self.request(self.base + "/approve-direction", data={"revision": 1}, expected=400)
        self.request(self.base + "/build", data={"draft_revision": 0}, expected=409)
        self.draft()
        self.request(self.base + "/approve-build",
                     data={"draft_revision": self.workspace["draft_revision"]}, expected=409)
        self.chat("review")
        self.workspace = self.request(self.base + "/approve-build",
                                      data={"draft_revision": self.workspace["draft_revision"]})
        self.request(self.base + "/build",
                     data={"draft_revision": self.workspace["draft_revision"]}, expected=202)
        self.assertEqual(self.wait_idle()["stage"], "built")
        response = self.client.get(self.base + "/download")
        try:
            self.assertEqual(response.status_code, 200)
            with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
                self.assertIn("site.json", archive.namelist())
                approval = json.loads(archive.read("approval.json"))
                self.assertEqual(approval["draft_revision"], self.workspace["draft_revision"])
                self.assertEqual(approval["mode"], "guided")
        finally:
            response.close()
        preview = self.client.get(self.base + "/preview/index.html")
        self.assertEqual(preview.status_code, 200)
        self.assertIn("sandbox", preview.headers["Content-Security-Policy"])
        preview.close()
        self.assertNotIn("export_dir", self.workspace)
        self.assertNotIn("path", self.workspace["artifact"])

    def test_material_changes_invalidate_approvals_and_outputs(self):
        self.create()
        self.draft()
        self.workspace = self.request(self.base + "/materials", data={
            "revision": self.workspace["revision"], "name": "Institutional policy",
            "role": "policy", "text": "Instructor-only policy reference.",
        })
        self.assertIsNone(self.workspace["draft"])
        self.assertIsNone(self.workspace["direction_revision"])
        self.assertEqual(self.workspace["materials"][0]["role"], "policy")
        self.request(self.base + "/analyze", data={
            "revision": self.workspace["revision"], "provider": "guided",
        }, expected=409)

    def test_owner_and_csrf_are_enforced(self):
        self.create()
        second = web.app.test_client()
        session = second.get("/mentor/api/session").json
        self.assertEqual(second.get(self.base).status_code, 404)
        response = second.put(self.base + "/brief", json={"brief": "Other browser", "revision": 1},
                              headers={"X-Mentor-CSRF": session["csrf"]})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.post("/mentor/api/workspaces",
                                         json={"title": "No CSRF"}).status_code, 403)
        self.assertEqual(self.client.post("/mentor/api/workspaces", json={"title": "Bad origin"},
                                         headers={"X-Mentor-CSRF": self.csrf,
                                                  "Origin": "https://untrusted.example"}).status_code, 403)

    def test_import_into_private_workspace(self):
        self.create()
        response = self.client.post(self.base + "/source", data={
            "file": (io.BytesIO(cartridge_bytes()), "course.imscc"), "revision": "1",
        }, headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(response.status_code, 200, response.json)
        self.workspace = response.json
        info = self.client.get(self.base + "/inventory").json
        self.assertEqual(info["course_items"], 1)
        self.assertEqual(self.workspace["source_type"], "imscc")
        self.draft()
        self.assertIn("Evidence diagram", self.workspace["draft"]["items"][0]["html"])

    def test_canvas_acquisition_does_not_start_analysis(self):
        self.create()

        def acquire(course_id, destination):
            directory = destination / f"course_{course_id}_Test"
            (directory / "pages").mkdir(parents=True)
            (directory / "pages" / "overview.json").write_text(json.dumps({
                "url": "overview", "title": "Overview",
                "body": "<p>Original Canvas content for instructor review.</p>",
            }))
            (directory / "export_summary.json").write_text(json.dumps({"course_name": "Test"}))
            return directory

        with patch("mentor_routes.acquire_canvas_course", side_effect=acquire) as acquire_mock:
            self.workspace = self.request(self.base + "/canvas", data={
                "course_id": "123", "revision": self.workspace["revision"],
            })
        self.assertEqual(acquire_mock.call_args.args[0], "123")
        self.assertEqual(self.workspace["source_type"], "api")
        self.assertIsNone(self.workspace["draft"])
        self.assertEqual(self.workspace["jobs"], [])
        self.assertEqual(self.client.get(self.base + "/inventory").json["course_items"], 1)

    def test_stale_draft_approval_rejected(self):
        self.create()
        self.draft()
        self.chat("review")
        old = self.workspace["draft_revision"]
        item = self.workspace["draft"]["items"][0]
        self.workspace = self.request(self.base + "/draft", method="PUT", data={
            "draft_revision": old, "draft": {"items": [
                {"id": item["id"], "title": "Instructor edit", "html": "<p>Approved goals.</p>"}
            ]},
        })
        self.request(self.base + "/approve-build", data={"draft_revision": old}, expected=409)
        self.request(self.base + "/build",
                     data={"draft_revision": self.workspace["draft_revision"]}, expected=409)

    def test_new_analysis_requires_new_final_conversation(self):
        self.create()
        self.draft()
        self.chat("review")
        self.request(self.base + "/analyze", data={
            "revision": self.workspace["revision"], "provider": "guided",
        }, expected=202)
        self.wait_idle()
        self.request(self.base + "/approve-build",
                     data={"draft_revision": self.workspace["draft_revision"]}, expected=409)

    def test_credentials_are_encrypted_not_returned_and_explicitly_injected(self):
        key = "unit-test-provider-key-not-real"
        result = self.request("/mentor/api/credentials", data={
            "provider": "openai", "api_key": key, "remember": True, "model": "fixture-text",
        })
        self.assertNotIn(key, json.dumps(result))
        self.assertNotIn(key, self.client.get("/mentor/api/session").get_data(as_text=True))
        self.assertNotIn(key.encode(), (self.root / "credentials.sqlite").read_bytes())
        self.create()
        with patch("ai_client.AIClient") as factory:
            factory.return_value.call.return_value = json.dumps({
                "reply": "Which assessment must stay unchanged?"
            })
            self.chat(provider="openai")
            self.assertEqual(factory.call_args.kwargs["api_key"], key)
            self.assertNotIn(key, factory.return_value.call.call_args.args[1])
        self.request("/mentor/api/credentials/openai", method="DELETE")
        self.assertEqual(self.client.get("/mentor/api/session").json["credentials"], [])

    def test_provider_failure_never_grants_approval(self):
        self.request("/mentor/api/credentials", data={
            "provider": "openai", "api_key": "unit-test-key", "model": "fixture-text",
        })
        self.create()
        with patch("ai_client.AIClient") as factory:
            factory.return_value.call.side_effect = RuntimeError("sensitive upstream request detail")
            result = self.request(self.base + "/chat", data={
                "message": "Discuss course constraints.", "provider": "openai",
            }, expected=502)
        self.assertNotIn("sensitive", json.dumps(result))
        self.assertIsNone(self.fetch()["direction_revision"])

    def test_ai_interview_draft_and_revision_keep_original_resources(self):
        self.create()
        response = self.client.post(self.base + "/source", data={
            "file": (io.BytesIO(cartridge_bytes()), "course.imscc"), "revision": "1",
        }, headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(response.status_code, 200)
        self.workspace = response.json
        original = next((self.root / "courses" / self.workspace["id"]).rglob("pages/*.json"))
        source_id = json.loads(original.read_text())["id"]
        self.request("/mentor/api/credentials", data={"provider": "openai", "api_key": "test-only-ai-key", "model": "fixture-text"})
        initial = {"id": source_id, "type": "pages", "title": "Stronger overview",
                   "html": "<p>Investigate the evidence and document your reasoning.</p>",
                   "reason": "Targets authentic reasoning.", "source_ids": ["course:" + source_id]}
        revised = {**initial, "title": "Instructor-reviewed overview",
                   "html": "<p>Compare two evidence sources and explain uncertainty.</p>"}
        with patch("ai_client.AIClient") as factory:
            factory.return_value.call.side_effect = [
                json.dumps({"reply": "Which objectives must remain unchanged?"}),
                json.dumps({"summary": "Targeted proposal", "items": [initial], "warnings": []}),
                json.dumps({"reply": "Here is the requested revision.", "items": [revised]}),
            ]
            self.chat(provider="openai")
            self.workspace = self.request(self.base + "/approve-direction",
                                          data={"revision": self.workspace["revision"]})
            self.request(self.base + "/analyze", data={
                "revision": self.workspace["revision"], "provider": "openai",
            }, expected=202)
            self.wait_idle()
            self.assertIn("Evidence diagram", self.workspace["draft"]["items"][0]["html"])
            self.assertIn("original_html", self.workspace["draft"]["items"][0])
            self.chat(kind="review", provider="openai")
        self.assertIn("Evidence diagram", self.workspace["draft"]["items"][0]["html"])
        self.assertEqual(self.workspace["reviewed_draft_revision"], self.workspace["draft_revision"])
        self.assertNotIn("raw", self.workspace["draft"]["items"][0])

    def test_hosted_legacy_and_mentor_routes_fail_closed(self):
        with patch.dict(os.environ, {"VERCEL": "1"}):
            for path in ("/mentor", "/setup", "/run", "/mentor/api/workspaces"):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json["code"], "HOSTED_SETUP_REQUIRED")

    def test_same_cookie_can_resume_from_another_app_client(self):
        self.create()
        cookie = self.client.get_cookie("mentor_browser")
        resumed = web.app.test_client()
        resumed.set_cookie("mentor_browser", cookie.value)
        self.assertEqual(resumed.get(self.base).json["id"], self.workspace["id"])

    def test_failed_review_does_not_qualify_for_build_approval(self):
        self.create()
        self.draft()
        self.request("/mentor/api/credentials", data={
            "provider": "openai", "api_key": "unit-test-review-key", "model": "fixture-text",
        })
        with patch("ai_client.AIClient") as factory:
            factory.return_value.call.side_effect = RuntimeError("provider failed")
            self.request(self.base + "/chat", data={
                "kind": "review", "provider": "openai", "message": "Please review this draft."
            }, expected=502)
        self.fetch()
        self.assertIsNone(self.workspace["active_job"])
        self.assertIsNone(self.workspace["reviewed_draft_revision"])
        self.request(self.base + "/approve-build",
                     data={"draft_revision": self.workspace["draft_revision"]}, expected=409)

    def test_owner_can_recover_interrupted_job_but_not_a_stale_id(self):
        from mentor_store import MentorStore
        self.create()
        self.chat()
        self.workspace = self.request(self.base + "/approve-direction",
                                      data={"revision": self.workspace["revision"]})
        # Reserve a job without launching a worker, modeling process interruption.
        cookie = self.client.get_cookie("mentor_browser")
        from itsdangerous import URLSafeTimedSerializer
        import hashlib
        token = URLSafeTimedSerializer((self.root / ".browser-key").read_bytes().decode(),
                                       salt="faculty-mentor-browser").loads(cookie.value)
        owner = hashlib.sha256(token.encode()).hexdigest()
        database = MentorStore(self.root / "workspaces.sqlite")
        active = database.begin_analysis(owner, self.workspace["id"], self.workspace["revision"])
        self.request(self.base + "/cancel-job", data={"job_id": "wrong"}, expected=409)
        recovered = self.request(self.base + "/cancel-job",
                                 data={"job_id": active["active_job"]["id"]})
        self.assertIsNone(recovered["active_job"])
        with self.assertRaises(Exception):
            database.finish_analysis(owner, self.workspace["id"], active["active_job"]["id"],
                                     {"summary": "late result"})

    def test_saved_key_is_redacted_from_brief_and_supplemental_notes(self):
        key = "unit-test-redaction-key"
        self.request("/mentor/api/credentials", data={"provider": "openai", "api_key": key})
        self.create()
        self.workspace = self.request(self.base + "/brief", method="PUT", data={
            "brief": "Accidental credential " + key, "revision": self.workspace["revision"],
        })
        self.assertNotIn(key, self.workspace["brief"])
        self.workspace = self.request(self.base + "/materials", data={
            "revision": self.workspace["revision"], "text": "Accidental credential " + key,
        })
        self.assertNotIn(key, self.workspace["materials"][0]["text"])


class CredentialAndDraftTests(unittest.TestCase):
    def test_vault_expiry_and_ownership(self):
        with tempfile.TemporaryDirectory() as temp:
            now = [1000]
            vault = CredentialVault(Path(temp), clock=lambda: now[0])
            vault.save("alice", "openai", {"api_key": "test-only-key"})
            self.assertIsNone(vault.get("bob", "openai"))
            self.assertEqual(CredentialVault(Path(temp), clock=lambda: now[0]).get(
                "alice", "openai")["api_key"], "test-only-key")
            now[0] += 12 * 60 * 60 + 1
            self.assertIsNone(vault.get("alice", "openai"))

    def test_blank_ai_draft_has_no_invented_baseline_scores(self):
        from unittest.mock import Mock
        client = Mock()
        client.call.return_value = json.dumps({
            "summary": "Proposed introductory course",
            "items": [{"id": "", "type": "pages", "title": "Week 1",
                       "html": "<h1>Week 1</h1><p>Investigate local evidence.</p><script>bad()</script>",
                       "reason": "Supports the instructor's goals.", "source_ids": ["instructor-brief"]}],
            "warnings": ["Instructor review required."],
        })
        draft = generate_draft({
            "id": "test", "title": "New course", "source_type": "blank",
            "brief": "Teach evidence-based reasoning.", "materials": [],
        }, client)
        self.assertEqual(len(draft["items"]), 1)
        self.assertNotIn("<script", draft["items"][0]["html"])
        self.assertNotIn("ai_readiness_score", draft)

    def test_unknown_model_item_and_citation_rejected(self):
        from unittest.mock import Mock
        workspace = {"title": "New course", "source_type": "blank", "materials": [], "brief": "Goals"}
        for item in (
            {"id": "nonexistent", "title": "Test", "html": "<p>Text</p>"},
            {"id": "", "title": "Test", "html": "<p>Text</p>", "source_ids": ["invented-source"]},
        ):
            client = Mock()
            client.call.return_value = json.dumps({"items": [item]})
            with self.assertRaises(ValueError):
                generate_draft(workspace, client)

    def test_manual_edit_preserves_other_items_and_sanitizes(self):
        draft = {"items": [{"id": "a", "title": "A", "html": "<p>A</p>"},
                           {"id": "b", "title": "B", "html": "<p>B</p>"}]}
        updated = validate_manual_draft(draft, {"items": [
            {"id": "a", "title": "Changed", "html": "<p>Changed</p><script>bad()</script>"}
        ]})
        self.assertEqual(len(updated["items"]), 2)
        self.assertNotIn("<script", updated["items"][0]["html"])
        self.assertEqual(updated["items"][1], draft["items"][1])


if __name__ == "__main__":
    unittest.main()
