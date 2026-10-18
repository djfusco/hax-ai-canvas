"""Hosted storage and preparation boundary; SDK imports are lazy for offline tests.

Verified against vercel 0.12.0. create_process is the published detached API
(there is no start_process). No guest output is consumed. Only bounded prepared
results cross back into the Function. Callers persist every returned sandbox
metadata update with their ledger's attempt fencing.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import quote

from sandbox_prepare import CHUNK, DEPENDENCIES, MAX_INPUT, MAX_RESULT, ROOT, digest, private_url


def blob_path(path):
    if (not isinstance(path, str) or len(path) > 1024
            or not re.fullmatch(r"(raw|prepared|artifacts)/[A-Za-z0-9_./-]+", path)
            or any(p in ("", ".", "..") for p in path.split("/"))):
        raise ValueError("Invalid registered Blob pathname")
    return path


def _field(value, key):
    return value[key] if isinstance(value, dict) else getattr(value, key)


def _copy_bounded(stream, destination, maximum, expected=None):
    total, sha = 0, hashlib.sha256()
    created = False
    try:
        with Path(destination).open("xb") as output:
            created = True
            while True:
                remaining = maximum if expected is None else min(maximum, expected)
                chunk = stream.read(min(CHUNK, remaining - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum or (expected is not None and total > expected):
                    raise ValueError("Result exceeds transfer limit")
                output.write(chunk)
                sha.update(chunk)
        if expected is not None and total != expected:
            raise ValueError("Transfer size mismatch")
        return {"sha256": sha.hexdigest(), "size": total}
    except Exception:
        if created:
            Path(destination).unlink(missing_ok=True)
        raise


class SandboxStartError(RuntimeError):
    """Sanitized failure carrying only a deterministic cleanup handle."""

    def __init__(self, sandbox, cleanup_pending=True):
        super().__init__("Sandbox setup failed")
        self.sandbox = sandbox
        self.cleanup_pending = cleanup_pending


class VercelBlobStore:
    """Private-only, pathname-only adapter. Never accepts caller-supplied URLs."""

    def __init__(self, sdk=None):
        if sdk is None:
            from vercel.blob import sync as sdk
        self.sdk = sdk

    def head(self, path):
        path = blob_path(path)
        result = self.sdk.head_blob(path)
        receipt = {key: _field(result, key) for key in ("pathname", "url", "size", "etag")}
        if receipt["pathname"] != path:
            raise ValueError("Blob pathname mismatch")
        parsed = private_url(receipt["url"])
        if parsed.path != "/" + quote(path, safe="/"):
            raise ValueError("Blob URL pathname mismatch")
        if type(receipt["size"]) is not int or receipt["size"] < 0:
            raise ValueError("Invalid Blob size")
        return receipt

    def download(self, path, destination, max_bytes, expected_size=None):
        path = blob_path(path)
        if type(max_bytes) is not int or not 0 < max_bytes <= MAX_INPUT:
            raise ValueError("Invalid download bound")
        metadata = self.head(path)
        expected = metadata["size"] if expected_size is None else expected_size
        if type(expected) is not int or not 0 <= expected <= max_bytes or metadata["size"] != expected:
            raise ValueError("Blob size exceeds limit or changed")
        with self.sdk.stream_blob(path, access="private") as download:
            # SDK download is an iterator, not a file-like reader.
            total, sha = 0, hashlib.sha256()
            created = False
            try:
                with Path(destination).open("xb") as output:
                    created = True
                    for chunk in download:
                        total += len(chunk)
                        if total > max_bytes or total > expected:
                            raise ValueError("Blob exceeds transfer limit")
                        sha.update(chunk)
                        output.write(chunk)
                if total != expected:
                    raise ValueError("Blob size mismatch")
            except Exception:
                if created:
                    Path(destination).unlink(missing_ok=True)
                raise
        return {"sha256": sha.hexdigest(), "size": total}

    def put_file(self, path, local):
        path = blob_path(path)
        original = digest(local)
        try:
            with Path(local).open("rb") as stream:
                result = self.sdk.put_blob(
                    path, stream, access="private", content_length=original["size"],
                    add_random_suffix=False, allow_overwrite=False,
                    content_type="application/octet-stream", cache_control_max_age=0)
        except Exception:
            # A replay may find the immutable attempt object already published.
            # Also recover a lost PUT acknowledgement. Do not assume the server
            # returns a particular duplicate-object status. HEAD alone is not a
            # content proof; verify it by bounded streaming.
            head = self.head(path)
            if head["size"] != original["size"] or head["size"] > MAX_RESULT:
                raise ValueError("Existing prepared Blob mismatch") from None
            with tempfile.TemporaryDirectory(prefix="hax-verify-") as directory:
                actual = self.download(path, Path(directory) / "result", MAX_RESULT, head["size"])
            if actual != original:
                raise ValueError("Existing prepared Blob mismatch") from None
            return dict(head, sha256=actual["sha256"])
        head = self.head(path)
        if (head["size"] != original["size"] or head["etag"] != _field(result, "etag")
                or _field(result, "pathname") != path or head["url"] != _field(result, "url")):
            raise ValueError("Stored Blob verification failed")
        return dict(head, sha256=original["sha256"])

    def delete(self, path):
        path = blob_path(path)
        try:
            self.sdk.delete_blob(path)
        except self.sdk.BlobNotFoundError:
            return None


class VercelPreparationWorker:
    def __init__(self, blob=None, sandbox_api=None, source_root=None):
        self.blob = blob if blob is not None else VercelBlobStore()
        if sandbox_api is None:
            from vercel.sandbox import sync as sandbox_api
        self.api = sandbox_api
        self.source_root = Path(source_root) if source_root else Path(__file__).parent

    def _policy(self, url=None):
        api = self.api
        if url is None:
            return api.NetworkPolicy.deny_all()
        parsed = private_url(url)
        # Broker credentials exist only in the external network policy.
        token = os.environ["BLOB_READ_WRITE_TOKEN"]
        rule = api.NetworkPolicyRule(
            match=api.NetworkPolicyRequestMatcher(
                path=api.NetworkPolicyMatcher("exact", parsed.path), method=("GET",)),
            transform=(api.NetworkPolicyTransform(
                headers={"Authorization": "Bearer " + token, "Host": parsed.hostname}),))
        fallback = api.NetworkPolicyRule(response=api.NetworkPolicyResponse(status_code=403))
        return api.NetworkPolicy.custom({parsed.hostname: (rule, fallback)})

    def _launch(self, box, phase):
        return box.create_process(
            "python3", [str(ROOT / "sandbox_prepare.py"), phase],
            cwd=str(ROOT), env={}, kill_after=900,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    def _checkpoint(self, box, metadata):
        box.fs.write_text(str(ROOT / "controller.json"), json.dumps(metadata))
        return metadata

    def _metadata(self, box):
        with box.fs.open(str(ROOT / "controller.json"), "rb") as stream:
            data = stream.read(4097)
        if len(data) > 4096:
            raise ValueError("Invalid controller checkpoint")
        meta = json.loads(data)
        if (set(meta) != {"name", "process_id", "phase", "started_at"}
                or meta["name"] != box.name
                or meta["phase"] not in ("setup", "download", "locking", "prepare")):
            raise ValueError("Invalid controller checkpoint")
        return meta

    def start(self, record):
        if record.get("sandbox"):
            return dict(record["sandbox"])
        pathname = blob_path(record["pathname"])
        if not pathname.startswith("raw/"):
            raise ValueError("Preparation requires a registered raw path")
        raw = self.blob.head(pathname)
        if private_url(raw["url"]).path != "/" + quote(pathname, safe="/"):
            raise ValueError("Raw Blob URL pathname mismatch")
        expected = record["size"]
        if type(expected) is not int or not 0 < expected <= MAX_INPUT or raw["size"] != expected:
            raise ValueError("Registered source size mismatch")
        kind = record["kind"]
        if kind not in ("course", "material"):
            raise ValueError("Invalid upload kind")
        if kind == "course" and Path(record["filename"]).suffix.lower() == ".haxcourse":
            kind = "prepared"
        if record.get("blob") != raw:
            raise ValueError("Registered source metadata mismatch")
        manifest = {"url": raw["url"], "expected_size": expected, "kind": kind,
                    "filename": record["filename"], "role": record.get("role", "reference")}
        if not isinstance(manifest["filename"], str) or len(manifest["filename"]) > 255:
            raise ValueError("Invalid source filename")
        # Setup has no user data and can only access pinned dependency sources.
        setup_policy = self.api.NetworkPolicy.custom({"pypi.org": (), "files.pythonhosted.org": ()})
        name = "hax-" + record["id"] + "-" + str(record["attempts"])
        started_at = time.time()
        try:
            box = self.api.create_sandbox(
                name=name, execution_time_limit=900, env={}, network_policy=setup_policy)
        except Exception:
            # Named create is unique per attempt. Recover a completed checkpoint
            # after an uncertain response; never create an alternate-name VM.
            try:
                return self._metadata(self.api.get_sandbox(name=name))
            except Exception:
                raise SandboxStartError({
                    "name": name, "process_id": "", "phase": "setup",
                    "started_at": started_at}) from None
        try:
            box.fs.mkdir(str(ROOT))
            for name in ("sandbox_prepare.py", "prepare_course.py", "course_package.py",
                         "imscc_import.py", "mentor_materials.py"):
                box.fs.write_bytes(str(ROOT / name), (self.source_root / name).read_bytes())
            box.fs.write_text(str(ROOT / "manifest.json"), json.dumps(manifest, ensure_ascii=True))
            process = box.create_process(
                "python3", ["-m", "pip", "install", "--disable-pip-version-check",
                            "--no-input", *DEPENDENCIES],
                env={}, kill_after=900, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return self._checkpoint(box, {
                "name": box.name, "process_id": process.id,
                "phase": "setup", "started_at": started_at})
        except Exception:
            cleanup_pending = True
            try:
                box.destroy(delete_orphan_snapshots=True)
                cleanup_pending = False
            except Exception:
                pass
            raise SandboxStartError({
                "name": box.name, "process_id": "", "phase": "setup",
                "started_at": started_at}, cleanup_pending) from None

    def poll(self, record):
        meta = dict(record["sandbox"])
        if time.time() - meta["started_at"] >= 900:
            return {"done": True, "success": False, "error": "Preparation deadline exceeded"}
        box = self.api.get_sandbox(name=meta["name"])
        # Remote checkpoint wins over a stale Workflow/ledger response. In
        # particular, a replay of setup must never restore egress after parsing.
        meta = self._metadata(box)
        if time.time() - meta["started_at"] >= 900:
            return {"done": True, "success": False, "error": "Preparation deadline exceeded"}
        if meta["phase"] == "locking":
            box.update_network_policy(self._policy())
            process = self._launch(box, "prepare")
            meta.update(phase="prepare", process_id=process.id)
            self._checkpoint(box, meta)
            return {"done": False, "sandbox": meta}
        process = box.get_process(meta["process_id"]).refresh()
        status = getattr(process.status, "value", process.status)
        if status != "exited":
            return {"done": False, "sandbox": meta}
        if process.returncode != 0:
            return {"done": True, "success": False, "error": "Preparation failed"}
        if meta["phase"] == "setup":
            raw = self.blob.head(record["pathname"])
            if raw != record["blob"] or raw["size"] != record["size"]:
                return {"done": True, "success": False, "error": "Source changed"}
            box.update_network_policy(self._policy(raw["url"]))
            process = self._launch(box, "download")
            meta.update(phase="download", process_id=process.id)
            self._checkpoint(box, meta)
            return {"done": False, "sandbox": meta}
        if meta["phase"] == "download":
            # Persist the irreversible lock transition BEFORE making the API
            # call. A crash/retry here can only tighten policy, never reopen it.
            meta.update(phase="locking")
            self._checkpoint(box, meta)
            box.update_network_policy(self._policy())
            process = self._launch(box, "prepare")
            meta.update(phase="prepare", process_id=process.id)
            self._checkpoint(box, meta)
            return {"done": False, "sandbox": meta}
        if meta["phase"] != "prepare":
            raise ValueError("Invalid sandbox phase")
        with box.fs.open(str(ROOT / "result.json"), "rb") as stream:
            receipt = json.loads(stream.read(4097))
        if (set(receipt) != {"size", "sha256"} or type(receipt["size"]) is not int
                or not 0 < receipt["size"] <= MAX_RESULT
                or not re.fullmatch("[a-f0-9]{64}", receipt["sha256"])):
            raise ValueError("Invalid preparation receipt")
        fd, name = tempfile.mkstemp(prefix="hax-prepared-")
        os.close(fd)
        path = Path(name)
        path.unlink()
        try:
            with box.fs.open(str(ROOT / "result.bin"), "rb") as stream:
                actual = _copy_bounded(stream, path, MAX_RESULT, receipt["size"])
            if actual != receipt:
                raise ValueError("Preparation result digest mismatch")
            return {"done": True, "success": True, "local_result": path, "receipt": actual}
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def destroy(self, record):
        """Caller records cleanup failure and retries; never destroy before persist."""
        if record.get("sandbox"):
            try:
                self.api.get_sandbox(name=record["sandbox"]["name"]).destroy(
                    delete_orphan_snapshots=True)
            except self.api.SandboxApiError as exc:
                if exc.status_code != 404:
                    raise


def finish(record, local_result, uploads, attach_source, blob=None):
    """Persist -> prepared ledger -> validated atomic attachment -> raw deletion.

    Exceptions before attachment deliberately retain raw. Callback validates the
    persisted object and returns a source/material dictionary, never mutates DB.
    """
    blob = blob if blob is not None else VercelBlobStore()
    local_result = Path(local_result)
    maximum = 2 * 1024**2 if record["kind"] == "material" else MAX_RESULT
    if not 0 < local_result.stat().st_size <= maximum:
        raise ValueError("Prepared result exceeds limit")
    suffix = "result.json" if record["kind"] == "material" else "course.haxcourse"
    path = blob_path(
        f"prepared/{record['owner']}/{record['workspace_id']}/{record['id']}"
        f"/attempt-{record['attempts']}/{suffix}")
    receipt = blob.put_file(path, local_result)
    if digest(local_result) != {k: receipt[k] for k in ("sha256", "size")}:
        raise ValueError("Prepared content changed")
    uploads.prepared(record["id"], receipt, attempt=record["attempts"])
    attachment = attach_source(record, local_result, receipt)
    uploads.attach(record["id"], attachment, attempt=record["attempts"])
    ready = dict(record, state="ready")
    cleanup = cleanup_upload(ready, uploads, blob)
    return {"workspace_id": record["workspace_id"], "receipt": receipt, "cleanup": cleanup}


def cleanup_upload(record, uploads, blob, now=None):
    """Delete only this registered raw pathname; accepted delete is logical.

    Blob caches can serve stale HEAD after deletion, so success means SDK delete
    acceptance, not strong erasure. Ledger MUST reconcile terminal intents until
    client-token expiry + retention TTL to remove late/replayed uploads.
    """
    now = time.time() if now is None else now
    state = record.get("state", record.get("status"))
    expired = record.get("expires_at", float("inf")) <= now
    if state not in ("ready", "failed", "canceled", "expired") and not expired:
        return {"deleted": False, "pending": False}
    path = blob_path(record["pathname"])
    if not path.startswith("raw/"):
        raise ValueError("Cleanup only accepts registered raw paths")
    try:
        blob.delete(path)
    except Exception:
        uploads.record_cleanup(record["id"], False)
        return {"deleted": False, "pending": True}
    uploads.record_cleanup(record["id"], True)
    return {"deleted": True, "logical_deleted": True, "pending": False}
