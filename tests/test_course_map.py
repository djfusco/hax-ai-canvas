import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "tests")]
import course_map as cmap
from canvas_api_import import import_api_bundle

BUNDLE = json.loads((ROOT / "tests/fixtures/extension_bundle.json").read_text())


def course():
    return import_api_bundle(copy.deepcopy(BUNDLE))


class FakeClient:
    def __init__(self, response, fits=True):
        self.response, self.fits, self.prompts = response, fits, []

    def preflight_structured(self, system, prompt, schema, max_tokens=4096):
        if not self.fits:
            raise ValueError("too large")
        return {}

    def call_structured(self, system, prompt, schema, max_tokens=4096):
        self.prompts.append((prompt, schema))
        return self.response(prompt, schema) if callable(self.response) else self.response


class CourseMapTests(unittest.TestCase):
    def setUp(self):
        self.model = course()
        self.map = cmap.build_course_map(self.model)
        self.by_title = {e["title"]: e for e in self.map["assessments"]}

    def test_weighted_grade_shares(self):
        # Essays 40% over Essay 20 + Project 30 + Debate 5 points; Labs 30%; Quizzes 30% over 10 + 5.
        self.assertTrue(self.map["grading"]["weighted_groups"])
        self.assertAlmostEqual(self.by_title["Project"]["grade_share"], 40 * 30 / 55, places=1)
        self.assertAlmostEqual(self.by_title["Lab"]["grade_share"], 30.0)
        self.assertAlmostEqual(self.by_title["Cell quiz"]["grade_share"], 20.0)
        self.assertAlmostEqual(sum(e["grade_share"] for e in self.map["assessments"]), 100.0, places=1)
        self.assertEqual(self.map["assessments"][0]["title"], "Lab")  # sorted by share
        self.assertEqual(set(self.map["assessment_summary"]["high_stakes"]), {"Lab", "Cell quiz", "Project"})

    def test_unweighted_points_share(self):
        model = course()
        model["course"]["weighted_groups"] = False
        shares = {e["title"]: e["grade_share"] for e in cmap.build_course_map(model)["assessments"]}
        self.assertAlmostEqual(shares["Project"], 100 * 30 / 85, places=1)

    def test_types_rubrics_and_workload(self):
        self.assertEqual(self.by_title["Lab"]["type"], "lab")
        self.assertEqual(self.by_title["Debate cells"]["type"], "discussion")
        self.assertEqual(self.by_title["Organelle check"]["type"], "quiz")
        self.assertTrue(self.by_title["Essay"]["rubric"])
        self.assertEqual(self.by_title["Essay"]["rubric_criteria"], 2)
        self.assertEqual(self.map["assessment_summary"]["with_rubric"], 1)
        weeks = {w["week"]: w for w in self.map["workload"]}
        self.assertEqual(self.by_title["Cell quiz"]["due_week"], 1)
        self.assertEqual(self.by_title["Essay"]["due_week"], 2)
        self.assertEqual(self.by_title["Lab"]["due_week"], 3)
        self.assertEqual(weeks[2]["titles"], ["Essay"])
        self.assertIn("Project", self.map["assessment_summary"]["undated"])

    def test_structure_resources_outcomes(self):
        self.assertEqual([m["title"] for m in self.map["structure"]["modules"]], ["Week 1", "Week 2"])
        self.assertEqual(self.map["resources"]["link_domains"], {"ncbi.nlm.nih.gov": 1})
        self.assertEqual(self.map["resources"]["media"]["video"]["files"], 1)
        self.assertIn("Campbell", self.map["resources"]["syllabus_readings"][0])
        outcomes = {o["title"]: o for o in self.map["outcomes"]}
        self.assertEqual(len(outcomes["Construct a scientific argument"]["aligned_items"]), 1)
        self.assertEqual(self.map["alignment_status"], "rubric_only")
        self.assertIn("Lab", self.map["outcome_coverage"]["graded_items_without_outcome"])

    def test_alignment_with_canvas_outcomes(self):
        lab = next(i["id"] for i in self.model["items"] if i["title"] == "Lab")
        safety = next(o["id"] for o in self.model["outcomes"] if o["title"].startswith("Use lab"))
        client = FakeClient({"alignments": [{"outcome_id": safety, "item_ids": [lab], "rationale": "Lab work."}]})
        alignment = cmap.propose_alignment(self.model, client)
        self.assertEqual(alignment["status"], "proposed")
        mapped = cmap.build_course_map(self.model, alignment)
        outcome = next(o for o in mapped["outcomes"] if o["id"] == safety)
        self.assertEqual(outcome["proposed_items"], [lab])
        self.assertNotIn("Lab", mapped["outcome_coverage"]["graded_items_without_outcome"])
        prompt, schema = client.prompts[0]
        self.assertIn(lab, schema["properties"]["alignments"]["items"]["properties"]["item_ids"]["items"]["enum"])

    def test_alignment_extracts_syllabus_outcomes(self):
        model = course()
        model["outcomes"] = []
        model["course"]["syllabus_html"] = "<h2>Objectives</h2><ul><li>Explain osmosis.</li></ul>"
        essay = next(i["id"] for i in model["items"] if i["title"] == "Essay")
        client = FakeClient({"syllabus_outcomes": [{"id": "s1", "title": "Explain osmosis"}],
                             "alignments": [{"outcome_id": "s1", "item_ids": [essay], "rationale": "Writing."}]})
        alignment = cmap.propose_alignment(model, client)
        self.assertEqual(alignment["syllabus_outcomes"], [{"id": "syllabus-1", "title": "Explain osmosis"}])
        self.assertEqual(alignment["alignments"][0]["outcome_id"], "syllabus-1")
        mapped = cmap.build_course_map(model, alignment)
        self.assertEqual(mapped["outcomes"][0]["source"], "syllabus")
        self.assertIn("syllabus_excerpt", client.prompts[0][0])

    def test_alignment_rejects_bad_references(self):
        bad_item = FakeClient({"alignments": [{"outcome_id": self.model["outcomes"][0]["id"],
                                               "item_ids": ["invented"], "rationale": ""}]})
        with self.assertRaisesRegex(cmap.CourseMapError, "does not exist"):
            cmap.propose_alignment(self.model, bad_item)
        bad_outcome = FakeClient({"alignments": [{"outcome_id": "nope", "item_ids": [], "rationale": ""}]})
        with self.assertRaisesRegex(cmap.CourseMapError, "unknown or repeated outcome"):
            cmap.propose_alignment(self.model, bad_outcome)
        with self.assertRaisesRegex(cmap.CourseMapError, "not valid JSON"):
            cmap.propose_alignment(self.model, FakeClient("{oops"))
        with self.assertRaisesRegex(cmap.CourseMapError, "larger-context"):
            cmap.propose_alignment(self.model, FakeClient({}, fits=False))

    def test_no_outcomes_no_syllabus_skips_ai(self):
        model = course()
        model["outcomes"], model["course"]["syllabus_html"] = [], ""
        client = FakeClient({})
        self.assertEqual(cmap.propose_alignment(model, client)["alignments"], [])
        self.assertEqual(client.prompts, [])

    def test_compact_map_respects_budget(self):
        text = cmap.compact_map(self.map, budget=6000)
        self.assertIn("COURSE: Biology 101: Cells (BIO-101)", text)
        self.assertIn("weighted groups (Essays 40%", text)
        self.assertIn("Lab [lab]: 30% of grade, week 3", text)
        short = cmap.compact_map(self.map, budget=300)
        self.assertLessEqual(len(short), 300 + 120)
        self.assertIn("omitted for length", short)

    def test_large_course_prompt_is_bounded(self):
        model = course()
        for n in range(480):
            model["items"].append({"id": f"p{n}", "kind": "page", "title": f"Page {n}", "html": "<p>" + "x" * 5000 + "</p>",
                                   "module_id": None, "published": True})
        client = FakeClient(lambda prompt, schema: {"alignments": []})
        cmap.propose_alignment(model, client)
        self.assertLess(len(client.prompts[0][0]), cmap.EXCERPT_BUDGET + 60000)


if __name__ == "__main__":
    unittest.main()
