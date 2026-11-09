"""Extension bundle adapter and intake endpoint."""
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "python/app"), str(ROOT / "tests")]
import course_model as cm
from canvas_api_import import BundleError, import_api_bundle
from test_slim_import import text_entries

BUNDLE = json.loads((ROOT / "tests/fixtures/extension_bundle.json").read_text())


def bundle(**changes):
    value = copy.deepcopy(BUNDLE)
    value.update(changes)
    return value


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.model = import_api_bundle(bundle())
        self.by_title = {i["title"]: i for i in self.model["items"]}

    def test_course_and_policy(self):
        course = self.model["course"]
        self.assertEqual((course["title"], course["code"], course["term"]), ("Biology 101: Cells", "BIO-101", "Fall 2026"))
        self.assertTrue(course["weighted_groups"])
        self.assertIn("Campbell", course["syllabus_html"])
        self.assertEqual(course["late_policy"]["late_submission_deduction"], 10.0)
        self.assertNotIn("course_id", course["late_policy"])
        self.assertEqual(self.model["provenance"]["source"], "extension")

    def test_matches_export_path(self):
        """The same course via the extension yields the same pedagogical data as via export."""
        from slim_import import import_slim
        import zipfile
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "slim.zip"
            with zipfile.ZipFile(path, "w") as archive:
                for name, data in text_entries().items():
                    archive.writestr(name, data)
            exported = import_slim(path)

        def summary(model):
            titles = {i["id"]: i["title"] for i in model["items"]}
            groups = {g["id"]: g["name"] for g in model["assignment_groups"]}
            return {
                "weights": sorted((g["name"], g["weight"]) for g in model["assignment_groups"]),
                "assignments": sorted((titles[i], d["points"], groups.get(d["group_id"]), bool(d["rubric_id"]))
                                      for i, d in model["assignments"].items()
                                      if titles[i] in ("Essay", "Lab", "Project", "Cell quiz")),
                "rubric": sorted((c["description"], c["points"]) for r in model["rubrics"].values()
                                 for c in r["criteria"]),
                "outcomes": sorted((o["title"], tuple(sorted(titles[i] for i in o["item_ids"])))
                                   for o in model["outcomes"]),
                "week1": [titles[i] for i in model["modules"][0]["items"]][:4],
                "requirements": [(titles[r["item_id"]], r["type"], r["min_score"])
                                 for r in model["modules"][0]["requirements"]],
            }
        self.assertEqual(summary(self.model), summary(exported))

    def test_quizzes_including_new_quizzes(self):
        classic = self.model["quizzes"][self.by_title["Cell quiz"]["id"]]
        self.assertEqual((classic["time_limit"], classic["allowed_attempts"]), (20, 2))
        self.assertEqual(len(classic["questions"]), 2)
        new = self.by_title["Organelle check"]
        self.assertEqual(new["kind"], "quiz")
        self.assertEqual(self.model["quizzes"][new["id"]]["questions"][0]["type"], "hot-spot")
        self.assertIn(new["id"], self.model["assignments"])
        # The quiz's backing assignment is not a separate item.
        self.assertEqual([i["title"] for i in self.model["items"]].count("Cell quiz"), 1)

    def test_graded_discussion_and_privacy_boundaries(self):
        debate = self.by_title["Debate cells"]
        self.assertEqual(debate["kind"], "discussion")
        self.assertEqual(self.model["assignments"][debate["id"]]["points"], 5.0)
        self.assertNotIn("Announcement", self.by_title)
        serialized = json.dumps(self.model)
        self.assertNotIn("STUDENT REPLY", serialized)
        self.assertNotIn("99", json.dumps([i.get("html") for i in self.model["items"]]))

    def test_modules_links_and_files(self):
        week1, week2 = self.model["modules"]
        self.assertEqual(week2["prerequisites"], [week1["id"]])
        self.assertTrue(week1["sequential"])
        self.assertEqual(self.by_title["NCBI"]["kind"], "external_link")
        self.assertEqual(self.by_title["Organelle check"]["module_id"], week2["id"])
        media = {m["path"]: m for m in self.model["media_inventory"]}
        welcome = self.by_title["Welcome"]["id"]
        self.assertEqual(media["files/900/lecture one.mp4"]["referenced_by"], [welcome])
        self.assertEqual(media["files/900/lecture one.mp4"]["bytes"], 4_000_000_000)

    def test_defensive_parsing(self):
        messy = bundle(assignments=[{"id": 1, "name": None, "points_possible": "ten", "rubric": "x"},
                                    "not a dict", {"name": "no id"}],
                       pages=[{"page_id": 5, "title": "x" * 900, "body": 12}],
                       modules=[{"id": 1, "items": [{"type": "Page", "page_url": "missing"}]}],
                       quizzes=[], new_quizzes=[], discussion_topics=[], files=[{"id": 1}])
        model = import_api_bundle(messy)
        titles = [i["title"] for i in model["items"]]
        self.assertIn("Untitled assignment", titles)
        self.assertEqual(max(map(len, titles)), cm.MAX_TITLE)
        with self.assertRaisesRegex(BundleError, "unsupported version"):
            import_api_bundle(bundle(version=2))
        with self.assertRaisesRegex(BundleError, "must be a list"):
            import_api_bundle(bundle(pages={"a": 1}))
        with self.assertRaisesRegex(BundleError, "no course"):
            import_api_bundle(bundle(course=None))


class ExtensionIntakeTests(unittest.TestCase):
    def setUp(self):
        import app as web
        self.web = web
        self.temp = tempfile.TemporaryDirectory()
        self.old_root = web.app.config.get("MENTOR_DATA_ROOT")
        web.app.config.update(TESTING=True, MENTOR_DATA_ROOT=Path(self.temp.name))
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.client = web.app.test_client()
        self.csrf = self.client.get("/mentor/api/session").json["csrf"]
        self.workspace = self.client.post("/mentor/api/workspaces", json={"title": "Bio"},
                                          headers={"X-Mentor-CSRF": self.csrf}).json
        self.url = f"/mentor/api/workspaces/{self.workspace['id']}/intake/extension"

    def tearDown(self):
        self.environment.stop()
        if self.old_root is None:
            self.web.app.config.pop("MENTOR_DATA_ROOT", None)
        else:
            self.web.app.config["MENTOR_DATA_ROOT"] = self.old_root
        self.temp.cleanup()

    def post(self, data, expected=200):
        response = self.client.post(self.url, data={
            "bundle": (io.BytesIO(data), "course.json"), "revision": str(self.workspace["revision"]),
        }, content_type="multipart/form-data", headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
        return response.json

    def test_import(self):
        value = bundle()
        value["pages"][0]["body"] += "<p>Thanks to Jane Smith.</p>"
        result = self.post(json.dumps(value).encode())
        self.assertEqual(result["course"]["source"], "extension")
        self.assertEqual(result["course"]["counts"]["quizzes"], 2)
        self.assertEqual(result["course"]["privacy"]["counts"]["names"], 1)
        self.assertEqual(result["source_type"], "extension")

    def test_rejections(self):
        self.assertIn("not valid JSON", self.post(b"{nope", expected=400)["error"])
        self.assertIn("unsupported version", self.post(json.dumps({"version": 9}).encode(), expected=400)["error"])
        with patch("canvas_api_import.MAX_BUNDLE_BYTES", 10):
            self.assertIn("16 MiB", self.post(json.dumps(bundle()).encode(), expected=400)["error"])


if __name__ == "__main__":
    unittest.main()
