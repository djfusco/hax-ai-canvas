"""Slim export intake endpoint: import, privacy strip, validate, store."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "python/app"), str(ROOT / "tests")]
import app as web
from mentor_store import MentorStore
from test_slim_import import text_entries


def slim_package(extra=None):
    entries = text_entries()
    entries["wiki_content/welcome.html"] = entries["wiki_content/welcome.html"].replace(
        "<p>Welcome to cells.</p>", "<p>Welcome to cells. Thanks to Jane Smith; questions to ta@school.edu.</p>")
    entries["media_inventory.json"] = json.dumps(
        [{"path": "web_resources/lecture one.mp4", "bytes": 4_000_000_000, "mime": "video/mp4"}])
    entries.update(extra or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class SlimIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_root = web.app.config.get("MENTOR_DATA_ROOT")
        web.app.config.update(TESTING=True, MENTOR_DATA_ROOT=self.root)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.client = web.app.test_client()
        self.csrf = self.client.get("/mentor/api/session").json["csrf"]
        response = self.client.post("/mentor/api/workspaces", json={"title": "Biology redesign"},
                                    headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        self.workspace = response.json
        self.url = f"/mentor/api/workspaces/{self.workspace['id']}/intake/export"

    def tearDown(self):
        self.environment.stop()
        if self.old_root is None:
            web.app.config.pop("MENTOR_DATA_ROOT", None)
        else:
            web.app.config["MENTOR_DATA_ROOT"] = self.old_root
        self.temp.cleanup()

    def upload(self, data, *, revision=None, csrf=None, client=None, extra=None, expected=200):
        form = {"package": (io.BytesIO(data), "slim.zip"),
                "revision": str(self.workspace["revision"] if revision is None else revision)}
        form.update(extra or {})
        response = (client or self.client).post(self.url, data=form, content_type="multipart/form-data",
                                                headers={"X-Mentor-CSRF": self.csrf if csrf is None else csrf})
        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
        return response.json

    def stored_model(self):
        import sqlite3
        store = MentorStore(self.root / "workspaces.sqlite")
        with sqlite3.connect(self.root / "workspaces.sqlite") as conn:
            owner = conn.execute("SELECT owner FROM mentor_workspaces").fetchone()[0]
        return store.get_course_model(owner, self.workspace["id"])

    def test_imports_strips_and_stores(self):
        result = self.upload(slim_package(), extra={"instructor_name": "Ana Lopez"})
        course = result["course"]
        self.assertEqual(course["title"], "Biology 101: Cells")
        self.assertEqual(course["source"], "export")
        self.assertEqual(course["counts"]["assignments"], 3)
        self.assertEqual(course["counts"]["rubrics"], 1)
        self.assertEqual(course["privacy"]["counts"]["emails"], 1)
        self.assertEqual(course["privacy"]["counts"]["names"], 1)
        self.assertNotIn("touched", course["privacy"])
        self.assertEqual(result["source_type"], "export")
        self.assertEqual(result["revision"], self.workspace["revision"] + 1)
        model = self.stored_model()
        welcome = next(i for i in model["items"] if i["title"] == "Welcome")
        self.assertNotIn("Jane Smith", welcome["html"])
        self.assertNotIn("ta@school.edu", welcome["html"])
        self.assertTrue(model["course"]["weighted_groups"])
        # The raw package is never persisted.
        self.assertFalse(list(self.root.rglob("*.zip")))

    def test_course_map_endpoint(self):
        base = f"/mentor/api/workspaces/{self.workspace['id']}/course-map"
        self.assertEqual(self.client.get(base).status_code, 404)
        self.upload(slim_package())
        course_map = self.client.get(base).json
        self.assertEqual(course_map["course"]["title"], "Biology 101: Cells")
        self.assertTrue(course_map["grading"]["weighted_groups"])
        self.assertEqual(course_map["assessment_summary"]["with_rubric"], 1)

    def test_alignment_is_tied_to_the_imported_course(self):
        from mentor_store import WorkspaceConflict
        import sqlite3
        result = self.upload(slim_package())
        store = MentorStore(self.root / "workspaces.sqlite")
        with sqlite3.connect(self.root / "workspaces.sqlite") as conn:
            owner = conn.execute("SELECT owner FROM mentor_workspaces").fetchone()[0]
        alignment = {"version": 1, "status": "proposed", "alignments": [], "syllabus_outcomes": []}
        with self.assertRaises(WorkspaceConflict):
            store.save_course_alignment(owner, self.workspace["id"], "stale", alignment)
        store.save_course_alignment(owner, self.workspace["id"], result["course"]["imported_at"], alignment)
        base = f"/mentor/api/workspaces/{self.workspace['id']}/course-map"
        self.assertEqual(self.client.get(base).json["alignment_status"], "proposed")
        self.workspace = result
        self.upload(slim_package())  # re-import clears the old proposal
        self.assertEqual(self.client.get(base).json["alignment_status"], "rubric_only")

    def test_reimport_replaces_course(self):
        first = self.upload(slim_package())
        self.workspace = first
        second = self.upload(slim_package({"wiki_content/extra.html":
                                           "<html><head><title>Extra</title></head><body><p>More.</p></body></html>"}))
        self.assertEqual(second["course"]["counts"]["pages"], first["course"]["counts"]["pages"] + 1)
        self.assertEqual(len([i for i in self.stored_model()["items"] if i["title"] == "Extra"]), 1)

    def test_rejections(self):
        self.upload(b"not a zip", expected=400)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("readme.txt", "hello")
        error = self.upload(buffer.getvalue(), expected=400)["error"]
        self.assertIn("imsmanifest.xml", error)
        self.upload(slim_package(), revision=999, expected=409)
        self.upload(slim_package(), csrf="wrong", expected=403)
        with patch("slim_import.MAX_SLIM_BYTES", 1000):
            self.assertIn("48 MiB", self.upload(slim_package(), expected=400)["error"])
        response = self.client.post(self.url, data={"revision": "1"}, content_type="multipart/form-data",
                                    headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.stored_model())

    def test_documents_only_intake(self):
        url = f"/mentor/api/workspaces/{self.workspace['id']}/intake/documents"
        response = self.client.post(url, data={
            "documents": (io.BytesIO(b"BIO 101 Syllabus. Objectives: explain osmosis. Contact me at prof@school.edu"),
                          "Syllabus.txt"),
            "notes": "A large intro course; students struggle with lab reports.",
            "title": "Biology 101", "revision": str(self.workspace["revision"]),
        }, content_type="multipart/form-data", headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        course = response.json["course"]
        self.assertEqual((course["source"], course["title"], course["counts"]["materials"]), ("documents", "Biology 101", 2))
        self.assertGreaterEqual(course["privacy"]["counts"]["emails"], 1)
        model = self.stored_model()
        self.assertNotIn("prof@school.edu", json.dumps(model))
        self.assertIn("explain osmosis", model["course"]["syllabus_html"])
        course_map = self.client.get(f"/mentor/api/workspaces/{self.workspace['id']}/course-map").json
        self.assertEqual(course_map["assessment_summary"]["graded_items"], 0)
        empty = self.client.post(url, data={"revision": str(response.json["revision"])},
                                 content_type="multipart/form-data", headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(empty.status_code, 400)

    def chunk(self, upload_id, index, total, data, client=None, csrf=None, expected=200):
        response = (client or self.client).post(
            f"/mentor/api/workspaces/{self.workspace['id']}/intake/chunks",
            data={"upload_id": upload_id, "index": str(index), "total": str(total),
                  "chunk": (io.BytesIO(data), "part")},
            content_type="multipart/form-data", headers={"X-Mentor-CSRF": csrf or self.csrf})
        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))

    def assembled(self, upload_id, expected=200):
        response = self.client.post(self.url, data={"upload_id": upload_id, "revision": str(self.workspace["revision"])},
                                    content_type="multipart/form-data", headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
        return response.json

    def test_chunked_upload(self):
        data = slim_package()
        upload_id = "a" * 32
        size = len(data) // 3 + 1
        for index in (2, 0, 1):  # order of arrival does not matter
            self.chunk(upload_id, index, 3, data[index * size:(index + 1) * size])
        result = self.assembled(upload_id)
        self.assertEqual(result["course"]["counts"]["assignments"], 3)
        self.assertIn("incomplete", self.assembled(upload_id, expected=400)["error"])  # pieces are consumed

    def test_chunked_upload_rejections(self):
        data = slim_package()
        self.chunk("b" * 32, 0, 2, data[:100])
        self.assertIn("incomplete", self.assembled("b" * 32, expected=400)["error"])
        self.chunk("not-hex", 0, 1, data, expected=400)
        self.chunk("c" * 32, 0, 99, data, expected=400)
        self.chunk("c" * 32, 3, 2, data, expected=400)
        other = web.app.test_client()
        csrf = other.get("/mentor/api/session").json["csrf"]
        self.chunk("d" * 32, 0, 1, data, client=other, csrf=csrf, expected=404)
        # Pieces from another workspace are not assembled here.
        second = self.client.post("/mentor/api/workspaces", json={"title": "Other"},
                                  headers={"X-Mentor-CSRF": self.csrf}).json
        response = self.client.post(f"/mentor/api/workspaces/{second['id']}/intake/chunks",
                                    data={"upload_id": "e" * 32, "index": "0", "total": "1",
                                          "chunk": (io.BytesIO(data), "part")},
                                    content_type="multipart/form-data", headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(response.status_code, 200)
        self.assertIn("incomplete", self.assembled("e" * 32, expected=400)["error"])

    def test_other_browser_cannot_import(self):
        other = web.app.test_client()
        csrf = other.get("/mentor/api/session").json["csrf"]
        self.upload(slim_package(), client=other, csrf=csrf, expected=404)


if __name__ == "__main__":
    unittest.main()
