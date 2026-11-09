"""Plan and document API routes (AI calls are mocked)."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "python/app"), str(ROOT / "tests")]
import app as web
from test_slim_intake import slim_package


class PlanApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_root = web.app.config.get("MENTOR_DATA_ROOT")
        web.app.config.update(TESTING=True, MENTOR_DATA_ROOT=Path(self.temp.name))
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.client = web.app.test_client()
        self.csrf = self.client.get("/mentor/api/session").json["csrf"]
        workspace = self.call("POST", "/mentor/api/workspaces", {"title": "Bio"}, expected=201)
        self.base = f"/mentor/api/workspaces/{workspace['id']}"
        response = self.client.post(self.base + "/intake/export", data={
            "package": (io.BytesIO(slim_package()), "slim.zip"), "revision": str(workspace["revision"])},
            content_type="multipart/form-data", headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.call("POST", "/mentor/api/credentials", {"provider": "openai", "api_key": "k", "model": "fixture-text"})
        course_map = self.client.get(self.base + "/course-map").json
        self.essay = next(a["id"] for a in course_map["assessments"] if a["title"] == "Essay")

    def tearDown(self):
        self.environment.stop()
        if self.old_root is None:
            web.app.config.pop("MENTOR_DATA_ROOT", None)
        else:
            web.app.config["MENTOR_DATA_ROOT"] = self.old_root
        self.temp.cleanup()

    def call(self, method, path, data=None, expected=200):
        response = self.client.open(path, method=method, json=data, headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
        return response.json

    def ai(self, *responses):
        factory = patch("ai_client.AIClient")
        mock = factory.start()
        self.addCleanup(factory.stop)
        mock.return_value.call_structured.side_effect = [json.dumps(r) if isinstance(r, dict) else r for r in responses]
        return mock.return_value

    def consult(self):
        proposal = {"topic": "assessment", "title": "Essay outline checkpoint", "change": "Outline in week 1.",
                    "rationale": "Early feedback.", "principle": "Formative assessment", "evidence": [self.essay],
                    "affected_items": [self.essay], "effort": "small", "ai_dimension": "resilient_assessment"}
        self.ai({"reply": "Snapshot.", "topic_updates": [], "proposed_changes": [], "next_topic": ""},
                {"reply": "An idea.", "topic_updates": [], "proposed_changes": [proposal], "next_topic": ""})
        self.call("POST", self.base + "/consult", {"mode": "opening", "provider": "openai"})
        self.call("POST", self.base + "/consult", {"message": "Ideas for the essay?", "provider": "openai"})

    def test_full_plan_and_documents_flow(self):
        self.consult()
        view = self.call("GET", self.base + "/plan")
        self.assertEqual(view["proposals"][0]["status"], "proposed")
        view = self.call("POST", self.base + "/consult/proposals/p-1", {"status": "accepted", "title": "Week 1 outline"})
        self.assertEqual((view["proposals"][0]["status"], view["proposals"][0]["title"]), ("accepted", "Week 1 outline"))

        self.ai({"vision": "Evidence-based first-year course.", "ai_policy": "Cite AI use.",
                 "outcomes": [{"text": "Argue from evidence.", "note": "unchanged"}], "open_questions": ["Exams?"]})
        plan = self.call("POST", self.base + "/plan/draft", {"provider": "openai"})["plan"]
        self.assertEqual(plan["vision"], "Evidence-based first-year course.")
        plan = self.call("PUT", self.base + "/plan", {"vision": "Edited vision."})["plan"]
        self.assertIsNotNone(plan["edited_at"])
        html = self.client.get(self.base + "/plan/export").data.decode()
        self.assertIn("Week 1 outline", html)
        self.assertIn("Edited vision.", html)
        docx = self.client.get(self.base + "/plan/export?format=docx")
        self.assertIn("attachment", docx.headers["Content-Disposition"])
        self.assertTrue(docx.data.startswith(b"PK"))

        targets = self.call("GET", self.base + "/documents")["targets"]
        self.assertIn(("rubric", self.essay), [(t["kind"], t["item_id"]) for t in targets])
        self.ai({"title": "Syllabus", "html": "<h1>Syllabus</h1><p>Outline in week 1.</p>",
                 "summary_of_changes": "Added outline.", "applied_proposals": ["p-1"]},
                {"title": "Essay rubric", "summary_of_changes": "", "applied_proposals": ["p-1"],
                 "criteria": [{"criterion": "Thesis", "description": "",
                               "ratings": [{"label": "Strong", "points": 10, "description": "Clear"},
                                           {"label": "Weak", "points": 2, "description": "Vague"}]}]})
        view = self.call("POST", self.base + "/documents/generate", {"provider": "openai", "kind": "syllabus"})
        view = self.call("POST", self.base + "/documents/generate",
                         {"provider": "openai", "kind": "rubric", "item_id": self.essay})
        self.assertEqual([d["id"] for d in view["documents"]], ["doc-1", "doc-2"])
        self.assertFalse(view["busy"])
        self.call("GET", self.base + "/documents/export", expected=400)  # nothing approved yet
        view = self.call("PUT", self.base + "/documents/doc-1", {"status": "approved"})
        view = self.call("PUT", self.base + "/documents/doc-2", {"html": "<p>Edited rubric</p>"})
        self.assertEqual(view["documents"][1]["status"], "draft")
        word = self.client.get(self.base + "/documents/doc-1/export?format=docx")
        self.assertTrue(word.data.startswith(b"PK"))
        package = zipfile.ZipFile(io.BytesIO(self.client.get(self.base + "/documents/export").data))
        self.assertIn("documents/Syllabus.docx", package.namelist())
        self.assertIn("Improvement plan.docx", package.namelist())
        self.assertNotIn("documents/Essay-rubric.html", package.namelist(), "drafts are not exported")
        hax = zipfile.ZipFile(io.BytesIO(self.client.get(self.base + "/documents/export?format=hax").data))
        self.assertIn("site.json", hax.namelist())
        self.call("DELETE", self.base + "/documents/doc-2")
        self.assertEqual(len(self.call("GET", self.base + "/documents")["documents"]), 1)

    def test_errors(self):
        self.call("POST", self.base + "/consult/proposals/p-1", {"status": "accepted"}, expected=400)
        self.ai({"vision": "x"})
        error = self.call("POST", self.base + "/plan/draft", {"provider": "openai"}, expected=502)["error"]
        self.assertIn("consultation", error)
        self.assertFalse(self.call("GET", self.base + "/plan")["busy"], "lease released after failure")
        self.ai(RuntimeError("sk-secret-upstream"))
        error = self.call("POST", self.base + "/documents/generate",
                          {"provider": "openai", "kind": "syllabus"}, expected=502)["error"]
        self.assertNotIn("sk-secret", error)
        self.call("PUT", self.base + "/documents/doc-9", {"status": "approved"}, expected=400)
        self.assertEqual(self.client.get(self.base + "/documents/doc-9/export").status_code, 404)
        self.call("POST", self.base + "/plan/draft", {"provider": "guided"}, expected=400)


if __name__ == "__main__":
    unittest.main()
