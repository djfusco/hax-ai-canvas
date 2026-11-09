import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

import course_model as cm


def sample():
    model = cm.empty_course("export", "Biology 101")
    model["modules"] = [{"id": "m1", "title": "Week 1", "position": 1, "items": ["p1", "a1"],
                         "prerequisites": [], "requirements": [{"item_id": "p1", "type": "must_view", "min_score": None}],
                         "sequential": False}]
    model["items"] = [
        {"id": "p1", "kind": "page", "title": "Welcome", "html": "<p>Hi</p>", "module_id": "m1", "published": True},
        {"id": "a1", "kind": "assignment", "title": "Essay", "html": "<p>Write</p>", "module_id": "m1", "published": True},
        {"id": "q1", "kind": "quiz", "title": "Quiz 1", "html": "", "module_id": None, "published": True},
    ]
    model["assignment_groups"] = [{"id": "g1", "name": "Essays", "weight": 40.0, "position": 1}]
    model["outcomes"] = [{"id": "o1", "title": "Argue", "description": "", "mastery_points": 3, "item_ids": ["a1"]}]
    model["rubrics"] = {"r1": {"title": "Essay rubric", "points": 10, "criteria": [
        {"description": "Thesis", "long_description": "", "points": 10, "outcome_id": "o1",
         "ratings": [{"description": "Full", "points": 10}]}]}}
    model["assignments"] = {"a1": {"points": 10, "due_at": "2026-01-01T00:00:00Z", "submission_types": ["online_upload"],
                                   "grading_type": "points", "group_id": "g1", "rubric_id": "r1",
                                   "peer_reviews": False, "group_work": False, "allowed_attempts": None}}
    model["quizzes"] = {"q1": {"time_limit": 30, "allowed_attempts": 1, "points": 5, "quiz_type": "assignment",
                               "questions": [{"text": "What is a cell?", "type": "essay_question", "points": 5}]}}
    model["media_inventory"] = [{"path": "web_resources/lecture.mp4", "mime": "video/mp4", "bytes": 10 ** 9,
                                 "referenced_by": ["p1"]}]
    model["materials"] = [{"id": "material-1", "name": "syllabus.pdf", "role": "policy", "text": "Policy"}]
    model["provenance"]["counts"] = {"pages": 1}
    return model


class CourseModelTests(unittest.TestCase):
    def test_valid_model(self):
        model = sample()
        self.assertIs(cm.validate_course(model), model)
        cm.validate_course(cm.empty_course("extension"))

    def assertInvalid(self, mutate, fragment):
        model = sample()
        mutate(model)
        with self.assertRaises(cm.CourseModelError) as ctx:
            cm.validate_course(model)
        self.assertIn(fragment, str(ctx.exception))

    def test_reference_errors(self):
        self.assertInvalid(lambda m: m["modules"][0]["items"].append("nope"), "modules[0].items[2]")
        self.assertInvalid(lambda m: m["assignments"]["a1"].update(rubric_id="missing"), "assignments.a1.rubric_id")
        self.assertInvalid(lambda m: m["assignments"]["a1"].update(group_id="missing"), "assignments.a1.group_id")
        self.assertInvalid(lambda m: m["assignments"].update(zz={}), "assignments.zz")
        self.assertInvalid(lambda m: m["rubrics"]["r1"]["criteria"][0].update(outcome_id="x"), "outcome_id")
        self.assertInvalid(lambda m: m["items"][0].update(module_id="m9"), "items[0].module_id")
        self.assertInvalid(lambda m: m["media_inventory"][0]["referenced_by"].append("x"), "referenced_by")
        self.assertInvalid(lambda m: m["modules"][0]["requirements"][0].update(item_id="x"), "requirements[0].item_id")

    def test_type_errors(self):
        self.assertInvalid(lambda m: m["items"][0].update(kind="video"), "items[0].kind")
        self.assertInvalid(lambda m: m["items"][0].update(title=" "), "items[0].title")
        self.assertInvalid(lambda m: m["assignments"]["a1"].update(points="10"), "assignments.a1.points")
        self.assertInvalid(lambda m: m["assignments"]["a1"].update(points=float("nan")), "assignments.a1.points")
        self.assertInvalid(lambda m: m["assignment_groups"][0].update(weight=-1), "weight")
        self.assertInvalid(lambda m: m["provenance"].update(source="ftp"), "provenance.source")
        self.assertInvalid(lambda m: m["materials"][0].update(role="secret"), "materials[0].role")
        self.assertInvalid(lambda m: m.update(version=2), "version")
        self.assertInvalid(lambda m: m["items"].append(copy.deepcopy(m["items"][0])), "is duplicated")

    def test_limits(self):
        self.assertInvalid(lambda m: m["items"][0].update(html="x" * (cm.MAX_ITEM_HTML_BYTES + 1)), "items[0].html")
        def many_questions(m):
            m["quizzes"]["q1"]["questions"] = [{"text": "q", "type": "t", "points": 1}] * (cm.MAX_QUIZ_QUESTIONS + 1)
        self.assertInvalid(many_questions, "quizzes.q1.questions")
        def total_html(m):
            m["items"] = [{"id": f"i{n}", "kind": "page", "title": "t", "html": "x" * (cm.MAX_ITEM_HTML_BYTES - 10)}
                          for n in range(9)]
            m["modules"][0]["items"] = []
            m["modules"][0]["requirements"] = []
            m["assignments"] = {}
            m["quizzes"] = {}
            m["outcomes"][0]["item_ids"] = []
            m["media_inventory"] = []
        self.assertInvalid(total_html, "16 MiB")

    def test_errors_do_not_echo_content(self):
        model = sample()
        model["items"][0]["title"] = "SECRET-" + "x" * 600
        with self.assertRaises(cm.CourseModelError) as ctx:
            cm.validate_course(model)
        self.assertNotIn("SECRET", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
