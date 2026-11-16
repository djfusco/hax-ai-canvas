import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
import course_model as cm
from privacy_strip import strip_course


def model_with(html, **extra):
    model = cm.empty_course("export", "Biology 101")
    model["items"] = [{"id": "p1", "kind": "page", "title": "Announcements", "html": html,
                       "module_id": None, "published": True}]
    model.update(extra)
    return model


def strip(html, **kwargs):
    model, report = strip_course(model_with(html), **kwargs)
    return model["items"][0]["html"], report


class PrivacyStripTests(unittest.TestCase):
    def test_contact_details_and_ids(self):
        html, report = strip("<p>Email jane.doe@school.edu or call (555) 123-4567. Student ID: 912345678. "
                             '<a href="/courses/12/users/4455">profile</a></p>')
        self.assertNotIn("jane.doe", html)
        self.assertNotIn("123-4567", html)
        self.assertNotIn("912345678", html)
        self.assertNotIn("4455", html)
        self.assertIn("[email removed]", html)
        self.assertIn("/courses/12/users/[user]", html)
        self.assertEqual(report["counts"]["emails"], 1)
        self.assertEqual(report["counts"]["phones"], 1)
        self.assertEqual(report["counts"]["student_ids"], 1)
        self.assertEqual(report["counts"]["user_links"], 1)
        self.assertEqual(report["touched"], ["p1"])

    def test_student_names_in_class_context(self):
        html, report = strip("<p>Thanks to Jane Smith for the example. Great point, Omar! "
                             "Building on Priya's post, posted by Lee Wong. @Maria see above.</p>")
        for name in ("Jane", "Smith", "Omar", "Priya", "Lee Wong", "Maria"):
            self.assertNotIn(name, html)
        self.assertEqual(report["counts"]["names"], 5)

    def test_false_positive_guards(self):
        original = ("<p>Thanks to everyone. Thank you, Professor Lopez. Read Campbell's textbook chapter 3. "
                    "Due 2026-09-10. ISBN 978-0-13-468599-1. Course id 3. Thanks to Dr. Rivera.</p>")
        html, report = strip(original, instructor_names=["Ana Lopez"])
        self.assertEqual(html, original)
        self.assertEqual(report["total"], 0)

    def test_instructor_name_kept(self):
        html, _ = strip("<p>Posted by Ana Lopez. Thanks to Ana for organizing.</p>", instructor_names=["Ana Lopez"])
        self.assertIn("Ana Lopez", html)
        self.assertIn("Thanks to Ana", html)

    def test_all_text_fields_and_idempotence(self):
        model = model_with("<p>ok</p>")
        model["course"]["syllabus_html"] = "<p>TA: ta@school.edu</p>"
        model["items"].append({"id": "q1", "kind": "quiz", "title": "Quiz", "html": "", "module_id": None, "published": True})
        model["quizzes"] = {"q1": {"time_limit": None, "allowed_attempts": None, "points": None, "quiz_type": None,
                                   "questions": [{"text": "As Sam's answer showed, why?", "type": "essay", "points": 1}]}}
        model["materials"] = [{"id": "material-1", "name": "notes.txt", "role": "reference",
                               "text": "Call 555-222-3333"}]
        model["course"]["syllabus_html"] += '<p><a href="/courses/1/users/42">a student</a></p>'
        model["question_banks"] = [{"id": "bank-1", "title": "Bank", "questions": [
            {"text": "Thanks to Priya Shah for this one. Why?", "type": "essay", "points": 1}]}]
        model["rubrics"] = {"r1": {"title": "R", "points": 4, "criteria": [{
            "description": "Thesis", "long_description": "", "points": 4, "outcome_id": None,
            "ratings": [{"description": "Full marks", "long_description": "Ask kim@school.edu", "points": 4}]}]}}
        model["outcomes"] = [{"id": "o1", "title": "Explain", "description": "Mail lee@school.edu",
                              "mastery_points": None, "item_ids": []}]
        model["modules"] = [{"id": "m1", "title": "Posted by Omar Diaz", "position": 1, "items": []}]
        stripped, report = strip_course(model)
        self.assertIn("ta@school.edu", stripped["course"]["syllabus_html"], "staff contacts stay in the syllabus")
        self.assertNotIn("users/42", stripped["course"]["syllabus_html"], "links to people are still removed there")
        self.assertNotIn("Sam", stripped["quizzes"]["q1"]["questions"][0]["text"])
        self.assertNotIn("555", stripped["materials"][0]["text"])
        self.assertNotIn("Priya", stripped["question_banks"][0]["questions"][0]["text"])
        self.assertNotIn("kim@", stripped["rubrics"]["r1"]["criteria"][0]["ratings"][0]["long_description"])
        self.assertNotIn("lee@", stripped["outcomes"][0]["description"])
        self.assertNotIn("Omar", stripped["modules"][0]["title"])
        self.assertEqual(set(report["touched"]), {"syllabus", "q1", "material-1", "bank-1", "r1", "o1", "m1"})
        cm.validate_course(stripped)
        again, second = strip_course(copy.deepcopy(stripped))
        self.assertEqual(again, stripped)
        self.assertEqual(second["total"], 0)

    def test_staff_contacts_are_kept_only_where_they_belong(self):
        model = model_with("<p>Email me: prof@school.edu or call 814-555-1234.</p>")
        model["items"][0]["title"] = "Contact your instructor"
        model["items"].append({"id": "p2", "kind": "page", "title": "Week 3 notes", "module_id": None,
                               "published": True, "html": "<p>Ask jo@gmail.com, 814-555-9999. Thanks to Jo Park!</p>"})
        model["items"].append({"id": "d1", "kind": "discussion", "title": "Help forum", "module_id": None,
                               "published": True, "html": "<p>Write to sam@school.edu</p>"})
        stripped, report = strip_course(model)
        contact, notes, forum = (i["html"] for i in stripped["items"])
        self.assertIn("prof@school.edu", contact)
        self.assertIn("814-555-1234", contact)
        self.assertNotIn("jo@gmail.com", notes)
        self.assertNotIn("814-555-9999", notes)
        self.assertNotIn("Jo Park", notes)
        self.assertNotIn("sam@school.edu", forum, "only pages count as contact pages, not discussions")
        self.assertEqual(report["kept_contacts"], [stripped["items"][0]["id"]])

    def test_report_never_contains_removed_text(self):
        _, report = strip("<p>Thanks to Jane Smith, jane@school.edu</p>")
        self.assertNotIn("Jane", repr(report))
        self.assertNotIn("school.edu", repr(report))


if __name__ == "__main__":
    unittest.main()
