"""Trusted Sandbox entrypoint. No credentials, shell interpolation, or raw output.

Download and parsing are separate processes: the controller MUST install deny-all
after download exits and before invoking prepare. All paths below are constants.
"""
import hashlib
import fcntl
import http.client
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import time
from urllib.parse import urlsplit

MAX_INPUT = 8 * 1024**3
MAX_RESULT = 64 * 1024**2
CHUNK = 64 * 1024
ROOT = Path("/vercel/sandbox/hax")
DEPENDENCIES = ("defusedxml==0.7.1", "pypdf==5.9.0",
                "python-docx==1.2.0", "python-pptx==1.0.2")


def private_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname
            or not parsed.hostname.endswith(".private.blob.vercel-storage.com")
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.query or parsed.fragment or "\\" in url
            or any(c.isspace() or ord(c) < 32 for c in url)):
        raise ValueError("Invalid private Blob URL")
    return parsed


def digest(path):
    size = 0
    sha = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(CHUNK):
            sha.update(chunk)
            size += len(chunk)
    return {"sha256": sha.hexdigest(), "size": size}


def download(manifest, root=ROOT, connection_factory=http.client.HTTPSConnection):
    """Ordinary GET; the external firewall injects authorization, never this VM."""
    parsed = private_url(manifest["url"])
    expected = manifest["expected_size"]
    if type(expected) is not int or not 0 < expected <= MAX_INPUT:
        raise ValueError("Invalid expected source size")
    target = root / "input.bin"
    if (root / "download.json").exists():
        receipt = json.loads((root / "download.json").read_text("utf-8"))
        if digest(target) != receipt or receipt["size"] != expected:
            raise ValueError("Existing download mismatch")
        return receipt
    if shutil.disk_usage(root).free < expected + 256 * 1024**2:
        raise ValueError("Insufficient sandbox disk")
    connection = connection_factory(parsed.hostname, timeout=30)
    deadline = time.monotonic() + 15 * 60
    try:
        connection.request("GET", parsed.path, headers={"Accept-Encoding": "identity"})
        response = connection.getresponse()
        if (response.status != 200
                or response.getheader("Content-Encoding", "identity") != "identity"):
            raise ValueError("Source download failed")
        length = response.getheader("Content-Length")
        if length is not None and int(length) != expected:
            raise ValueError("Source size mismatch")
        total, sha = 0, hashlib.sha256()
        with target.open("xb") as output:
            while True:
                if time.monotonic() > deadline:
                    raise ValueError("Download deadline exceeded")
                chunk = response.read(min(CHUNK, expected - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > expected:
                    raise ValueError("Source exceeds expected size")
                output.write(chunk)
                sha.update(chunk)
        if total != expected:
            raise ValueError("Source size mismatch")
        receipt = {"size": total, "sha256": sha.hexdigest()}
        (root / "download.json").write_text(json.dumps(receipt), encoding="utf-8")
        return receipt
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        connection.close()


def prepare(manifest, root=ROOT):
    source, output = root / "input.bin", root / "result.bin"
    if (root / "result.json").exists():
        receipt = json.loads((root / "result.json").read_text("utf-8"))
        if digest(output) != receipt:
            raise ValueError("Existing result mismatch")
        return receipt
    if digest(source) != json.loads((root / "download.json").read_text("utf-8")):
        raise ValueError("Source changed after download")
    kind = manifest["kind"]
    if kind == "material":
        from mentor_materials import extract_material
        value = extract_material(source, original_name=manifest["filename"], role=manifest["role"])
        data = json.dumps(value, ensure_ascii=True).encode("utf-8")
        if len(data) > 2 * 1024**2:
            raise ValueError("Material result exceeds limit")
        with output.open("xb") as stream:
            stream.write(data)
    elif kind == "prepared":
        from course_package import import_course_package
        import_course_package(source, root / "validated")
        # Validation never rewrites a supplied prepared review package.
        if source.stat().st_size > MAX_RESULT:
            raise ValueError("Prepared result exceeds limit")
        shutil.copyfile(source, output)
    elif kind == "course":
        from prepare_course import prepare_course
        prepare_course(source, output)
    else:
        raise ValueError("Invalid preparation kind")
    receipt = digest(output)
    if not 0 < receipt["size"] <= MAX_RESULT:
        raise ValueError("Prepared result exceeds limit")
    (root / "result.json").write_text(json.dumps(receipt), encoding="utf-8")
    return receipt


def main():
    # SDK DEVNULL controls local stream handles, not necessarily guest logging.
    # Silence descriptors before loading any untrusted parser.
    descriptor = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(descriptor, 1)
        os.dup2(descriptor, 2)
    finally:
        os.close(descriptor)
    # Sandbox execution_time_limit also bounds setup + download + parse together.
    signal.alarm(15 * 60)
    try:
        with (ROOT / "phase.lock").open("a") as lock:
            # A retried detached launch cannot race an already-running phase.
            fcntl.flock(lock, fcntl.LOCK_EX)
            manifest = json.loads((ROOT / "manifest.json").read_text("utf-8"))
            if sys.argv[1:] == ["download"]:
                download(manifest)
            elif sys.argv[1:] == ["prepare"]:
                prepare(manifest)
            else:
                return 2
        return 0
    except Exception:
        # Parser exception strings can contain course content or filenames.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
