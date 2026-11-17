"""Course design by Understanding by Design, and the check against PRAW's design standards."""
import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "python/app"), str(ROOT / "tests")]
import consultation
import course_build as cb
import course_design as cd
import course_site
from canvas_api_import import import_api_bundle
from course_map import build_course_map
from test_course_build import BUNDLE
from test_hax_native import PracticeAI

U1 = "Students will understand that living things are built from cells whose structure fits their function"
U2 = "Students will understand that a scientific claim is only as strong as the evidence offered for it"


def good_design():
    return {
        "big_ideas": ["Structure fits function", "Claims need evidence"],
        "understandings": [U1, "a scientific claim is only as strong as the evidence offered for it"],
        "overarching_questions": ["How do we know what we cannot see?", "What makes an explanation convincing?"],
        "topical_questions": ["Why are cells so small?"],
        "knowledge": ["Parts of a cell"], "skills": ["Construct an argument from evidence"],
        "misunderstandings": [{"misconception": "Bigger organisms have bigger cells",
                               "pre_assess": "A prediction question before the reading",
                               "confront": "Compare cell sizes across species"}],
        "evidence": [
            {"item_id": "assignment-201", "kind": "performance_task", "targets": [U2], "facets": ["explanation", "application"],
             "problem": True, "note": "Students argue from data."},
            {"item_id": "assignment-202", "kind": "academic_prompt", "targets": [U1.upper()], "facets": ["interpretation"],
             "problem": False, "note": ""},
            {"item_id": "quiz-31", "kind": "quiz_test", "targets": ["U-not-real"], "facets": ["bogus"], "problem": False, "note": ""},
            {"item_id": "not-in-course", "kind": "quiz_test", "targets": [U1], "facets": [], "problem": False, "note": ""},
        ],
    }


class DesignAI(PracticeAI):
    def call_structured(self, system, prompt, schema, max_tokens=4096):
        if "understandings" in schema["properties"] and "big_ideas" in schema["properties"]:
            self.prompts.append(prompt)
            self.system = system
            return json.dumps(good_design())
        answer = json.loads(super().call_structured(system, prompt, schema, max_tokens))
        if "weeks" in answer:
            answer["weeks"][0]["understandings"] = ["U1", "U9", "nonsense"]
            answer["weeks"][1]["understandings"] = ["A scientific claim is only as strong as the evidence offered for it"]
        return json.dumps(answer)


