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

    def test_other_browser_cannot_import(self):
        other = web.app.test_client()
        csrf = other.get("/mentor/api/session").json["csrf"]
        self.upload(slim_package(), client=other, csrf=csrf, expected=404)


if __name__ == "__main__":
    unittest.main()
