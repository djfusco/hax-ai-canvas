"""Phase 2-3: proposal decisions, the improvement plan, and revised documents."""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "tests")]
import consultation
import improvement_plan as ip
import revised_documents as rd
from canvas_api_import import import_api_bundle
from course_map import build_course_map

BUNDLE = json.loads((ROOT / "tests/fixtures/extension_bundle.json").read_text())


class Client:
    def __init__(self, *responses):
        self.responses, self.prompts = list(responses), []

    def preflight_structured(self, *args, **kwargs):
        return {}

    def call_structured(self, system, prompt, schema, max_tokens=4096):
        self.prompts.append((prompt, schema))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return json.dumps(value) if isinstance(value, dict) else value


def setup():
    model = import_api_bundle(copy.deepcopy(BUNDLE))
    course_map = build_course_map(model)
    essay = next(i["id"] for i in model["items"] if i["title"] == "Essay")
    state = consultation.new_state()
    state["turns"] = 3
    state["topics"]["purpose"]["facts"].append({"text": "First-year non-majors", "evidence": ["instructor"]})
    base = {"topic": "assessment", "principle": "Formative assessment", "evidence": [essay], "effort": "small",
            "status": "proposed", "proposed_at": "2026-11-09T00:00:00+00:00"}
    state["proposals"] = [
        {**base, "id": "p-1", "title": "Add an essay outline checkpoint", "change": "Graded outline in week 1.",
         "rationale": "Early feedback.", "affected_items": [essay], "ai_dimension": "resilient_assessment"},
        {**base, "id": "p-2", "title": "Weekly AI study partner", "change": "Students use an AI tutor for practice.",
         "rationale": "More practice.", "affected_items": [], "ai_dimension": "course_tutor"},
        {**base, "id": "p-3", "title": "Drop the final exam", "change": "Replace with a portfolio.",
         "rationale": "Authentic assessment.", "affected_items": [], "ai_dimension": "none"},
    ]
    return model, course_map, state, essay


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.model, self.map, self.state, self.essay = setup()

    def test_decisions_and_edits_are_instructor_only(self):
        state = ip.decide(self.state, "p-1", "accepted", {"title": "Essay outline checkpoint (week 1)"})
        state = ip.decide(state, "p-3", "deferred")
        self.assertEqual([p["id"] for p in ip.accepted(state)], ["p-1"])
        self.assertTrue(state["proposals"][0]["edited_by_instructor"])
        self.assertEqual(self.state["proposals"][0]["status"], "proposed", "input not mutated")
        state = ip.decide(state, "p-3", "proposed")
        self.assertIsNone(state["proposals"][2]["decided_at"])
        for args, message in ((("nope", "accepted"), "no longer exists"), (("p-1", "maybe"), "Choose"),
                              (("p-1", "accepted", {"title": ""}), "title")):
            with self.assertRaisesRegex(ip.PlanError, message):
                ip.decide(state, *args)

    def test_draft_plan_from_accepted_changes(self):
        state = ip.decide(ip.decide(self.state, "p-1", "accepted"), "p-2", "accepted")
        client = Client({"vision": "A course where first-years build arguments from evidence.",
                         "outcomes": [{"text": "Construct a scientific argument from evidence.", "note": "unchanged"},
                                      {"text": "Use AI tools critically to practise.", "note": "new"}],
                         "ai_policy": "You may use AI for practice; cite it.", "open_questions": ["Keep the final exam?"]})
        plan = ip.draft_plan(ip.new_plan(), self.model, self.map, state, client)
        self.assertEqual(len(plan["outcomes"]), 2)
        self.assertIsNotNone(plan["drafted_at"])
        prompt = client.prompts[0][0]
        self.assertIn("Weekly AI study partner", prompt)
        self.assertNotIn("Drop the final exam", prompt, "only accepted changes feed the plan")
        self.assertIn("First-year non-majors", prompt)

    def test_draft_plan_validation(self):
        with self.assertRaisesRegex(ip.PlanError, "Hold the consultation"):
            ip.draft_plan(ip.new_plan(), self.model, self.map, consultation.new_state(), Client())
        with self.assertRaisesRegex(ip.PlanError, "provider request failed"):
            ip.draft_plan(ip.new_plan(), self.model, self.map, self.state, Client(RuntimeError("sk-secret")))
        with self.assertRaisesRegex(ip.PlanError, "vision"):
            ip.draft_plan(ip.new_plan(), self.model, self.map, self.state,
                          Client({"vision": "", "outcomes": [], "ai_policy": "x", "open_questions": []}))

    def test_edit_and_render(self):
        plan = ip.edit_plan(ip.new_plan(), {"vision": "Line one\nLine <two>", "ai_policy": "Cite AI use.",
                                            "outcomes": [{"text": "Argue from evidence", "note": "new"}, "Explain osmosis"],
                                            "open_questions": ["Keep exams?"]})
        self.assertEqual(plan["outcomes"][1], {"text": "Explain osmosis", "note": ""})
        state = ip.decide(ip.decide(self.state, "p-1", "accepted"), "p-3", "deferred")
        html = ip.render_html("Bio <101>", plan, state)
        self.assertIn("Course Improvement Plan: Bio &lt;101&gt;", html)
        self.assertIn("Line &lt;two&gt;", html)
        self.assertIn("Add an essay outline checkpoint", html)
        self.assertIn("Deferred for later", html)
        self.assertNotIn("Weekly AI study partner", html)
        from docx import Document
        document = Document(io.BytesIO(ip.render_docx("Bio 101", plan, state)))
        text = "\n".join(p.text for p in document.paragraphs)
        self.assertIn("Add an essay outline checkpoint", text)
        self.assertIn("Argue from evidence", text)
        with self.assertRaises(ip.PlanError):
            ip.edit_plan(plan, {"outcomes": [""]})


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.model, self.map, state, self.essay = setup()
        self.state = ip.decide(state, "p-1", "accepted")
        self.plan = ip.edit_plan(ip.new_plan(), {"vision": "Evidence-based course.", "ai_policy": "Cite AI."})

    def test_targets(self):
        targets = rd.targets(self.model, self.state)
        self.assertEqual(targets[0]["kind"], "syllabus")
        essay = [t for t in targets if t["item_id"] == self.essay]
        self.assertEqual({t["kind"] for t in essay}, {"assignment", "rubric"})
        self.assertEqual(essay[0]["proposals"], ["p-1"])
        self.assertTrue(any(t["title"] == "Lab" and not t["proposals"] for t in targets))

    def test_generate_syllabus_and_assignment(self):
        client = Client({"title": "BIO 101 Syllabus", "html": "<h1>BIO 101</h1><p>Outline due week 1.</p><script>x</script>",
                         "summary_of_changes": "Added outline checkpoint.", "applied_proposals": ["p-1"]})
        document = rd.generate("syllabus", self.model, self.map, self.state, self.plan, client)
        self.assertNotIn("<script", document["html"])
        self.assertEqual((document["status"], document["applied_proposals"]), ("draft", ["p-1"]))
        self.assertIn("Campbell", client.prompts[0][0])
        self.assertEqual(client.prompts[0][1]["properties"]["applied_proposals"]["items"]["enum"], ["p-1"])
        client = Client({"title": "Essay", "html": "<h2>Purpose</h2><p>Argue.</p>", "summary_of_changes": "",
                         "applied_proposals": []})
        document = rd.generate("assignment", self.model, self.map, self.state, self.plan, client, item_id=self.essay)
        self.assertIn("Thesis and evidence", client.prompts[0][0], "existing rubric is supplied")
        self.assertEqual(document["item_id"], self.essay)

    def test_generate_rubric(self):
        client = Client({"title": "Essay rubric", "summary_of_changes": "", "applied_proposals": ["p-1"],
                         "criteria": [{"criterion": "Thesis", "description": "Clear claim",
                                       "ratings": [{"label": "Strong", "points": 10, "description": "Precise <claim>"},
                                                   {"label": "Weak", "points": 3, "description": "Vague"}]},
                                      {"criterion": "Evidence", "description": "",
                                       "ratings": [{"label": "Strong", "points": 10, "description": "Cited"},
                                                   {"label": "Weak", "points": 2, "description": "Missing"}]}]})
        document = rd.generate("rubric", self.model, self.map, self.state, self.plan, client, item_id=self.essay)
        self.assertIn("Total: 20 points", document["html"])
        self.assertIn("Precise &lt;claim&gt;", document["html"])
        self.assertEqual(len(document["rubric"]), 2)

    def test_generation_rejects_bad_output(self):
        cases = [({"title": "x", "html": "", "summary_of_changes": "", "applied_proposals": []}, "empty"),
                 ({"title": "x", "html": "<p>ok</p>", "summary_of_changes": "", "applied_proposals": ["p-3"]}, "not accepted"),
                 ("{nope", "not valid JSON")]
        for raw, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(rd.DocumentError, message):
                rd.generate("syllabus", self.model, self.map, self.state, self.plan, Client(raw))
        with self.assertRaisesRegex(rd.DocumentError, "Choose an assignment"):
            rd.generate("rubric", self.model, self.map, self.state, self.plan, Client(), item_id="nope")
        bad_rubric = {"title": "R", "summary_of_changes": "", "applied_proposals": [],
                      "criteria": [{"criterion": "C", "description": "", "ratings": [{"label": "A", "points": -1, "description": ""},
                                                                                     {"label": "B", "points": 1, "description": ""}]}]}
        with self.assertRaisesRegex(rd.DocumentError, "rating is malformed"):
            rd.generate("rubric", self.model, self.map, self.state, self.plan, Client(bad_rubric), item_id=self.essay)

    def test_edit_approve_export(self):
        base = {"kind": "syllabus", "item_id": None, "title": "Syllabus", "summary": "", "applied_proposals": [],
                "status": "draft", "created_at": "", "updated_at": None,
                "html": "<h1>Syllabus</h1><p>Welcome <strong>all</strong>.</p><ul><li>Outline</li></ul>"
                        "<table><tr><th>Week</th><th>Topic</th></tr><tr><td>1</td><td>Cells</td></tr></table>"}
        documents = rd.add([], base)
        documents = rd.add(documents, {**base, "title": "Syllabus"})
        self.assertEqual([d["id"] for d in documents], ["doc-1", "doc-2"])
        documents = rd.edit(documents, "doc-1", {"status": "approved"})
        edited = rd.edit(documents, "doc-1", {"html": "<p>Changed</p><iframe src=x></iframe>"})
        self.assertEqual(edited[0]["status"], "draft", "edits need re-approval")
        self.assertNotIn("iframe", edited[0]["html"])
        with self.assertRaises(rd.DocumentError):
            rd.edit(documents, "doc-1", {"html": "<script>x</script>"})
        from docx import Document
        word = Document(io.BytesIO(rd.to_docx(documents[0])))
        self.assertIn("Welcome all.", [p.text for p in word.paragraphs])
        self.assertEqual(word.tables[0].cell(1, 1).text, "Cells")
        archive = zipfile.ZipFile(io.BytesIO(rd.zip_package("Bio", documents, [("plan.html", "<p>plan</p>")])))
        self.assertEqual(sorted(archive.namelist()), ["README.txt", "documents/Syllabus.docx",
                                                      "documents/Syllabus.html", "plan.html"])
        with tempfile.TemporaryDirectory() as temp:
            result = rd.hax_package("w1", "Bio", documents, Path(temp) / "site")
            names = zipfile.ZipFile(result["path"]).namelist()
            self.assertIn("site.json", names)
            self.assertIn("index.html", names)
            self.assertEqual(result["items"], 1)
        self.assertEqual(rd.remove(documents, "doc-2")[0]["id"], "doc-1")
        with self.assertRaises(rd.DocumentError):
            rd.hax_package("w1", "Bio", rd.edit(documents, "doc-1", {"status": "draft"}), Path("/nonexistent"))


if __name__ == "__main__":
    unittest.main()
