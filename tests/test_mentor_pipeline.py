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
        self.assertEqual(len(client.calls), 61)
        self.assertEqual(len(draft["items"]), 30)
        self.assertEqual(draft["items"][1:], self.originals[1:])
        self.assertEqual(draft["baseline_assessment"]["coverage"]["items_evaluated"], 30)
        self.assertEqual(draft["assessment"]["coverage"]["items_evaluated"], 30)
        self.assertTrue(assessment_current(draft["items"], draft["assessment"]))
        phases = [phase for phase, _ in progress]
        self.assertLess(phases.index("drafting"), phases.index("validating_draft"))
        self.assertLess(phases.index("validating_draft"), phases.index("assessing_draft"))
        self.assertTrue(any("Baseline assessment completed: 30 of 30" in text for _, text in progress))
        schema = client.calls[30][1]["properties"]["items"]["items"]["properties"]
        self.assertEqual(set(schema["id"]["enum"]), {"", *[str(i) for i in range(30)]})
        self.assertNotIn("course:0", schema["id"]["enum"])
        self.assertIn("course:0", schema["source_ids"]["items"]["enum"])
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
            self.assertEqual(len(client.calls), 31)
            self.assertEqual(progress[-1][0], last_phase)
            self.assertIn(expected, str(error.exception))
            self.assertNotIn("PRIVATE_", str(error.exception))
            self.assertTrue(any("Baseline assessment completed: 30 of 30" in text for _, text in progress))


if __name__ == "__main__":
    unittest.main()
