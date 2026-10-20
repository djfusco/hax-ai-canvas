"""Thirty-item transition tests; synthetic data and no provider requests."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "tests")]
import mentor_service as service
from mentor_assessment import assessment_current
from test_mentor_assessment import item, workspace, payload, response_for


class PipelineClient:
    def __init__(self, draft_response=None):
        self.calls = []
        self.plans = []
        self.draft_response = draft_response

    def preflight_structured(self, system, prompt, schema, max_tokens):
        self.plans.append((prompt, schema))
        return {"warnings": []}

    def call(self, *args, **kwargs):
        raise AssertionError("The structured interface is required.")

    def close(self):
        pass

    def call_structured(self, system, prompt, schema, max_tokens):
        self.calls.append((prompt, schema))
        if prompt.startswith("Assess this one item"):
            return json.dumps(response_for(payload(prompt)))
        if prompt.startswith("Synthesize this course assessment"):
            return json.dumps({
                "scaffolding_analysis": "Good progression.",
                "cross_cutting_concerns": [],
                "coverage_gaps": [],
                "course_recommendations": ["Improve rubric clarity"],
                "limitations": [],
            })
        if "priority_order" in str(schema):
            return json.dumps({"priority_order": [], "key_concerns": {}, "course_gaps": []})
        if isinstance(self.draft_response, Exception):
            raise self.draft_response
        return self.draft_response if self.draft_response is not None else json.dumps({
            "summary": "One targeted enhancement; originals retained.",
            "items": [{
                "id": "0", "type": "assignments", "title": "Evidence comparison",
                "html": "<p>Compare documented evidence and justify your reasoning.</p>",
                "reason": "Make the reasoning requirement explicit.",
                "source_ids": ["course:0", "instructor-brief"],
            }], "warnings": [],
        })


class MentorPipelineTests(unittest.TestCase):
    def setUp(self):
        self.originals = [item(str(index)) for index in range(30)]
        self.state = {**workspace(), "id": "synthetic-30", "source_type": "imscc"}
        self.source = patch.object(service, "source_items", return_value=(self.originals, []))
        self.source.start()
        self.addCleanup(self.source.stop)

    def test_thirty_item_assessment_enhancement_and_draft_assessment_complete(self):
        client = PipelineClient()
        progress = []
        draft = service.generate_draft(self.state, client, assess=True,
                                       progress=lambda *value: progress.append(value))
        # 30 baseline + 1 synthesis + 1 triage + 3 batches + 30 draft + 1 draft synthesis
        self.assertEqual(len(client.calls), 66)
        self.assertEqual(len(draft["items"]), 30)
        self.assertEqual(draft["items"][1:], self.originals[1:])
        self.assertEqual(draft["baseline_assessment"]["coverage"]["items_evaluated"], 30)
        self.assertEqual(draft["assessment"]["coverage"]["items_evaluated"], 30)
        self.assertTrue(assessment_current(draft["items"], draft["assessment"]))
        phases = [phase for phase, _ in progress]
        self.assertLess(phases.index("drafting"), phases.index("validating_draft"))
        self.assertLess(phases.index("validating_draft"), phases.index("assessing_draft"))
        self.assertTrue(any("Baseline assessment completed: 30 of 30" in text for _, text in progress))
        # triage call at index 31; first batch draft at index 32
        triage_props = client.calls[31][1]["properties"]
        self.assertIn("priority_order", triage_props)
        batch_schema = client.calls[32][1]["properties"]["items"]["items"]["properties"]
        self.assertNotIn("course:0", batch_schema["id"]["enum"])
        self.assertIn("course:0", batch_schema["source_ids"]["items"]["enum"])
        self.assertNotIn("summary", client.calls[0][1]["properties"])

    def test_failures_after_thirty_baseline_items_are_safe_and_not_mislabeled(self):
        unknown = json.dumps({"items": [{"id": "PRIVATE_UNKNOWN_ID", "title": "X", "html": "<p>X</p>"}]})
        citations = json.dumps({"items": [{"id": "0", "title": "X", "html": "<p>X</p>",
                                         "source_ids": [{"PRIVATE_KEY": "PRIVATE_VALUE"}]}]})
        for response, expected, last_phase in (
                ("PRIVATE_MALFORMED_JSON", "AI draft generation failed", "drafting"),
                (unknown, "unknown course item", "validating_draft"),
                (citations, "source_ids", "validating_draft"),
                (RuntimeError("PRIVATE_UPSTREAM_KEY"), "draft generation request failed", "drafting")):
            client, progress = PipelineClient(response), []
            with self.subTest(expected=expected), self.assertRaises(service.MentorResponseError) as error:
                service.generate_draft(self.state, client, assess=True,
                                       progress=lambda *value: progress.append(value))
            # 30 baseline + 1 synthesis + 1 triage (succeeds) + 1 first-batch call (fails)
            self.assertEqual(len(client.calls), 33)
            self.assertEqual(progress[-1][0], last_phase)
            self.assertIn(expected, str(error.exception))
            self.assertNotIn("PRIVATE_", str(error.exception))
            self.assertTrue(any("Baseline assessment completed: 30 of 30" in text for _, text in progress))


class GenerateDraftPreflightTests(unittest.TestCase):
    def setUp(self):
        self.originals = [item(str(i)) for i in range(3)]
        self.state = {**workspace(), "id": "preflight-test", "source_type": "imscc"}
        self.source = patch.object(service, "source_items", return_value=(self.originals, []))
        self.source.start()
        self.addCleanup(self.source.stop)

    def test_preflight_includes_estimated_assessment_context_when_assess_true(self):
        """When assess=True, first _fit_prompt call must include estimated assessment context."""
        client = PipelineClient()
        service.generate_draft(self.state, client, assess=True)
        first_prompt = client.plans[0][0] if client.plans else ""
        self.assertIn("BASELINE ASSESSMENT PRIORITIES", first_prompt,
                      "Initial preflight must include estimated assessment context so "
                      "model-too-small errors surface before paid baseline calls")

    def test_preflight_without_assess_does_not_include_assessment_context(self):
        """When assess=False, preflight should not include assessment context estimate."""
        client = PipelineClient()
        service.generate_draft(self.state, client, assess=False)
        if client.plans:
            first_prompt = client.plans[0][0]
            self.assertNotIn("BASELINE ASSESSMENT PRIORITIES", first_prompt)


class ReviewDraftTests(unittest.TestCase):
    def setUp(self):
        self.original = {**item("0"), "raw": {}}
        self.draft = {
            "summary": "Test draft", "warnings": [],
            "items": [{"id": "0", "type": "assignments", "title": "Evidence comparison",
                       "html": "<p>Test.</p>", "source_ids": ["course:0"]}],
        }
        self.state = {**workspace(), "id": "test-review", "source_type": "imscc",
                      "draft": self.draft}

    def test_review_draft_uses_structured_interface(self):
        """review_draft must pass schema= to _call_fitted; without it PipelineClient.call() raises."""
        client = PipelineClient()
        with patch.object(service, "source_items", return_value=([self.original], [])):
            reply, revised = service.review_draft(
                self.state, "Please improve this assignment.", client)
        self.assertGreater(len(client.calls), 0, "review_draft must use call_structured, not call()")
        self.assertIsInstance(reply, str)


class TriageBatchingTests(unittest.TestCase):
    def test_small_course_uses_single_call(self):
        """≤ 12 items: no triage call, single draft call."""
        originals = [item(str(i)) for i in range(10)]
        state = {**workspace(), "id": "batch-test-10", "source_type": "imscc"}
        with patch.object(service, "source_items", return_value=(originals, [])):
            client = PipelineClient()
            progress = []
            service.generate_draft(state, client, progress=lambda *v: progress.append(v))
        triage_calls = [p for p, _ in client.calls if "priority_order" in str(_)]
        self.assertEqual(len(triage_calls), 0)

    def test_large_course_triggers_triage(self):
        """15 items: triage call appears."""
        originals = [item(str(i)) for i in range(15)]
        state = {**workspace(), "id": "batch-test-15", "source_type": "imscc"}
        call_log = []
        def tracking_call_structured(system, prompt, schema, max_tokens):
            call_log.append((prompt[:80], schema))
            if "priority_order" in str(schema):
                return json.dumps({
                    "priority_order": [str(i) for i in range(15)],
                    "key_concerns": {},
                    "course_gaps": [],
                })
            if prompt.startswith("Assess this one item"):
                return json.dumps(response_for(payload(prompt)))
            if prompt.startswith("Synthesize this course assessment"):
                return json.dumps({
                    "scaffolding_analysis": "Good.",
                    "cross_cutting_concerns": [], "coverage_gaps": [],
                    "course_recommendations": [], "limitations": [],
                })
            return json.dumps({"summary": "batch draft", "items": [], "warnings": []})
        client = PipelineClient()
        client.call_structured = tracking_call_structured
        with patch.object(service, "source_items", return_value=(originals, [])):
            service.generate_draft(state, client)
        triage_calls = [p for p, s in call_log if "priority_order" in str(s)]
        self.assertGreater(len(triage_calls), 0)

    def test_batched_proposals_merged_across_batches(self):
        """Items proposed in different batches all appear in final result."""
        originals = [item(str(i)) for i in range(15)]
        state = {**workspace(), "id": "batch-test-merge", "source_type": "imscc"}
        batch_num = [0]
        def batch_response(system, prompt, schema, max_tokens):
            if "priority_order" in str(schema):
                return json.dumps({
                    "priority_order": [str(i) for i in range(15)],
                    "key_concerns": {},
                    "course_gaps": [],
                })
            if prompt.startswith("Assess this one item"):
                return json.dumps(response_for(payload(prompt)))
            if prompt.startswith("Synthesize this course assessment"):
                return json.dumps({
                    "scaffolding_analysis": "Good.",
                    "cross_cutting_concerns": [], "coverage_gaps": [],
                    "course_recommendations": [], "limitations": [],
                })
            batch_num[0] += 1
            iid = str(batch_num[0] - 1)
            return json.dumps({
                "summary": f"batch {batch_num[0]}",
                "items": [{"id": iid, "type": "assignments", "title": "Updated",
                           "html": "<p>Updated content for this batch item.</p>",
                           "reason": "improved", "source_ids": ["instructor-brief"]}],
                "warnings": [],
            })
        client = PipelineClient()
        client.call_structured = batch_response
        with patch.object(service, "source_items", return_value=(originals, [])):
            result = service.generate_draft(state, client)
        self.assertEqual(len(result["items"]), 15)


class BriefGapCheckTests(unittest.TestCase):
    def test_interview_prompt_contains_gap_check_instruction(self):
        state = {**workspace(), "id": "gap-check-test"}
        prompts = []
        client = PipelineClient()
        orig = client.call_structured
        def capture(system, prompt, schema, max_tokens):
            prompts.append(prompt)
            return json.dumps({"reply": "What are the learning outcomes?"})
        client.call_structured = capture
        service.interview(state, "Make it more engaging", client)
        self.assertTrue(any("BRIEF COMPLETENESS CHECK" in p for p in prompts))

    def test_interview_prompt_gap_check_covers_four_elements(self):
        state = {**workspace(), "id": "gap-check-test2"}
        prompts = []
        client = PipelineClient()
        def capture(system, prompt, schema, max_tokens):
            prompts.append(prompt)
            return json.dumps({"reply": "What learning outcomes should students achieve?"})
        client.call_structured = capture
        service.interview(state, "Make it more engaging", client)
        gap_prompt = next(p for p in prompts if "BRIEF COMPLETENESS CHECK" in p)
        self.assertIn("learning outcomes", gap_prompt)
        self.assertIn("student context", gap_prompt)
        self.assertIn("constraints", gap_prompt)
        self.assertIn("AI use policy", gap_prompt)


if __name__ == "__main__":
    unittest.main()
