"""Consultation and outcome-mapping API routes (AI calls are mocked)."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "python/app"), str(ROOT / "tests")]
import app as web
from test_slim_intake import slim_package


def reply(text="Here's what I see.", **extra):
    value = {"reply": text, "topic_updates": [], "proposed_changes": [], "next_topic": ""}
    value.update(extra)
    return json.dumps(value)


class ConsultApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_root = web.app.config.get("MENTOR_DATA_ROOT")
        web.app.config.update(TESTING=True, MENTOR_DATA_ROOT=self.root)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.client = web.app.test_client()
        self.csrf = self.client.get("/mentor/api/session").json["csrf"]
        self.workspace = self.post("/mentor/api/workspaces", {"title": "Bio"}, expected=201)
        self.base = f"/mentor/api/workspaces/{self.workspace['id']}"

    def tearDown(self):
        self.environment.stop()
        if self.old_root is None:
            web.app.config.pop("MENTOR_DATA_ROOT", None)
        else:
            web.app.config["MENTOR_DATA_ROOT"] = self.old_root
        self.temp.cleanup()

    def post(self, path, data, expected=200):
        response = self.client.post(path, json=data, headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
        return response.json

    def import_course(self):
        response = self.client.post(self.base + "/intake/export", data={
            "package": (io.BytesIO(slim_package()), "slim.zip"), "revision": str(self.workspace["revision"]),
        }, content_type="multipart/form-data", headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.post("/mentor/api/credentials", {"provider": "openai", "api_key": "test-only-ai-key",
                                              "model": "fixture-text"})

    def ai(self, *responses):
        factory = patch("ai_client.AIClient")
        mock = factory.start()
        self.addCleanup(factory.stop)
        mock.return_value.call_structured.side_effect = list(responses)
        return mock.return_value

    def test_requires_course_and_provider(self):
        self.post(self.base + "/consult", {"mode": "opening", "provider": "guided"}, expected=400)
        self.post("/mentor/api/credentials", {"provider": "openai", "api_key": "k", "model": "fixture-text"})
        self.ai(reply())
        error = self.post(self.base + "/consult", {"mode": "opening", "provider": "openai"}, expected=409)["error"]
        self.assertIn("Import a course", error)

    def test_opening_turn_and_resume(self):
        self.import_course()
        ai = self.ai(
            reply("Your course has two modules and weighted grading. Who are your students?",
                  topic_updates=[{"topic": "purpose", "status": "in_progress", "facts": [], "goals": [],
                                  "open_questions": ["Who takes the course?"], "resolved_questions": []}]),
            reply("Thanks, that helps.", topic_updates=[{
                "topic": "purpose", "status": "covered",
                "facts": [{"text": "Mostly first-year non-majors", "evidence": ["instructor"]}],
                "goals": ["Less memorization"], "open_questions": [],
                "resolved_questions": ["Who takes the course?"]}], next_topic="outcomes"),
        )
        view = self.post(self.base + "/consult", {"mode": "opening", "provider": "openai"})
        self.assertEqual([m["role"] for m in view["messages"]], ["assistant"])
        self.assertIn("weighted grading", view["messages"][0]["content"])
        self.assertIn("Lab [lab]: 30% of grade", ai.call_structured.call_args.args[1])
        self.post(self.base + "/consult", {"mode": "opening", "provider": "openai"}, expected=400)

        view = self.post(self.base + "/consult", {"message": "Mostly first-year non-majors.", "provider": "openai"})
        self.assertEqual([m["role"] for m in view["messages"]], ["assistant", "user", "assistant"])
        state = view["state"]
        self.assertEqual(state["topics"]["purpose"]["status"], "covered")
        self.assertEqual(state["current_topic"], "outcomes")
        self.assertFalse(view["busy"])
        resumed = self.client.get(self.base + "/consult").json
        self.assertIn("Covered so far: Purpose and learners", resumed["resume"])
        self.assertEqual(len(resumed["topics"]), 6)
        # Consultation messages stay out of the legacy interview transcript kinds.
        workspace = self.client.get(self.base).json
        self.assertEqual({m["kind"] for m in workspace["messages"] if m["role"] == "user"}, {"consult"})

    def test_failures_release_the_lease_and_hide_provider_text(self):
        self.import_course()
        self.ai(RuntimeError("upstream said sk-live-secret"), reply("Recovered."))
        error = self.post(self.base + "/consult", {"mode": "opening", "provider": "openai"}, expected=502)["error"]
        self.assertNotIn("sk-live-secret", error)
        self.assertFalse(self.client.get(self.base + "/consult").json["busy"])
        view = self.post(self.base + "/consult", {"mode": "opening", "provider": "openai"})
        self.assertEqual(view["messages"][-1]["content"], "Recovered.")

    def test_one_turn_at_a_time(self):
        self.import_course()
        from mentor_store import MentorStore
        import sqlite3
        with sqlite3.connect(self.root / "workspaces.sqlite") as conn:
            owner = conn.execute("SELECT owner FROM mentor_workspaces").fetchone()[0]
        MentorStore(self.root / "workspaces.sqlite").begin_consult(owner, self.workspace["id"])
        self.ai(reply())
        self.post(self.base + "/consult", {"mode": "opening", "provider": "openai"}, expected=409)
        self.post(self.base + "/consult/settings", {"directness": "direct"}, expected=409)

    def test_settings(self):
        self.import_course()
        view = self.post(self.base + "/consult/settings", {"directness": "gentle", "topic": "assessment"})
        self.assertEqual((view["state"]["directness"], view["state"]["current_topic"]), ("gentle", "assessment"))
        self.post(self.base + "/consult/settings", {"topic": "astrology"}, expected=400)

    def test_outcome_alignment_route(self):
        self.import_course()
        course_map = self.client.get(self.base + "/course-map").json
        outcome = next(o for o in course_map["outcomes"] if o["title"].startswith("Use lab"))
        lab = next(a for a in course_map["assessments"] if a["title"] == "Lab")
        self.ai(json.dumps({"alignments": [{"outcome_id": outcome["id"], "item_ids": [lab["id"]],
                                            "rationale": "Lab practice."}]}))
        mapped = self.post(self.base + "/course-map/align", {"provider": "openai"})
        self.assertEqual(mapped["alignment_status"], "proposed")
        self.assertEqual(self.client.get(self.base + "/course-map").json["alignment_status"], "proposed")
        self.ai(json.dumps({"alignments": [{"outcome_id": "nope", "item_ids": [], "rationale": ""}]}))
        self.assertIn("unknown", self.post(self.base + "/course-map/align", {"provider": "openai"}, expected=502)["error"])


if __name__ == "__main__":
    unittest.main()
