"""Private local disk imports; no HTTP, credentials, network, or approval authority.

Only ``complete`` returns a private path. Callers must never serialize that path
to a browser. Workspace/revision fencing and CSRF belong to the route layer.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import threading
import time
from types import SimpleNamespace
import uuid
import zipfile
import zlib

from course_package import (
    DOCUMENT_EXTENSIONS, MAX_PACKAGE_BYTES, MIB,
    _central_directory_bound, import_course_package, inspect_zip,
)
from imscc_import import IMSCCImportError
from local_assets import copy_passive_document

MAX_CHUNK_BYTES = 8 * MIB
MAX_ARCHIVE_BYTES = 8 * 1024**3
DISK_RESERVE = 16 * MIB
COPY_CHUNK = 64 * 1024
MEDIA_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico",
    ".mp3", ".mp4", ".m4a", ".m4v", ".wav", ".ogg", ".webm", ".mov",
}
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


class LocalImportError(ValueError):
    """Invalid or unsafe local transfer."""


class LocalImportNotFound(LocalImportError):
    """Unknown import in the requesting owner's namespace."""


class LocalImportConflict(LocalImportError):
    """Offset or lifecycle conflict; fetch current state before retrying."""


def _disk_check(directory, required):
    if shutil.disk_usage(directory).free < required + DISK_RESERVE:
        raise LocalImportError("Not enough free local disk space; free space and retry.")


def _private_directory(path):
    if path.is_symlink():
        raise LocalImportError("Managed directories cannot be symbolic links.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise LocalImportError("Invalid managed directory.")


def _regular(path):
    if path.is_symlink() or not path.is_file():
        raise LocalImportError("Managed file is missing or unsafe.")
    # Protect hard links as well as symbolic links.
    if path.stat().st_nlink != 1:
        raise LocalImportError("Managed files cannot be linked.")
    return path


def _open_private(path, flags, mode=0o600):
    return os.fdopen(os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), mode),
                     "r+b" if flags & os.O_RDWR else "wb")


