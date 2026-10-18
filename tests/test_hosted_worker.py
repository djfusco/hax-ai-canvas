"""Offline tests: fake cloud clients only; optional installed-SDK signature checks."""
from contextlib import contextmanager
import hashlib
import inspect
import io
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
import hosted_worker as worker
import sandbox_prepare as guest


URL = "https://test.private.blob.vercel-storage.com/raw/owner/work/upload/source"


def record():
    raw = {"pathname": "raw/owner/work/upload/source", "url": URL, "size": 3, "etag": "tag"}
    return {"id": "upload", "owner": "owner", "workspace_id": "work", "attempts": 1,
            "pathname": raw["pathname"], "size": 3, "blob": raw, "kind": "course",
            "filename": "course.imscc", "state": "processing", "expires_at": time.time() + 1000}


class FakeBlobSDK:
    class BlobNotFoundError(Exception):
        pass
    class BlobPreconditionFailedError(Exception):
        pass

    def __init__(self):
        self.data = b"abc"
        self.extra = b""
        self.public = False
        self.deleted = []

    def head_blob(self, path):
        host = "public" if self.public else "private"
        return SimpleNamespace(pathname=path, size=len(self.data), etag="tag",
                               url=f"https://test.{host}.blob.vercel-storage.com/{path}")

    @contextmanager
    def stream_blob(self, path, *, access):
        assert access == "private"
        yield iter((self.data, self.extra))

    def put_blob(self, path, body, **kwargs):
        self.put_kwargs = kwargs
        self.body_type = type(body)
        self.data = body.read(1024)
        return self.head_blob(path)

    def delete_blob(self, path):
        self.deleted.append(path)


class Policy:
    @staticmethod
    def custom(allow):
        return SimpleNamespace(mode="custom", allow=allow)

    @staticmethod
    def deny_all():
        return SimpleNamespace(mode="deny-all", allow={})


class MemoryFS:
    def __init__(self):
        self.files = {}
        self.opened = []

    def mkdir(self, path):
        pass

    def write_bytes(self, path, data):
        self.files[path] = data

    def write_text(self, path, text):
        self.write_bytes(path, text.encode())

    def open(self, path, mode):
        self.opened.append(path)
        return io.BytesIO(self.files[path])


class FakeBox:
    def __init__(self):
        self.name = "hax-upload-1"
        self.fs = MemoryFS()
        self.processes = {}
        self.events = []
        self.destroy = Mock()

    def create_process(self, command, args, **kwargs):
        pid = str(len(self.processes) + 1)
        process = SimpleNamespace(id=pid, status="running", returncode=None)
        process.refresh = lambda: process
        self.processes[pid] = process
        self.events.append(("process", command, args, kwargs))
        return process

    def get_process(self, pid):
        return self.processes[pid]

    def complete(self, pid, returncode=0):
        self.processes[pid].status = "exited"
        self.processes[pid].returncode = returncode

    def update_network_policy(self, policy):
        self.events.append(("policy", policy))


class FakeSandboxApiError(Exception):
    def __init__(self, status_code):
        super().__init__("Mock Sandbox API error")
        self.status_code = status_code


def fake_api(box):
    api = SimpleNamespace(
        NetworkPolicy=Policy, NetworkPolicyRule=SimpleNamespace,
        NetworkPolicyTransform=SimpleNamespace, NetworkPolicyRequestMatcher=SimpleNamespace,
        NetworkPolicyMatcher=lambda kind, value: SimpleNamespace(kind=kind, value=value),
        NetworkPolicyResponse=SimpleNamespace, create_sandbox=Mock(return_value=box),
        get_sandbox=Mock(return_value=box), SandboxApiError=FakeSandboxApiError)
    return api


