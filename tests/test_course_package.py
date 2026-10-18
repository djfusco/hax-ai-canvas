"""Synthetic offline review-package tests; no user archives or network access."""

import io
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
import course_package as package
import imscc_import
import prepare_course as preparer


MANIFEST = """<manifest identifier="source-manifest">
<metadata><title>Prepared Biology</title></metadata>
<organizations><organization><item identifier="week"><title>Week One</title>
<item identifierref="page"><title>Read</title></item>
</item></organization></organizations><resources>
<resource identifier="page" type="webcontent" href="wiki_content/page.html"/>
<resource identifier="media" type="webcontent" href="web_resources/movie.mp4"/>
<resource identifier="doc" type="webcontent" href="web_resources/notes.txt"/>
</resources></manifest>"""
PAGE = '<title>Reading</title><p>Study cells.</p><img src="../web_resources/pic.png"/>'


def sparse_archive(path, media_size):
    """Write a real ZIP32 central directory around an unallocated stored payload.

    A 2 GiB payload fits the unsigned ZIP32 fields. Its CRC deliberately cannot
    be validated: tests fail if the preparer ever opens this excluded entry.
    """
    records = []
    with path.open("wb") as stream:
        for name, data, size in (
                ("imsmanifest.xml", MANIFEST.encode(), None),
                ("wiki_content/page.html", PAGE.encode(), None),
                ("web_resources/movie.mp4", None, media_size)):
            name = name.encode()
            offset = stream.tell()
            size = len(data) if data is not None else size
            crc = zlib.crc32(data) if data is not None else 0
            stream.write(struct.pack("<4s5H3I2H", b"PK\x03\x04", 20, 0, 0, 0, 0,
                                     crc, size, size, len(name), 0))
            stream.write(name)
            if data is None:
                stream.seek(size, os.SEEK_CUR)
                stream.truncate()
            else:
                stream.write(data)
            records.append((name, offset, crc, size))
        central_start = stream.tell()
        for name, offset, crc, size in records:
            stream.write(struct.pack("<4s6H3I5H2I", b"PK\x01\x02", 20, 20, 0, 0, 0, 0,
                                     crc, size, size, len(name), 0, 0, 0, 0, 0, offset))
            stream.write(name)
        central_size = stream.tell() - central_start
        stream.write(struct.pack("<4s4H2IH", b"PK\x05\x06", 0, 0, len(records), len(records),
                                 central_size, central_start, 0))


class CoursePackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.archive = self.root / "source.imscc"
        self.output = self.root / "review.haxcourse"
        self.exports = self.root / "exports"

    def tearDown(self):
        self.temp.cleanup()

    def write_zip(self, path, entries):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, data in entries.items() if isinstance(entries, dict) else entries:
                    archive.writestr(name, data)

    def entries(self):
        return {
            "imsmanifest.xml": MANIFEST,
            "wiki_content/page.html": PAGE,
            "web_resources/movie.mp4": b"excluded",
            "web_resources/pic.png": b"excluded-image",
            "web_resources/notes.txt": "Use microscopy to study cells.",
            "course_settings/syllabus.html": "<p>Week one: cells.</p>",
        }

    def make_package(self):
        self.write_zip(self.archive, self.entries())
        return preparer.prepare_course(self.archive, self.output)

    def unpack_package(self):
        with zipfile.ZipFile(self.output) as archive:
            return {name: archive.read(name) for name in archive.namelist()}

    def mutate_json(self, entries, prefix, mutate):
        name = next(n for n in entries if n.startswith(prefix) and n.endswith(".json"))
        data = json.loads(entries[name])
        mutate(data)
        entries[name] = json.dumps(data)

    def reject_package(self, entries):
        bad = self.root / "bad.haxcourse"
        self.write_zip(bad, entries)
        with self.assertRaises(imscc_import.IMSCCImportError):
            package.import_course_package(bad, self.exports)
        self.assertEqual(list(self.exports.iterdir()) if self.exports.exists() else [], [])

    def test_roundtrip_namespaces_ids_and_references(self):
        manifest = self.make_package()
        self.assertEqual(manifest["source_type"], "prepared")
        self.assertEqual(manifest["counts"]["pages"], 1)
        self.assertEqual({e["path"] for e in manifest["exclusions"]},
                         {"web_resources/movie.mp4", "web_resources/pic.png"})
        self.assertTrue(any("Missing archive reference" in w for w in manifest["warnings"]))
        entries = self.unpack_package()
        supplied_summary = json.loads(entries["course/export_summary.json"])
        self.assertNotIn("export_dir", supplied_summary)
        self.assertNotIn("export_directory", supplied_summary)
        first = package.import_course_package(self.output, self.exports)
        second = package.import_course_package(self.output, self.exports)
        self.assertNotEqual(first["course_id"], second["course_id"])
        self.assertRegex(first["course_id"], r"^imscc-[0-9a-f]{32}$")
        self.assertEqual(first["source_type"], "imscc")
        self.assertEqual(first["provenance"]["source"], "prepared")
        self.assertTrue(first["provenance"]["media_excluded"])
        destination = Path(first["export_dir"])
        self.assertEqual(first, json.loads((destination / "export_summary.json").read_text()))
        page = json.loads(next((destination / "pages").glob("*.json")).read_text())
        module = json.loads(next((destination / "modules").glob("*.json")).read_text())
        self.assertTrue(page["id"].startswith(first["course_id"]))
        self.assertEqual(module["items"][0]["content_id"], page["id"])
        resource = first["resource_paths"]["wiki_content/page.html"]
        self.assertEqual(resource["id"], page["id"])
        self.assertEqual(resource["target"], page["url"])
        file = json.loads(next((destination / "files").glob("*.json")).read_text())
        self.assertEqual((destination / "files" / file["filename"]).read_text(),
                         self.entries()["web_resources/notes.txt"])
        self.assertEqual(first["resource_paths"]["web_resources/notes.txt"]["target"], file["filename"])
        self.assertEqual(first["resource_identifiers"], ["page", "media", "doc"])

    def test_all_normalizer_record_types(self):
        entries = self.entries()
        entries.update({
            "assignment/task.html": "<title>Lab</title><p>Write a report.</p>",
            "assignment/assignment_settings.xml": "<assignment><title>Lab</title>"
                "<description>Write a report.</description><points_possible>5</points_possible></assignment>",
            "q/assessment_qti.xml": "<questestinterop><assessment title='Quiz'><section>"
                "<item ident='q'><presentation><material><mattext>Explain cells.</mattext></material>"
                "<response_lid><render_choice><response_label ident='a'><material><mattext>Yes</mattext>"
                "</material></response_label></render_choice></response_lid></presentation>"
                "</item></section></assessment></questestinterop>",
            "discussion/topic.xml": "<topic><title>Discuss</title><text>Discuss cells.</text></topic>",
        })
        entries["imsmanifest.xml"] = MANIFEST.replace("</resources>", """
        <resource identifier="quiz" type="imsqti" href="q/assessment_qti.xml"/>
        <resource identifier="discuss" type="imsdt" href="discussion/topic.xml"/>
        </resources>""")
        self.write_zip(self.archive, entries)
        preparer.prepare_course(self.archive, self.output)
        result = package.import_course_package(self.output, self.exports)
        for folder in package.FOLDERS:
            self.assertEqual(result["counts"][folder], 1, folder)
        quiz = json.loads(next((Path(result["export_dir"]) / "quizzes").glob("*.json")).read_text())
        self.assertTrue(quiz["questions"][0]["id"].startswith(result["course_id"]))
        self.assertTrue(quiz["questions"][0]["answers"][0]["id"].startswith(result["course_id"]))

    def test_cli_roundtrip_and_bounded_summary(self):
        self.write_zip(self.archive, self.entries())
        command = [sys.executable, str(Path(preparer.__file__)), str(self.archive),
                   "--output", str(self.output)]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertLess(len(completed.stdout), 2000)
        self.assertLess(len(completed.stderr), 2000)
        self.assertIn("not a full backup", completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["excluded_entries"], 2)
        self.assertEqual(package.import_course_package(self.output, self.exports)["counts"]["pages"], 1)
        refused = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("already exists", refused.stderr)

    def test_sparse_two_gib_media_is_never_opened(self):
        media_size = 2 * 1024**3
        try:
            sparse_archive(self.archive, media_size)
        except OSError as exc:
            self.skipTest(f"Filesystem cannot create sparse 2 GiB fixture: {exc}")
        info = self.archive.stat()
        if not hasattr(info, "st_blocks") or info.st_blocks * 512 > 16 * package.MIB:
            self.skipTest("Filesystem does not expose/support low-allocation sparse fixture.")
        self.assertGreater(info.st_size, media_size)
        original_open = zipfile.ZipFile.open
        opened = []

        def guarded(archive, name, *args, **kwargs):
            entry_name = name.filename if isinstance(name, zipfile.ZipInfo) else name
            opened.append(entry_name)
            if entry_name == "web_resources/movie.mp4":
                raise AssertionError("Excluded media was opened")
            return original_open(archive, name, *args, **kwargs)

        with patch.object(zipfile.ZipFile, "open", guarded):
            manifest = preparer.prepare_course(self.archive, self.output)
        self.assertNotIn("web_resources/movie.mp4", opened)
        self.assertEqual(manifest["exclusions"][0]["size"], media_size)
        self.assertLess(self.output.stat().st_size, 100000)
        self.assertEqual(self.archive.stat().st_size, info.st_size)
        self.assertEqual(package.import_course_package(self.output, self.exports)["counts"]["pages"], 1)

    def test_overwrite_and_source_protection(self):
        self.make_package()
        before = self.output.read_bytes()
        with self.assertRaisesRegex(imscc_import.IMSCCImportError, "already exists"):
            preparer.prepare_course(self.archive, self.output)
        self.assertEqual(self.output.read_bytes(), before)
        original = self.archive.read_bytes()
        with self.assertRaises(imscc_import.IMSCCImportError):
            preparer.prepare_course(self.archive, self.archive)
        self.assertEqual(self.archive.read_bytes(), original)

    def test_no_documents_and_optional_active_document_exclusion(self):
        entries = self.entries()
        entries["web_resources/active.pdf"] = b"%PDF-1.7 /JavaScript (never execute)"
        entries["web_resources/hidden.txt"] = b"SQLite format 3\x00"
        self.write_zip(self.archive, entries)
        manifest = preparer.prepare_course(self.archive, self.output, include_documents=False)
        self.assertEqual(manifest["counts"]["files"], 0)
        self.assertEqual(len(manifest["exclusions"]), 5)
        second = self.root / "documents.haxcourse"
        manifest = preparer.prepare_course(self.archive, second)
        self.assertEqual(manifest["counts"]["files"], 1)
        self.assertEqual(len(manifest["exclusions"]), 4)

    def test_original_paths_duplicates_and_symlinks_rejected(self):
        cases = [
            [("imsmanifest.xml", MANIFEST), (name, "bad")] for name in (
                "../outside", "/outside", "C:/bad", "a\\bad", "a//b", "a/./b", "a/../b", "a\nb")]
        cases.extend([
            [("imsmanifest.xml", MANIFEST), ("same.txt", "x"), ("same.txt", "y")],
            [("imsmanifest.xml", MANIFEST), ("A.txt", "x"), ("a.txt", "y")],
            [("imsmanifest.xml", MANIFEST), ("a", "x"), ("a/b.txt", "y")],
        ])
        link = zipfile.ZipInfo("link.mp4")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        cases.append([("imsmanifest.xml", MANIFEST), (link, "outside")])
        for entries in cases:
            with self.subTest(entries=entries):
                self.write_zip(self.archive, entries)
                with self.assertRaises(imscc_import.IMSCCImportError):
                    preparer.prepare_course(self.archive, self.output)
                self.assertFalse(self.output.exists())

    def test_unsafe_unused_xml_rejected(self):
        for xml in ("<!DOCTYPE r><r/>", '<!DOCTYPE r [<!ENTITY e "bad">]><r>&e;</r>', "<r>"):
            with self.subTest(xml=xml):
                entries = self.entries()
                entries["unreferenced.xml"] = xml
                self.write_zip(self.archive, entries)
                with self.assertRaises(imscc_import.IMSCCImportError):
                    preparer.prepare_course(self.archive, self.output)
                self.assertFalse(self.output.exists())

    def test_limits_and_no_shared_limit_mutation(self):
        before = {k: getattr(imscc_import, k) for k in (
            "MAX_UPLOAD_BYTES", "MAX_ENTRY_BYTES", "MAX_EXPANDED_BYTES", "MAX_XML_BYTES", "MAX_ENTRIES")}
        self.write_zip(self.archive, self.entries())
        with self.assertRaisesRegex(imscc_import.IMSCCImportError, "input size"):
            preparer.prepare_course(self.archive, self.output, max_input_bytes=10)
        with patch.object(preparer, "MAX_KEPT_BYTES", 10):
            with self.assertRaisesRegex(imscc_import.IMSCCImportError, "64 MiB"):
                preparer.prepare_course(self.archive, self.output)
        with patch.object(preparer, "MAX_XML_BYTES", 10):
            with self.assertRaisesRegex(imscc_import.IMSCCImportError, "10 MiB"):
                preparer.prepare_course(self.archive, self.output)
        self.make_package()
        self.assertEqual(before, {k: getattr(imscc_import, k) for k in before})

    def test_hostile_package_archive_paths_and_active_files(self):
        self.make_package()
        original = self.unpack_package()
        for name in ("../outside", "course/.env", "course/state.sqlite",
                     "course/files/code.py", "course/files/active.html", "course/files/a.svg",
                     "course/pages/a.json/child.txt"):
            with self.subTest(name=name):
                self.reject_package({**original, name: "not allowed"})
        link = zipfile.ZipInfo("course/files/link.txt")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.reject_package(list(original.items()) + [(link, "outside")])
        self.reject_package(list(original.items()) + [("HAXCOURSE.JSON", original["haxcourse.json"])])

    def test_hostile_json_and_schema(self):
        self.make_package()
        original = self.unpack_package()
        for value in (b'{"version":1,"version":1}', b'{"a":NaN}', b'[]', b'{"a":'):
            with self.subTest(value=value):
                self.reject_package({**original, "haxcourse.json": value})
        mutations = [
            ("haxcourse", lambda d: d.update(version=2)),
            ("haxcourse", lambda d: d.update(version=True)),
            ("haxcourse", lambda d: d["counts"].update(pages=400)),
            ("haxcourse", lambda d: d.update(exclusions=[{"path": "../bad", "size": 1, "reason": "bad"}])),
            ("course/pages/", lambda d: d.update(body={"bad": "shape"})),
            ("course/pages/", lambda d: d.update(source_path="../bad")),
            ("course/pages/", lambda d: d.update(database="malicious.sqlite")),
            ("course/pages/", lambda d: d.update(published="yes")),
            ("course/files/", lambda d: d.update(filename="../../outside")),
            ("course/modules/", lambda d: d["items"][0].update(content_id="unknown")),
            ("course/export_summary", lambda d: d["resource_paths"]["wiki_content/page.html"].update(id="unknown")),
        ]
        for prefix, mutate in mutations:
            with self.subTest(prefix=prefix, mutate=mutate):
                entries = dict(original)
                self.mutate_json(entries, prefix, mutate)
                self.reject_package(entries)

    def test_supplied_ids_targets_and_export_paths_never_become_destinations(self):
        self.make_package()
        entries = self.unpack_package()
        old_summary = json.loads(entries["course/export_summary.json"])
        old_page = json.loads(next(v for k, v in entries.items() if k.startswith("course/pages/")))
        old_id = old_page["id"]
        evil_id = "../../write-outside"
        self.mutate_json(entries, "course/pages/", lambda d: d.update(id=evil_id, url="../../evil"))
        self.mutate_json(entries, "course/modules/",
                         lambda d: d["items"][0].update(content_id=evil_id))
        old_summary.update(export_dir="/outside", export_directory="/outside", course_id="evil")
        for reference in old_summary["resource_paths"].values():
            if reference["id"] == old_id:
                reference["id"] = evil_id
                reference["target"] = "../../outside"
        entries["course/export_summary.json"] = json.dumps(old_summary)
        self.write_zip(self.output, entries)
        result = package.import_course_package(self.output, self.exports)
        self.assertTrue(Path(result["export_dir"]).is_relative_to(self.exports.resolve()))
        self.assertNotIn(evil_id, json.dumps(result))
        page = json.loads(next((Path(result["export_dir"]) / "pages").glob("*.json")).read_text())
        self.assertTrue(page["url"].startswith("page-imscc-"))

    def test_package_size_and_expansion_caps_cleanup(self):
        self.make_package()
        with patch.object(package, "MAX_PACKAGE_BYTES", 10):
            with self.assertRaises(imscc_import.IMSCCImportError):
                package.import_course_package(self.output, self.exports)
        with patch.object(package, "MAX_PACKAGE_EXPANDED", 10):
            with self.assertRaises(imscc_import.IMSCCImportError):
                package.import_course_package(self.output, self.exports)
        with patch.object(package, "MAX_JSON_BYTES", 10):
            with self.assertRaises(imscc_import.IMSCCImportError):
                package.import_course_package(self.output, self.exports)
        self.assertFalse(self.exports.exists())
        with patch.object(preparer.os, "link", side_effect=OSError("simulated race or disk error")):
            destination = self.root / "failed.haxcourse"
            with self.assertRaises(imscc_import.IMSCCImportError):
                preparer.prepare_course(self.archive, destination)
        self.assertFalse(destination.exists())
        self.assertFalse(list(self.root.glob(".haxcourse-*")))
        self.assertFalse(list(self.root.glob(".hax-prepare-*")))

    def test_office_documents_are_bounded_and_macro_free(self):
        with io.BytesIO() as buffer:
            with zipfile.ZipFile(buffer, "w") as doc:
                doc.writestr("[Content_Types].xml", "<Types/>")
                doc.writestr("word/document.xml", "<document><p>Read this.</p></document>")
            good = buffer.getvalue()
        package.validate_document(good, ".docx")
        for name, value in (
                ("word/vbaProject.bin", b"code"),
                ("word/embeddings/database.sqlite", b"SQLite format 3"),
                ("word/_rels/document.xml.rels",
                 b'<Relationships><Relationship TargetMode="External" Target="file:///bad"/></Relationships>'),
                ("word/unsafe.xml", b"<!DOCTYPE a><a/>")):
            with self.subTest(name=name), io.BytesIO() as buffer:
                with zipfile.ZipFile(buffer, "w") as doc:
                    doc.writestr("[Content_Types].xml", "<Types/>")
                    doc.writestr(name, value)
                with self.assertRaises(imscc_import.IMSCCImportError):
                    package.validate_document(buffer.getvalue(), ".docx")

    def test_small_document_roundtrip(self):
        entries = self.entries()
        entries["web_resources/reading.pdf"] = b"%PDF-1.4\n%Synthetic offline fixture\n%%EOF"
        for suffix in ("docx", "pptx"):
            with io.BytesIO() as buffer:
                with zipfile.ZipFile(buffer, "w") as document:
                    document.writestr("[Content_Types].xml", "<Types/>")
                    document.writestr("word/document.xml" if suffix == "docx" else "ppt/slides/slide1.xml",
                                      "<document><p>Cell theory.</p></document>")
                entries["web_resources/notes." + suffix] = buffer.getvalue()
        self.write_zip(self.archive, entries)
        result = preparer.prepare_course(self.archive, self.output)
        self.assertEqual(result["counts"]["files"], 4)
        imported = package.import_course_package(self.output, self.exports)
        self.assertEqual(imported["counts"]["files"], 4)

    def test_atomic_publication_does_not_replace_concurrent_destination(self):
        self.write_zip(self.archive, self.entries())
        real_link = os.link

        def race(source, destination):
            Path(destination).write_bytes(b"concurrent writer")
            return real_link(source, destination)

        with patch.object(preparer.os, "link", side_effect=race):
            with self.assertRaises(imscc_import.IMSCCImportError):
                preparer.prepare_course(self.archive, self.output)
        self.assertEqual(self.output.read_bytes(), b"concurrent writer")
        self.assertFalse(list(self.root.glob(".haxcourse-*")))
        self.assertFalse(list(self.root.glob(".hax-prepare-*")))

    def test_central_directory_bound_and_declared_expansion(self):
        self.write_zip(self.archive, self.entries())
        with patch.object(package, "MAX_CENTRAL_BYTES", 1):
            with self.assertRaisesRegex(imscc_import.IMSCCImportError, "central directory"):
                preparer.prepare_course(self.archive, self.output)
        with zipfile.ZipFile(self.archive) as archive:
            with self.assertRaisesRegex(imscc_import.IMSCCImportError, "declared expansion"):
                package.inspect_zip(archive, max_expanded=1)
            with self.assertRaisesRegex(imscc_import.IMSCCImportError, "too many entries"):
                package.inspect_zip(archive, max_entries=1)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