def _atomic_json(path, value):
    fd, name = tempfile.mkstemp(prefix=".metadata-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class LocalImportManager:
    """Resumable byte transfer with atomic acknowledgments and process locks.

    Incomplete writes are truncated to the last persisted offset on retry.
    Cancellation removes payload bytes immediately, retaining only a tombstone.
    ``cleanup`` removes old inactive transfers; applications may schedule it.
    """

    def __init__(self, root):
        candidate = Path(root).absolute()
        _private_directory(candidate)
        self.root = candidate.resolve()

    def _owner(self, owner):
        if not isinstance(owner, str) or not owner or len(owner) > 4096:
            raise LocalImportError("Invalid import owner.")
        directory = self.root / hashlib.sha256(owner.encode()).hexdigest()
        _private_directory(directory)
        return directory

    def _directory(self, owner, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise LocalImportNotFound("Local import not found.")
        directory = self._owner(owner) / identifier
        if directory.is_symlink():
            raise LocalImportError("Unsafe managed import directory.")
        if not directory.is_dir():
            raise LocalImportNotFound("Local import not found.")
        return directory

    @contextmanager
    def _locked(self, directory):
        key = str(directory)
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(key, threading.RLock())
        with lock:
            filename = directory / ".lock"
            if filename.exists():
                _regular(filename)
            with _open_private(filename, os.O_CREAT | os.O_RDWR) as stream:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0, 2)
                    if stream.tell() == 0:
                        stream.write(b"\0")
                        stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if os.name == "nt":
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _read(self, directory):
        filename = _regular(directory / "metadata.json")
        if filename.stat().st_size > 16384:
            raise LocalImportError("Invalid local import metadata.")
        return json.loads(filename.read_text(encoding="utf-8"))

    @staticmethod
    def _public(record):
        return {k: record[k] for k in
                ("id", "filename", "size", "offset", "status", "workspace_id", "revision")}

    def create(self, owner, workspace_id, revision, filename, size):
        if (not isinstance(workspace_id, str) or not workspace_id or len(workspace_id) > 512
                or type(revision) is not int or revision < 0):
            raise LocalImportError("Invalid workspace or revision.")
        if (not isinstance(filename, str) or not filename or len(filename) > 255
                or "/" in filename or "\\" in filename or ":" in filename
                or any(ord(c) < 32 or ord(c) == 127 for c in filename)
                or filename in (".", "..") or filename.endswith((" ", "."))):
            raise LocalImportError("Choose a safe archive filename without directories.")
        suffix = Path(filename).suffix.lower()
        if suffix not in (".imscc", ".haxcourse"):
            raise LocalImportError("Choose an .imscc or .haxcourse file.")
        maximum = MAX_PACKAGE_BYTES if suffix == ".haxcourse" else MAX_ARCHIVE_BYTES
        if type(size) is not int or not 0 < size <= maximum:
            raise LocalImportError("File exceeds the 64 MiB review-package or 8 GiB archive limit.")
        parent = self._owner(owner)
        _disk_check(parent, size)
        identifier = uuid.uuid4().hex
        directory = parent / identifier
        directory.mkdir(mode=0o700)
        record = dict(id=identifier, workspace_id=workspace_id, revision=revision,
                      filename=filename, size=size, offset=0, status="uploading",
                      updated_at=time.time())
        try:
            with _open_private(directory / ("source" + suffix), os.O_CREAT | os.O_EXCL | os.O_WRONLY):
                pass
            _atomic_json(directory / "metadata.json", record)
        except Exception:
            shutil.rmtree(directory)
            raise
        return self._public(record)

    def get(self, owner, id):
        directory = self._directory(owner, id)
        with self._locked(directory):
            return self._public(self._read(directory))

    def append(self, owner, id, offset, stream, content_length):
        if (type(offset) is not int or offset < 0 or type(content_length) is not int
                or not 0 < content_length <= MAX_CHUNK_BYTES):
            raise LocalImportError("Each chunk must contain 1 byte to 8 MiB and a nonnegative offset.")
        directory = self._directory(owner, id)
        with self._locked(directory):
            record = self._read(directory)
            if record["status"] != "uploading":
                raise LocalImportConflict("This local import is no longer accepting chunks.")
            end = offset + content_length
            if (offset > record["offset"] or end > record["size"]
                    or offset < record["offset"] < end):
                raise LocalImportConflict("Chunk offset does not match the acknowledged local offset.")
            payload = _regular(directory / ("source" + Path(record["filename"]).suffix.lower()))
            _disk_check(directory, content_length)
            # A chunk is staged before changing the acknowledged payload. Never
            # hold the archive or an unbounded request body in Python memory.
            with tempfile.TemporaryFile(dir=directory) as chunk:
                remaining = content_length
                while remaining:
                    data = stream.read(min(COPY_CHUNK, remaining))
                    if not data or not isinstance(data, bytes) or len(data) > remaining:
                        raise LocalImportError("Chunk body does not match Content-Length.")
                    chunk.write(data)
                    remaining -= len(data)
                if stream.read(1):
                    raise LocalImportError("Chunk body exceeds Content-Length.")
                chunk.seek(0)
                with _open_private(payload, os.O_RDWR) as target:
                    if target.seek(0, 2) < record["offset"]:
                        raise LocalImportError("Local import payload is truncated; cancel and retry.")
                    target.truncate(record["offset"])
                    target.seek(offset)
                    if offset < record["offset"]:
                        remaining = content_length
                        while remaining:
                            amount = min(COPY_CHUNK, remaining)
                            if target.read(amount) != chunk.read(amount):
                                raise LocalImportConflict("Retried chunk differs from acknowledged bytes.")
                            remaining -= amount
                        return self._public(record)
                    shutil.copyfileobj(chunk, target, COPY_CHUNK)
                    target.flush()
                    os.fsync(target.fileno())
                record.update(offset=end, updated_at=time.time())
                _atomic_json(directory / "metadata.json", record)
            return self._public(record)

    def complete(self, owner, id):
        directory = self._directory(owner, id)
        with self._locked(directory):
            record = self._read(directory)
            if record["status"] == "cancelled" or record["offset"] != record["size"]:
                raise LocalImportConflict("Transfer is cancelled or incomplete.")
            payload = _regular(directory / ("source" + Path(record["filename"]).suffix.lower()))
            if payload.stat().st_size != record["size"]:
                raise LocalImportConflict("Local payload size does not match the completed transfer.")
            record.update(status="complete", updated_at=time.time())
            _atomic_json(directory / "metadata.json", record)
            return {**self._public(record), "path": str(payload)}

    def cancel(self, owner, id):
        directory = self._directory(owner, id)
        with self._locked(directory):
            record = self._read(directory)
            (directory / ("source" + Path(record["filename"]).suffix.lower())).unlink(missing_ok=True)
            record.update(status="cancelled", updated_at=time.time())
            _atomic_json(directory / "metadata.json", record)
            return self._public(record)

    def cleanup(self, max_age=86400, *, include_completed=False):
        """Expire abandoned uploads/tombstones; completed sources stay for jobs.

        Call cancel after a completed source is no longer needed. Only private,
        server-created names are considered; unknown files are never followed.
        """
        if type(max_age) not in (int, float) or max_age < 0:
            raise LocalImportError("Invalid cleanup age.")
        count = 0
        for owner in self.root.iterdir():
            if owner.is_symlink() or not owner.is_dir() or not re.fullmatch(r"[a-f0-9]{64}", owner.name):
                continue
            for directory in owner.iterdir():
                if directory.is_symlink() or not directory.is_dir() or not re.fullmatch(r"[a-f0-9]{32}", directory.name):
                    continue
                with self._locked(directory):
                    record = self._read(directory)
                    recovered = include_completed and record["status"] == "complete"
                    if not recovered and (record["status"] == "complete"
                                          or record["updated_at"] > time.time() - max_age):
                        continue
                    # Keep the lock/metadata tombstone so a waiting process cannot
                    # append into a deleted-and-recreated UUID directory.
                    (directory / ("source" + Path(record["filename"]).suffix.lower())).unlink(missing_ok=True)
                    if record["status"] != "cancelled":
                        record.update(status="cancelled", updated_at=time.time())
                        _atomic_json(directory / "metadata.json", record)
                        count += 1
        return count


def _media_header(source, suffix):
    header = source.read(64)
    if suffix == ".png":
        valid = header.startswith(b"\x89PNG\r\n\x1a\n")
    elif suffix in (".jpg", ".jpeg"):
        valid = header.startswith(b"\xff\xd8\xff")
    elif suffix == ".gif":
        valid = header.startswith((b"GIF87a", b"GIF89a"))
    elif suffix == ".webp":
        valid = header.startswith(b"RIFF") and header[8:12] == b"WEBP"
    elif suffix == ".ico":
        valid = header.startswith(b"\0\0\1\0")
    elif suffix == ".avif":
        valid = header[4:8] == b"ftyp" and b"avif" in header[8:]
    elif suffix in (".mp4", ".m4a", ".m4v", ".mov"):
        valid = header[4:8] in (b"ftyp", b"moov", b"mdat", b"wide")
    elif suffix == ".wav":
        valid = header.startswith(b"RIFF") and header[8:12] == b"WAVE"
    elif suffix == ".ogg":
        valid = header.startswith(b"OggS")
    elif suffix == ".webm":
        valid = header.startswith(b"\x1a\x45\xdf\xa3")
    else:  # MP3 frame sync or ID3 tag.
        valid = header.startswith(b"ID3") or (len(header) > 1 and header[0] == 255 and header[1] & 224 == 224)
    if not valid:
        raise IMSCCImportError("Invalid or unsupported passive media signature.")


def _restore_assets(archive_path, summary, progress):
    """Add validated passive assets before HTML references are normalized."""
    root = Path(summary["export_dir"])
    files = root / "files"
    files.mkdir(exist_ok=True)
    metadata = []
    existing_paths = {name: ref for name, ref in summary["resource_paths"].items()
                      if ref["kind"] == "file"}
    # Review documents were validated under a smaller, older transport contract.
    # Revalidate every final asset, including those already in that package.
    for value in files.iterdir():
        value.unlink()
    summary["resource_paths"] = {name: ref for name, ref in summary["resource_paths"].items()
                                 if ref["kind"] != "file"}
    _central_directory_bound(archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        entries = inspect_zip(archive)
        selected = [(name, info) for name, info in entries.items()
                    if PurePosixPath(name).suffix.lower() in MEDIA_EXTENSIONS | DOCUMENT_EXTENSIONS]
        # Office validation may temporarily require both a staged and final copy.
        _disk_check(root, 2 * sum(info.file_size for _, info in selected))
        for index, (name, info) in enumerate(selected, 1):
            suffix = PurePosixPath(name).suffix.lower()
            # Final assets bypass review-size limits, not archive/disk limits.
            filename = hashlib.sha256(name.encode()).hexdigest() + suffix
            target = files / filename
            progress("copying_assets", f"Copying validated local asset {index} of {len(selected)}.")
            try:
                if info.file_size > MAX_ARCHIVE_BYTES:
                    raise IMSCCImportError("Individual asset exceeds the 8 GiB safety limit.")
                if suffix in DOCUMENT_EXTENSIONS:
                    copy_passive_document(archive, info, target)
                else:
                    with archive.open(info) as source:
                        _media_header(source, suffix)
                    with archive.open(info) as source, target.open("xb") as destination:
                        total = 0
                        while True:
                            chunk = source.read(COPY_CHUNK)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > info.file_size or total > MAX_ARCHIVE_BYTES:
                                raise IMSCCImportError("Asset exceeds its declared safe size.")
                            destination.write(chunk)
                        if total != info.file_size:
                            raise IMSCCImportError("Asset size mismatch.")
            except IMSCCImportError:
                target.unlink(missing_ok=True)
                summary["warnings"].append(
                    f"Unsupported, active, invalid or over-limit attachment excluded: {name}")
                continue
            item_id = (existing_paths[name]["id"] if name in existing_paths else
                       summary["course_id"] + "-file-" + hashlib.sha256(name.encode()).hexdigest()[:24])
            record = {"id": item_id, "filename": filename, "display_name": PurePosixPath(name).name,
                      "source_path": name, "source_type": "imscc"}
            (files / (item_id + ".json")).write_text(json.dumps(record), encoding="utf-8")
            summary["resource_paths"][name] = {"kind": "file", "id": item_id, "target": filename}
            metadata.append({"filename": filename, "display_name": record["display_name"],
                             "size": info.file_size})
    summary["counts"]["files"] = len(list(files.glob("*.json")))
    return metadata


def _revalidate_review_assets(summary):
    """Apply the same passive checks to old review packages, not only IMSCC."""
    files = Path(summary["export_dir"]) / "files"
    metadata = []
    removed = set()
    validated = {}
    class LocalFiles:
        @staticmethod
        def open(info):
            return (files / info.filename).open("rb")
    for path in files.glob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        asset = files / record["filename"]
        if asset.name in validated:
            if not validated[asset.name]:
                removed.add(record["id"])
                path.unlink()
            continue
        fd, filename = tempfile.mkstemp(prefix=".validated-", dir=files)
        os.close(fd)
        staged = Path(filename)
        staged.unlink()  # The validator uses exclusive creation.
        try:
            copy_passive_document(LocalFiles(), SimpleNamespace(
                filename=asset.name, file_size=asset.stat().st_size), staged)
            os.replace(staged, asset)
            metadata.append({"filename": asset.name, "display_name": record["display_name"],
                             "size": asset.stat().st_size})
            validated[asset.name] = True
        except IMSCCImportError:
            validated[asset.name] = False
            removed.add(record["id"])
            asset.unlink(missing_ok=True)
            path.unlink()
            summary["warnings"].append("Active or invalid review document excluded: " + record["display_name"])
        finally:
            staged.unlink(missing_ok=True)
    summary["resource_paths"] = {name: ref for name, ref in summary["resource_paths"].items()
                                 if ref["id"] not in removed}
    summary["counts"]["files"] = len(list(files.glob("*.json")))
    return metadata


def prepare_local_source(path, destination, progress=None):
    """Normalize a trusted startup/managed source without modifying the original.

    Returns private normalized source details. ``assets`` contains only generated
    filenames, display names and sizes, never source/archive filesystem paths.
    Destination must be new; failures remove only this invocation's output.
    """
    from prepare_course import prepare_course
    from imscc_content import prepare_import_content
    from mentor_service import source_items

    progress = progress or (lambda phase, message: None)
    source, destination = Path(path), Path(destination)
    if source.is_symlink() or not source.is_file():
        raise IMSCCImportError("Choose a regular local archive file, not a symbolic link.")
    suffix = source.suffix.lower()
    if suffix not in (".imscc", ".haxcourse"):
        raise IMSCCImportError("Choose an .imscc or .haxcourse file.")
    maximum = MAX_PACKAGE_BYTES if suffix == ".haxcourse" else MAX_ARCHIVE_BYTES
    if source.stat().st_size > maximum:
        raise IMSCCImportError("Local source exceeds the archive or review-package size limit.")
    if destination.exists() or destination.is_symlink():
        raise IMSCCImportError("Preparation destination already exists; refusing to overwrite.")
    if source.resolve().is_relative_to(destination.resolve()):
        raise IMSCCImportError("The original archive cannot be inside the preparation destination.")
    destination.mkdir(parents=True, mode=0o700)
    try:
        _disk_check(destination, min(source.stat().st_size, 128 * MIB))
        progress("inspecting", "Inspecting local archive safety and instructional limits.")
        with tempfile.TemporaryDirectory(prefix=".local-review-", dir=destination) as work:
            progress("preparing", "Preparing bounded instructional review; media stay out of AI context.")
            if suffix == ".haxcourse":
                summary = import_course_package(source, destination)
                progress("copying_assets", "Validating passive documents retained in this review package.")
                assets = _revalidate_review_assets(summary)
                summary["warnings"].append(
                    "This .haxcourse review cannot restore media excluded when it was prepared.")
            else:
                package = Path(work) / "review.haxcourse"
                prepare_course(source, package)
                summary = import_course_package(package, destination)
                assets = _restore_assets(source, summary, progress)
                summary["warnings"] = [
                    warning for warning in summary["warnings"]
                    if not warning.startswith(("Prepared instructional review only:",
                                               "Prepared review package:", "Excluded "))
                ] + ["Instructional review is bounded; supported local assets were copied separately. "
                     "Excluded or unavailable external resources cannot be restored."]
                retained = set(summary["resource_paths"])
                exclusions = [entry for entry in summary.get("exclusions", [])
                              if entry["path"] not in retained]
                summary["exclusions"] = exclusions
                if exclusions:
                    summary["warnings"].append(
                        f"Excluded {len(exclusions)} unsupported or over-limit archive entries; "
                        "the private normalized summary contains the exclusion inventory.")
                summary["provenance"] = {"source": "local", "media_excluded": False}
            # Mappings must be present before placeholder/relative-link rewriting.
            output = Path(summary["export_dir"])
            (output / "export_summary.json").write_text(json.dumps(summary), encoding="utf-8")
            prepare_import_content(output)
            summary = json.loads((output / "export_summary.json").read_text(encoding="utf-8"))
            # Preserve the mentor's stricter item/text quotas, even for 8 GiB inputs.
            source_items({"id": "local-preflight", "export_dir": str(output), "source_type": "imscc"})
            return {"source_type": "imscc", "course_id": summary["course_id"],
                    "export_dir": str(output), "warnings": summary["warnings"], "assets": assets}
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, zlib.error) as exc:
        shutil.rmtree(destination)
        raise IMSCCImportError("Local archive contains corrupt or invalid compressed data.") from exc
    except OSError as exc:
        shutil.rmtree(destination)
        raise IMSCCImportError("Local file preparation failed; check permissions and free disk space.") from exc
    except IMSCCImportError as exc:
        shutil.rmtree(destination)
        # Existing bounded parsers may include their temporary filename in an
        # error. Keep that machine-local location out of route-visible messages.
        message = str(exc)
        for private in sorted({str(source), str(source.resolve()),
                               str(destination), str(destination.resolve())}, key=len, reverse=True):
            message = message.replace(private, "[local source]")
        raise IMSCCImportError(message) from exc
    except Exception:
        shutil.rmtree(destination)
        raise
