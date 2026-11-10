"""Canvas-importable package of revised documents: round trip through the importer."""
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "tests")]
import canvas_package
from slim_import import import_slim
from test_slim_intake import slim_package


def load(data):
    temp = tempfile.TemporaryDirectory()
    path = Path(temp.name) / "course.imscc"
    path.write_bytes(data)
    try:
        return import_slim(path)
    finally:
        temp.cleanup()


class CanvasPackageTests(unittest.TestCase):
    def setUp(self):
        self.model = load(slim_package())
        self.ids = {i["title"]: i["id"] for i in self.model["items"]}

    def documents(self):
        essay, lab, welcome = self.ids["Essay"], self.ids["Lab"], self.ids["Welcome"]
        return [
            {"id": "doc-1", "kind": "syllabus", "item_id": None, "title": "Syllabus", "status": "approved",
             "html": "<h2>Syllabus</h2><p>New &amp; improved.</p>", "created_at": "2026-11-01T00:00:00Z"},
            {"id": "doc-2", "kind": "assignment", "item_id": essay, "title": "Essay", "status": "approved",
             "html": "<p>Outline first.</p>", "created_at": "2026-11-01T00:00:00Z"},
            {"id": "doc-3", "kind": "assignment", "item_id": essay, "title": "Essay", "status": "approved",
             "html": "<p>Outline first, then 1,500 words.</p>", "created_at": "2026-11-01T00:00:00Z",
             "updated_at": "2026-11-02T00:00:00Z"},
            {"id": "doc-4", "kind": "assignment", "item_id": lab, "title": "Lab", "status": "draft",
             "html": "<p>Not approved.</p>", "created_at": "2026-11-01T00:00:00Z"},
            {"id": "doc-5", "kind": "page", "item_id": welcome, "title": "Welcome", "status": "approved",
             "html": "<p>Why this course matters.</p>", "created_at": "2026-11-01T00:00:00Z"},
            {"id": "doc-6", "kind": "rubric", "item_id": essay, "title": "Rubric: Essay", "status": "approved",
             "html": "<table></table>", "created_at": "2026-11-01T00:00:00Z",
             "rubric": [{"criterion": "Argument", "description": "Clear claim", "ratings": [
                 {"label": "Strong", "points": 10.0, "description": "Clear and supported"},
                 {"label": "Weak", "points": 2.0, "description": "Unclear"}]}]},
        ]

    def test_round_trip(self):
        data, summary = canvas_package.build("Biology", self.model, self.documents())
        self.assertEqual(summary, {"pages": 2, "assignments": 1, "rubrics": 1})
        archive = zipfile.ZipFile(__import__("io").BytesIO(data))
        names = archive.namelist()
        self.assertEqual(names[0], "imsmanifest.xml")
        self.assertIn("course_settings/canvas_export.txt", names)
        self.assertNotIn("course_settings/course_settings.xml", names, "no course settings are changed")
        for name in names:
            if name.endswith(".xml"):
                ET.fromstring(archive.read(name))  # well-formed

        imported = load(data)
        self.assertEqual(imported["course"]["title"], "Biology (revised)")
        by_title = {i["title"]: i for i in imported["items"]}
        self.assertEqual(set(by_title), {"Syllabus (revised)", "Welcome (revised)", "Essay (revised)"})
        self.assertEqual(by_title["Essay (revised)"]["kind"], "assignment")
        self.assertIn("1,500 words", by_title["Essay (revised)"]["html"], "latest approved version")
        self.assertIn("New &amp; improved", by_title["Syllabus (revised)"]["html"])
        self.assertFalse(by_title["Essay (revised)"]["published"])
        details = imported["assignments"][by_title["Essay (revised)"]["id"]]
        self.assertEqual((details["points"], details["due_at"], details["peer_reviews"]),
                         (20.0, "2026-09-10T23:59:00Z", True))
        rubric = imported["rubrics"][details["rubric_id"]]
        self.assertEqual(rubric["title"], "Rubric: Essay (revised)")
        self.assertEqual(rubric["criteria"][0]["description"], "Argument")
        self.assertEqual([r["points"] for r in rubric["criteria"][0]["ratings"]], [10.0, 2.0])
        self.assertEqual([m["title"] for m in imported["modules"]], ["Course Consultant revisions"])
        self.assertEqual(len(imported["modules"][0]["items"]), 3)

    def test_original_rubric_carried_and_nothing_approved(self):
        essay = self.ids["Essay"]
        documents = [{"id": "doc-1", "kind": "assignment", "item_id": essay, "title": "Essay", "status": "approved",
                      "html": "<p>x</p>", "created_at": "2026-11-01T00:00:00Z"}]
        data, summary = canvas_package.build("Biology", self.model, documents)
        self.assertEqual(summary["rubrics"], 1)
        imported = load(data)
        details = next(iter(imported["assignments"].values()))
        original = self.model["rubrics"][self.model["assignments"][essay]["rubric_id"]]
        self.assertEqual(len(imported["rubrics"][details["rubric_id"]]["criteria"]), len(original["criteria"]))
        with self.assertRaises(canvas_package.PackageError):
            canvas_package.build("Biology", self.model, [dict(documents[0], status="draft")])


if __name__ == "__main__":
    unittest.main()
