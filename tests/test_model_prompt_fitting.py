"""Offline model-budget fitting tests; no credentials, SDKs or network calls."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "tests"))

import mentor_assessment as assessment
import mentor_service as service
from test_mentor_assessment import MockClient, item, workspace


class BudgetClient(MockClient):
    def __init__(self, limit, transform=None):
        super().__init__(transform)
        self.limit = limit
        self.preflights = []

    def preflight(self, system, prompt, max_tokens):
        size = len((system + prompt).encode("utf-8"))
        self.preflights.append((prompt, size))
        if size > self.limit:
            raise ValueError("Synthetic input budget exceeded")
        return {"max_output_tokens": max_tokens, "warnings": []}

    def call(self, system, prompt, max_tokens):
        self.preflight(system, prompt, max_tokens)
        return super().call(system, prompt, max_tokens)


def assessment_budget(extra=1200):
    title, sources, _ = assessment._context(workspace())
    prepared = assessment._prepare_items([item(html="<p>x</p>")])[0]
    prepared["text"] = ""
    sources["course:one"] = {"source_id": "course:one", "role": "course_item", "text": ""}
    return len((assessment.SYSTEM + assessment._prompt(title, prepared, "draft", sources)).encode()) + extra


class AssessmentFittingTests(unittest.TestCase):
    def test_all_retained_text_chunked_with_no_aggregate_scores(self):
        original = item(html="<p>" + "Compare documented evidence and explain reasoning. " * 180 + "</p>")
        client = BudgetClient(assessment_budget())
        progress = []
        report = assessment.assess_design(
            workspace(), [original], client, progress=lambda *value: progress.append(value))
        chunks = report["items"][0]["chunks"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(report["coverage"]["chunks_total"], len(chunks))
        self.assertEqual(report["coverage"]["chunks_evaluated"], len(client.calls))
        retained = assessment._prepare_items([original])[0]["text"]
        actual = "".join(next(source["text"] for source in call["data"]["evidence_sources"]
                              if source["source_id"] == "course:one") for call in client.calls)
        self.assertEqual(actual, retained)
        self.assertEqual(chunks[0]["start"], 0)
        self.assertEqual(chunks[-1]["end"], len(retained))
        self.assertTrue(all(value["score"] is None
                            for value in report["items"][0]["dimensions"].values()))
        self.assertEqual(report["items"][0]["blooms"]["rationale"], "See chunk-specific judgments")
        self.assertTrue(assessment.assessment_current([original], report))
        self.assertTrue(any("total planned chunk requests" in message for _, message in progress))
        for call, chunk in zip(client.calls, chunks):
            self.assertEqual(call["data"]["excerpt_coverage"]["start"], chunk["start"])
            self.assertLessEqual(len((call["system"] + call["prompt"]).encode()), client.limit)

    def test_legacy_single_request_report_remains_current_but_partial_counters_fail(self):
        report = assessment.assess_design(workspace(), [item()], MockClient())
        report["coverage"].pop("chunks_total")
        report["coverage"].pop("chunks_evaluated")
        self.assertTrue(assessment.assessment_current([item()], report))
        report["coverage"]["chunks_total"] = 1
        self.assertFalse(assessment.assessment_current([item()], report))

    def test_chunk_identity_gaps_hashes_quotes_counters_and_fake_completion_rejected(self):
        original = item(html="<p>" + "A distinct instructional evidence paragraph. " * 180 + "</p>")
        report = assessment.assess_design(workspace(), [original], BudgetClient(assessment_budget()))
        mutations = [
            lambda report: report["coverage"].update(chunks_evaluated=0),
            lambda report: report["coverage"].update(chunks_total=True),
            lambda report: report["items"][0]["chunks"].pop(),
            lambda report: report["items"][0]["chunks"][1].update(start=0),
            lambda report: report["items"][0]["chunks"][0].update(chunk_id="fake"),
            lambda report: report["items"][0]["chunks"][0].update(index=True),
            lambda report: report["items"][0]["chunks"][0].update(text_sha256="0" * 64),
            lambda report: report["items"][0]["chunks"][0].update(content_sha256="0" * 64),
            lambda report: report["items"][0]["chunks"][0].update(evidence=[]),
            lambda report: report["items"][0]["dimensions"]["alignment"].update(score=5),
            lambda report: (report["items"][0].pop("chunks"),
                            report["coverage"].update(chunks_total=1, chunks_evaluated=1)),
        ]
        for mutate in mutations:
            candidate = deepcopy(report)
            mutate(candidate)
            self.assertFalse(assessment.assessment_current([original], candidate))

    def test_unseen_other_chunk_evidence_rejected_during_inference(self):
        original = item(html="<p>" + "first segment " * 300 + "UNIQUE_UNSENT_TAIL</p>")
        def unseen(response, data):
            response["evidence"] = [{"source_id": "course:one", "quote": "UNIQUE_UNSENT_TAIL"}]
        client = BudgetClient(assessment_budget(), unseen)
        with self.assertRaises(assessment.AssessmentError):
            assessment.assess_design(workspace(), [original], client)
        self.assertEqual(len(client.calls), 1)

    def test_essential_brief_and_later_item_failure_precede_all_calls(self):
        for state, items in (
            ({**workspace(), "brief": "必須 constraints " * 2000}, [item()]),
            (workspace(), [item(), {**item("two"), "title": "X" * 4096}]),
        ):
            client = BudgetClient(assessment_budget(100))
            with self.assertRaises(assessment.AssessmentError):
                assessment.assess_design(state, items, client)
            self.assertEqual(client.calls, [])

    def test_bounded_chunk_count_fails_before_first_call(self):
        client = BudgetClient(assessment_budget(400))
        with patch.object(assessment, "MAX_ITEM_CHUNKS", 2):
            with self.assertRaisesRegex(assessment.AssessmentError, "chunks"):
                assessment.assess_design(
                    workspace(), [item(html="<p>" + "documented evidence " * 500 + "</p>")], client)
        self.assertEqual(client.calls, [])

    def test_total_chunk_count_is_bounded_before_any_call(self):
        client = BudgetClient(assessment_budget())
        with patch.object(assessment, "MAX_ASSESSMENT_CHUNKS", 1):
            with self.assertRaisesRegex(assessment.AssessmentError, "bounded"):
                assessment.assess_design(workspace(), [item("one"), item("two")], client)
        self.assertEqual(client.calls, [])

    def test_retention_ceiling_and_unicode_coverage_are_truthful(self):
        original = item(html="<p>" + "証拠を比較して説明する " * 1500 + "UNRETAINED_TAIL</p>")
        client = BudgetClient(assessment_budget(2000))
        report = assessment.assess_design(workspace(), [original], client)
        self.assertEqual(report["coverage"]["truncated_items"], ["one"])
        chunks = report["items"][0]["chunks"]
        self.assertEqual(chunks[-1]["end"], assessment.MAX_ITEM_EXCERPT)
        self.assertTrue(assessment.assessment_current([original], report))
        self.assertTrue(any("omitted content" in limitation
                            for limitation in report["items"][0]["limitations"]))
        self.assertTrue(all("UNRETAINED_TAIL" not in call["prompt"] for call in client.calls))

    def test_supplemental_reductions_disclosed_and_roles_preserved(self):
        state = workspace()
        for material in state["materials"]:
            material["text"] = "policy constraints " * 200
        client = BudgetClient(assessment_budget(100))
        report = assessment.assess_design(state, [item()], client)
        self.assertTrue(any("Supplemental excerpts were reduced" in value for value in report["warnings"]))
        sources = {entry["source_id"]: entry for entry in client.calls[0]["data"]["evidence_sources"]}
        self.assertEqual(sources["policy-one"]["role"], "policy")
        self.assertEqual(sources["instructor-brief"]["text"], state["brief"])
        self.assertIn("omitted policy", client.calls[0]["prompt"])

    def test_late_chunk_failure_returns_no_partial_report_or_retry(self):
        class Failed(BudgetClient):
            def call(self, system, prompt, max_tokens):
                if self.calls:
                    raise RuntimeError("synthetic private diagnostic")
                return super().call(system, prompt, max_tokens)
        client = Failed(assessment_budget())
        with self.assertRaises(assessment.AssessmentError) as error:
            assessment.assess_design(
                workspace(), [item(html="<p>" + "documented evidence " * 500 + "</p>")], client)
        self.assertNotIn("private diagnostic", str(error.exception))
        self.assertEqual(len(client.calls), 1)


class MentorClient:
    def __init__(self, limit=1000000):
        self.limit = limit
        self.calls = []
        self.preflights = []

    def preflight(self, system, prompt, max_tokens):
        self.preflights.append(prompt)
        if len((system + prompt).encode()) > self.limit:
            raise ValueError("Synthetic budget")
        return {"max_output_tokens": max_tokens, "warnings": []}

    def call(self, system, prompt, max_tokens):
        self.preflight(system, prompt, max_tokens)
        self.calls.append(prompt)
        return json.dumps({
            "reply": "Review the evidence.", "summary": "Proposed evidence review.", "warnings": [],
            "items": [] if "INSTRUCTOR REVIEW:" in prompt else [{
                "id": "", "type": "pages", "title": "Evidence",
                "html": "<p>Compare evidence.</p>", "reason": "Instructor direction.",
                "source_ids": ["instructor-brief"],
            }],
        })


class ServiceFittingTests(unittest.TestCase):
    def state(self):
        return {**workspace(), "id": "synthetic", "source_type": "blank"}

    def test_interview_fits_with_unchanged_brief_and_visible_disclosure(self):
        state = self.state()
        state["messages"] = [{"content": "conversation " * 1000} for _ in range(12)]
        state["materials"][0]["text"] = "reference " * 1000
        client = MentorClient(5000)
        reply = service.interview(state, "Keep essential instructor intent.", client)
        self.assertEqual(len(client.calls), 1)
        self.assertIn(state["brief"], client.calls[0])
        self.assertIn("Keep essential instructor intent.", client.calls[0])
        self.assertIn("CONVERSATION COVERAGE:", client.calls[0])
        self.assertIn("Context coverage:", reply)
        self.assertIn("SUPPLEMENT policy-one (policy", client.calls[0])

    def test_essential_prompts_refuse_before_any_call_including_baseline(self):
        for operation in (
            lambda state, client: service.interview(state, "message", client),
            lambda state, client: service.generate_draft(state, client, assess=True),
            lambda state, client: service.review_draft(state, "message", client),
        ):
            state = self.state()
            state["brief"] = "essential instructor constraint " * 1000
            state["draft"] = {"items": [item()]}
            client = MentorClient(1000)
            with self.assertRaisesRegex(service.ModelCapabilityError, "essential"):
                operation(state, client)
            self.assertEqual(client.calls, [])

    def test_review_projection_is_valid_json_and_snapshot_unchanged(self):
        state = self.state()
        state["draft"] = {
            "summary": "A long draft", "items": [item(str(index), "<p>" + "content " * 1000 + "</p>")
                                               for index in range(20)],
            "private_field": "PRIVATE_NOT_FOR_PROMPT",
        }
        before = deepcopy(state["draft"])
        client = MentorClient(7000)
        service.review_draft(state, "Discuss without changing approval.", client)
        self.assertEqual(state["draft"], before)
        prompt = client.calls[0]
        projection = json.loads(prompt.split("CURRENT DRAFT (bounded): ", 1)[1]
                                .split("\n\nINSTRUCTOR REVIEW:", 1)[0])
        self.assertEqual(len(projection["items"]), 20)
        self.assertEqual(projection["items"][0]["source_ids"], item()["source_ids"])
        self.assertIn("html_excerpt", projection["items"][0])
        self.assertNotIn("PRIVATE_NOT_FOR_PROMPT", prompt)

    def test_generation_keeps_all_originals_and_discloses_fitting(self):
        state = self.state()
        originals = [item(str(index), "<p>" + "source evidence " * 1000 + "</p>") for index in range(10)]
        client = MentorClient(6000)
        with patch.object(service, "source_items", return_value=(originals, [])):
            result = service.generate_draft(state, client)
        self.assertEqual(len(result["items"]), 11)
        self.assertEqual(result["items"][:10], originals)
        self.assertTrue(any("reduced" in value for value in result["warnings"]))
        self.assertIn(state["brief"], client.calls[0])
        self.assertIn("SOURCE course:9", client.calls[0])

    def test_review_scope_limits_are_enforced_before_calls(self):
        for items in (
            [item(str(index)) for index in range(501)],
            [item(html="<p>" + "x" * (2 * 1024**2) + "</p>")],
        ):
            state = self.state()
            state["draft"] = {"items": items}
            client = MentorClient()
            with self.assertRaises(ValueError):
                service.review_draft(state, "Review", client)
            self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
