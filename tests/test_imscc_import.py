"""Synthetic cartridge tests: no network or user data."""
import json
from pathlib import Path
import stat
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
import imscc_import as importer


def manifest(resources="", organizations="", version="1p1"):
    return f"""<manifest xmlns="http://www.imsglobal.org/xsd/imsccv{version}/imscp_v1p1" identifier="manifest-original">
    <metadata><lom xmlns="http://ltsc.ieee.org/xsd/LOM"><general><title><string>Biology &amp; Art</string></title>
    </general></lom></metadata><organizations>{organizations}</organizations>
    <resources>{resources}</resources></manifest>"""


def resource(identifier, kind, href="", files=(), dependencies=()):
    return (f'<resource identifier="{identifier}" type="{kind}" href="{href}">'
            + "".join(f'<file href="{f}"/>' for f in files)
            + "".join(f'<dependency identifierref="{d}"/>' for d in dependencies)
            + "</resource>")


PAGE = """<html><head><title>Welcome</title><meta name="workflow_state" content="unpublished"/></head>
<body><p>Welcome to this detailed introductory learning page.</p>
<img src="$IMS-CC-FILEBASE$/images/cell.png"/><a href="assignment/task.html">Work</a></body></html>"""
ASSIGNMENT = """<assignment xmlns="http://canvas.instructure.com/xsd/cccv1p0" identifier="original">
<title>Lab assignment</title><points_possible>12.5</points_possible>
<submission_types>online_upload,online_text_entry</submission_types>
<due_at>2026-11-01T00:00:00Z</due_at><workflow_state>published</workflow_state></assignment>"""
QTI = """<questestinterop xmlns="http://www.imsglobal.org/xsd/ims_qtiasiv1p2">
<assessment ident="q" title="Cell quiz"><section ident="s"><item ident="question" title="Cell parts">
<itemmetadata><qtimetadata><qtimetadatafield><fieldlabel>question_type</fieldlabel>
<fieldentry>multiple_choice_question</fieldentry></qtimetadatafield></qtimetadata></itemmetadata>
<presentation><material><mattext>&lt;p&gt;Which is a cell part?&lt;/p&gt;</mattext></material>
<response_lid><render_choice><response_label ident="a"><material><mattext>Nucleus</mattext></material>
</response_label><response_label ident="b"><material><mattext>Rock</mattext></material>
</response_label></render_choice></response_lid></presentation></item></section></assessment></questestinterop>"""
DISCUSSION = """<topic xmlns="http://www.imsglobal.org/xsd/imsdt_v1p1" identifier="topic">
<title>Discuss cells</title><text>&lt;p&gt;Discuss the relationship between cells and art.&lt;/p&gt;</text></topic>"""


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / "exports"
        self.archive = self.root / "course.imscc"

    def tearDown(self):
        self.temp.cleanup()

    def make(self, entries):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, value in entries.items() if isinstance(entries, dict) else entries:
                    archive.writestr(name, value)

    def run_import(self, entries):
        self.make(entries)
        return importer.import_imscc(self.archive, self.output)

    def rejected(self, entries):
        self.make(entries)
        with self.assertRaises(importer.IMSCCImportError):
            importer.import_imscc(self.archive, self.output)
        self.assertEqual(list(self.output.iterdir()) if self.output.exists() else [], [])

    def items(self, summary, folder):
        return [json.loads(p.read_text()) for p in (Path(summary["export_dir"]) / folder).glob("*.json")]

    def complete_entries(self):
        return {
            "imsmanifest.xml": manifest(
                resource("page", "webcontent", "wiki_content/welcome.html")
                + resource("task", "webcontent", "assignment/task.html", dependencies=["task-meta"])
                + resource("task-meta", "associatedcontent", files=["assignment/assignment_settings.xml"])
                + resource("quiz", "imsqti_xmlv1p2", files=["quiz/assessment_qti.xml", "quiz/assessment_meta.xml"])
                + resource("talk", "imsdt_xmlv1p1", files=["discussion/topic.xml", "discussion/topicMeta.xml"])
                + resource("image", "webcontent", "web_resources/images/cell.png")),
            "wiki_content/welcome.html": PAGE,
            "assignment/task.html": "<title>Task</title><p>Write a detailed laboratory report.</p>",
            "assignment/assignment_settings.xml": ASSIGNMENT,
            "quiz/assessment_qti.xml": QTI,
            "quiz/assessment_meta.xml": "<quiz><title>Canvas quiz title</title><published>false</published></quiz>",
            "discussion/topic.xml": DISCUSSION,
            "discussion/topicMeta.xml": "<topicMeta><workflow_state>unpublished</workflow_state></topicMeta>",
            "web_resources/images/cell.png": b"\x89PNG\r\nsynthetic",
            "course_settings/syllabus.html": "<p>Course syllabus and schedule.</p>",
            "course_settings/module_meta.xml": """<modules xmlns="http://canvas.instructure.com/xsd/cccv1p0">
            <module identifier="module-original"><title>Week 1</title><position>1</position>
            <prerequisites>prior-module</prerequisites><items><item identifier="mi"><title>Read</title>
            <content_type>WikiPage</content_type><identifierref>page</identifierref><position>1</position>
            <indent>2</indent></item></items></module></modules>""",
        }

    def test_canvas_11_complete_normalization(self):
        result = self.run_import(self.complete_entries())
        output = Path(result["export_dir"])
        self.assertEqual(result["course_name"], "Biology & Art")
        self.assertEqual(result["source_type"], "imscc")
        self.assertRegex(result["course_id"], r"^imscc-[a-f0-9]{32}$")
        self.assertEqual(result["counts"], dict(pages=1, assignments=1, quizzes=1, discussions=1, files=1, modules=1, syllabus=1))
        self.assertTrue(output.is_absolute())
        self.assertEqual(json.loads((output / "export_summary.json").read_text()), result)
        self.assertEqual(result["manifest_identifier"], "manifest-original")
        self.assertEqual(result["original_filename"], "course.imscc")
        page = self.items(result, "pages")[0]
        self.assertEqual(page["body"], PAGE)
        self.assertFalse(page["published"])
        self.assertEqual(result["resource_paths"]["wiki_content/welcome.html"]["target"], page["url"])
        assignment = self.items(result, "assignments")[0]
        self.assertEqual(assignment["name"], "Lab assignment")
        self.assertEqual(assignment["points_possible"], 12.5)
        self.assertEqual(assignment["due_at"], "2026-11-01T00:00:00Z")
        self.assertEqual(assignment["submission_types"], ["online_upload", "online_text_entry"])
        quiz = self.items(result, "quizzes")[0]
        self.assertFalse(quiz["published"])
        self.assertEqual(quiz["title"], "Canvas quiz title")
        self.assertEqual(quiz["questions"][0]["question_text"], "<p>Which is a cell part?</p>")
        self.assertEqual([a["text"] for a in quiz["questions"][0]["answers"]], ["Nucleus", "Rock"])
        discussion = self.items(result, "discussions")[0]
        self.assertEqual(discussion["title"], "Discuss cells")
        self.assertIn("relationship", discussion["message"])
        self.assertFalse(discussion["published"])
        asset = result["resource_paths"]["web_resources/images/cell.png"]
        self.assertEqual((output / "files" / asset["target"]).read_bytes(), b"\x89PNG\r\nsynthetic")
        self.assertEqual(self.items(result, "files")[0]["filename"], asset["target"])
        self.assertEqual(self.items(result, "syllabus")[0]["body"], (output / "syllabus.html").read_text())
        module = self.items(result, "modules")[0]
        self.assertEqual(module["items"][0]["content_id"], page["id"])
        self.assertEqual(module["items"][0]["indent"], "2")
        self.assertIn("prerequisites", json.dumps(module["source_metadata"]))
        for folder in ("pages", "assignments", "quizzes", "discussions", "syllabus", "files", "modules"):
            item = self.items(result, folder)[0]
            self.assertEqual(item["source_type"], "imscc")
            self.assertTrue(item["source_path"])
            self.assertTrue(item["id"].startswith(result["course_id"]))
        self.assertTrue(any("Partial quiz coverage" in w for w in result["warnings"]))
        self.assertFalse(list(self.output.glob(".imscc-*")))

    def test_cc13_namespace_and_manifest_modules(self):
        result = self.run_import({
            "imsmanifest.xml": manifest(resource("p", "webcontent", "wiki_content/p.html"),
                '<organization><item identifier="m"><title>Module</title><item identifierref="p"><title>Page</title></item></item></organization>',
                version="1p3"),
            "wiki_content/p.html": PAGE,
        })
        self.assertEqual(result["counts"]["modules"], 1)
        self.assertEqual(result["counts"]["pages"], 1)

    def test_fresh_course_scoped_ids(self):
        first = self.run_import({"imsmanifest.xml": manifest(), "wiki_content/p.html": PAGE})
        second = importer.import_imscc(self.archive, self.output)
        self.assertNotEqual(first["course_id"], second["course_id"])
        self.assertNotEqual(self.items(first, "pages")[0]["id"], self.items(second, "pages")[0]["id"])

    def test_duplicate_resources_and_companion_assignment_settings(self):
        result = self.run_import({
            "imsmanifest.xml": manifest(resource("a", "webcontent", "a/task.html")
                                       + resource("alias", "webcontent", "a/task.html")),
            "a/task.html": "<title>Task</title><p>Laboratory report.</p>",
            "a/assignment_settings.xml": ASSIGNMENT,
        })
        self.assertEqual(result["counts"]["assignments"], 1)
        self.assertEqual(result["counts"]["pages"], 0)

    def test_standalone_associated_assignment(self):
        result = self.run_import({
            "imsmanifest.xml": manifest(resource("task", "associatedcontent/imscc_xmlv1p1/learning-application-resource",
                                                "task/assignment_settings.xml")),
            "task/assignment_settings.xml": ASSIGNMENT.replace(
                "</assignment>", "<description>&lt;p&gt;Describe the laboratory results.&lt;/p&gt;</description></assignment>"),
        })
        self.assertEqual(result["counts"]["assignments"], 1)
        self.assertIn("laboratory results", self.items(result, "assignments")[0]["description"])

    def test_real_canvas_lor_assignment_guid_html_and_syllabus(self):
        result = self.run_import({
            "imsmanifest.xml": manifest(
                resource("assignment-guid", "associatedcontent/imscc_xmlv1p1/learning-application-resource",
                         "g9f83/biology-report.html", files=["g9f83/assignment_settings.xml"])
                + resource("settings", "associatedcontent/imscc_xmlv1p1/learning-application-resource",
                           files=["course_settings/syllabus.html"])),
            "g9f83/biology-report.html": "<title>Biology report</title><p>Explain the laboratory findings in detail.</p>",
            "g9f83/assignment_settings.xml": ASSIGNMENT,
            "course_settings/syllabus.html": "<p>Real Canvas syllabus.</p>",
        })
        self.assertEqual(result["counts"]["assignments"], 1)
        self.assertEqual(result["counts"]["pages"], 0)
        self.assertEqual(result["counts"]["syllabus"], 1)
        assignment = self.items(result, "assignments")[0]
        self.assertEqual(assignment["source_path"], "g9f83/biology-report.html")
        self.assertIn("laboratory findings", assignment["description"])
        self.assertEqual(assignment["points_possible"], 12.5)

    def test_dependent_assignment_metadata_does_not_reclassify_page(self):
        result = self.run_import({
            "imsmanifest.xml": manifest(
                resource("page", "webcontent", "wiki_content/page.html", dependencies=["other"])
                + resource("other", "associatedcontent/imscc_xmlv1p1/learning-application-resource",
                           "other/task.html", files=["other/assignment_settings.xml"])),
            "wiki_content/page.html": PAGE,
            "other/task.html": "<title>Task</title><p>Explain the laboratory findings.</p>",
            "other/assignment_settings.xml": ASSIGNMENT,
        })
        self.assertEqual(result["counts"]["pages"], 1)
        self.assertEqual(result["counts"]["assignments"], 1)
        self.assertEqual(self.items(result, "pages")[0]["title"], "Welcome")

    def test_syllabus_manifest_resource(self):
        result = self.run_import({
            "imsmanifest.xml": manifest(resource("s", "webcontent", "course_settings/syllabus.html")),
            "course_settings/syllabus.html": "<p>Syllabus</p>",
        })
        self.assertEqual(result["counts"]["syllabus"], 1)
        self.assertEqual(result["counts"]["pages"], 0)

    def test_qti2_referenced_text(self):
        result = self.run_import({
            "imsmanifest.xml": manifest(resource("new", "imsqti_xmlv2p1", "quiz/test.xml")),
            "quiz/test.xml": '<assessmentTest xmlns="http://www.imsglobal.org/xsd/imsqti_v2p1" title="New quiz"><assessmentItemRef href="items/q.xml"/></assessmentTest>',
            "quiz/items/q.xml": '<assessmentItem xmlns="http://www.imsglobal.org/xsd/imsqti_v2p1" identifier="q"><itemBody><p>What is a cell?</p><choiceInteraction><simpleChoice identifier="a">An organism</simpleChoice></choiceInteraction></itemBody></assessmentItem>',
        })
        question = self.items(result, "quizzes")[0]["questions"][0]
        self.assertIn("What is a cell?", question["question_text"])
        self.assertEqual(question["answers"][0]["text"], "An organism")
        self.assertTrue(any("Partial quiz coverage" in w for w in result["warnings"]))

    def test_warnings_empty_quiz_external_missing_and_unsupported(self):
        result = self.run_import({
            "imsmanifest.xml": manifest(resource("empty", "imsqti_xmlv1p2", "q.xml")
                + resource("lti", "imsbasiclti_xmlv1p0", "external.xml")
                + resource("missing", "webcontent", "absent.html")
                + resource("unknown", "vendor/unsupported", "other.xml")
                + resource("external", "webcontent", "https://example.test/never-fetched")),
            "q.xml": "<questestinterop/>", "external.xml": "<lti/>", "other.xml": "<vendor/>",
            "web_resources/run.js": "never execute",
        })
        text = "\n".join(result["warnings"])
        for message in ("missing usable question text", "External LTI", "Missing archive reference",
                        "Unhandled resource", "External resource", "No supported instructional", "asset not imported"):
            self.assertIn(message, text)
        self.assertFalse((Path(result["export_dir"]) / "web_resources").exists())

    def test_missing_manifest_and_bad_xml(self):
        for entries in ({}, {"imsmanifest.xml": "<manifest>"}, {"imsmanifest.xml": "<not_a_manifest/>"}):
            with self.subTest(entries=entries):
                self.rejected(entries)

    def test_malformed_zip(self):
        self.archive.write_bytes(b"not a zip")
        with self.assertRaises(importer.IMSCCImportError):
            importer.import_imscc(self.archive, self.output)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_dtd_entities_in_unused_xml(self):
        for xml in ('<!DOCTYPE m><m/>', '<!DOCTYPE m [<!ENTITY e "boom">]><m>&e;</m>',
                    '<!DOCTYPE m [<!ENTITY e SYSTEM "file:///etc/passwd">]><m>&e;</m>'):
            with self.subTest(xml=xml):
                self.rejected({"imsmanifest.xml": manifest(), "unused.xml": xml})

    def test_path_attacks(self):
        for path in ("../escape.txt", "/absolute.txt", "C:/drive.txt", "C:drive.txt", "foo\\bar.txt",
                     "foo/../bar.txt", "./foo.txt", "//host/share.txt", "foo//bar.txt", "bad\nname.txt"):
            with self.subTest(path=path):
                self.rejected({"imsmanifest.xml": manifest(), path: "unsafe"})

    def test_links_and_special_files(self):
        for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK):
            link = zipfile.ZipInfo("link")
            link.create_system = 3
            link.external_attr = (mode | 0o777) << 16
            with self.subTest(mode=mode):
                self.rejected([("imsmanifest.xml", manifest()), (link, "target")])

    def test_duplicate_and_case_colliding_paths(self):
        for paths in (("same.txt", "same.txt"), ("Asset.txt", "asset.txt")):
            with self.subTest(paths=paths):
                self.rejected([("imsmanifest.xml", manifest()), (paths[0], "a"), (paths[1], "b")])

    def test_encrypted_entry(self):
        self.make({"imsmanifest.xml": manifest()})
        data = bytearray(self.archive.read_bytes())
        central = data.index(b"PK\x01\x02")
        flags = struct.unpack_from("<H", data, central + 8)[0]
        struct.pack_into("<H", data, central + 8, flags | 1)
        self.archive.write_bytes(data)
        with self.assertRaisesRegex(importer.IMSCCImportError, "Encrypted"):
            importer.import_imscc(self.archive, self.output)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_limits_patched_small(self):
        for constant, value in (("MAX_UPLOAD_BYTES", 1), ("MAX_ENTRIES", 1), ("MAX_EXPANDED_BYTES", 100),
                                ("MAX_ENTRY_BYTES", 100), ("MAX_XML_BYTES", 20)):
            with self.subTest(constant=constant), patch.object(importer, constant, value):
                self.rejected({"imsmanifest.xml": manifest(), "asset.txt": "x" * 1024})

    def test_ratio_limit_and_small_file_tolerance(self):
        self.rejected({"imsmanifest.xml": manifest(), "large.txt": "x" * (1024 * 1024 + 1)})
        result = self.run_import({"imsmanifest.xml": manifest(), "small.txt": "x" * 10000})
        self.assertEqual(result["counts"]["files"], 1)

    def test_xml_depth_limit(self):
        with patch.object(importer, "MAX_XML_DEPTH", 3):
            self.rejected({"imsmanifest.xml": "<manifest><a><b><c/></b></a></manifest>"})

    def test_cleanup_after_normalization_write_failure(self):
        self.make(self.complete_entries())
        with patch.object(importer._Cartridge, "assets", side_effect=OSError("disk full")):
            with self.assertRaises(importer.IMSCCImportError):
                importer.import_imscc(self.archive, self.output)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_source_identifiers_and_missing_module_refs(self):
        entries = self.complete_entries()
        entries["course_settings/module_meta.xml"] = entries["course_settings/module_meta.xml"].replace(
            "<identifierref>page</identifierref>", "<identifierref>unavailable</identifierref>")
        result = self.run_import(entries)
        self.assertEqual(self.items(result, "assignments")[0]["source_identifier"], "task")
        self.assertIn("task", result["resource_identifiers"])
        self.assertTrue(any("Module reference" in w for w in result["warnings"]))

    def test_encoded_reference_attacks(self):
        result = self.run_import({
            "imsmanifest.xml": manifest(resource("escape", "webcontent", "%2e%2e/secret.html")
                                       + resource("windows", "webcontent", "C:%5csecret.html")),
        })
        self.assertEqual(result["counts"]["pages"], 0)
        self.assertTrue(any("Unsafe resource reference" in w or "External resource" in w for w in result["warnings"]))

    def test_cyclic_dependencies(self):
        result = self.run_import({
            "imsmanifest.xml": manifest(resource("one", "webcontent", "wiki_content/p.html", dependencies=["two"])
                                       + resource("two", "associatedcontent", dependencies=["one"])),
            "wiki_content/p.html": PAGE,
        })
        self.assertEqual(result["counts"]["pages"], 1)


if __name__ == "__main__":
    unittest.main()
