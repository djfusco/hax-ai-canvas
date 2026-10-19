"""Real mentor/assessment/store integration using synthetic AI responses."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "tests")]

from mentor_assessment import AssessmentError, assessment_current
from mentor_service import generate_draft, review_draft, validate_manual_draft
from mentor_store import MentorStore, WorkspaceConflict
from test_mentor_assessment import payload, response_for


class DesignClient:
    provider = "openai"
    model = "synthetic-design-model"

    def __init__(self, identifier="", invalid=False):
        self.identifier = identifier
        self.invalid = invalid
        self.calls = []

    def call(self, system, prompt, max_tokens):
        self.calls.append(prompt)
        if prompt.startswith("Assess this one item"):
            if self.invalid:
                return '{"item_id":"invalid"}'
            return json.dumps(response_for(payload(prompt)))
        if "Create a concrete draft following" in prompt:
            return json.dumps({
                "summary": "Synthetic design for instructor review.",
                "items": [{"id": self.identifier, "type": "assignments", "title": "Evidence task",
                           "html": "<p>Compare two claims using evidence and explain your judgment.</p>",
                           "reason": "Align the task with the approved goals.",
                           "source_ids": ["instructor-brief"]}], "warnings": []})
        if "CURRENT DRAFT" in prompt:
            return json.dumps({"reply": "Review the exact revised design before approval.", "items": []})
        raise AssertionError("Unexpected synthetic request")


class LocalAiDesignTests(unittest.TestCase):
    def state(self):
        return {
            "id": "synthetic-design", "title": "Evidence literacy", "source_type": "blank",
            "brief": "Learners should evaluate evidence, explain reasoning and disclose AI assistance.",
            "materials": [
                {"id": "policy", "name": "Instructor policy", "role": "policy",
                 "text": "Require disclosure of permitted AI assistance."},
                {"id": "example", "name": "Student-facing example", "role": "student",
                 "text": "Compare evidence from two local observations."}],
        }

    def test_new_course_gets_real_draft_assessment_without_fake_baseline(self):
        state = self.state()
        client = DesignClient()
        phases = []
        draft = generate_draft(state, client, assess=True,
                               progress=lambda phase, text: phases.append(phase))
        self.assertTrue(draft["assessment_required"])
        self.assertNotIn("baseline_assessment", draft)
        self.assertTrue(assessment_current(draft["items"], draft["assessment"]))
        self.assertEqual(len(client.calls), 2)
        self.assertIn("assessing_draft", phases)
        self.assertIn("drafting", phases)
        self.assertIn("Instructor policy", client.calls[-1])

    def test_imported_course_assesses_originals_and_proposed_design(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pages").mkdir()
            (root / "pages" / "original.json").write_text(json.dumps({
                "id": "original", "title": "Original evidence lesson",
                "body": "<p>Explain the evidence behind two competing claims.</p>"}))
            state = {**self.state(), "source_type": "imscc", "export_dir": str(root)}
            client = DesignClient(identifier="original")
            draft = generate_draft(state, client, assess=True)
        self.assertEqual(draft["baseline_assessment"]["scope"], "baseline")
        self.assertEqual(draft["assessment"]["scope"], "draft")
        self.assertEqual(draft["baseline_assessment"]["coverage"]["items_evaluated"], 1)
        self.assertTrue(assessment_current(draft["items"], draft["assessment"]))
        self.assertIn("BASELINE ASSESSMENT PRIORITIES", client.calls[1])

    def test_manual_change_requires_fresh_ai_review(self):
        state = self.state()
        client = DesignClient()
        draft = generate_draft(state, client, assess=True)
        item = draft["items"][0]
        edited = validate_manual_draft(draft, {"items": [{
            "id": item["id"], "title": item["title"], "html":
                "<p>Compare three claims using evidence and explain your reasoning.</p>"}]})
        self.assertNotIn("assessment", edited)
        reply, refreshed = review_draft({**state, "draft": edited}, "Assess my change.", client, assess=True)
        self.assertIn("Review", reply)
        self.assertIsNotNone(refreshed)
        self.assertTrue(assessment_current(refreshed["items"], refreshed["assessment"]))
        self.assertNotEqual(draft["assessment"]["items"][0]["content_sha256"],
                            refreshed["assessment"]["items"][0]["content_sha256"])

    def test_bad_provider_assessment_is_not_a_scored_draft(self):
        with self.assertRaises(AssessmentError):
            generate_draft(self.state(), DesignClient(invalid=True), assess=True)

    def test_store_refuses_current_revision_with_stale_assessment(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = MentorStore(Path(temporary) / "workspaces.sqlite")
            state = store.create("owner", "Evidence literacy")
            state = store.save_brief("owner", state["id"], self.state()["brief"], state["revision"])
            state = store.approve_direction("owner", state["id"], state["revision"])
            state = store.begin_analysis("owner", state["id"], state["revision"])
            draft = generate_draft(self.state(), DesignClient(), assess=True)
            state = store.finish_analysis("owner", state["id"], state["active_job"]["id"], draft)
            state = store.begin_review("owner", state["id"], state["draft_revision"])
            state = store.finish_review("owner", state["id"], state["active_job"]["id"], None)
            approved = store.approve_build("owner", state["id"], state["draft_revision"])
            self.assertEqual(approved["build_revision"], approved["draft_revision"])
            stale = deepcopy(draft)
            stale["items"][0]["html"] = "<p>Different unassessed task.</p>"
            state = store.save_draft("owner", state["id"], stale, state["draft_revision"])
            state = store.begin_review("owner", state["id"], state["draft_revision"])
            state = store.finish_review("owner", state["id"], state["active_job"]["id"], None)
            with self.assertRaises(WorkspaceConflict):
                store.approve_build("owner", state["id"], state["draft_revision"])

    def test_guided_mode_never_pretends_to_generate_framework_scores(self):
        draft = generate_draft(self.state(), None, assess=True)
        self.assertEqual(draft["mode"], "guided")
        self.assertNotIn("assessment", draft)
        self.assertNotIn("assessment_required", draft)


if __name__ == "__main__":
    unittest.main()