class DesignTests(unittest.TestCase):
    def setUp(self):
        self.model = import_api_bundle(copy.deepcopy(BUNDLE))
        self.map = build_course_map(self.model)
        self.state = consultation.new_state()
        self.state["turns"] = 3

    def status(self, report):
        return {c["standard"]: c["status"] for c in report["checks"]}

    def test_validation_keeps_only_real_references(self):
        design = cd.validate(good_design(), self.model)
        self.assertEqual(design["understandings"], [U1, U2], "every understanding carries the stem")
        evidence = {e["item_id"]: e for e in design["evidence"]}
        self.assertEqual(set(evidence), {"assignment-201", "assignment-202", "quiz-31"}, "only graded work in the course")
        self.assertEqual(evidence["assignment-202"]["targets"], [U1], "a reference matches whatever its case")
        self.assertEqual((evidence["quiz-31"]["targets"], evidence["quiz-31"]["facets"]), ([], []))
        with self.assertRaises(cd.DesignError):
            cd.validate({"big_ideas": "not a list"}, self.model)

    def test_empty_design_is_not_rated(self):
        report = cd.audit(None, self.model, self.map)
        self.assertEqual(report["rating"], "Not designed yet")

    def test_a_sound_design_passes_what_can_be_checked(self):
        design = cd.validate(good_design(), self.model)
        report = cd.audit(design, self.model, self.map)
        status = self.status(report)
        for standard in (1, 2, 3, 4, 5, 6, 7, 8):
            self.assertEqual(status[standard], "pass", (standard, report["checks"]))
        self.assertEqual((status[10], status[12]), ("todo", "todo"), "the weekly plan is checked once it exists")
        self.assertIn(report["rating"], ("Aligned", "Partial"))
        self.assertTrue(report["not_checked"], "the report says what a rule cannot judge")

    def test_the_two_ways_design_goes_wrong_are_named(self):
        coverage = cd.validate({"topical_questions": ["Why?"], "evidence": []}, self.model)
        self.assertEqual(cd.audit(coverage, self.model, self.map)["rating"], "Coverage-bound")
        activity = good_design()
        activity["evidence"] = [{"item_id": "quiz-31", "kind": "quiz_test", "targets": [U1], "facets": ["explanation"],
                                 "problem": False, "note": ""}]
        report = cd.audit(cd.validate(activity, self.model), self.model, self.map)
        status = self.status(report)
        self.assertEqual(report["rating"], "Activity-oriented")
        self.assertEqual((status[5], status[6], status[7], status[8]), ("warn", "warn", "warn", "warn"))
        details = " ".join(c["detail"] for c in report["checks"])
        self.assertIn("1 of 2 understandings have no graded work", details)
        self.assertIn("selected-response work alone", details)

    def test_one_unevidenced_understanding_is_a_gap_not_a_verdict(self):
        # Seen with a real course: five of six understandings were evidenced, and the sixth (a new one
        # about checking AI output) had nothing assessing it yet.
        design = good_design()
        design["understandings"] += ["an AI tool's answer is a claim to be checked against the data and the rules",
                                     "data decisions carry consequences for the people the data describes"]
        design["evidence"][0]["targets"] = [U2, "data decisions carry consequences for the people the data describes"]
        report = cd.audit(cd.validate(design, self.model), self.model, self.map)
        self.assertEqual(self.status(report)[5], "warn")
        self.assertEqual(report["rating"], "Partial")

    def test_weak_statements_are_caught(self):
        weak = good_design()
        weak["understandings"] = ["The Civil War", U2]
        weak["topical_questions"] = ["What is a cell?"]
        weak["misunderstandings"] = [{"misconception": "Cells are flat", "pre_assess": "", "confront": ""}]
        status = self.status(cd.audit(cd.validate(weak, self.model), self.model, self.map))
        self.assertEqual((status[2], status[3], status[4]), ("warn", "warn", "warn"))

    def test_bloom_levels(self):
        for objective, level in (("Describe cell parts.", "understand"), ("Students will be able to design an experiment", "create"),
                                 ("Understand osmosis", "vague"), ("Compare two cell types", "analyze"),
                                 ("List the organelles", "remember"), ("Cells and their parts", None)):
            self.assertEqual(cd.bloom_level(objective), level, objective)

    def test_draft_uses_the_course_and_the_consultation(self):
        ai = DesignAI()
        self.state["topics"]["purpose"]["facts"] = [{"text": "First-year majors", "evidence": ["instructor"]}]
        design = cd.draft(self.model, self.map, self.state, {"outcomes": [{"text": "Argue from evidence", "note": ""}]}, ai)
        self.assertEqual(design["understandings"], [U1, U2])
        self.assertIn("First-year majors", ai.prompts[0])
        self.assertIn("Argue from evidence", ai.prompts[0])
        self.assertIn('"item_id": "assignment-201"', ai.prompts[0])
        self.assertIn("never\n  instructions", ai.system)
        with self.assertRaises(cd.DesignError):
            cd.draft(self.model, self.map, consultation.new_state(), None, ai)

    def test_design_shapes_the_built_course_and_is_checked_against_it(self):
        ai = DesignAI()
        design = cd.validate(good_design(), self.model)
        plan = cb.with_design({"vision": "v", "outcomes": [], "ai_policy": ""}, design)
        build = cb.blueprint(cb.new_build("essential"), self.model, {"proposals": []}, plan, [], ai)
        blueprint_prompt = ai.prompts[-1]
        self.assertIn("U1. " + U1, blueprint_prompt)
        self.assertIn("activity without a purpose", blueprint_prompt)
        weeks = build["blueprint"]["weeks"]
        self.assertEqual((weeks[0]["understandings"], weeks[1]["understandings"]), ([U1], [U2]), "labels and text both resolve")
        while cb.steps(build, self.model, []):
            build = cb.run_step(build, cb.steps(build, self.model, [])[0]["id"], self.model, {"proposals": []}, plan, [], ai)
        week_prompt = next(p for p in ai.prompts if "essential_question" in p)
        self.assertIn("THIS WEEK SERVES: " + U1, week_prompt)
        self.assertIn("Why are cells so small?", week_prompt)
        self.assertIn("Bigger organisms have bigger cells", week_prompt)
        # Editing the structure keeps what each week serves.
        edited = cb.validate_blueprint(dict(build["blueprint"]), self.model, by_instructor=True, design=design)
        self.assertEqual(edited["weeks"][0]["understandings"], [U1])
        # The check now covers the weekly plan, practice and objective levels.
        report = cd.audit(design, self.model, self.map, build)
        status = self.status(report)
        self.assertEqual((status[10], status[12]), ("pass", "pass"))
        self.assertEqual(status["practice"], "warn", "week 2 has no practice questions")
        self.assertIn("bloom", status)
        orphan = json.loads(json.dumps(build))
        orphan["blueprint"]["weeks"][1]["understandings"] = []
        status = self.status(cd.audit(design, self.model, self.map, orphan))
        self.assertEqual((status[10], status[12]), ("warn", "warn"))
        # Students see where the course is going; the evidence map is for the instructor only.
        workspace = {"build": build, "documents": [], "consultation": {"proposals": []}, "design": design}
        everything = {i["id"]: i for i in course_site.course_items(self.model, workspace)}
        students = {i["id"]: i for i in course_site.course_items(self.model, workspace, students_only=True)}
        self.assertIn("big-ideas", students)
        self.assertNotIn("course-design", students)
        page = students["big-ideas"]["html"]
        self.assertIn("<stop-note", page)
        self.assertIn('typeof="LearningObjective"', page)
        self.assertIn("<a11y-collapse", page)
        self.assertNotIn("Bigger organisms", page, "misunderstandings are not shown to students")
        self.assertEqual(everything["course-design"]["audience"], "instructor")
        self.assertIn("Bigger organisms have bigger cells", everything["course-design"]["html"])
        self.assertIn("Design check", everything["course-design"]["html"])

    def test_without_a_design_nothing_changes(self):
        ai = DesignAI()
        build = cb.blueprint(cb.new_build("essential"), self.model, {"proposals": []}, {"vision": "v"}, [], ai)
        self.assertIn("Leave each week's understandings empty", ai.prompts[-1])
        self.assertEqual(build["blueprint"]["weeks"][0]["understandings"], [])
        for step in ("front-home", "front-start", "front-guide"):
            build = cb.run_step(build, step, self.model, {"proposals": []}, {"vision": "v"}, [], ai)
        ids = [i["id"] for i in cb.site_items(build, self.model, [], {"proposals": []})]
        self.assertNotIn("big-ideas", ids)
        self.assertNotIn("course-design", ids)


