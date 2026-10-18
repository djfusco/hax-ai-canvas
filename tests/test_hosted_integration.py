"""Hosted integration against recording PostgreSQL and in-memory private Blob.

No Vercel, PostgreSQL, Sandbox, provider or Canvas network calls are made.
"""
from contextlib import nullcontext
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "python/app"), str(ROOT / "tests")]
import app as web
from hosted_backend import HostedBackend
from hosted_config import settings, sign_upload, verify_upload, HostedSetupError
from hosted_data import PostgresMentorStore, PostgresCredentialVault, PostgresUploads
from hosted_jobs import (_validate_persisted, execute_mentor, cleanup_batch,
                         start_preparation, stop_preparation)
from hosted_worker import finish
from mentor_store import WorkspaceConflict
from prepare_course import prepare_course
from test_hosted_data import MockDriver
from test_local_import import cartridge_bytes


def fixture_environment():
    return {
        "HAX_HOSTED_ENABLED": "1", "DATABASE_URL": "postgresql://fixture",
        "HAX_APP_ORIGIN": "https://mentor.example",
        "HAX_SESSION_SECRET": "test-session-" + "s" * 40,
        "HAX_UPLOAD_SIGNING_SECRET": "test-upload-" + "u" * 40,
        "HAX_INTERNAL_SECRET": "test-internal-" + "i" * 40,
        "HAX_BETA_ACCESS_CODE": "test-invitation-" + "b" * 40,
        "HAX_MENTOR_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "CRON_SECRET": "test-cron-" + "c" * 40, "BLOB_READ_WRITE_TOKEN": "test-blob",
    }


class MemoryBlob:
    def __init__(self):
        self.objects, self.events = {}, []
        self.delete_failure = False

    def head(self, path):
        data = self.objects[path]
        return {"pathname": path, "url": "https://fixture.private.blob.vercel-storage.com/" + path,
                "size": len(data), "etag": hashlib.sha256(data).hexdigest()}

    def download(self, path, destination, max_bytes, expected_size=None):
        data = self.objects[path]
        if len(data) > max_bytes or (expected_size is not None and len(data) != expected_size):
            raise ValueError("Transfer bounds exceeded.")
        Path(destination).write_bytes(data)
        self.events.append(("download", path))
        return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def put_file(self, path, local):
        data = Path(local).read_bytes()
        if path in self.objects and self.objects[path] != data:
            raise ValueError("Immutable object conflict.")
        self.objects[path] = data
        self.events.append(("persist", path))
        return {**self.head(path), "sha256": hashlib.sha256(data).hexdigest()}

    def delete(self, path):
        self.events.append(("delete", path))
        if self.delete_failure:
            raise RuntimeError("simulated delete failure")
        self.objects.pop(path, None)


class FakeHostedBackend(HostedBackend):
    def __init__(self, config, directory):
        self.config = config
        self.driver = MockDriver(Path(directory) / "postgres.sqlite")
        self.store = PostgresMentorStore(config.database_url, connect=self.driver)
        self.vault = PostgresCredentialVault(config.database_url, config.encryption_key, connect=self.driver)
        self.uploads = PostgresUploads(config.database_url, connect=self.driver)
        self.blob = MemoryBlob()
        self.dispatched = []

    def dispatch_upload(self, upload_id):
        self.dispatched.append(upload_id)
        return "run-" + upload_id

    def dispatch_mentor(self, owner, workspace, kind, provider="guided"):
        return execute_mentor(owner, workspace["id"], workspace["active_job"]["id"],
                              kind, provider, self)


class HostedApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = fixture_environment()
        self.environ = patch.dict(os.environ, self.env, clear=True)
        self.environ.start()
        self.service = FakeHostedBackend(settings(), self.temp.name)
        self.old_factory = web.app.config.get("HOSTED_BACKEND_FACTORY")
        self.old_limiter = web.app.config.get("HOSTED_LOGIN_LIMITER")
        web.app.config.update(TESTING=True, HOSTED_BACKEND_FACTORY=lambda config: self.service,
                              HOSTED_LOGIN_LIMITER=lambda config, peer: True)
        self.client = web.app.test_client()
        session = self.get("/mentor/api/session").json
        self.csrf = session["csrf"]
        self.assertFalse(session["authenticated"])
        self.workspace = None

    def tearDown(self):
        self.environ.stop()
        for name, previous in (("HOSTED_BACKEND_FACTORY", self.old_factory),
                               ("HOSTED_LOGIN_LIMITER", self.old_limiter)):
            if previous is None:
                web.app.config.pop(name, None)
            else:
                web.app.config[name] = previous
        self.temp.cleanup()

    def get(self, path, client=None):
        return (client or self.client).get(path, base_url="https://mentor.example")

    def request(self, path, data=None, method="POST", expected=200, headers=None):
        response = self.client.open(
            path, method=method, json=data, base_url="https://mentor.example",
            headers={"X-Mentor-CSRF": self.csrf, **(headers or {})},
        )
        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
        return response.json

    def login(self):
        return self.request("/mentor/api/login", {"code": self.env["HAX_BETA_ACCESS_CODE"]})

    def create(self):
        self.login()
        self.workspace = self.request("/mentor/api/workspaces", {"title": "Hosted course"}, expected=201)
        self.base = "/mentor/api/workspaces/" + self.workspace["id"]
        return self.workspace

    def intent(self):
        raw = cartridge_bytes()
        intent = self.request(self.base + "/uploads", {
            "revision": self.workspace["revision"], "filename": "course.imscc",
            "size": len(raw), "kind": "course",
        }, expected=201)
        return raw, intent

    def test_admission_and_legacy_endpoint_isolation(self):
        self.request("/mentor/api/workspaces", {"title": "Unauthenticated"}, expected=401)
        self.request("/mentor/api/login", {"code": "wrong"}, expected=401)
        self.login()
        self.assertTrue(self.get("/mentor/api/session").json["authenticated"])
        for path in ("/setup", "/run", "/history", "/import"):
            self.assertEqual(self.get(path).status_code, 404)
        response = self.get("/mentor")
        self.assertIn(b"hosted-upload.js", response.data)
        cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)

    def test_upload_ticket_is_integer_expiry_and_not_storage_authority(self):
        self.create()
        raw, intent = self.intent()
        ticket = verify_upload(intent["ticket"], self.env["HAX_UPLOAD_SIGNING_SECRET"])
        self.assertEqual(type(ticket["expires_at"]), int)
        self.assertEqual(ticket["size"], len(raw))
        self.assertEqual(ticket["workspace_id"], self.workspace["id"])
        self.assertNotIn(self.env["BLOB_READ_WRITE_TOKEN"], intent["ticket"])
        self.assertNotIn(self.env["HAX_INTERNAL_SECRET"], intent["ticket"])
        self.assertEqual(self.service.store.get(ticket["owner"], self.workspace["id"])["stage"], "uploading")
        self.request(self.base + "/source", {}, expected=400)

    def test_browser_completion_uses_actual_private_head(self):
        self.create()
        raw, intent = self.intent()
        self.service.blob.objects[intent["pathname"]] = b"wrong-size"
        self.request("/mentor/api/uploads/" + intent["upload_id"] + "/complete", {}, expected=400)
        self.assertEqual(self.service.dispatched, [])
        self.service.blob.objects[intent["pathname"]] = raw
        self.request("/mentor/api/uploads/" + intent["upload_id"] + "/complete",
                     {"url": "https://untrusted.invalid/blob"}, expected=202)
        self.assertEqual(self.service.dispatched, [intent["upload_id"]])
        self.request("/mentor/api/uploads/" + intent["upload_id"] + "/complete", {}, expected=202)
        self.assertEqual(len(self.service.dispatched), 1)

    def test_verified_callback_requires_internal_secret_and_persisted_claims(self):
        self.create()
        raw, intent = self.intent()
        self.service.blob.objects[intent["pathname"]] = raw
        path = "/mentor/internal/upload-complete"
        self.request(path, {"ticket": intent["ticket"]}, expected=401)
        self.request(path, {"ticket": intent["ticket"], "blob": {"url": "https://ignored.invalid"}},
                     headers={"Authorization": "Bearer " + self.env["HAX_INTERNAL_SECRET"]}, expected=202)
        self.assertEqual(len(self.service.dispatched), 1)

    def test_different_browser_cannot_read_or_complete_upload(self):
        self.create()
        raw, intent = self.intent()
        second = web.app.test_client()
        session = self.get("/mentor/api/session", second).json
        headers = {"X-Mentor-CSRF": session["csrf"]}
        second.post("/mentor/api/login", json={"code": self.env["HAX_BETA_ACCESS_CODE"]},
                    headers=headers, base_url="https://mentor.example")
        self.assertEqual(self.get(self.base, second).status_code, 404)
        response = second.post("/mentor/api/uploads/" + intent["upload_id"] + "/complete",
                               json={}, headers=headers, base_url="https://mentor.example")
        self.assertEqual(response.status_code, 404)

    def process(self):
        raw, intent = self.intent()
        self.service.blob.objects[intent["pathname"]] = raw
        self.request("/mentor/api/uploads/" + intent["upload_id"] + "/complete", {}, expected=202)
        record = self.service.uploads.claim(intent["upload_id"], 2400, worker_id="test-run")
        with tempfile.TemporaryDirectory() as temp:
            source, package = Path(temp) / "course.imscc", Path(temp) / "course.haxcourse"
            source.write_bytes(raw)
            prepare_course(source, package)
            result = finish(record, package, self.service.uploads,
                            lambda rec, path, receipt: _validate_persisted(self.service, rec, path, receipt),
                            blob=self.service.blob)
        self.workspace = self.get(self.base).json
        return record, result

    def test_persist_validate_attach_then_delete_raw_and_stable_hydration(self):
        self.create()
        record, _ = self.process()
        saved = self.service.uploads.internal_get(record["id"])
        self.assertEqual(saved["status"], "ready")
        self.assertEqual(saved["cleanup_status"], "deleted")
        self.assertNotIn(record["pathname"], self.service.blob.objects)
        events = self.service.blob.events
        self.assertLess(next(i for i, e in enumerate(events) if e[0] == "persist"),
                        next(i for i, e in enumerate(events) if e[0] == "delete"))
        self.assertLess(next(i for i, e in enumerate(events) if e[0] == "download"),
                        next(i for i, e in enumerate(events) if e[0] == "delete"))
        owner = record["owner"]
        workspace = self.service.store.get(owner, self.workspace["id"])
        from mentor_service import source_items
        with self.service.materialize(workspace) as first:
            first_id = source_items(first)[0][0]["id"]
        with self.service.materialize(workspace) as second:
            self.assertEqual(first_id, source_items(second)[0][0]["id"])
        self.assertEqual(self.get(self.base + "/inventory").json["course_items"], 1)

    def test_cleanup_failure_retains_ready_result_and_retries(self):
        self.create()
        self.service.blob.delete_failure = True
        record, _ = self.process()
        saved = self.service.uploads.internal_get(record["id"])
        self.assertEqual(saved["status"], "ready")
        self.assertEqual(saved["cleanup_status"], "error")
        self.assertIn(record["pathname"], self.service.blob.objects)
        self.service.blob.delete_failure = False
        controller = SimpleNamespace(destroy=lambda record: None)
        cleanup_batch(self.service, controller=controller)
        self.assertNotIn(record["pathname"], self.service.blob.objects)
        self.assertEqual(self.service.uploads.internal_get(record["id"])["cleanup_status"], "deleted")

    def claimed_upload(self):
        self.create()
        raw, intent = self.intent()
        self.service.blob.objects[intent["pathname"]] = raw
        self.request("/mentor/api/uploads/" + intent["upload_id"] + "/complete", {}, expected=202)
        return self.service.uploads.claim(intent["upload_id"], 2400, worker_id="test-run")

    def test_cancel_between_sandbox_creation_and_save_retains_cleanup_handle(self):
        record = self.claimed_upload()
        destroyed = []

        def start(current):
            reservation = self.service.uploads.internal_get(current["id"])["sandbox"]
            self.assertEqual(reservation["name"], "hax-" + current["id"] + "-1")
            self.assertEqual(reservation["process_id"], "")
            self.service.uploads.cancel(current["owner"], current["id"])
            return {**reservation, "process_id": "started-process"}

        controller = SimpleNamespace(start=start, destroy=lambda rec: destroyed.append(rec["sandbox"]["name"]))
        with self.assertRaises(WorkspaceConflict):
            start_preparation(record["id"], 1, self.service, controller)
        self.assertEqual(stop_preparation(record["id"], 1, self.service, controller)["status"], "stopped")
        self.assertEqual(destroyed, ["hax-" + record["id"] + "-1"])
        self.assertEqual(self.service.uploads.internal_get(record["id"])["sandbox_cleanup_status"], "deleted")

    def test_empty_process_reservation_retries_creation_then_does_not_restart(self):
        record = self.claimed_upload()
        reservation = {"name": "hax-" + record["id"] + "-1", "process_id": "",
                       "phase": "setup", "started_at": time.time()}
        self.service.uploads.save_sandbox(record["id"], reservation, attempt=1)
        starts = []

        def start(current):
            starts.append(current["id"])
            self.assertIsNone(current["sandbox"])
            return {**reservation, "process_id": "started-process"}

        controller = SimpleNamespace(start=start)
        start_preparation(record["id"], 1, self.service, controller)
        start_preparation(record["id"], 1, self.service, controller)
        self.assertEqual(starts, [record["id"]])

    def test_cleanup_reclaimed_previous_sandbox_without_current_sandbox(self):
        record = self.claimed_upload()
        old = {"name": "hax-" + record["id"] + "-1", "process_id": "old-process",
               "phase": "setup", "started_at": time.time()}
        self.service.uploads.save_sandbox(record["id"], old, attempt=1)
        self.service.uploads.clock = lambda: record["lease_until"] + 1
        reclaimed = self.service.uploads.claim(record["id"], 2400, worker_id="next-run")
        self.assertIsNone(reclaimed["sandbox"])
        self.service.uploads.cancel(record["owner"], record["id"])
        destroyed = []
        controller = SimpleNamespace(destroy=lambda rec: destroyed.append(rec["sandbox"]["name"])
                                     if rec.get("sandbox") else None)
        cleanup_batch(self.service, controller)
        self.assertEqual(destroyed, [old["name"]])
        self.assertEqual(self.service.uploads.internal_get(record["id"])["sandbox_cleanup_status"], "deleted")

    def test_hosted_guided_conversations_are_dispatched_with_both_gates(self):
        self.create()
        self.workspace = self.request(self.base + "/chat", {
            "message": "Teach beginners to reason about evidence.", "kind": "interview", "provider": "guided"
        }, expected=202)
        self.workspace = self.get(self.base).json
        self.request(self.base + "/approve-direction", {"revision": self.workspace["revision"]})
        self.request(self.base + "/analyze",
                     {"revision": self.workspace["revision"], "provider": "guided"}, expected=202)
        self.workspace = self.get(self.base).json
        self.assertEqual(self.workspace["stage"], "draft")
        self.request(self.base + "/approve-build", {"draft_revision": self.workspace["draft_revision"]}, expected=409)
        self.request(self.base + "/chat",
                     {"message": "I reviewed the snapshot.", "kind": "review", "provider": "guided"}, expected=202)
        self.workspace = self.get(self.base).json
        self.request(self.base + "/approve-build", {"draft_revision": self.workspace["draft_revision"]})
        self.request(self.base + "/build", {"draft_revision": self.workspace["draft_revision"]}, expected=202)
        self.workspace = self.get(self.base).json
        self.assertEqual(self.workspace["stage"], "built")
        self.assertNotIn("blob_path", self.workspace["artifact"])
        response = self.get(self.base + "?draft_revision=" + str(self.workspace["draft_revision"]))
        self.assertTrue(response.is_streamed)
        self.assertTrue(response.json["keep_draft"])
        self.assertNotIn("draft", response.json)


