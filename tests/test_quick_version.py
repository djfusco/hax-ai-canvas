"""Quick AI version and refining afterwards (AI calls are mocked)."""
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
import consultation
from test_slim_intake import slim_package


def turn(reply, topic, proposals=()):
    return json.dumps({"reply": reply, "topic_updates": [{
        "topic": topic, "status": "covered", "facts": [{"text": f"{topic} fact", "evidence": ["course-map"]}],
        "goals": [f"Assumption: students are first-years ({topic})"], "open_questions": [], "resolved_questions": []}],
        "proposed_changes": list(proposals), "next_topic": ""})


class QuickVersionTests(unittest.TestCase):
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
        self.client.post(self.base + "/intake/export", data={
            "package": (io.BytesIO(slim_package()), "s.zip"), "revision": str(workspace["revision"])},
            content_type="multipart/form-data", headers={"X-Mentor-CSRF": self.csrf})
        self.call("POST", "/mentor/api/credentials", {"provider": "openai", "api_key": "sk-test-only-fixture-key", "model": "fixture-text"})
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
        mock.return_value.call_structured.side_effect = [r if isinstance(r, str) else json.dumps(r) for r in responses]
        return mock.return_value

    def test_quick_version_then_refine(self):
        proposal = {"topic": "assessment", "title": "Outline checkpoint", "change": "Outline in week 1.",
                    "rationale": "Early feedback.", "principle": "Formative assessment", "evidence": [self.essay],
                    "affected_items": [self.essay], "effort": "small", "ai_dimension": "resilient_assessment"}
        ai = self.ai(*[turn(f"Decided {t}.", t, [proposal] if t == "assessment" else []) for t in consultation.TOPICS])
        for _ in consultation.TOPICS:
            view = self.call("POST", self.base + "/consult", {"mode": "auto", "provider": "openai"})
        state = view["state"]
        self.assertTrue(all(state["topics"][t]["status"] == "covered" for t in consultation.TOPICS))
        self.assertIn("QUICK AI VERSION", ai.call_structured.call_args_list[0].args[1])
        self.assertTrue(view["messages"][0]["content"].startswith("**Quick AI version: Purpose and learners.**"))
        self.assertEqual([m["role"] for m in view["messages"]], ["assistant"] * 6)

        plan = self.call("POST", self.base + "/consult/proposals/accept-all")
        self.assertEqual((plan["proposals"][0]["status"], plan["proposals"][0]["decided_by"]), ("accepted", "ai"))

        self.ai({"title": "Syllabus", "html": "<h1>Syllabus</h1><p>Outline in week 1.</p>",
                 "summary_of_changes": "", "applied_proposals": ["p-1"]},
                {"title": "Essay", "html": "<p>Write an outline first.</p>", "summary_of_changes": "",
                 "applied_proposals": ["p-1"]})
        self.call("POST", self.base + "/documents/generate", {"provider": "openai", "kind": "syllabus", "approve": True})
        docs = self.call("POST", self.base + "/documents/generate",
                         {"provider": "openai", "kind": "assignment", "item_id": self.essay, "approve": True})["documents"]
        self.assertEqual([(d["status"], d.get("auto")) for d in docs], [("approved", True), ("approved", True)])
        site = self.client.get(self.base + "/site/index.html").data.decode()
        self.assertIn("Syllabus", site)

        # Refine: the instructor asks for a change to one document.
        ai = self.ai({"title": "Essay", "html": "<p>Write an outline first, then a 1,500-word essay.</p>",
                      "summary_of_changes": "Set a length.", "applied_proposals": ["p-1"]})
        docs = self.call("POST", self.base + f"/documents/{docs[1]['id']}/revise",
                         {"provider": "openai", "instruction": "Give the essay a word limit of 1,500."})["documents"]
        revised = docs[1]
        self.assertEqual((revised["id"], revised["status"]), ("doc-2", "draft"))
        self.assertIn("1,500-word", revised["html"])
        self.assertEqual(revised["history"][0]["request"], "Give the essay a word limit of 1,500.")
        self.assertNotIn("auto", revised)
        self.assertIn("Write an outline first.", ai.call_structured.call_args.args[1], "current text is supplied")

        # Rewrite in place from the latest plan, approved.
        self.ai({"title": "Essay v3", "html": "<p>Rewritten.</p>", "summary_of_changes": "", "applied_proposals": []})
        docs = self.call("POST", self.base + "/documents/generate", {"provider": "openai", "kind": "assignment",
                                                                     "item_id": self.essay, "replace_id": "doc-2"})["documents"]
        self.assertEqual([d["id"] for d in docs], ["doc-1", "doc-2"])
        self.assertEqual(docs[1]["title"], "Essay v3")

        # The downloaded package is the whole course.
        package = zipfile.ZipFile(io.BytesIO(self.client.get(self.base + "/documents/export?format=hax").data))
        manifest = json.loads(package.read("site.json"))
        self.assertGreater(len(manifest["items"]), 8)

        # Instructor decisions are recorded as theirs.
        plan = self.call("POST", self.base + "/consult/proposals/p-1", {"status": "rejected"})
        self.assertEqual(plan["proposals"][0]["decided_by"], "instructor")

    def test_revise_validation(self):
        self.call("POST", self.base + "/documents/doc-1/revise",
                  {"provider": "openai", "instruction": "Shorter please"}, expected=502)


if __name__ == "__main__":
    unittest.main()
