"""Images the instructor chooses to include, and plain notes for what cannot be shown."""
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "python/app"), str(ROOT / "tests")]
import app as web
import course_files
import slim_import
from test_slim_import import text_entries

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WELCOME = ('<html><head><title>Welcome</title></head><body><p>Welcome to cells.</p>'
           '<img src="$IMS-CC-FILEBASE$/img/cell%20diagram.png" alt="A plant cell">'
           '<img src="$IMS-CC-FILEBASE$/img/missing.png" alt="Mitosis stages">'
           '<img src="https://example.org/logo.png" alt="Logo">'
           '<video src="$IMS-CC-FILEBASE$/lecture%20one.mp4"></video>'
           '<a href="%24IMS-CC-FILEBASE%24/reading.pdf">Reading</a></body></html>')


def package(images=True, extra=None):
    entries = text_entries()
    entries["wiki_content/welcome.html"] = WELCOME
    entries["media_inventory.json"] = json.dumps([
        {"path": "web_resources/img/cell diagram.png", "bytes": len(PNG), "mime": "image/png"},
        {"path": "web_resources/lecture one.mp4", "bytes": 4_000_000_000, "mime": "video/mp4"}])
    if images:
        entries["course_images/img/cell diagram.png"] = PNG
        entries["course_images/img/Cell Diagram.jpg"] = JPEG          # a different picture with a similar name
        entries["course_images/other/cell diagram.png"] = PNG         # same name in another folder
        entries["course_images/not-an-image.png"] = b"<svg onload=alert(1)>"
        entries["course_images/huge.png"] = PNG + os.urandom(course_files.MAX_IMAGE_BYTES)  # random: not a zip bomb
        entries["course_images/vector.svg"] = b"<svg></svg>"
    entries.update(extra or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class ResolveTests(unittest.TestCase):
    def test_included_images_are_shown_and_the_rest_is_said_plainly(self):
        html = course_files.resolve(WELCOME, {"web_resources/img/cell diagram.png": "cell-diagram.png"}, "files/")
        self.assertIn('src="files/cell-diagram.png"', html)
        self.assertIn("<em>[Image not included: Mitosis stages]</em>", html)
        self.assertIn('src="https://example.org/logo.png"', html, "images on the web are untouched")
        self.assertIn("[Video in the Canvas course, not included here]", html)
        self.assertIn("Reading<em> (in the Canvas course)</em>", html)
        for broken in ("FILEBASE", "<video", "reading.pdf"):
            self.assertNotIn(broken, html)

    def test_course_links_and_ordinary_links_are_left_alone(self):
        html = ('<a href="$IMSCC_ITEM_REFERENCE$/page-1">Next</a><a href="week-1-cells">Week</a>'
                '<a href="mailto:a@b.org">Mail</a><a href="#top">Top</a><a href="/courses/9/files/3/download">File</a>'
                '<img src="/courses/9/files/4/preview" alt="">')
        out = course_files.resolve(html, {}, "files/")
        for kept in ('href="$IMSCC_ITEM_REFERENCE$/page-1"', 'href="week-1-cells"', 'href="mailto:a@b.org"', 'href="#top"'):
            self.assertIn(kept, out)
        self.assertIn("File<em> (in the Canvas course)</em>", out)
        self.assertIn("[Image not included]", out)
        self.assertEqual(course_files.resolve("<p>plain</p>", {}), "<p>plain</p>")

    def test_only_real_small_raster_images_are_collected(self):
        path = Path(tempfile.mkdtemp()) / "p.zip"
        path.write_bytes(package())
        files = []
        model = slim_import.import_slim(path, files=files)
        self.assertEqual(sorted(f["name"] for f in files), ["cell-diagram-2.png", "cell-diagram.jpg", "cell-diagram.png"],
                         "named safely and uniquely; the type comes from the bytes")
        stored = course_files.stored_names(model)
        self.assertEqual(stored["web_resources/img/cell diagram.png"], "cell-diagram.png")
        warnings = " ".join(model["provenance"]["warnings"])
        self.assertIn("not-an-image.png", warnings)
        self.assertIn("huge.png", warnings)
        self.assertNotIn("vector.svg", [f["name"] for f in files])
        self.assertEqual(model["provenance"]["counts"]["images"], 3)
        # Unless the caller asks, images in a package are ignored.
        self.assertEqual(course_files.stored_names(slim_import.import_slim(path)), {})


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_root = web.app.config.get("MENTOR_DATA_ROOT")
        web.app.config.update(TESTING=True, MENTOR_DATA_ROOT=Path(self.temp.name))
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.client = web.app.test_client()
        self.csrf = self.client.get("/mentor/api/session").json["csrf"]
        self.workspace = self.call("POST", "/mentor/api/workspaces", {"title": "Bio"}, expected=201)
        self.base = f"/mentor/api/workspaces/{self.workspace['id']}"

    def tearDown(self):
        self.environment.stop()
        if self.old_root is None:
            web.app.config.pop("MENTOR_DATA_ROOT", None)
        else:
            web.app.config["MENTOR_DATA_ROOT"] = self.old_root
        self.temp.cleanup()

    def call(self, method, path, data=None, expected=200):
        response = self.client.open(path, method=method, json=data, headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
        return response.json

    def upload(self, data):
        revision = self.call("GET", self.base)["revision"]
        response = self.client.post(self.base + "/intake/export", data={
            "package": (io.BytesIO(data), "s.zip"), "revision": str(revision)},
            content_type="multipart/form-data", headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.json

    def rows(self):
        db = sqlite3.connect(Path(self.temp.name) / "workspaces.sqlite")
        try:
            return db.execute("SELECT COUNT(*) FROM mentor_course_files").fetchone()[0]
        finally:
            db.close()

    def welcome_page(self):
        index = self.client.get(self.base + "/site/index.html").data.decode()
        import re
        for path in set(re.findall(r'href="(pages/[0-9a-f]+/preview\.html)"', index)):
            page = self.client.get(self.base + "/site/" + path)
            if "Welcome to cells." in page.data.decode():
                return path, page
        self.fail("welcome page not found")

    def test_images_reach_every_site_output_and_go_when_the_course_does(self):
        self.assertEqual(self.upload(package())["course"]["counts"]["images"], 3)
        self.assertEqual(self.rows(), 3)
        # The private preview shows the picture and serves it, as an image and nothing else.
        path, page = self.welcome_page()
        html = page.data.decode()
        self.assertIn('src="../../files/cell-diagram.png"', html)
        self.assertIn("[Image not included: Mitosis stages]", html)
        self.assertNotIn("FILEBASE", html)
        image = self.client.get(self.base + "/site/files/cell-diagram.png")
        self.assertEqual((image.status_code, image.mimetype, image.data), (200, "image/png", PNG))
        self.assertEqual(image.headers["X-Content-Type-Options"], "nosniff")
        for missing in ("/site/files/nope.png", "/site/files/..%2f..%2fworkspaces.sqlite", "/site/files/CELL.PNG"):
            self.assertEqual(self.client.get(self.base + missing).status_code, 404, missing)
        # Another person's browser cannot fetch it.
        other = web.app.test_client()
        self.assertIn(other.get(self.base + "/site/files/cell-diagram.png").status_code, (401, 403, 404))
        # The HAX site ZIP carries the picture in files/ and the page points at it.
        archive = zipfile.ZipFile(io.BytesIO(self.client.get(self.base + "/documents/export?format=hax").data))
        self.assertEqual(archive.read("files/cell-diagram.png"), PNG)
        manifest = json.loads(archive.read("site.json"))
        welcome = archive.read(next(i["location"] for i in manifest["items"] if i["title"] == "Welcome")).decode()
        self.assertIn('src="files/cell-diagram.png"', welcome)
        self.assertIn("[Image not included: Mitosis stages]", welcome)
        self.assertNotIn("FILEBASE", welcome)
        # The share link serves the same picture to a visitor with no account.
        share = self.call("POST", self.base + "/share", {})["url"]
        token_path = share.split("/mentor/", 1)[1]
        self.assertEqual(other.get("/mentor/" + token_path + "site/files/cell-diagram.png").data, PNG)
        # Replacing the course without pictures removes the old ones; deleting the course removes all.
        self.upload(package(images=False))
        self.assertEqual(self.rows(), 0)
        self.assertEqual(self.client.get(self.base + "/site/files/cell-diagram.png").status_code, 404)
        self.assertIn("[Image not included: A plant cell]", self.welcome_page()[1].data.decode())
        self.upload(package())
        self.call("DELETE", self.base)
        self.assertEqual(self.rows(), 0)


if __name__ == "__main__":
    unittest.main()
