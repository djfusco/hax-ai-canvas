"""Whole-course site: build order, revised versions, stable preview pages, package."""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "python/app"), str(ROOT / "tests")]
import course_site
from canvas_api_import import import_api_bundle

BUNDLE = json.loads((ROOT / "tests/fixtures/extension_bundle.json").read_text())


def doc(kind, title, html, item_id=None, status="approved"):
    return {"id": "doc-" + title, "kind": kind, "item_id": item_id, "title": title, "html": html, "status": status}


class CourseSiteTests(unittest.TestCase):
    def setUp(self):
        self.model = import_api_bundle(copy.deepcopy(BUNDLE))
        self.ids = {i["title"]: i["id"] for i in self.model["items"]}

    def test_order_and_revisions(self):
        documents = [doc("syllabus", "New syllabus", "<p>Revised syllabus</p>"),
                     doc("assignment", "Essay (revised)", "<p>Outline first.</p>", self.ids["Essay"]),
                     doc("rubric", "Rubric: Essay", "<table><tr><td>Thesis</td></tr></table>", self.ids["Essay"]),
                     doc("assignment", "Draft lab", "<p>Not approved</p>", self.ids["Lab"], status="draft")]
        items = course_site.build_items(self.model, documents)
        titles = [i["title"] for i in items]
        self.assertEqual(titles[:2], ["New syllabus", "Week 1"])
        self.assertLess(titles.index("Essay (revised)"), titles.index("Rubric: Essay"))
        self.assertIn("Lab", titles, "draft revisions are not used")
        self.assertNotIn("Draft lab", titles)
        essay = next(i for i in items if i["title"] == "Essay (revised)")
        self.assertTrue(essay["revised"])
        self.assertIn("20 points", essay["html"])
        quiz = next(i for i in items if i["title"] == "Cell quiz")
        self.assertIn("Quiz with 2 questions", quiz["html"])
        self.assertNotIn("osmosis", quiz["html"], "quiz questions are not shown")
        self.assertEqual(len({i["id"] for i in items}), len(items))

    def test_render_pages_at_stable_addresses(self):
        items = course_site.build_items(self.model, [])
        places = course_site.locations(items)
        self.assertEqual(places, course_site.locations(course_site.build_items(self.model, [])))
        index = course_site.render("Bio", items, "index.html")
        self.assertIn("Private student-view preview", index)
        self.assertIn(places[self.ids["Essay"]], index)
        page = course_site.render("Bio", items, places[self.ids["Essay"]])
        self.assertIn("Argue a position on cell theory", page)
        self.assertIsNone(course_site.render("Bio", items, "pages/nope/preview.html"))

    def test_package(self):
        items = course_site.build_items(self.model, [])
        with tempfile.TemporaryDirectory() as temp:
            result = course_site.package("w1", "Bio", items, Path(temp) / "site")
            names = zipfile.ZipFile(result["path"]).namelist()
        self.assertIn("site.json", names)
        self.assertEqual(result["items"], len(items))


class SitePreviewRouteTests(unittest.TestCase):
    def test_preview_route(self):
        import os
        from unittest.mock import patch
        import app as web
        from test_slim_intake import slim_package
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            web.app.config.update(TESTING=True, MENTOR_DATA_ROOT=Path(temp))
            client = web.app.test_client()
            csrf = client.get("/mentor/api/session").json["csrf"]
            workspace = client.post("/mentor/api/workspaces", json={"title": "Bio"}, headers={"X-Mentor-CSRF": csrf}).json
            base = f"/mentor/api/workspaces/{workspace['id']}"
            self.assertEqual(client.get(base + "/site/index.html").status_code, 404)
            client.post(base + "/intake/export", data={"package": (io.BytesIO(slim_package()), "s.zip"),
                                                       "revision": str(workspace["revision"])},
                        content_type="multipart/form-data", headers={"X-Mentor-CSRF": csrf})
            redirect = client.get(base + "/site")
            self.assertEqual(redirect.status_code, 302)
            index = client.get(base + "/site/index.html")
            self.assertEqual(index.status_code, 200)
            self.assertIn("script-src 'none'", index.headers["Content-Security-Policy"])
            self.assertIn(b"Biology 101: Cells", index.data)
            self.assertEqual(client.get(base + "/site/pages/x/preview.html").status_code, 404)
            other = web.app.test_client()
            self.assertNotEqual(other.get(base + "/site/index.html").status_code, 200)
            web.app.config.pop("MENTOR_DATA_ROOT", None)


if __name__ == "__main__":
    unittest.main()