class BlobTests(unittest.TestCase):
    def setUp(self):
        self.sdk = FakeBlobSDK()
        self.blob = worker.VercelBlobStore(self.sdk)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.local = Path(self.directory.name) / "file"

    def test_path_validation_no_urls_or_traversal(self):
        for path in ("https://example.com/raw/a", "raw/../a", "raw//a", "raw/a?b",
                     "other/a", "raw/a\\b", "/raw/a", "raw/a%2fb", "raw/a/"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.blob.head(path)
        self.assertEqual(self.blob.head("prepared/a")["pathname"], "prepared/a")

    def test_private_head_only(self):
        self.sdk.public = True
        with self.assertRaises(ValueError):
            self.blob.head("raw/a")

    def test_streaming_download_hash_and_bounds(self):
        actual = self.blob.download("raw/a", self.local, 3, 3)
        self.assertEqual(actual, {"size": 3, "sha256": hashlib.sha256(b"abc").hexdigest()})
        self.assertEqual(self.local.read_bytes(), b"abc")

    def test_overflow_removes_partial_and_does_not_materialize(self):
        self.sdk.extra = b"d"
        with self.assertRaises(ValueError):
            self.blob.download("raw/a", self.local, 3, 3)
        self.assertFalse(self.local.exists())

    def test_expected_size_checked_before_stream(self):
        with self.assertRaises(ValueError):
            self.blob.download("raw/a", self.local, 100, 2)
        self.assertFalse(self.local.exists())

    def test_put_streams_private_then_verifies_head(self):
        self.local.write_bytes(b"abc")
        receipt = self.blob.put_file("prepared/a", self.local)
        self.assertEqual(receipt["sha256"], hashlib.sha256(b"abc").hexdigest())
        self.assertEqual(self.sdk.put_kwargs["access"], "private")
        self.assertFalse(self.sdk.put_kwargs["allow_overwrite"])
        self.assertNotEqual(self.sdk.body_type, bytes)
        self.blob.delete("raw/a")
        self.assertEqual(self.sdk.deleted, ["raw/a"])

    def test_missing_blob_delete_is_idempotent(self):
        self.sdk.delete_blob = Mock(side_effect=self.sdk.BlobNotFoundError)
        self.assertIsNone(self.blob.delete("raw/a"))

    def test_retry_existing_immutable_object_verifies_actual_hash(self):
        self.local.write_bytes(b"abc")
        self.sdk.put_blob = Mock(side_effect=self.sdk.BlobPreconditionFailedError)
        self.assertEqual(self.blob.put_file("prepared/a", self.local)["size"], 3)
        self.sdk.data = b"xyz"
        with self.assertRaises(ValueError):
            self.blob.put_file("prepared/a", self.local)

    def test_refuses_existing_download_destination(self):
        self.local.write_bytes(b"original")
        with self.assertRaises(FileExistsError):
            self.blob.download("raw/a", self.local, 3)
        # Never remove a destination the adapter did not create.
        self.assertEqual(self.local.read_bytes(), b"original")


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.box = FakeBox()
        self.api = fake_api(self.box)
        self.record = record()
        self.blob = Mock()
        self.blob.head.return_value = self.record["blob"]
        self.controller = worker.VercelPreparationWorker(self.blob, self.api)

    def start(self):
        self.record["sandbox"] = self.controller.start(self.record)
        return self.record["sandbox"]

    def advance(self):
        self.box.complete(self.record["sandbox"]["process_id"])
        result = self.controller.poll(self.record)
        if "sandbox" in result:
            self.record["sandbox"] = result["sandbox"]
        return result

    def test_detached_start_no_cloud_credentials_guest_or_wait(self):
        meta = self.start()
        self.assertEqual(meta["phase"], "setup")
        self.api.create_sandbox.assert_called_once()
        kwargs = self.api.create_sandbox.call_args.kwargs
        self.assertEqual(kwargs["env"], {})
        self.assertEqual(set(kwargs["network_policy"].allow), {"pypi.org", "files.pythonhosted.org"})
        event = self.box.events[0]
        self.assertEqual(event[1:3], ("python3", [
            "-m", "pip", "install", "--disable-pip-version-check", "--no-input", *guest.DEPENDENCIES]))
        self.assertEqual(event[3]["env"], {})
        self.assertNotIn(b"BLOB_READ_WRITE_TOKEN", b"".join(
            data for path, data in self.box.fs.files.items() if path.endswith("manifest.json")))

    def test_start_duplicate_and_running_poll(self):
        first = self.start()
        self.assertEqual(self.controller.start(self.record), first)
        self.api.create_sandbox.assert_called_once()
        self.assertFalse(self.controller.poll(self.record)["done"])
        self.assertEqual(len(self.box.events), 1)

    def test_setup_retry_recovers_unique_checkpoint(self):
        meta = self.start()
        self.record.pop("sandbox")
        self.api.create_sandbox.side_effect = RuntimeError("conflict")
        self.assertEqual(self.controller.start(self.record), meta)

    def test_setup_cleanup_failure_keeps_sanitized_retry_handle(self):
        self.box.fs.write_bytes = Mock(side_effect=RuntimeError("sensitive cloud error"))
        self.box.destroy.side_effect = RuntimeError("cleanup unavailable")
        with self.assertRaises(worker.SandboxStartError) as error:
            self.start()
        self.assertTrue(error.exception.cleanup_pending)
        self.assertEqual(error.exception.sandbox["name"], self.box.name)
        self.assertNotIn("sensitive", str(error.exception))

    def test_uncertain_create_keeps_same_name_cleanup_handle(self):
        self.api.create_sandbox.side_effect = RuntimeError("uncertain")
        self.api.get_sandbox.side_effect = RuntimeError("unavailable")
        with self.assertRaises(worker.SandboxStartError) as error:
            self.start()
        self.assertEqual(error.exception.sandbox["name"], "hax-upload-1")
        self.assertTrue(error.exception.cleanup_pending)

    @patch.dict("os.environ", {"BLOB_READ_WRITE_TOKEN": "test-token-not-real"}, clear=True)
    def test_broker_exact_get_fallback_and_lock_before_parse(self):
        self.start()
        result = self.advance()
        self.assertFalse(result["done"])
        policy = self.box.events[1][1]
        self.assertEqual(set(policy.allow), {"test.private.blob.vercel-storage.com"})
        rule, fallback = next(iter(policy.allow.values()))
        self.assertEqual(rule.match.method, ("GET",))
        self.assertEqual(rule.match.path.value, "/raw/owner/work/upload/source")
        self.assertEqual(rule.transform[0].headers["Host"], "test.private.blob.vercel-storage.com")
        self.assertEqual(fallback.response.status_code, 403)
        self.advance()
        self.assertEqual(self.box.events[3][1].mode, "deny-all")
        self.assertEqual(self.box.events[4][2][-1], "prepare")
        self.assertNotIn("test-token-not-real", repr([e for e in self.box.events if e[0] == "process"]))
        self.assertNotIn("test-token-not-real", repr(self.box.fs.files))

    @patch.dict("os.environ", {"BLOB_READ_WRITE_TOKEN": "test-token-not-real"}, clear=True)
    def test_stale_phase_replay_never_reopens_network(self):
        stale = dict(self.start())
        self.advance()
        self.advance()
        count = len(self.box.events)
        self.record["sandbox"] = stale
        self.assertFalse(self.controller.poll(self.record)["done"])
        self.assertEqual(len(self.box.events), count)

    @patch.dict("os.environ", {"BLOB_READ_WRITE_TOKEN": "test-token-not-real"}, clear=True)
    def test_failed_lock_retries_only_deny_all(self):
        self.start()
        self.advance()
        self.box.complete(self.record["sandbox"]["process_id"])
        original_update = self.box.update_network_policy
        self.box.update_network_policy = Mock(side_effect=RuntimeError("network"))
        with self.assertRaises(RuntimeError):
            self.controller.poll(self.record)
        checkpoint = json.loads(self.box.fs.files[str(guest.ROOT / "controller.json")])
        self.assertEqual(checkpoint["phase"], "locking")
        self.box.update_network_policy = original_update
        result = self.controller.poll(self.record)
        self.assertEqual(result["sandbox"]["phase"], "prepare")
        self.assertEqual(self.box.events[-2][1].mode, "deny-all")

    @patch.dict("os.environ", {"BLOB_READ_WRITE_TOKEN": "test-token-not-real"}, clear=True)
    def test_oversize_receipt_rejected_before_result_stream(self):
        self.start()
        self.advance()
        self.advance()
        self.box.complete(self.record["sandbox"]["process_id"])
        receipt = {"size": guest.MAX_RESULT + 1, "sha256": "a" * 64}
        self.box.fs.write_text(str(guest.ROOT / "result.json"), json.dumps(receipt))
        with self.assertRaises(ValueError):
            self.controller.poll(self.record)
        self.assertNotIn(str(guest.ROOT / "result.bin"), self.box.fs.opened)

    @patch.dict("os.environ", {"BLOB_READ_WRITE_TOKEN": "test-token-not-real"}, clear=True)
    def test_bounded_result_receipt_no_raw_download(self):
        self.start()
        self.advance()
        self.advance()
        result_bytes = b"bounded prepared package"
        receipt = {"size": len(result_bytes), "sha256": hashlib.sha256(result_bytes).hexdigest()}
        self.box.fs.write_bytes(str(guest.ROOT / "result.bin"), result_bytes)
        self.box.fs.write_text(str(guest.ROOT / "result.json"), json.dumps(receipt))
        result = self.advance()
        self.addCleanup(result["local_result"].unlink, missing_ok=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["receipt"], receipt)
        self.assertEqual(result["local_result"].read_bytes(), result_bytes)
        self.assertFalse(any(path.endswith("input.bin") for path in self.box.fs.opened))
        self.box.destroy.assert_not_called()
        self.controller.destroy(self.record)
        self.box.destroy.assert_called_once()

    def test_deadline_and_failures_are_content_free(self):
        self.start()
        self.box.complete("1", 1)
        self.assertEqual(self.controller.poll(self.record)["error"], "Preparation failed")
        self.record["sandbox"]["started_at"] = 0
        self.assertEqual(self.controller.poll(self.record)["error"], "Preparation deadline exceeded")

    def test_destroy_accepts_only_sdk_404_for_lookup_or_delete(self):
        self.start()
        for operation in (self.api.get_sandbox, self.box.destroy):
            for status in (404, 403, 500):
                with self.subTest(operation=operation, status=status):
                    operation.side_effect = FakeSandboxApiError(status)
                    if status == 404:
                        self.assertIsNone(self.controller.destroy(self.record))
                    else:
                        with self.assertRaises(FakeSandboxApiError):
                            self.controller.destroy(self.record)
                    operation.side_effect = None
            operation.side_effect = RuntimeError("unrelated failure")
            with self.assertRaises(RuntimeError):
                self.controller.destroy(self.record)
            operation.side_effect = None

    def test_prepared_manifest_and_source_metadata_check(self):
        self.record["filename"] = "x.haxcourse"
        self.start()
        manifest = json.loads(self.box.fs.files[str(guest.ROOT / "manifest.json")])
        self.assertEqual(manifest["kind"], "prepared")
        self.record.pop("sandbox")
        self.blob.head.return_value = dict(self.record["blob"], etag="changed")
        with self.assertRaises(ValueError):
            self.controller.start(self.record)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.record = record()
        self.events = []
        self.uploads = Mock()
        self.blob = Mock()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.local = Path(self.directory.name) / "result"
        self.local.write_bytes(b"prepared")
        self.receipt = dict(guest.digest(self.local), pathname="prepared/path", url=URL, etag="tag")
        self.blob.put_file.side_effect = lambda *a: self.events.append("persist") or self.receipt
        self.uploads.prepared.side_effect = lambda *a, **k: self.events.append("prepared")
        self.uploads.attach.side_effect = lambda *a, **k: self.events.append("attach")
        self.blob.delete.side_effect = lambda *a: self.events.append("delete")

    def callback(self, record, path, receipt):
        self.events.append("validate")
        return {"source_type": "prepared"}

    def test_persist_validate_attach_before_delete_with_fencing(self):
        result = worker.finish(self.record, self.local, self.uploads, self.callback, self.blob)
        self.assertEqual(self.events, ["persist", "prepared", "validate", "attach", "delete"])
        self.assertEqual(self.blob.put_file.call_args.args[0],
                         "prepared/owner/work/upload/attempt-1/course.haxcourse")
        self.uploads.prepared.assert_called_once_with("upload", self.receipt, attempt=1)
        self.assertEqual(self.uploads.attach.call_args.kwargs, {"attempt": 1})
        self.assertEqual(result["workspace_id"], "work")
        self.assertNotIn("workspace", result)

    def test_each_failure_before_attach_preserves_raw(self):
        for step in ("put_file", "prepared", "attach", "callback"):
            with self.subTest(step=step):
                blob, uploads = Mock(), Mock()
                blob.put_file.return_value = self.receipt
                callback = self.callback
                if step == "callback":
                    callback = Mock(side_effect=ValueError("validation"))
                else:
                    getattr(blob if step == "put_file" else uploads, step).side_effect = ValueError(step)
                with self.assertRaises(ValueError):
                    worker.finish(self.record, self.local, uploads, callback, blob)
                blob.delete.assert_not_called()

    def test_cleanup_retry_and_live_processing_guard(self):
        self.assertFalse(worker.cleanup_upload(self.record, self.uploads, self.blob)["deleted"])
        self.blob.delete.assert_not_called()
        self.record["state"] = "ready"
        self.blob.delete.side_effect = RuntimeError("network")
        self.assertTrue(worker.cleanup_upload(self.record, self.uploads, self.blob)["pending"])
        self.uploads.record_cleanup.assert_called_with("upload", False)
        self.blob.delete.side_effect = None
        self.assertTrue(worker.cleanup_upload(self.record, self.uploads, self.blob)["deleted"])
        self.uploads.record_cleanup.assert_called_with("upload", True)

    def test_expired_intent_and_only_registered_raw_path(self):
        self.record["expires_at"] = 0
        self.assertTrue(worker.cleanup_upload(self.record, self.uploads, self.blob)["deleted"])
        self.blob.delete.assert_called_once_with(self.record["pathname"])
        self.record["pathname"] = "prepared/other"
        with self.assertRaises(ValueError):
            worker.cleanup_upload(self.record, self.uploads, self.blob)


class GuestTests(unittest.TestCase):
    def test_real_synthetic_course_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_root, prepared_root = root / "raw", root / "prepared"
            raw_root.mkdir()
            prepared_root.mkdir()
            with zipfile.ZipFile(raw_root / "input.bin", "w") as archive:
                archive.writestr(
                    "imsmanifest.xml",
                    '<manifest identifier="synthetic"><metadata><title>Test</title></metadata>'
                    '<organizations/><resources><resource identifier="page" type="webcontent" '
                    'href="wiki_content/page.html"/></resources></manifest>')
                archive.writestr("wiki_content/page.html", "<title>Test</title><p>Evidence.</p>")
                archive.writestr("web_resources/movie.mp4", b"excluded synthetic media")
            (raw_root / "download.json").write_text(json.dumps(guest.digest(raw_root / "input.bin")))
            with patch("socket.create_connection", side_effect=AssertionError("Unexpected network")):
                receipt = guest.prepare({"kind": "course"}, raw_root)
                original = (raw_root / "result.bin").read_bytes()
                with zipfile.ZipFile(io.BytesIO(original)) as archive:
                    self.assertNotIn("web_resources/movie.mp4", archive.namelist())
                    manifest = json.loads(archive.read("haxcourse.json"))
                    self.assertEqual(manifest["counts"]["pages"], 1)
                (prepared_root / "input.bin").write_bytes(original)
                (prepared_root / "download.json").write_text(json.dumps(receipt))
                replay = guest.prepare({"kind": "prepared"}, prepared_root)
            self.assertEqual(replay, receipt)
            self.assertEqual((prepared_root / "result.bin").read_bytes(), original)
    def test_download_bounded_no_auth_no_redirects(self):
        for data, status, succeeds in ((b"abc", 200, True), (b"abcd", 200, False),
                                       (b"ab", 200, False), (b"abc", 302, False)):
            with self.subTest(data=data, status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                response = io.BytesIO(data)
                response.status = status
                response.getheader = lambda name, default=None: default
                connection = Mock()
                connection.getresponse.return_value = response
                factory = Mock(return_value=connection)
                manifest = {"url": URL, "expected_size": 3}
                with patch.object(guest.shutil, "disk_usage", return_value=SimpleNamespace(free=10**12)):
                    if succeeds:
                        result = guest.download(manifest, root, factory)
                        self.assertEqual(result, guest.digest(root / "input.bin"))
                        # Idempotent retry validates the existing bytes.
                        self.assertEqual(guest.download(manifest, root, factory), result)
                        factory.assert_called_once()
                    else:
                        with self.assertRaises(ValueError):
                            guest.download(manifest, root, factory)
                        self.assertFalse((root / "input.bin").exists())
                connection.request.assert_called_once_with(
                    "GET", "/raw/owner/work/upload/source", headers={"Accept-Encoding": "identity"})
                connection.close.assert_called_once()

    def test_disk_preflight_and_private_url_validation(self):
        for url in ("https://evil.test/raw/a", URL + "?token=x", URL + "#x",
                    URL.replace("https", "http"), URL.replace("test.", "user@")):
            with self.subTest(url=url), self.assertRaises(ValueError):
                guest.private_url(url)
        with tempfile.TemporaryDirectory() as directory:
            factory = Mock()
            with patch.object(guest.shutil, "disk_usage", return_value=SimpleNamespace(free=1)):
                with self.assertRaises(ValueError):
                    guest.download({"url": URL, "expected_size": 3}, Path(directory), factory)
            factory.assert_not_called()

    def test_material_extraction_bounded_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input.bin").write_bytes(b"evidence")
            (root / "download.json").write_text(json.dumps(guest.digest(root / "input.bin")))
            manifest = {"kind": "material", "filename": "notes.txt", "role": "policy"}
            result = guest.prepare(manifest, root)
            material = json.loads((root / "result.bin").read_text())
            self.assertEqual(material["text"], "evidence")
            self.assertEqual(material["role"], "policy")
            self.assertEqual(guest.prepare(manifest, root), result)

    def test_prepared_package_validated_but_original_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = b"opaque supplied package"
            (root / "input.bin").write_bytes(content)
            (root / "download.json").write_text(json.dumps(guest.digest(root / "input.bin")))
            importer = Mock()
            module = SimpleNamespace(import_course_package=importer)
            with patch.dict(sys.modules, {"course_package": module}):
                guest.prepare({"kind": "prepared"}, root)
            importer.assert_called_once_with(root / "input.bin", root / "validated")
            self.assertEqual((root / "result.bin").read_bytes(), content)

    def test_receipt_tampering_rejected_before_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input.bin").write_bytes(b"abc")
            (root / "download.json").write_text('{"size":3,"sha256":"wrong"}')
            with self.assertRaises(ValueError):
                guest.prepare({"kind": "course"}, root)


class PublishedBindingTests(unittest.TestCase):
    def test_actual_sdk_bindings_when_installed(self):
        try:
            from vercel.blob import sync as blob
            from vercel.sandbox import sync as sandbox
        except ImportError:
            self.skipTest("optional published vercel SDK not installed")
        # Inspect only; never instantiate a service/session or perform HTTP.
        inspect.signature(blob.put_blob).bind("prepared/a", io.BytesIO(b"a"),
                                               access="private", content_length=1)
        inspect.signature(sandbox.create_sandbox).bind(
            name="hax-test", execution_time_limit=900, env={},
            network_policy=sandbox.NetworkPolicy.deny_all())
        inspect.signature(sandbox.SyncSandbox.create_process).bind(
            None, "python3", ["trusted.py"], cwd=str(guest.ROOT), env={},
            kill_after=900, stdout=-3, stderr=-3)
        inspect.signature(sandbox.SyncSandbox.get_process).bind(None, "process")
        self.assertTrue(hasattr(sandbox.SyncProcess, "refresh"))
        self.assertTrue(hasattr(sandbox.SyncProcess, "returncode"))
        error = sandbox.SandboxApiError(SimpleNamespace(status_code=404), "test-only")
        self.assertEqual(error.status_code, 404)
        api = fake_api(FakeBox())
        for name in ("NetworkPolicy", "NetworkPolicyRule", "NetworkPolicyTransform",
                     "NetworkPolicyRequestMatcher", "NetworkPolicyMatcher", "NetworkPolicyResponse"):
            setattr(api, name, getattr(sandbox, name))
        controller = worker.VercelPreparationWorker(Mock(), api)
        with patch.dict("os.environ", {"BLOB_READ_WRITE_TOKEN": "test-only"}, clear=True):
            policy = controller._policy(URL)
        self.assertEqual(policy.mode, "custom")
        self.assertEqual(next(iter(policy.allow.values()))[1].response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
