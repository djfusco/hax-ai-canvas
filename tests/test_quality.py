"""Automatic quality checks and the evaluation harness (AI calls are scripted)."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "python/app"), str(ROOT / "tests"), str(ROOT / "scripts")]
import consultation
import quality_checks as qc
from canvas_api_import import import_api_bundle
from test_quick_version import turn
from test_slim_intake import slim_package

BUNDLE = json.loads((ROOT / "tests/fixtures/extension_bundle.json").read_text())


def statuses(report):
    return {c["check"]: c["status"] for g in report["groups"] for c in g["checks"]}


class QualityCheckTests(unittest.TestCase):
    def setUp(self):
        self.model = import_api_bundle(copy.deepcopy(BUNDLE))
        self.item = self.model["items"][0]["id"]

    def state(self, evidence, proposals=()):
        state = consultation.new_state()
        for topic in consultation.TOPICS:
            state["topics"][topic].update(status="covered", facts=[{"text": "Fact", "evidence": evidence}],
                                          goals=["Assumption: first-year students"])
        state["proposals"] = list(proposals)
        return state

    def test_flags_weak_work(self):
        state = self.state(["instructor"], [{"id": "p-1", "title": "Use AI", "principle": "", "evidence": ["course-map"],
                                             "ai_dimension": "none", "status": "accepted"}] * 2)
        messages = [{"role": "assistant", "content": "Here is advice. Email me at a.student@example.edu", "kind": "consult"}]
        plan = {"vision": "", "outcomes": ["Students will understand cells."], "ai_policy": "No AI."}
        documents = [{"id": "doc-1", "title": "Syllabus", "status": "draft", "html": '<p>Short</p><img src="x.png">',
                      "applied_proposals": ["p-9"]}]
        result = statuses(qc.evaluate(self.model, state, messages, plan, documents))
        self.assertEqual(result["No invented instructor statements"], "fail")
        self.assertEqual(result["Findings cite the course"], "warn")
        for name in ("Proposals made", "Proposals cite the course", "Proposals name a principle",
                     "No duplicate proposals", "Plan has a vision",
                     "Outcomes are measurable", "Student AI policy", "Documents approved",
                     "Documents pass accessibility errors", "Documents apply accepted changes only",
                     "No personal contact details in AI text"):
            self.assertEqual(result[name], "warn", name)
        self.assertEqual(result["Where AI is proposed"], "pass", "proposing no AI is never marked down")
        self.assertEqual(result["Documents have content"], "fail")

    def test_passes_strong_work(self):
        proposals = [{"id": f"p-{n}", "title": f"Change {n}", "principle": "Constructive alignment",
                      "evidence": [self.item], "ai_dimension": dimension, "status": "accepted"}
                     for n, dimension in enumerate(consultation.AI_DIMENSIONS[1:], 1)]
        state = self.state([self.item], proposals)
        messages = [{"role": "assistant", "content": "Assumption: most students are first-years.", "kind": "consult"}]
        plan = {"vision": "A course where students argue from evidence.",
                "outcomes": ["Analyse primary sources.", "Construct an evidence-based argument."],
                "ai_policy": "You may use AI to brainstorm and check grammar. Cite any AI use and keep your drafts; "
                             "AI may not write graded work for you."}
        documents = [{"id": "doc-1", "title": "Syllabus", "status": "approved", "applied_proposals": ["p-1"],
                      "html": "<h2>Syllabus</h2><p>" + "This course builds evidence-based argument. " * 3 + "</p>"}]
        report = qc.evaluate(self.model, state, messages, plan, documents)
        self.assertEqual(report["summary"]["warn"] + report["summary"]["fail"], 0, report)


class HarnessTests(unittest.TestCase):
    def test_end_to_end_with_scripted_ai(self):
        import app as web
        import evaluate_consultant as ev
        from model_capabilities import resolve_capabilities
        from test_course_build import FakeAI
        with tempfile.TemporaryDirectory() as temp:
            export = Path(temp) / "course.zip"
            export.write_bytes(slim_package())
            proposal = {"topic": "assessment", "title": "Outline checkpoint", "change": "Outline in week 1.",
                        "rationale": "Early feedback.", "principle": "Formative assessment", "evidence": ["course-map"],
                        "affected_items": [], "effort": "small", "ai_dimension": "resilient_assessment"}
            turns = iter([turn(f"Assumption: decided {t}.", t, [proposal] if t == "assessment" else [])
                          for t in consultation.TOPICS])
            builder = FakeAI(weeks=[{"title": "Cells", "theme": "t", "objectives": ["Describe a cell."], "items": [],
                                     "hours": 5, "instructor_focus": "f"}])

            def answer(system, prompt, schema, max_tokens=4096):
                keys = set(schema["properties"])
                if "reply" in keys:
                    return next(turns)
                if "vision" in keys:
                    return json.dumps({"vision": "Evidence first.", "ai_policy": "Cite AI use.",
                                       "outcomes": [{"text": "Argue from evidence.", "note": "new"}], "open_questions": []})
                if "big_ideas" in keys:
                    return json.dumps({"big_ideas": ["Claims need evidence"], "knowledge": [], "skills": [],
                                       "understandings": ["Students will understand that claims are only as strong as their evidence"],
                                       "overarching_questions": ["What makes an explanation convincing?"],
                                       "topical_questions": ["Why are cells so small?"], "misunderstandings": [], "evidence": []})
                if "summary_of_changes" in keys:
                    return json.dumps({"title": "Syllabus", "html": "<h1>Syllabus</h1><p>Outline in week 1.</p>",
                                       "summary_of_changes": "Added checkpoint.", "applied_proposals": ["p-1"]})
                return builder.call_structured(system, prompt, schema, max_tokens)

            factory = patch("ai_client.AIClient")
            mock = factory.start()
            self.addCleanup(factory.stop)
            mock.return_value.call_structured.side_effect = answer
            # The harness chooses the model as the screen does, from the provider's list.
            old = web.app.config.get("MODEL_DISCOVERY")
            web.app.config["MODEL_DISCOVERY"] = lambda provider, saved: {"models": [
                {"id": "fixture-text", "name": "Fixture", "compatibility": "text",
                 "capabilities": resolve_capabilities("openai", "fixture-text")}], "manual_allowed": False}
            self.addCleanup(lambda: web.app.config.pop("MODEL_DISCOVERY", None) if old is None
                            else web.app.config.update(MODEL_DISCOVERY=old))
            with patch.dict(os.environ, {}, clear=True):
                report = ev.run(export, "openai", "fixture-text", "sk-eval-fixture-key-123", Path(temp) / "out",
                                max_documents=2, log=lambda *_: None)
                with self.assertRaises(ev.EvaluationError):
                    ev.run(export, "openai", "not-in-the-list", "sk-eval-fixture-key-123", Path(temp) / "out2",
                           log=lambda *_: None)
            self.assertEqual(report["errors"], [])
            self.assertEqual(len(report["messages"]), len(consultation.TOPICS))
            self.assertEqual(report["plan"]["vision"], "Evidence first.")
            self.assertGreaterEqual(len(report["documents"]), 1)
            self.assertIn("Consultation", [g["name"] for g in report["quality"]["groups"]])
            self.assertEqual(len(report["design"]["understandings"]), 1)
            self.assertEqual(report["design_audit"]["rating"], "Activity-oriented")
            self.assertEqual((len(report["structure"]["weeks"]), report["steps_left"]), (1, []))
            self.assertEqual(report["practice"][0]["week"], 1)
            self.assertEqual(report["site"]["instructor_pages"], 0)
            self.assertEqual(report["site"]["unresolved_references"], 0)
            self.assertGreater(report["site"]["elements"]["oer-schema"], 0)
            self.assertTrue((Path(temp) / "out/site/site.json").is_file())
            text = (Path(temp) / "out/report.md").read_text()
            for heading in ("## Reviewer scoring (fill in)", "## Course design", "## Week-by-week structure",
                            "## The HAX course site", "Outline checkpoint"):
                self.assertIn(heading, text)
            self.assertTrue(list((Path(temp) / "out/documents").glob("*.html")))
            for path in (Path(temp) / "out").rglob("*"):
                if path.is_file() and path.suffix in (".md", ".json", ".html", ".txt", ".js"):
                    self.assertNotIn("sk-eval-fixture-key-123", path.read_text())

    def test_key_is_required_from_the_environment(self):
        import evaluate_consultant as ev
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(SystemExit):
            ev.main(["--export", "x.zip", "--provider", "openai", "--model", "m"])


if __name__ == "__main__":
    unittest.main()
