"""Synthetic local runtime tests; no AI or external network services."""

import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from local_imports import (
    COPY_CHUNK, MAX_ARCHIVE_BYTES, MAX_CHUNK_BYTES, LocalImportConflict,
    LocalImportError, LocalImportManager, LocalImportNotFound, prepare_local_source,
)
from imscc_import import IMSCCImportError
from prepare_course import prepare_course


class ImportManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = LocalImportManager(self.root / "imports")

    def tearDown(self):
        self.temp.cleanup()

    def create(self, size=6, filename="course.imscc"):
        return self.manager.create("owner-a", "workspace-one", 3, filename, size)

    def append(self, record, offset, data, length=None):
        return self.manager.append("owner-a", record["id"], offset, io.BytesIO(data),
                                   len(data) if length is None else length)

    def test_offsets_idempotent_retries_and_private_completion(self):
        record = self.create()
        self.assertNotIn("path", record)
        self.assertEqual(record["offset"], 0)
        self.assertEqual(self.append(record, 0, b"abc")["offset"], 3)
        self.assertEqual(self.append(record, 0, b"abc")["offset"], 3)
        for offset, data in [(0, b"bad"), (4, b"xx"), (2, b"cd")]:
            with self.assertRaises(LocalImportConflict):
                self.append(record, offset, data)
        with self.assertRaises(LocalImportConflict):
            self.manager.complete("owner-a", record["id"])
        self.append(record, 3, b"def")
        complete = self.manager.complete("owner-a", record["id"])
        self.assertEqual(Path(complete["path"]).read_bytes(), b"abcdef")
        self.assertEqual(self.manager.complete("owner-a", record["id"]), complete)
        self.assertNotIn("path", self.manager.get("owner-a", record["id"]))
        self.assertEqual(complete["status"], "complete")
        self.assertEqual(complete["workspace_id"], "workspace-one")
        with self.assertRaises(LocalImportConflict):
            self.append(record, 0, b"abc")

    def test_owner_namespace_and_identifier_restrictions(self):
        record = self.create()
        for owner, identifier in [("owner-b", record["id"]), ("owner-a", "../outside"),
                                  ("owner-a", "Z" * 32), ("owner-a", "a" * 32)]:
            with self.assertRaises(LocalImportNotFound):
                self.manager.get(owner, identifier)
        other = self.manager.create("owner-a", "workspace-two", 0, "two.imscc", 1)
        self.assertNotEqual(record["id"], other["id"])

    def test_filename_sizes_and_types(self):
        for filename in ("../x.imscc", r"a\x.imscc", "C:x.imscc", "/x.imscc",
                         "a\n.imscc", "course.zip", ".imscc", "x.imscc "):
            with self.subTest(filename=filename), self.assertRaises(LocalImportError):
                self.create(filename=filename)
        for size in (0, -1, True, 2.0, MAX_ARCHIVE_BYTES + 1):
            with self.subTest(size=size), self.assertRaises(LocalImportError):
                self.create(size=size)
        with self.assertRaises(LocalImportError):
            self.create(filename="course.haxcourse", size=64 * 1024**2 + 1)

    def test_exact_archive_limit_is_admitted_without_allocating_an_archive(self):
        with patch("local_imports._disk_check"):
            record = self.create(size=MAX_ARCHIVE_BYTES)
        self.assertEqual(record["size"], 8 * 1024**3)
        payload = next((self.root / "imports").rglob("source.imscc"))
        self.assertEqual(payload.stat().st_size, 0)
        self.manager.cancel("owner-a", record["id"])

    def test_short_long_and_oversized_chunks_leave_no_acknowledged_bytes(self):
        record = self.create()
        for data, length in [(b"ab", 3), (b"abcd", 3), (b"", 0),
                             (b"x", MAX_CHUNK_BYTES + 1)]:
            with self.assertRaises(LocalImportError):
                self.append(record, 0, data, length)
        self.assertEqual(self.manager.get("owner-a", record["id"])["offset"], 0)
        self.append(record, 0, b"abcdef")

    def test_interrupted_unacknowledged_tail_is_truncated(self):
        record = self.create()
        self.append(record, 0, b"abc")
        payload = next((self.root / "imports").rglob("source.imscc"))
        with payload.open("ab") as stream:
            stream.write(b"unacknowledged")
        self.append(record, 3, b"def")
        self.assertEqual(payload.read_bytes(), b"abcdef")

    def test_disk_failure_does_not_create_transfer(self):
        with patch("local_imports.shutil.disk_usage", return_value=shutil._ntuple_diskusage(10, 9, 1)):
            with self.assertRaisesRegex(LocalImportError, "disk"):
                self.create()
        self.assertFalse(list((self.root / "imports").rglob("metadata.json")))

    def test_cancel_cleanup_and_completed_input_retention(self):
        record = self.create()
        self.append(record, 0, b"abc")
        self.assertEqual(self.manager.cancel("owner-a", record["id"])["status"], "cancelled")
        self.manager.cancel("owner-a", record["id"])
        self.assertFalse(list((self.root / "imports").rglob("source.imscc")))
        with self.assertRaises(LocalImportConflict):
            self.append(record, 0, b"abc")
        with self.assertRaises(LocalImportConflict):
            self.manager.complete("owner-a", record["id"])
        active = self.create()
        done = self.create()
        self.append(done, 0, b"abcdef")
        completed = self.manager.complete("owner-a", done["id"])
        self.assertEqual(self.manager.cleanup(max_age=0), 1)
        self.assertEqual(self.manager.get("owner-a", active["id"])["status"], "cancelled")
        self.assertTrue(Path(completed["path"]).exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_symlink_payload_and_owner_directory_are_rejected(self):
        record = self.create()
        payload = next((self.root / "imports").rglob("source.imscc"))
        outside = self.root / "outside"
        outside.write_bytes(b"untouched")
        payload.unlink()
        payload.symlink_to(outside)
        with self.assertRaises(LocalImportError):
            self.append(record, 0, b"abc")
        self.assertEqual(outside.read_bytes(), b"untouched")
        directory = payload.parent
        for value in directory.iterdir():
            value.unlink()
        directory.rmdir()
        directory.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(LocalImportError):
            self.manager.get("owner-a", record["id"])

    def test_parallel_retry_serializes_across_manager_instances(self):
        record = self.create()
        other = LocalImportManager(self.root / "imports")
        errors = []
        def write(manager):
            try:
                manager.append("owner-a", record["id"], 0, io.BytesIO(b"abc"), 3)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=write, args=(manager,)) for manager in (self.manager, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.manager.get("owner-a", record["id"])["offset"], 3)

    def test_chunk_read_size_is_bounded(self):
        class Bounded(io.BytesIO):
            def read(self, size=-1):
                self_test.assertLessEqual(size, COPY_CHUNK)
                self_test.assertGreater(size, 0)
                return super().read(size)
        self_test = self
        size = 2 * COPY_CHUNK + 3
        record = self.create(size=size)
        self.manager.append("owner-a", record["id"], 0, Bounded(b"x" * size), size)


