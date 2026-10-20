"""Framework-grounded assessment tests; synthetic text and mocked clients only."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from mentor_assessment import (
    AIAS_LABELS, AssessmentError, BLOOM_LABELS, DIMENSIONS, MAX_ITEM_EXCERPT,
    MAX_MATERIAL_CONTEXT, assess_design, assessment_current,
)


def item(identifier="one", html=None):
    return {
        "id": identifier, "title": "Evidence comparison " + identifier, "type": "assignments",
        "html": html or "<p>Compare two claims using documented evidence and explain your reasoning.</p>",
        "source_ids": ["instructor-brief"],
    }


def workspace():
    return {
        "title": "Synthetic evidence literacy",
        "brief": "Learners should evaluate evidence and explain disciplinary judgments.",
        "materials": [
            {"id": "policy-one", "role": "policy", "name": "Course AI policy",
             "text": "Disclose any permitted AI assistance and critically verify its output."},
            {"id": "reference-one", "role": "reference", "name": "Disciplinary example",
             "text": "Compare competing explanations with a documented chain of evidence."},
            {"id": "student-one", "role": "student", "name": "Synthetic learner sample",
             "text": "I selected a claim and explained why its evidence was incomplete."},
        ],
    }


def payload(prompt):
    return json.loads(prompt.split("\n", 2)[1])


def response_for(data):
    identifier = data["item"]["id"]
    source = next(source for source in data["evidence_sources"]
                  if source["source_id"] == "course:" + identifier)
    return {
        "item_id": identifier,
        "blooms": {"level": 5, "rationale": "Learners must judge claims against documented evidence."},
        "aias": {"observed_level": None, "recommended_level": 3,
                 "rationale": "AI use is not specified; a collaborative design could require critique and disclosure."},
        "dimensions": {
            key: {"score": index + 2, "rationale": "The task requires documented evidence and an explanation of reasoning."}
            for index, key in enumerate(DIMENSIONS)
        },
        "strengths": ["The task requires an evidence-based explanation."],
        "concerns": ["Success criteria and AI-use conditions need further specification."],
        "recommendations": ["Add criteria for judging evidence and require a short process explanation."],
        "evidence": [{"source_id": source["source_id"], "quote": source["text"][:100]}],
        "limitations": ["Observed AI-use expectations are not stated in the item."],
    }


class MockClient:
    provider = "openai"
    model = "synthetic-assessment-model"

    def __init__(self, transform=None):
        self.calls = []
        self.transform = transform

    def call(self, system, prompt, max_tokens):
        data = payload(prompt)
        self.calls.append({"system": system, "prompt": prompt, "max_tokens": max_tokens, "data": data})
        response = response_for(data)
        if self.transform:
            transformed = self.transform(response, data)
            if transformed is not None:
                response = transformed
        return response if isinstance(response, str) else json.dumps(response)


class AssessmentTests(unittest.TestCase):
    def evaluate(self, transform=None, items=None, **kwargs):
        client = MockClient(transform)
        report = assess_design(workspace(), items if items is not None else [item()], client, **kwargs)
        return report, client

    def test_complete_scope_is_assessed_not_only_targeted_proposals(self):
        items = [item(str(index)) for index in range(25)]
        phases = []
        report, client = self.evaluate(items=items, progress=lambda phase, message: phases.append((phase, message)))
        # 25 per-item calls + 1 synthesis call
        self.assertEqual(len(client.calls), len(items) + 1)
        self.assertEqual(report["coverage"], {
            "items_total": 25, "items_evaluated": 25, "truncated_items": [],
            "chunks_total": 25, "chunks_evaluated": 25,
        })
        self.assertEqual([value["item_id"] for value in report["items"]], [value["id"] for value in items])
        # 25 assessing_draft + 2 synthesizing progress messages
        self.assertEqual(len(phases), 27)
        assessing_phases = [(p, m) for p, m in phases if p == "assessing_draft"]
        self.assertEqual(len(assessing_phases), 25)
        self.assertTrue(all(isinstance(message, str) for _, message in assessing_phases))
        # Only per-item assessment calls have max_tokens=3500; synthesis call may differ
        item_calls = [c for c in client.calls if "evidence_sources" in c.get("data", {})]
        self.assertTrue(all(call["max_tokens"] == 3500 for call in item_calls))

    def test_server_hashes_labels_metadata_and_report_shape(self):
        report, _ = self.evaluate()
        assessed = report["items"][0]
        self.assertEqual(assessed["content_sha256"], hashlib.sha256(item()["html"].encode()).hexdigest())
        self.assertEqual(assessed["title"], item()["title"])
        self.assertEqual(assessed["item_type"], item()["type"])
        self.assertEqual(assessed["source_ids"], item()["source_ids"])
        self.assertEqual(assessed["blooms"]["label"], "Evaluate")
        self.assertEqual(assessed["aias"]["label"], "AI Collaboration")
        self.assertEqual(report["mode"], "ai")
        self.assertEqual(report["scope"], "draft")
        self.assertEqual(report["version"], 1)
        self.assertEqual(report["provider"], "openai")
        self.assertEqual(report["model"], "synthetic-assessment-model")
        self.assertNotIn("composite", report)
        self.assertNotIn("total_score", report)
        self.assertEqual(set(assessed["dimensions"]), set(DIMENSIONS))

    def test_authoritative_references_and_nonhierarchical_guidance(self):
        report, client = self.evaluate()
        frameworks = {reference["id"]: reference for reference in report["frameworks"]}
        self.assertEqual(frameworks["aias"]["version"], "2.1")
        self.assertEqual(frameworks["aias-research"]["url"], "https://doi.org/10.53761/rrm4y757")
        self.assertIn("johnbiggs.com.au", frameworks["constructive-alignment"]["url"])
        self.assertIn("tilthighered.com", frameworks["tilt"]["url"])
        self.assertIn("celt.iastate.edu", frameworks["revised-bloom"]["url"])
        self.assertIn("teaching.uic.edu", frameworks["authentic-assessment"]["url"])
        self.assertTrue(all(set(reference) == {"id", "title", "url", "version"}
                            for reference in report["frameworks"]))
        self.assertEqual(list(BLOOM_LABELS.values()), ["Remember", "Understand", "Apply", "Analyze", "Evaluate", "Create"])
        self.assertEqual(list(AIAS_LABELS.values()), ["No AI", "AI Planning", "AI Collaboration", "Full AI", "AI Exploration"])
        system = client.calls[0]["system"]
        for phrase in ("not a quality rank", "not inherently better", "Purpose", "TILT",
                       "not validated research", "NOT a probability of cheating"):
            self.assertIn(phrase.lower(), system.lower())
        self.assertTrue(any("not a validated research scale" in warning for warning in report["warnings"]))

    def test_brief_and_role_labelled_supplements_are_in_every_prompt(self):
        _, client = self.evaluate(items=[item("one"), item("two")])
        # Filter to only per-item assessment calls (synthesis call has no evidence_sources)
        assessment_calls = [c for c in client.calls if "evidence_sources" in c.get("data", {})]
        for call in assessment_calls:
            sources = {source["source_id"]: source for source in call["data"]["evidence_sources"]}
            self.assertEqual(sources["instructor-brief"]["text"], workspace()["brief"])
            self.assertEqual(sources["policy-one"]["role"], "policy")
            self.assertEqual(sources["reference-one"]["role"], "reference")
            self.assertEqual(sources["student-one"]["role"], "student")
            self.assertIn("not permission to publish", call["system"])

    def test_baseline_scope_is_explicit_and_not_current_for_draft(self):
        report, client = self.evaluate(scope="baseline")
        self.assertEqual(report["scope"], "baseline")
        self.assertEqual(client.calls[0]["data"]["scope"], "baseline")
        self.assertFalse(assessment_current([item()], report))

    def test_blank_baseline_does_not_invent_assessment_calls_or_scores(self):
        client = MockClient()
        report = assess_design(workspace(), [], client, scope="baseline")
        self.assertEqual(client.calls, [])
        self.assertEqual(report["items"], [])
        self.assertEqual(report["coverage"]["items_evaluated"], 0)
        self.assertIn("No baseline items", report["summary"])
        self.assertFalse(assessment_current([], report))

    def test_nullable_insufficient_evidence_is_not_replaced_by_fives(self):
        def insufficient(response, data):
            response["blooms"]["level"] = None
            response["aias"].update(observed_level=None, recommended_level=None)
            for dimension in response["dimensions"].values():
                dimension.update(score=None, rationale="No sufficient instructional evidence was supplied.")
            response["evidence"] = []
            response["limitations"] = ["No visible task or objectives are available to classify."]
        report, _ = self.evaluate(insufficient, items=[item(html="<img src='files/diagram.png'>")])
        assessed = report["items"][0]
        self.assertTrue(all(value["score"] is None for value in assessed["dimensions"].values()))
        self.assertEqual(assessed["blooms"]["label"], "Insufficient evidence")
        self.assertEqual(assessed["aias"]["label"], "Insufficient evidence")
        self.assertTrue(any("no visible instructional text" in value for value in assessed["limitations"]))

    def test_truncated_item_coverage_and_per_item_limitations_are_server_owned(self):
        long = "<p>" + "documented evidence " * 1000 + "UNSEEN_TAIL</p>"
        report, client = self.evaluate(items=[item(html=long)])
        self.assertEqual(report["coverage"]["truncated_items"], ["one"])
        sources = client.calls[0]["data"]["evidence_sources"]
        text = next(source["text"] for source in sources if source["source_id"] == "course:one")
        self.assertEqual(len(text), MAX_ITEM_EXCERPT)
        self.assertNotIn("UNSEEN_TAIL", client.calls[0]["prompt"])
        self.assertTrue(any("omitted content" in value for value in report["items"][0]["limitations"]))

    def test_quotes_outside_actual_item_excerpt_are_rejected(self):
        def unseen(response, data):
            response["evidence"] = [{"source_id": "course:one", "quote": "UNSEEN_TAIL"}]
        with self.assertRaises(AssessmentError):
            self.evaluate(unseen, items=[item(html="<p>" + "x" * 13000 + "UNSEEN_TAIL</p>")])

    def test_supplement_and_brief_truncation_are_explicit_and_balanced(self):
        state = workspace()
        state["brief"] = "b" * 7000
        state["materials"] = [{"id": f"material-{index}", "role": ("reference", "policy", "student")[index % 3],
                               "name": "Synthetic", "text": "x" * 6000}
                              for index in range(20)]
        client = MockClient()
        report = assess_design(state, [item()], client)
        sources = client.calls[0]["data"]["evidence_sources"]
        material_sources = [source for source in sources if source["source_id"].startswith("material-")]
        self.assertEqual(len(material_sources), 20)
        # Chunk selection may add omission disclosures; total stays at or near MAX_MATERIAL_CONTEXT
        self.assertLessEqual(sum(len(source["text"]) for source in material_sources), MAX_MATERIAL_CONTEXT + 1000)
        self.assertTrue(any("Instructor brief truncated" in warning for warning in report["warnings"]))
        self.assertEqual(sum("Supplemental material" in warning for warning in report["warnings"]), 20)

    def test_unseen_supplemental_text_cannot_be_cited(self):
        state = workspace()
        # Place "UNSEEN_POLICY" beyond the per-material budget (12000/3=4000 chars)
        state["materials"][0]["text"] = "x" * 4100 + "UNSEEN_POLICY"
        def unseen(response, data):
            response["evidence"].append({"source_id": "policy-one", "quote": "UNSEEN_POLICY"})
        with self.assertRaises(AssessmentError):
            assess_design(state, [item()], MockClient(unseen))

    def test_evidence_requires_allowlisted_id_and_exact_quote(self):
        changes = [
            lambda response: response["evidence"].append({"source_id": "unknown", "quote": "Compare"}),
            lambda response: response["evidence"].append({"source_id": "policy-one", "quote": "Invented policy"}),
            lambda response: response["evidence"].append({"source_id": "course:two", "quote": "Compare"}),
            lambda response: response.update(evidence=[]),
            lambda response: response.update(evidence=[{"source_id": "instructor-brief", "quote": workspace()["brief"]}]),
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(AssessmentError):
                self.evaluate(lambda response, data: change(response))

    def test_whitespace_normalized_quotes_and_known_supplemental_quotes_are_accepted(self):
        def cited(response, data):
            response["evidence"] = [
                {"source_id": "course:one", "quote": "Compare two\n claims"},
                {"source_id": "policy-one", "quote": "Disclose any permitted AI assistance"},
                {"source_id": "instructor-brief", "quote": "evaluate evidence"},
            ]
        report, _ = self.evaluate(cited)
        self.assertEqual(len(report["items"][0]["evidence"]), 3)

    def test_invalid_numeric_values_are_rejected_not_clamped_or_coerced(self):
        for value in (True, False, 0, 11, -1, 8.5, 5.0, "8", float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value), self.assertRaises(AssessmentError):
                self.evaluate(lambda response, data: response["dimensions"]["alignment"].update(score=value))
        for field, value in (("level", True), ("level", 7), ("level", "3")):
            with self.subTest(field=field, value=value), self.assertRaises(AssessmentError):
                self.evaluate(lambda response, data: response["blooms"].update({field: value}))
        with self.assertRaises(AssessmentError):
            self.evaluate(lambda response, data: response["aias"].update(recommended_level=6))

    def test_incomplete_or_unknown_response_fields_fail_hard(self):
        changes = [
            lambda response: response.pop("blooms"),
            lambda response: response.update(item_id="wrong"),
            lambda response: response.update(summary="Counterfeit report summary"),
            lambda response: response["blooms"].update(label="Create"),
            lambda response: response["dimensions"].pop("alignment"),
            lambda response: response.update(limitations=[]),
            lambda response: response.update(recommendations=[{"html": "<script>bad</script>"}]),
        ]
        for change in changes:
            def transform(response, data):
                change(response)
            with self.subTest(change=change), self.assertRaises(AssessmentError):
                self.evaluate(transform)

    def test_json_failures_duplicate_keys_and_oversize_responses_are_safe(self):
        for value in ("not-json", "[]", '{"item_id":"one","item_id":"two"}',
                      '{"item_id":NaN}', '{"item_id":1e999}', "x" * 96001,
                      "```json\n{}\n```", "[" * 2000 + "]" * 2000,
                      '{"item_id":' + "9" * 5000 + "}"):
            with self.subTest(value=value[:30]), self.assertRaises(AssessmentError) as error:
                self.evaluate(lambda response, data: value)
            self.assertNotIn(value[:30], str(error.exception))

    def test_validation_failures_identify_category_and_position_without_returned_values(self):
        changes = (
            ("not valid JSON", lambda response, data: "PRIVATE_INVALID_JSON"),
            ("duplicate JSON fields", lambda response, data: '{"PRIVATE_FIELD":1,"PRIVATE_FIELD":2}'),
            ("non-finite JSON number", lambda response, data: '{"PRIVATE_FIELD":NaN}'),
            ("missing/unexpected fields", lambda response, data: {**response, "PRIVATE_FIELD": "PRIVATE_VALUE"}),
            ("item_id did not match", lambda response, data: {**response, "item_id": "PRIVATE_ITEM_ID"}),
            ("dimensions.alignment.score", lambda response, data: {
                **response, "dimensions": {**response["dimensions"],
                                          "alignment": {"score": "PRIVATE_SCORE", "rationale": "Private"}}}),
            ("blooms.level", lambda response, data: {
                **response, "blooms": {"level": 99, "rationale": "Private"}}),
            ("aias.recommended_level", lambda response, data: {
                **response, "aias": {**response["aias"], "recommended_level": "PRIVATE_LEVEL"}}),
            ("blooms.rationale", lambda response, data: {
                **response, "blooms": {**response["blooms"], "rationale": ""}}),
            ("recommendations[0]", lambda response, data: {**response, "recommendations": ["PRIVATE_LONG" * 200]}),
            ("omitted limitations", lambda response, data: {**response, "limitations": []}),
            ("unknown or invalid evidence source", lambda response, data: {
                **response, "evidence": [{"source_id": "PRIVATE_SOURCE", "quote": "Compare"}]}),
            ("evidence quote did not exactly match", lambda response, data: {
                **response, "evidence": [{"source_id": "course:one", "quote": "PRIVATE_FABRICATION"}]}),
            ("without a supporting quote", lambda response, data: {**response, "evidence": []}),
        )
        for expected, transform in changes:
            with self.subTest(expected=expected):
                client = MockClient(transform)
                with self.assertRaises(AssessmentError) as failure:
                    assess_design(workspace(), [item()], client, scope="baseline")
                message = str(failure.exception)
                self.assertIn(expected, message)
                self.assertIn("baseline item 1 of 1, chunk 1 of 1", message)
                self.assertIn("No complete assessment or default scores were saved", message)
                self.assertNotIn("PRIVATE_", message)
                self.assertNotIn("supports structured JSON", message)
                self.assertLessEqual(len(message), 500)
                self.assertEqual(len(client.calls), 1)

    def test_native_schema_is_closed_portable_and_independently_generated(self):
        from mentor_assessment import response_schema
        schema = response_schema()
        def check(node):
            self.assertNotIn("minimum", node)
            self.assertNotIn("maxLength", node)
            if node.get("type") == "object":
                self.assertIs(node["additionalProperties"], False)
                self.assertEqual(set(node["required"]), set(node["properties"]))
                for child in node["properties"].values():
                    check(child)
            elif node.get("type") == "array":
                check(node["items"])
            elif "anyOf" in node:
                self.assertEqual(node["anyOf"], [{"type": "integer"}, {"type": "null"}])
        check(schema)
        self.assertEqual(set(schema["properties"]["dimensions"]["properties"]), set(DIMENSIONS))
        schema["properties"].clear()
        self.assertIn("evidence", response_schema()["properties"])

    def test_unverified_schema_fallback_is_disclosed_in_report(self):
        client = MockClient()
        client.capabilities = {"structured_outputs": False}
        report = assess_design(workspace(), [item()], client)
        self.assertTrue(any("prompt-only JSON" in warning for warning in report["warnings"]))
        self.assertTrue(assessment_current([item()], report))

    def test_provider_failures_do_not_echo_details_or_return_partial_report(self):
        class Failed(MockClient):
            def call(self, system, prompt, max_tokens):
                if self.calls:
                    raise RuntimeError("SYNTHETIC_PROVIDER_SECRET /private/synthetic/path")
                return super().call(system, prompt, max_tokens)
        client = Failed()
        with self.assertRaises(AssessmentError) as error:
            assess_design(workspace(), [item("one"), item("two")], client)
        self.assertIn("item 2 of 2", str(error.exception))
        self.assertNotIn("SYNTHETIC_PROVIDER_SECRET", str(error.exception))
        self.assertNotIn("/private", str(error.exception))
        self.assertEqual(len(client.calls), 1)

    def test_only_exact_outer_json_fences_are_accepted(self):
        for opening in ("```json\n", "```\n", "```JSON\r\n"):
            report, _ = self.evaluate(lambda response, data: opening + json.dumps(response) + "\n```")
            self.assertEqual(report["coverage"]["items_evaluated"], 1)
        for opening, closing in (("Explanation\n```json\n", "\n```"),
                                 ("```javascript\n", "\n```"),
                                 ("```json\n", "\n```\nMore commentary"),
                                 ("Before ", " after")):
            with self.subTest(opening=opening), self.assertRaises(AssessmentError):
                self.evaluate(lambda response, data: opening + json.dumps(response) + closing)

    def test_cancellation_progress_exception_is_not_hidden_as_provider_failure(self):
        class Cancelled(Exception):
            pass
        client = MockClient()
        def progress(phase, message):
            raise Cancelled("stale workspace")
        with self.assertRaises(Cancelled):
            assess_design(workspace(), [item()], client, progress=progress)
        self.assertEqual(client.calls, [])

    def test_all_inputs_preflight_before_first_paid_call(self):
        for items in ([item(), item()], [item(), {"id": "bad"}], [item()] * 501):
            client = MockClient()
            with self.subTest(items=len(items)), self.assertRaises(AssessmentError):
                assess_design(workspace(), items, client)
            self.assertEqual(client.calls, [])
        state = workspace()
        state["materials"][0]["id"] = "course:one"
        client = MockClient()
        with self.assertRaises(AssessmentError):
            assess_design(state, [item()], client)
        self.assertEqual(client.calls, [])
        for invalid_client in (None, object()):
            with self.assertRaises(AssessmentError):
                assess_design(workspace(), [item()], invalid_client)
        with self.assertRaises(AssessmentError):
            assess_design(workspace(), [item()], MockClient(), scope="published")

    def test_active_html_and_unrelated_workspace_private_fields_do_not_enter_prompt(self):
        state = workspace()
        state["export_dir"] = "/private/unrelated-course"
        state["messages"] = [{"content": "PRIVATE_CONVERSATION"}]
        state["api_key"] = "SYNTHETIC_NOT_REAL_KEY"
        report = assess_design(state, [item(html="<p>Compare evidence.</p><script>ACTIVE_SECRET</script>")], MockClient())
        self.assertEqual(len(report["items"]), 1)
        client = MockClient()
        assess_design(state, [item(html="<p>Compare evidence.</p><script>ACTIVE_SECRET</script>")], client)
        for private in ("PRIVATE_CONVERSATION", "/private/unrelated-course", "SYNTHETIC_NOT_REAL_KEY", "ACTIVE_SECRET"):
            self.assertNotIn(private, client.calls[0]["prompt"])

    def test_current_report_checks_exact_html_title_ids_and_complete_coverage(self):
        report, _ = self.evaluate(items=[item("one"), item("two")])
        self.assertTrue(assessment_current([item("one"), item("two")], report))
        self.assertTrue(assessment_current([item("two"), item("one")], report))
        for key, value in (("html", "<p>Changed.</p>"), ("title", "Changed"), ("id", "changed"),
                           ("type", "quizzes"), ("source_ids", ["policy-one"])):
            items = [item("one"), item("two")]
            items[0][key] = value
            self.assertFalse(assessment_current(items, report))
        changes = [
            lambda value: value.update(version=True),
            lambda value: value.update(scope="baseline"),
            lambda value: value.update(mode="guided"),
            lambda value: value["coverage"].update(items_evaluated=1),
            lambda value: value["coverage"].update(items_total=True),
            lambda value: value["coverage"].update(truncated_items=["unknown"]),
            lambda value: value["items"].pop(),
            lambda value: value["items"].append(deepcopy(value["items"][0])),
            lambda value: value["items"][0].update(content_sha256="0" * 64),
            lambda value: value["items"][0].update(item_type="quizzes"),
            lambda value: value["items"][0].update(source_ids=["policy-one"]),
            lambda value: value["items"][0]["dimensions"]["alignment"].update(score=True),
            lambda value: value["items"][0]["blooms"].pop("level"),
            lambda value: value["items"][0].pop("limitations"),
            lambda value: value["items"][0].update(evidence=[]),
            lambda value: value["items"][0]["evidence"][0].update(quote="Invented evidence"),
        ]
        for change in changes:
            candidate = deepcopy(report)
            change(candidate)
            self.assertFalse(assessment_current([item("one"), item("two")], candidate))
        for malformed in (None, [], {}, {"version": 1, "mode": "ai", "scope": "draft"}):
            self.assertFalse(assessment_current([item()], malformed))

    def test_hash_captures_markup_edits_even_when_visible_text_is_unchanged(self):
        report, _ = self.evaluate(items=[item(html="<p>Compare evidence.</p>")])
        self.assertFalse(assessment_current([item(html="<div>Compare evidence.</div>")], report))

    def test_returned_report_and_frameworks_do_not_mutate_input_or_future_reports(self):
        state, items = workspace(), [item()]
        before = deepcopy((state, items))
        report = assess_design(state, items, MockClient())
        self.assertEqual((state, items), before)
        report["frameworks"][0]["title"] = "mutated"
        report2 = assess_design(state, items, MockClient())
        self.assertNotEqual(report2["frameworks"][0]["title"], "mutated")


class ParallelAssessmentTests(unittest.TestCase):
    def test_items_are_assessed_concurrently_not_serially(self):
        """assess_design must run item LLM calls in parallel; items 2+ must start before item 1 ends."""
        import threading
        call_events = []
        call_lock = threading.Lock()
        # Barrier ensures all 3 worker threads must be in the LLM call simultaneously
        barrier = threading.Barrier(3, timeout=5)

        class BarrierClient:
            provider = "anthropic"
            model = "test"
            capabilities = {"structured_outputs": True}

            def call_structured(self, system, prompt, schema, max_tokens):
                with call_lock:
                    call_events.append(("start", json.loads(prompt.split("\n", 2)[1])["item"]["id"]))
                barrier.wait()  # all 3 items must arrive here before any continues
                data = payload(prompt)
                result = response_for(data)
                with call_lock:
                    call_events.append(("end", data["item"]["id"]))
                return json.dumps(result)

            def call(self, *args, **kwargs):
                raise AssertionError("Must use call_structured")

        items = [item(str(i)) for i in range(3)]
        report = assess_design(workspace(), items, BarrierClient(), scope="draft")
        self.assertEqual(report["coverage"]["items_evaluated"], 3)
        # If serial, the barrier.wait() would deadlock (only 1 thread at a time)
        # The fact that we got here without deadlock proves concurrency
        starts = [eid for ev, eid in call_events if ev == "start"]
        self.assertEqual(sorted(starts), ["0", "1", "2"])

    def test_parallel_result_order_matches_input_order(self):
        """Items in the result must be in input order regardless of completion order."""
        import threading
        # Item "0" arrives last (slow), but must appear first in result
        delay_event = threading.Event()

        class ReorderingClient:
            provider = "anthropic"
            model = "test"
            capabilities = {"structured_outputs": True}

            def call_structured(self, system, prompt, schema, max_tokens):
                data = payload(prompt)
                item_id = data["item"]["id"]
                if item_id == "0":
                    delay_event.wait(timeout=5)  # item 0 finishes last
                else:
                    delay_event.set()  # other items release item 0
                return json.dumps(response_for(data))

            def call(self, *args, **kwargs):
                raise AssertionError("Must use call_structured")

        items = [item(str(i)) for i in range(3)]
        report = assess_design(workspace(), items, ReorderingClient(), scope="draft")
        result_ids = [r["item_id"] for r in report["items"]]
        self.assertEqual(result_ids, ["0", "1", "2"], "Result order must match input order")


class RelevantChunkSelectionTests(unittest.TestCase):
    def test_relevant_chunks_replace_uniform_truncation(self):
        ws = {
            "title": "Test course",
            "brief": "Students will evaluate evidence.",
            "materials": [{
                "id": "mat-one", "role": "reference", "name": "Big document",
                "text": ("Completely irrelevant cooking content with lots of words. " * 50 + "\n\n"
                         "Rubric criteria: students must evaluate sources thoroughly. " * 10),
            }],
        }
        items_list = [item("one")]
        client = MockClient()
        report = assess_design(ws, items_list, client)
        call = client.calls[0]
        sources = {s["source_id"]: s for s in call["data"]["evidence_sources"]}
        mat_text = sources["mat-one"]["text"]
        self.assertIn("evaluate sources", mat_text)

    def test_supplement_budget_still_bounded(self):
        ws = {
            "title": "Test",
            "brief": "Outcomes.",
            "materials": [
                {"id": f"mat-{i}", "role": "reference", "name": f"Doc {i}",
                 "text": "Content about rubric criteria evaluation assessment " * 200}
                for i in range(5)
            ],
        }
        client = MockClient()
        report = assess_design(ws, [item("x")], client)
        call = client.calls[0]
        total_material_chars = sum(
            len(s["text"]) for s in call["data"]["evidence_sources"]
            if s["source_id"] != "instructor-brief" and not s["source_id"].startswith("course:")
        )
        self.assertLessEqual(total_material_chars, MAX_MATERIAL_CONTEXT + 500)


class SynthesisTests(unittest.TestCase):
    def test_synthesis_added_to_assess_design_result(self):
        items = [item(str(i)) for i in range(3)]
        client = MockClient()
        original_call = client.call
        synthesis_calls = []
        def call_with_synthesis(system, prompt, max_tokens):
            if prompt.startswith("Synthesize this course assessment"):
                synthesis_calls.append(prompt)
                return json.dumps({
                    "scaffolding_analysis": "Good progression.",
                    "cross_cutting_concerns": ["Criteria clarity"],
                    "coverage_gaps": [],
                    "course_recommendations": ["Add rubric to all assignments"],
                    "limitations": ["Only 3 items assessed"],
                })
            return original_call(system, prompt, max_tokens)
        client.call = call_with_synthesis
        report = assess_design(workspace(), items, client)
        self.assertIn("synthesis", report)
        synthesis = report["synthesis"]
        self.assertIn("bloom_distribution", synthesis)
        self.assertIn("dimension_averages", synthesis)
        self.assertIn("course_recommendations", synthesis)
        self.assertEqual(len(synthesis_calls), 1)

    def test_synthesis_failure_does_not_affect_item_results(self):
        items = [item(str(i)) for i in range(2)]
        client = MockClient()
        original_call = client.call
        def call_raising(system, prompt, max_tokens):
            if prompt.startswith("Synthesize this course assessment"):
                raise RuntimeError("synthesis LLM error")
            return original_call(system, prompt, max_tokens)
        client.call = call_raising
        report = assess_design(workspace(), items, client)
        self.assertEqual(len(report["items"]), 2)
        self.assertIn("synthesis", report)
        self.assertIn("synthesis_error", report["synthesis"])
        self.assertIn("bloom_distribution", report["synthesis"])

    def test_compute_statistics_bloom_distribution(self):
        from mentor_assessment import _compute_statistics
        assessed_items = [
            {"blooms": {"level": 3}, "aias": {"observed_level": 1, "recommended_level": 2},
             "dimensions": {k: {"score": 5} for k in DIMENSIONS}},
            {"blooms": {"level": 5}, "aias": {"observed_level": None, "recommended_level": 3},
             "dimensions": {k: {"score": 7} for k in DIMENSIONS}},
        ]
        stats = _compute_statistics(assessed_items)
        self.assertEqual(stats["bloom_distribution"]["3"], 1)
        self.assertEqual(stats["bloom_distribution"]["5"], 1)
        self.assertAlmostEqual(stats["dimension_averages"]["ai_leverage"], 6.0)

    def test_synthesis_skipped_for_single_item(self):
        client = MockClient()
        synthesis_calls = []
        original_call = client.call
        def call_tracking(system, prompt, max_tokens):
            if prompt.startswith("Synthesize this course assessment"):
                synthesis_calls.append(prompt)
            return original_call(system, prompt, max_tokens)
        client.call = call_tracking
        report = assess_design(workspace(), [item()], client)
        self.assertEqual(len(synthesis_calls), 0)
        self.assertNotIn("synthesis", report)


class ChunkUtilityTests(unittest.TestCase):
    def test_chunk_text_splits_on_paragraphs(self):
        from mentor_assessment import _chunk_text
        # Paragraphs >= 100 chars won't trigger merging
        p1 = "This is the first paragraph with sufficient length to avoid being merged with subsequent paragraphs by the algorithm."
        p2 = "This is the second paragraph with sufficient length to avoid being merged with subsequent paragraphs by the algorithm."
        p3 = "This is the third paragraph with sufficient length to avoid being merged with subsequent paragraphs by the algorithm."
        text = p1 + "\n\n" + p2 + "\n\n" + p3
        chunks = _chunk_text(text, max_chunk=1500)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(chunks[0], p1)
        self.assertEqual(chunks[1], p2)
        self.assertEqual(chunks[2], p3)

    def test_chunk_text_splits_long_paragraph_on_sentence(self):
        from mentor_assessment import _chunk_text
        # Sentences of 110 chars each; max_chunk=100 forces splits; each part > 100 so no re-merge
        long_para = ("A" * 109 + ". ") * 3  # 3 sentences of ~111 chars
        chunks = _chunk_text(long_para, max_chunk=100)
        self.assertGreater(len(chunks), 1)

    def test_chunk_text_merges_short_chunks(self):
        from mentor_assessment import _chunk_text
        # Two tiny paragraphs followed by a longer one; the tiny ones get merged with next
        short1 = "Hi."
        short2 = "Ok."
        long_p = "This is a longer paragraph with more substance than the short ones above it."
        text = short1 + "\n\n" + short2 + "\n\n" + long_p
        chunks = _chunk_text(text, max_chunk=1500)
        # short1 + short2 merge with long_p, result is one chunk
        self.assertEqual(len(chunks), 1)
        self.assertIn(short1, chunks[0])
        self.assertIn(long_p, chunks[0])

    def test_chunk_text_empty_returns_one_empty_chunk(self):
        from mentor_assessment import _chunk_text
        self.assertEqual(_chunk_text("", max_chunk=1500), [""])

    def test_select_chunks_returns_within_budget(self):
        from mentor_assessment import _select_chunks
        full_text = "\n\n".join(["Chunk about rubric " + str(i) + " criteria." for i in range(20)])
        result = _select_chunks(full_text, "Rubric assessment", "evaluate rubric criteria", budget=500)
        self.assertIsInstance(result, str)
        self.assertLessEqual(len(result), 700)  # budget + disclosure line

    def test_select_chunks_prefers_relevant_chunks(self):
        from mentor_assessment import _select_chunks
        text = ("Completely irrelevant content about cooking.\n\n"
                "The rubric criteria require students to evaluate evidence.\n\n"
                "More irrelevant content about gardening.")
        result = _select_chunks(text, "Rubric assessment", "evaluate rubric criteria", budget=200)
        self.assertIn("rubric criteria", result)

    def test_select_chunks_adds_omission_disclosure(self):
        from mentor_assessment import _select_chunks
        full_text = "\n\n".join(["Section " + str(i) + " " + "x" * 200 for i in range(10)])
        result = _select_chunks(full_text, "item", "text", budget=300)
        self.assertIn("omitted", result)

    def test_select_chunks_no_omission_when_all_fit(self):
        from mentor_assessment import _select_chunks
        full_text = "Short section."
        result = _select_chunks(full_text, "item", "text", budget=2000)
        self.assertNotIn("omitted", result)

    def test_select_chunks_zero_budget_returns_disclosure(self):
        from mentor_assessment import _select_chunks
        full_text = "Some content."
        result = _select_chunks(full_text, "item", "text", budget=0)
        self.assertIn("omitted", result)


if __name__ == "__main__":
    unittest.main()