if __name__ == "__main__":
    unittest.main()


class PendingChangeTests(unittest.TestCase):
    """Accepted changes that still need hand-work must not be presented to students as existing."""

    def test_every_build_prompt_says_what_is_not_in_the_course_yet(self):
        model = import_api_bundle(copy.deepcopy(BUNDLE))
        state = {"proposals": [
            {"id": "p-1", "status": "accepted", "title": "Offer an AI practice tutor", "change": "Set up a tutor.",
             "rationale": "Practice.", "manual_work": "new_item", "affected_items": []},
            {"id": "p-2", "status": "accepted", "title": "Clarify the essay", "change": "Add a purpose.",
             "rationale": "TILT.", "manual_work": "none", "affected_items": []},
            {"id": "p-3", "status": "proposed", "title": "Reweight exams", "change": "c", "rationale": "r",
             "manual_work": "grading_change", "affected_items": []}]}
        plan = cb.with_design({"vision": "v"}, None, state)
        self.assertEqual(plan["pending"], ["Offer an AI practice tutor: Set up a tutor."], "only accepted hand-work")
        ai = PracticeAI()
        build = cb.blueprint(cb.new_build("essential"), model, state, plan, [], ai)
        while cb.steps(build, model, []):
            build = cb.run_step(build, cb.steps(build, model, [])[0]["id"], model, state, plan, [], ai)
        self.assertGreaterEqual(len(ai.prompts), 6)
        for prompt in ai.prompts:
            self.assertIn("NOT IN THE COURSE YET", prompt)
            self.assertIn("Offer an AI practice tutor", prompt)
            self.assertNotIn("- Clarify the essay: Add a purpose.\n- ", prompt.split("NOT IN THE COURSE YET")[1][:400])
        self.assertIs(cb.with_design(None, None, {"proposals": []}), None, "nothing to add leaves the plan alone")