def make_course(path, extras=None, body=None):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("imsmanifest.xml", """<manifest identifier="synthetic">
          <metadata><title>Synthetic course</title></metadata><resources>
          <resource identifier="page" type="webcontent" href="wiki_content/intro.html">
          <file href="wiki_content/intro.html"/></resource>
          <resource identifier="picture" type="webcontent" href="web_resources/picture.png">
          <file href="web_resources/picture.png"/></resource>
          </resources></manifest>""")
        archive.writestr("wiki_content/intro.html", body or (
            '<html><head><title>Introduction</title></head><body><p>Evidence lesson.</p>'
            '<img src="$IMS-CC-FILEBASE$/picture.png" alt="Diagram">'
            '<a href="../web_resources/readme.txt">Reading</a>'
            '<script>neverExecute()</script></body></html>'))
        archive.writestr("web_resources/picture.png", b"\x89PNG\r\n\x1a\nsynthetic")
        archive.writestr("web_resources/readme.txt", b"Synthetic passive reading.")
        for name, data in (extras or {}).items():
            archive.writestr(name, data)


class LocalPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.archive = self.root / "course.imscc"

    def tearDown(self):
        self.temp.cleanup()

    def test_assets_and_mappings_survive_before_normalization(self):
        make_course(self.archive)
        original = self.archive.read_bytes()
        phases = []
        source = prepare_local_source(self.archive, self.root / "prepared",
                                      lambda phase, message: phases.append((phase, message)))
        output = Path(source["export_dir"])
        page = json.loads(next((output / "pages").glob("*.json")).read_text())
        self.assertIn('src="files/', page["body"])
        self.assertIn('href="files/', page["body"])
        self.assertNotIn("$IMS-CC-FILEBASE$", page["body"])
        self.assertNotIn("<script", page["body"])
        self.assertEqual(self.archive.read_bytes(), original)
        self.assertEqual({phase for phase, _ in phases}, {"inspecting", "preparing", "copying_assets"})
        self.assertEqual(len(source["assets"]), 2)
        for asset in source["assets"]:
            self.assertEqual(Path(asset["filename"]).name, asset["filename"])
            self.assertTrue((output / "files" / asset["filename"]).is_file())
            self.assertNotIn(str(self.root), json.dumps(asset))
        self.assertTrue(all(isinstance(message, str) for _, message in phases))

    def test_streamed_large_assets_not_in_review_and_unsupported_media_never_opened(self):
        # Nine MiB fixtures exercise streaming/review separation, not throughput.
        big = b"\0\0\0\x18ftypmp42" + b"x" * (9 * 1024**2)
        make_course(self.archive, {"web_resources/movie.mp4": big,
                                   "web_resources/ignored.bin": b"excluded" * 1000})
        original_open = zipfile.ZipFile.open
        reads = []
        def guard(archive, name, *args, **kwargs):
            filename = name.filename if isinstance(name, zipfile.ZipInfo) else name
            if filename == "web_resources/ignored.bin":
                raise AssertionError("Unsupported excluded data must not be inflated")
            if filename == "web_resources/movie.mp4":
                reads.append(filename)
            return original_open(archive, name, *args, **kwargs)
        with patch.object(zipfile.ZipFile, "open", guard):
            source = prepare_local_source(self.archive, self.root / "prepared")
        media = next((Path(source["export_dir"]) / "files").glob("*.mp4"))
        self.assertEqual(media.stat().st_size, len(big))
        self.assertEqual(len(reads), 2)  # signature check then streamed extraction
        self.assertFalse(any("movie" in p.read_text() for p in
                             (Path(source["export_dir"]) / "pages").glob("*.json")))

    def test_large_safe_document_retained_and_active_documents_excluded(self):
        make_course(self.archive, {
            "web_resources/large.pdf": b"%PDF-1.7\n" + b"passive " * (700 * 1024),
            "web_resources/active.pdf": b"%PDF-1.7\n/J#61vaScript (bad)",
            "web_resources/fake.png": b"<html>not a PNG</html>",
            "web_resources/active.svg": b'<svg onload="bad()"/>',
        })
        source = prepare_local_source(self.archive, self.root / "prepared")
        files = Path(source["export_dir"]) / "files"
        self.assertEqual(len(list(files.glob("*.pdf"))), 1)
        self.assertGreater(next(files.glob("*.pdf")).stat().st_size, 4 * 1024**2)
        self.assertEqual(len(list(files.glob("*.png"))), 1)
        self.assertFalse(list(files.glob("*.svg")))
        self.assertTrue(any("active.pdf" in warning for warning in source["warnings"]))

    def test_obfuscated_pdf_actions_split_across_chunks_and_opaque_objects_are_excluded(self):
        prefix = b"%PDF-1.7\n"
        split = prefix + b"x" * (COPY_CHUNK - len(prefix) - 3) + b"/J#61vaScript (bad)"
        make_course(self.archive, {
            "web_resources/split.pdf": split,
            "web_resources/objects.pdf": b"%PDF-1.7\n/Type/ObjStm /Filter/FlateDecode",
        })
        source = prepare_local_source(self.archive, self.root / "prepared")
        self.assertFalse(list((Path(source["export_dir"]) / "files").glob("*.pdf")))

    def test_review_package_remains_supported_and_warns_of_missing_media(self):
        make_course(self.archive)
        package = self.root / "review.haxcourse"
        prepare_course(self.archive, package)
        source = prepare_local_source(package, self.root / "prepared")
        self.assertEqual(source["source_type"], "imscc")
        self.assertFalse(list((Path(source["export_dir"]) / "files").glob("*.png")))
        self.assertTrue(any("cannot restore media" in warning for warning in source["warnings"]))

    def test_old_review_packages_get_stronger_active_document_checks(self):
        make_course(self.archive, {"web_resources/unsafe.pdf": b"%PDF-1.7\n/J#61vaScript (bad)"})
        package = self.root / "review.haxcourse"
        prepare_course(self.archive, package)
        source = prepare_local_source(package, self.root / "prepared")
        self.assertFalse(list((Path(source["export_dir"]) / "files").glob("*.pdf")))
        self.assertTrue(any("Active or invalid review document" in warning for warning in source["warnings"]))

    def test_unsafe_archive_paths_and_failures_clean_new_destination(self):
        make_course(self.archive, {"../outside.png": b"\x89PNG\r\n\x1a\nbad"})
        destination = self.root / "prepared"
        with self.assertRaises(IMSCCImportError):
            prepare_local_source(self.archive, destination)
        self.assertFalse(destination.exists())
        self.assertTrue(self.archive.exists())
        destination.mkdir()
        (destination / "preserve").write_text("owned by caller")
        with self.assertRaises(IMSCCImportError):
            prepare_local_source(self.archive, destination)
        self.assertTrue((destination / "preserve").exists())

    def test_preparer_errors_redact_private_filesystem_paths(self):
        make_course(self.archive)
        destination = self.root / "prepared"
        with patch("prepare_course.prepare_course",
                   side_effect=IMSCCImportError(f"Cannot read {self.archive} in {destination}")):
            with self.assertRaises(IMSCCImportError) as error:
                prepare_local_source(self.archive, destination)
        self.assertNotIn(str(self.root), str(error.exception))
        self.assertFalse(destination.exists())

    def test_symbolic_archive_and_archive_size_rejected(self):
        make_course(self.archive)
        link = self.root / "link.imscc"
        link.symlink_to(self.archive)
        with self.assertRaises(IMSCCImportError):
            prepare_local_source(link, self.root / "prepared")
        with patch("local_imports.MAX_ARCHIVE_BYTES", 10):
            with self.assertRaises(IMSCCImportError):
                prepare_local_source(self.archive, self.root / "prepared")

    def test_instructional_quotas_are_not_raised_for_large_archives(self):
        make_course(self.archive, body="<p>" + "a" * (2 * 1024**2) + "</p>")
        destination = self.root / "prepared"
        with self.assertRaisesRegex(ValueError, "2 MiB"):
            prepare_local_source(self.archive, destination)
        self.assertFalse(destination.exists())