class HostedConfigurationTests(unittest.TestCase):
    def test_missing_and_invalid_config_never_echoes_values(self):
        with self.assertRaises(HostedSetupError) as missing:
            settings({})
        self.assertIn("DATABASE_URL", str(missing.exception))
        env = fixture_environment()
        env["HAX_APP_ORIGIN"] = "https://user:secret@wrong.example"
        with self.assertRaises(HostedSetupError) as invalid:
            settings(env)
        self.assertNotIn("secret@", str(invalid.exception))

    def test_ticket_tamper_expiry_and_cross_language_payload_shape(self):
        record = {"id": "a" * 32, "owner": "b" * 64, "workspace_id": "c" * 32,
                  "pathname": "raw/" + "b" * 64 + "/" + "c" * 32 + "/" + "a" * 32 + "/source.imscc",
                  "size": 2 * 1024**3, "kind": "course", "token_expires_at": time.time() + 3600}
        signed = sign_upload(record, "test-signing-key")
        decoded = verify_upload(signed, "test-signing-key")
        self.assertEqual(decoded["size"], 2 * 1024**3)
        with self.assertRaises(ValueError):
            verify_upload(signed + "x", "test-signing-key")
        with self.assertRaises(ValueError):
            verify_upload(signed, "test-signing-key", now=time.time() + 4000)


if __name__ == "__main__":
    unittest.main()
