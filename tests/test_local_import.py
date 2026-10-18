"""Local import integration tests; no Canvas, AI or npm requests are made."""

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
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "python" / "app")]

import app as web
import pipeline_runner
import setup_wizard
from imscc_content import (
    ITEM_PREFIX, original_import_html, prepare_import_content,
    preserve_import_resources, rewrite_import_item_links, sanitize_import_html,
)


def cartridge_bytes():
    manifest = """<manifest identifier="course-source"
      xmlns="http://www.imsglobal.org/xsd/imsccv1p1/imscp_v1p1">
      <metadata><schema>IMS Common Cartridge</schema><schemaversion>1.1.0</schemaversion>
      <lom><general><title><string>CYBER 100 Sample</string></title></general></lom></metadata>
      <organizations/><resources>
      <resource identifier="page-one" type="webcontent" href="wiki_content/overview.html">
        <file href="wiki_content/overview.html"/></resource>
      <resource identifier="picture" type="webcontent" href="web_resources/picture.png">
        <file href="web_resources/picture.png"/></resource>
      </resources></manifest>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("imsmanifest.xml", manifest)
        archive.writestr("wiki_content/overview.html", """
          <html><head><title>Overview</title><meta name="identifier" content="page-one"></head>
          <body><p>This instructional page explains how to evaluate evidence carefully.</p>
          <img src="$IMS-CC-FILEBASE$/picture.png" alt="Evidence diagram"></body></html>""")
        archive.writestr("web_resources/picture.png", b"\x89PNG\r\n\x1a\nfixture")
    return buf.getvalue()


class LocalImportRoutesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root_patch = patch.object(web, "PROJECT_ROOT", self.root)
        self.root_patch.start()
        self.env_patch = patch.object(web, "load_env_values", return_value={})
        self.env_patch.start()
        self.os_patch = patch.dict(os.environ, {}, clear=True)
        self.os_patch.start()
        web.app.config["TESTING"] = True
        self.client = web.app.test_client()

    def tearDown(self):
        self.os_patch.stop()
        self.env_patch.stop()
        self.root_patch.stop()
        self.temp.cleanup()

    def upload(self, filename="course.imscc", content=None):
        return self.client.post("/run/import-imscc", data={
            "course_file": (io.BytesIO(cartridge_bytes() if content is None else content), filename)
        })

    def test_run_page_exposes_both_sources_without_token_gate(self):
        with patch.object(web, "run_all_checks", return_value={"all_ok": True}):
            response = self.client.get("/run")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'accept=".imscc"', response.data)
        self.assertIn(b'Use a local Canvas export file', response.data)
        self.assertNotIn(b'onclick="startRun()" disabled', response.data)

    def test_import_then_run_without_canvas_credentials(self):
        response = self.upload()
        self.assertEqual(response.status_code, 200, response.json)
        preview = response.json
        self.assertRegex(preview["course_id"], r"^imscc-[a-f0-9]{32}$")
        self.assertEqual(preview["counts"]["pages"], 1)
        self.assertNotIn("export_dir", preview)
        with patch.object(web, "start_run", return_value="test-run") as start:
            response = self.client.post("/run/start", json={
                "source_type": "imscc", "course_id": preview["course_id"], "no_ai": True,
                "do_build": False, "do_npm_install": False, "do_serve": False,
            })
        self.assertEqual(response.status_code, 200, response.json)
        self.assertFalse(start.call_args.args[1]["do_export"])
        self.assertFalse(start.call_args.args[1]["skip_extract"])

    def test_api_source_still_requires_credentials(self):
        with patch.object(web, "start_run") as start:
            response = self.client.post("/run/start", json={"course_id": "123", "no_ai": True})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Canvas URL and token", response.json["error"])
        start.assert_not_called()

    def test_api_request_remains_supported(self):
        with patch.object(web, "load_env_values", return_value={
            "CANVAS_URL": "https://canvas.example.edu", "CANVAS_TOKEN": "test-only"
        }), patch.object(web, "start_run", return_value="api-run") as start:
            response = self.client.post("/run/start", json={"course_id": "123", "no_ai": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(start.call_args.args[1]["source_type"], "api")
        self.assertTrue(start.call_args.args[1]["do_export"])

    def test_missing_ai_key_is_actionable(self):
        preview = self.upload().json
        with patch.object(web, "start_run") as start:
            response = self.client.post("/run/start", json={
                "source_type": "imscc", "course_id": preview["course_id"],
            })
        self.assertEqual(response.status_code, 400)
        self.assertIn("Skip AI", response.json["error"])
        start.assert_not_called()

    def test_invalid_imports_do_not_start_jobs_or_leave_exports(self):
        with patch.object(web, "start_run") as start:
            self.assertEqual(self.upload("course.zip").status_code, 400)
            self.assertEqual(self.upload(content=b"not a zip").status_code, 400)
            self.assertEqual(self.client.post("/run/import-imscc").status_code, 400)
        start.assert_not_called()
        self.assertFalse(list((self.root / "exports").glob("course_*")))

    def test_failed_preparation_cleans_new_export(self):
        with patch("imscc_content.prepare_import_content", side_effect=ValueError("bad resource")):
            response = self.upload()
        self.assertEqual(response.status_code, 400)
        self.assertFalse(list((self.root / "exports").glob("course_*")))

    def test_upload_size_is_bounded(self):
        with patch.object(web, "MAX_IMSCC_UPLOAD", 8):
            response = self.upload()
        self.assertEqual(response.status_code, 413)

    def test_cross_origin_import_and_run_are_rejected(self):
        headers = {"Origin": "https://untrusted.example"}
        self.assertEqual(self.client.post("/run/import-imscc", headers=headers).status_code, 403)
        self.assertEqual(self.client.post("/run/start", json={}, headers=headers).status_code, 403)

    def test_run_options_and_import_identity_are_validated(self):
        for data in (
            {"source_type": "unknown", "course_id": "123"},
            {"source_type": "imscc", "course_id": "../outside"},
            {"source_type": "imscc", "course_id": "imscc-" + "a" * 32},
            {"course_id": "123", "workers": "bad"},
            {"course_id": "123", "workers": 0},
            {"course_id": "123", "model_provider": "unknown"},
        ):
            with self.subTest(data=data), patch.object(web, "start_run") as start:
                self.assertEqual(self.client.post("/run/start", json=data).status_code, 400)
                start.assert_not_called()


class ImportedContentTests(unittest.TestCase):
    def test_passive_html_retains_resources_not_scripts(self):
        result = sanitize_import_html(
            '<p onclick="bad()">Good</p><script>bad()</script><svg onload="bad()"></svg>'
            '<a href="java&#10;script:bad()">Unsafe</a>'
            '<img src="files/photo.png" onerror="bad()"><input autofocus onfocus="bad()">'
        )
        for bad in ("onclick", "<script", "<svg", "javascript", "onerror", "<input", "onfocus"):
            self.assertNotIn(bad, result)
        self.assertIn('src="files/photo.png"', result)

    def test_original_import_is_not_truncated_and_keeps_question_text(self):
        raw = {"title": "Course", "body": "<p>" + "long content " * 500 + "</p>",
               "questions": [{"question_text": "<strong>Explain your evidence.</strong>",
                              "answers": [{"text": "First choice"}, {"text": "Second choice"}]}]}
        result = original_import_html(raw)
        self.assertIn(raw["body"], result)
        self.assertIn("<strong>Explain your evidence.</strong>", result)
        self.assertIn("First choice", result)
        self.assertIn("Second choice", result)

    def test_hashed_imported_pdf_syllabus_is_extracted_and_rendered(self):
        from extract import extract_course_data, find_latest_syllabus
        from sqlite_client import SQLiteClient
        from transform import transform_course_data

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = root / "files"
            files.mkdir()
            pdf = files / ("a" * 64 + ".pdf")
            pdf.write_bytes(b"test fixture; PDF text extraction is mocked")
            (files / "metadata.json").write_text(json.dumps({
                "filename": pdf.name, "display_name": "Syllabus.pdf",
                "source_path": "web_resources/Syllabus.pdf", "source_type": "imscc",
            }))
            self.assertEqual(find_latest_syllabus(files), pdf)
            course_id = "imscc-" + "b" * 32
            with SQLiteClient(str(root / "test.db")) as db, \
                 patch("extract.extract_syllabus_text", return_value="Original syllabus content."), \
                 patch("extract.get_export_dir", return_value=root):
                extract_course_data(db, course_id, root)
                self.assertEqual(len(db.get_pending_items(course_id)), 1)
                transform_course_data(db, course_id, no_ai=True)
                self.assertIn("Original syllabus content.",
                              db.get_completed_items(course_id)[0]["ai_enhanced_markdown"])

    def test_ai_output_keeps_original_media_and_links(self):
        result = preserve_import_resources(
            {"body": '<a href="files/report.pdf">Report</a><img src="files/photo.png">'},
            "<p>Improved explanation.</p><script>bad()</script>",
        )
        self.assertIn("Original course resources", result)
        self.assertIn('href="files/report.pdf"', result)
        self.assertIn('src="files/photo.png"', result)
        self.assertNotIn("<script", result)

    def test_cross_item_links_are_resolved_or_disabled(self):
        result = rewrite_import_item_links(
            f'<a href="{ITEM_PREFIX}item1#example">Page</a>'
            f'<a href="{ITEM_PREFIX}missing">Missing</a>',
            {"item1": "pages/generated/index.html"},
        )
        self.assertIn('href="pages/generated/index.html#example"', result)
        self.assertNotIn(ITEM_PREFIX, result)

    def test_import_runner_never_calls_api_or_falls_back_to_another_site(self):
        job = pipeline_runner.RunState("test", "imscc-" + "a" * 32, {
            "source_type": "imscc", "do_export": True, "do_pipeline": True,
            "do_build": False, "do_npm_install": False, "do_serve": False,
        })
        with patch.object(pipeline_runner, "_run_step", return_value=True) as step, \
             patch.object(pipeline_runner, "_find_site_dir") as find:
            pipeline_runner._run_pipeline(job)
        self.assertEqual(job.status, "complete")
        self.assertEqual(step.call_count, 1)
        self.assertIn("run_pipeline.py", step.call_args.args[2][1])
        find.assert_not_called()

    def test_canvas_credentials_are_optional_for_file_mode(self):
        with patch.object(setup_wizard, "load_env_values", return_value={}), \
             patch.dict(os.environ, {}, clear=True):
            self.assertTrue(setup_wizard.check_env_values("imscc")["ok"])
            self.assertFalse(setup_wizard.check_env_values("api")["ok"])

    def test_local_import_through_no_ai_and_site_output(self):
        from imscc_import import import_imscc
        from extract import extract_course_data
        from sqlite_client import SQLiteClient
        from transform import transform_course_data
        from load import load_course_site
        from build_site import build_hax_site, _create_site_name

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "course.imscc"
            archive.write_bytes(cartridge_bytes())
            result = import_imscc(archive, root / "exports")
            course_id = result["course_id"]
            export_dir = Path(result["export_dir"])
            prepare_import_content(export_dir)
            site = root / "site"
            site.mkdir()
            with SQLiteClient(str(root / "course.db")) as db, \
                 patch("extract.get_export_dir", return_value=export_dir):
                extract_course_data(db, course_id, export_dir)
                transform_course_data(db, course_id, no_ai=True)
                load_course_site(db, course_id, str(root / "exports"), str(root / "prep"))
                with patch("build_site.subprocess.run") as scaffold:
                    build_hax_site(db, course_id, str(root / "exports"), str(site))
                scaffold.assert_not_called()
            manifest = json.loads((site / "site.json").read_text())
            for item in manifest["items"]:
                self.assertTrue((site / item["location"]).is_file(), item["location"])
            content = "\n".join(p.read_text() for p in (site / "pages").rglob("*.html"))
            self.assertIn("Evidence diagram", content)
            self.assertTrue(list((site / "files").glob("*.png")))
            self.assertNotIn("$IMS-CC-FILEBASE$", content)
            self.assertIn(course_id, _create_site_name("CYBER 100 Sample", course_id))


if __name__ == "__main__":
    unittest.main()