@unittest.skipUnless(shutil.which("node"), "Node required for launcher tests")
class LauncherTests(unittest.TestCase):
    def test_arguments_private_updates_and_local_environment(self):
        script = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const {test} = require('node:test');
test('launcher arguments, private updates, and local environment', () => {
const launcher = require(process.env.HAX_TEST_LAUNCHER), root = process.env.HAX_TEST_TEMP;
launcher.checkNode('22.0.0');
assert.throws(() => launcher.checkNode('20.5.0'), /Node.js 22/);
assert.equal(launcher.pythonSupported('Python 3.10.0'), true);
assert.equal(launcher.pythonSupported('Python 3.14.0'), true);
assert.equal(launcher.pythonSupported('Python 3.9.9'), false);
assert.equal(launcher.pythonSupported('Python 2.7.0'), false);
const weird = path.join(root, 'data with spaces " $ shell; &');
const course = path.join(root, 'course with spaces " $ shell; &.imscc');
const args = launcher.parseArgs(['--data-dir', weird, '--course', course]);
assert.equal(args.dataDir, weird);
assert.equal(args.course, course);
assert.deepEqual(launcher.parseArgs(['--prepare', course, '--output', 'review.haxcourse']).prepare,
  [course, '--output', 'review.haxcourse']);
assert.throws(() => launcher.parseArgs(['--course']), /Missing/);
assert.throws(() => launcher.parseArgs(['--unknown']), /Unknown/);
launcher.configureDataDir(weird);
fs.mkdirSync(path.join(weird, 'mentor_data'), {recursive:true});
fs.writeFileSync(path.join(weird, 'mentor_data', 'private.sqlite'), 'synthetic sentinel');
fs.writeFileSync(path.join(weird, '.env'), 'synthetic sentinel');
launcher.syncPythonFiles();
fs.writeFileSync(path.join(weird, '.version'), 'older code');
launcher.syncPythonFiles();
assert.equal(fs.readFileSync(path.join(weird,'.env'),'utf8'), 'synthetic sentinel');
assert.equal(fs.readFileSync(path.join(weird,'mentor_data','private.sqlite'),'utf8'), 'synthetic sentinel');
process.env.HAX_EXECUTION_MODE = 'hosted';
process.env.HAX_STARTUP_COURSE = 'inherited-path';
const env = launcher.localEnvironment('/managed official/hax.js', course);
assert.equal(env.HAX_EXECUTION_MODE, 'local');
assert.equal(env.HAX_DATA_DIR, weird);
assert.equal(env.HAX_HAX_CLI, '/managed official/hax.js');
assert.equal(env.HAX_NODE, process.execPath);
assert.equal(env.HAX_STARTUP_COURSE, course);
assert.equal(launcher.localEnvironment('/managed.js').HAX_STARTUP_COURSE, undefined);
const result = launcher.run(process.execPath, ['-e', 'process.stdout.write(process.argv[1])', course]);
assert.equal(result.status, 0);
assert.equal(result.stdout, course);
for (const name of ['a.imscc', 'a.haxcourse', 'a.zip', 'a.sqlite3', 'a.sqlite-wal', 'imports', 'sites'])
  assert.equal(launcher.bundledEntry(name), false);
});
"""
        with tempfile.TemporaryDirectory() as temp:
            test = Path(temp) / "launcher.test.cjs"
            test.write_text(script)
            result = subprocess.run(["node", "--test", str(test)], capture_output=True, text=True,
                                    env={**os.environ, "HAX_TEST_LAUNCHER": str(ROOT / "bin" / "index.js"),
                                         "HAX_TEST_TEMP": temp})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_pinned_official_cli_resolution_and_version_rejection(self):
        script = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const {test} = require('node:test');
test('managed pinned official CLI resolution', () => {
  const launcher = require(process.env.HAX_TEST_LAUNCHER), root = process.env.HAX_TEST_TEMP;
  const pkg = path.join(root, 'node_modules', '@haxtheweb', 'create');
  fs.mkdirSync(path.join(pkg, 'dist'), {recursive:true});
  fs.writeFileSync(path.join(pkg, 'dist', 'create.js'), '// synthetic CLI; never executed');
  const manifest = {name:'@haxtheweb/create', version:'26.8.1', bin:{hax:'./dist/create.js'}};
  fs.writeFileSync(path.join(pkg, 'package.json'), JSON.stringify(manifest));
  assert.equal(launcher.resolveHaxCli([root]), fs.realpathSync(path.join(pkg, 'dist', 'create.js')));
  manifest.version = '0.0.0';
  fs.writeFileSync(path.join(pkg, 'package.json'), JSON.stringify(manifest));
  assert.throws(() => launcher.resolveHaxCli([root]), /26.8.1/);
});
"""
        with tempfile.TemporaryDirectory() as temp:
            test = Path(temp) / "cli.test.cjs"
            test.write_text(script)
            result = subprocess.run(["node", "--test", str(test)], capture_output=True, text=True,
                                    env={**os.environ, "HAX_TEST_LAUNCHER": str(ROOT / "bin" / "index.js"),
                                         "HAX_TEST_TEMP": temp})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipIf(os.name == "nt", "Actual POSIX process-group test; Windows not claimed")
    def test_shutdown_forwards_to_child_process_group(self):
        controller = r"""
const {managedChild} = require(process.argv[1]);
managedChild(process.execPath, [process.argv[2]], {stdio:'inherit'});
"""
        child = r"""
const fs = require('node:fs'), {spawn} = require('node:child_process');
spawn(process.execPath, [process.env.HAX_TEST_GRANDCHILD], {stdio:'inherit'});
process.on('SIGTERM', () => {fs.writeFileSync(process.env.HAX_TEST_CHILD_STOPPED, 'yes'); process.exit(0);});
setInterval(() => {}, 1000);
"""
        grandchild = r"""
const fs = require('node:fs');
process.on('SIGTERM', () => {fs.writeFileSync(process.env.HAX_TEST_GRAND_STOPPED, 'yes'); process.exit(0);});
fs.writeFileSync(process.env.HAX_TEST_READY, String(process.pid));
setInterval(() => {}, 1000);
"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            child_path, grand_path = root / "child.cjs", root / "grandchild.cjs"
            child_path.write_text(child)
            grand_path.write_text(grandchild)
            ready = root / "ready"
            env = {**os.environ, "HAX_TEST_GRANDCHILD": str(grand_path),
                   "HAX_TEST_READY": str(ready), "HAX_TEST_CHILD_STOPPED": str(root / "child-stopped"),
                   "HAX_TEST_GRAND_STOPPED": str(root / "grand-stopped")}
            process = subprocess.Popen(["node", "-e", controller, str(ROOT / "bin" / "index.js"),
                                        str(child_path)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists())
                process.send_signal(signal.SIGTERM)
                process.communicate(timeout=10)
                self.assertTrue((root / "child-stopped").exists())
                self.assertTrue((root / "grand-stopped").exists())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
