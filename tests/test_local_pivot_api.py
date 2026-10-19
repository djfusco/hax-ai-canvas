"""Exercise the real local route integration without invoking external services."""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "python/app"), str(ROOT / "tests")]
import app as web
from local_startup import application_lease
from mentor_service import build_artifact
from mentor_store import MentorStore
from test_local_import import cartridge_bytes


class LocalPivotApiTests(unittest.TestCase):
    def setUp(self):
        self.threads_before = set(threading.enumerate())
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        keys = {"TESTING": True, "MENTOR_DATA_ROOT": self.root, "LOCAL_HAX_ENABLED": False}
        self.old = {key: web.app.config.get(key) for key in keys}
        web.app.config.update(keys)
        self.client = web.app.test_client()
        self.csrf = self.client.get("/mentor/api/session").json["csrf"]
        self.workspace = self.call("/mentor/api/workspaces", {"title": "Local course"}, expected=201)
        self.base = "/mentor/api/workspaces/" + self.workspace["id"]

    def tearDown(self):
        # An idle/failed job can be visible before the worker's finally block
        # clears upload staging. Do not remove its database while it is writing.
        for worker in set(threading.enumerate()) - self.threads_before:
            worker.join(timeout=5)
        for manager in web.app.extensions.get("local_hax_sites", {}).values():
            manager.shutdown()
        for key, value in self.old.items():
            if value is None:
                web.app.config.pop(key, None)
            else:
                web.app.config[key] = value
        self.env.stop()
        self.temp.cleanup()

    def call(self, url, value=None, method="POST", expected=200):
        response = self.client.open(url, method=method, json=value,
                                    headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
        return response.json

    def fetch(self):
        self.workspace = self.client.get(self.base).json
        return self.workspace

    def wait(self):
        for _ in range(200):
            value = self.fetch()
            if not value["active_job"]:
                return value
            time.sleep(.02)
        self.fail("Local job did not finish")

    def reserve(self, content):
        return self.call(self.base + "/local-uploads", {
            "revision": self.workspace["revision"], "filename": "course.imscc", "size": len(content)},
            expected=201)

    def copy(self, record, content, offset=0, expected=200):
        response = self.client.put("/mentor/api/local-uploads/" + record["id"] + "?offset=" + str(offset),
                                   data=content, content_type="application/octet-stream",
                                   headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
        return response.json

    def approve(self):
        self.workspace = self.call(self.base + "/chat", {
            "provider": "guided", "message": "Keep the learning goals."})
        self.call(self.base + "/approve-direction", {"revision": self.workspace["revision"]})
        self.call(self.base + "/analyze", {
            "revision": self.workspace["revision"], "provider": "guided"}, expected=202)
        self.wait()
        self.workspace = self.call(self.base + "/chat", {
            "provider": "guided", "kind": "review", "message": "Keep this exact draft."})
        self.workspace = self.call(self.base + "/approve-build", {
            "draft_revision": self.workspace["draft_revision"]})

    def test_copy_progress_survives_reload_and_cancel_even_after_input_edit(self):
        record = self.reserve(b"abcdef")
        self.copy(record, b"abc")
        workspace = self.fetch()
        self.assertEqual(workspace["upload"]["id"], record["id"])
        self.assertEqual(workspace["upload"]["offset"], 3)
        self.assertEqual(workspace["upload"]["status"], "awaiting_upload")
        self.assertNotIn("path", workspace["upload"])
        self.call(self.base + "/local-uploads", {
            "revision": 1, "filename": "second.imscc", "size": 3}, expected=409)
        self.call(self.base + "/brief", {"revision": 1, "brief": "Changed"}, method="PUT")
        self.copy(record, b"def", offset=3, expected=409)
        self.call("/mentor/api/local-uploads/" + record["id"], method="DELETE")
        self.assertNotIn("upload", self.fetch())

    def test_real_local_preparation_attaches_media_and_removes_staging(self):
        content = cartridge_bytes()
        record = self.reserve(content)
        self.copy(record, content)
        self.call("/mentor/api/local-uploads/" + record["id"] + "/complete", {}, expected=202)
        workspace = self.wait()
        self.assertEqual(workspace["source_type"], "imscc")
        self.assertEqual(workspace["upload"]["status"], "ready")
        self.assertIsInstance(workspace["upload"]["created_at"], float)
        info = self.client.get(self.base + "/inventory").json
        self.assertEqual(info["course_items"], 1)
        self.assertTrue(list((self.root / "courses").rglob("*.png")))
        for _ in range(100):
            if not list((self.root / "uploads").rglob("source.imscc")):
                break
            time.sleep(.01)
        self.assertFalse(list((self.root / "uploads").rglob("source.imscc")))

    def test_local_preparation_failure_has_actionable_safe_message(self):
        content = b"not a zip"
        record = self.reserve(content)
        self.copy(record, content)
        self.call("/mentor/api/local-uploads/" + record["id"] + "/complete", {}, expected=202)
        workspace = self.wait()
        self.assertEqual(workspace["jobs"][-1]["status"], "failed")
        self.assertIn("Local preparation failed", workspace["jobs"][-1]["error"])
        self.assertEqual(workspace["upload"]["error"], workspace["jobs"][-1]["error"])
        self.assertEqual(workspace["revision"], 1)

    def test_other_browser_cannot_get_copy_or_trigger_site_lifecycle(self):
        record = self.reserve(b"abcdef")
        other = web.app.test_client()
        csrf = other.get("/mentor/api/session").json["csrf"]
        self.assertEqual(other.get("/mentor/api/local-uploads/" + record["id"]).status_code, 404)
        for action in ("start", "stop"):
            response = other.post(self.base + "/hax/" + action, json={},
                                  headers={"X-Mentor-CSRF": csrf})
            self.assertEqual(response.status_code, 404)

    def test_startup_path_is_not_a_browser_filesystem_api(self):
        source = self.root / "startup.imscc"
        source.write_bytes(cartridge_bytes())
        with patch.dict(os.environ, {"HAX_STARTUP_COURSE": str(source)}):
            selected = self.client.get("/mentor/api/session").json["local_source"]
            self.assertNotIn("path", selected)
            self.call(self.base + "/local-source", {
                "revision": 1, "source_id": "wrong", "path": "/etc/passwd"}, expected=404)
            self.call(self.base + "/local-source", {"revision": 1, "source_id": selected["id"]}, expected=202)
            self.assertEqual(self.wait()["upload"]["status"], "ready")
            self.assertEqual(source.read_bytes(), cartridge_bytes())

    def test_failed_automatic_start_keeps_built_artifact_and_exposes_json_error(self):
        self.approve()
        web.app.config["LOCAL_HAX_ENABLED"] = True
        class Sites:
            def stop(self, *args):
                return {"running": False, "url": None, "phase": "stopped", "error": None}
            def start(self, *args):
                raise RuntimeError("Managed HAX CLI missing. Reinstall the local application.")
            def status(self, *args):
                return {"running": False, "url": None, "phase": "stopped", "error": None}
            def shutdown(self):
                pass
        web.app.extensions.setdefault("local_hax_sites", {})[str(self.root.resolve())] = Sites()
        def build(workspace, destination, progress=None):
            destination.mkdir(parents=True)
            return {"directory": str(destination), "draft_revision": workspace["draft_revision"], "items": 1}
        with patch("local_hax.build_local_hax_site", side_effect=build):
            self.call(self.base + "/build", {"draft_revision": self.workspace["draft_revision"]}, expected=202)
            self.assertEqual(self.wait()["stage"], "built")
        for _ in range(100):
            status = self.client.get(self.base + "/hax").json
            if status["error"]:
                break
            time.sleep(.01)
        self.assertEqual(status["phase"], "failed")
        self.assertIn("Managed HAX CLI missing", status["error"])
        result = self.call(self.base + "/hax/start", {}, expected=503)
        self.assertIn("Managed HAX CLI missing", result["error"])
        self.assertEqual(self.fetch()["stage"], "built")

    def test_secondary_static_export_has_asset_budget_and_omission_record(self):
        export = self.root / "source" / "files"
        export.mkdir(parents=True)
        for name in ("small.png", "large.mp4", "private.txt"):
            (export / name).write_bytes(b"x" * (4 if name == "small.png" else 20))
            (export / (name + ".json")).write_text(json.dumps({"filename": name}))
        workspace = {**self.workspace, "export_dir": str(export.parent), "draft_revision": 1,
                     "draft": {"items": [{"id": "one", "title": "One", "html":
                         '<p>Approved</p><img src="files/small.png"><video src="files/large.mp4"></video>'}]}}
        result = build_artifact(workspace, self.root / "artifact", asset_budget=8)
        self.assertEqual(result["omitted_preview_assets"], 1)
        with zipfile.ZipFile(result["path"]) as archive:
            self.assertIn("files/small.png", archive.namelist())
            self.assertNotIn("files/large.mp4", archive.namelist())
            self.assertNotIn("files/private.txt", archive.namelist())
            self.assertIn("omits 1", json.loads(archive.read("approval.json"))["warnings"][-1])

    def test_application_lease_rejects_concurrent_recovery(self):
        with application_lease(self.root):
            with self.assertRaisesRegex(RuntimeError, "already open"):
                with application_lease(self.root):
                    pass
        with application_lease(self.root):
            pass


if __name__ == "__main__":
    unittest.main()
