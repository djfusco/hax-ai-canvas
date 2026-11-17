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
        # Packed as the browser packs them: numbered files and an index of their paths in the export.
        packed = [("1.png", "web_resources/img/cell diagram.png", PNG),
                  ("2.jpg", "web_resources/img/Cell Diagram.jpg", JPEG),             # a similar name, another picture
                  ("3.png", "web_resources/Lab 0: Getting Started/cell diagram.png", PNG),  # a real Canvas folder name
                  ("4.png", "web_resources/not-an-image.png", b"<svg onload=alert(1)>"),
                  ("5.png", "web_resources/huge.png", PNG + os.urandom(course_files.MAX_IMAGE_BYTES)),
                  ("6.png", "../outside.png", PNG)]                                 # not a path in the export
        for name, _, data in packed:
            entries["course_images/" + name] = data
        entries["course_images/7.svg"] = b"<svg></svg>"
        entries["course_images/stray.png"] = PNG                                    # not in the index
        entries["course_images/index.json"] = json.dumps(
            [{"name": "course_images/" + name, "path": path} for name, path, _ in packed]
            + [{"name": "course_images/7.svg", "path": "web_resources/vector.svg"}, {"name": "imsmanifest.xml", "path": "web_resources/x.png"}])
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
        self.assertEqual(stored["web_resources/Lab 0: Getting Started/cell diagram.png"], "cell-diagram-2.png",
                         "a folder name with a colon is fine: the path is only text")
        self.assertEqual(len(files), 3, "nothing unlisted, outside the export, or not an image is taken")
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


class InternalLinkTests(unittest.TestCase):
    """Links between parts of the course survive as course links (found testing a real export)."""

    def model(self):
        entries = text_entries()
        entries["wiki_content/welcome.html"] = (
            '<html><head><title>Welcome</title></head><body><p>Welcome to cells.</p>'
            '<a href="$CANVAS_OBJECT_REFERENCE$/assignments/gessay">the essay</a>'
            '<a href="%24CANVAS_OBJECT_REFERENCE%24/modules/mod1">week 1</a>'
            '<a href="$CANVAS_COURSE_REFERENCE$/assignments/syllabus">the syllabus</a>'
            '<a href="$WIKI_REFERENCE$/pages/welcome#top">this page</a>'
            '<a href="$WIKI_REFERENCE$/pages/no-such-page">gone</a>'
            '<a href="$CANVAS_COURSE_REFERENCE$/files">all files</a></body></html>')
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        path = Path(tempfile.mkdtemp()) / "p.zip"
        path.write_bytes(buffer.getvalue())
        return slim_import.import_slim(path)

    def test_canvas_links_become_course_links(self):
        import course_site
        import hax_preview
        model = self.model()
        welcome = next(i for i in model["items"] if i["title"] == "Welcome")
        essay = next(i for i in model["items"] if i["title"] == "Essay")
        html = welcome["html"]
        self.assertIn(f'href="$IMSCC_ITEM_REFERENCE$/{essay["id"]}"', html)
        self.assertIn(f'href="$IMSCC_ITEM_REFERENCE$/module--{model["modules"][0]["id"]}"', html)
        self.assertIn('href="$IMSCC_ITEM_REFERENCE$/syllabus"', html)
        self.assertIn(f'href="$IMSCC_ITEM_REFERENCE$/{welcome["id"]}#top"', html)
        self.assertIn("$WIKI_REFERENCE$/pages/no-such-page", html, "an unknown target is left for the outputs to mark")
        # On the HAX site they are links to pages; what only Canvas has becomes a note.
        items = course_site.build_items(model, [])
        page = hax_preview.page_html(next(i for i in items if i["id"] == welcome["id"]), course_site.slugs(items))
        for address in ('href="essay"', 'href="week-1"', 'href="syllabus"', 'href="welcome#top"'):
            self.assertIn(address, page)
        self.assertIn("gone<em> (in the Canvas course)</em>", page)
        self.assertIn("all files<em> (in the Canvas course)</em>", page)
        self.assertNotIn("REFERENCE", page)

    def test_module_pages_for_students_do_not_name_instructor_items(self):
        import course_site
        model = self.model()
        welcome = next(i for i in model["items"] if i["title"] == "Welcome")
        key = dict(welcome, id="page-key", title="Answer key: quiz 1", html="<p>answers</p>")
        model["items"].append(key)
        model["modules"][0]["items"].append("page-key")
        model["modules"].append({"id": "m-private", "title": "FOR INSTRUCTORS: resources", "position": 99,
                                 "items": [], "prerequisites": [], "requirements": [], "sequential": False})
        only = dict(welcome, id="page-only", title="For instructors: notes", html="<p>n</p>")
        model["items"].append(only)
        model["modules"][-1]["items"].append("page-only")
        workspace = {"build": None, "documents": [], "consultation": None}
        text = " ".join(i["title"] + i["html"] for i in course_site.course_items(model, workspace, students_only=True))
        self.assertNotIn("Answer key", text)
        self.assertNotIn("FOR INSTRUCTORS", text)
        self.assertIn("Answer key", " ".join(i["html"] for i in course_site.course_items(model, workspace)))


class LargeDownloadTests(RouteTests):
    def test_a_course_site_larger_than_the_hosting_limit_downloads_as_a_stream(self):
        # Six megabytes of pictures: more than the 4.5 MB a hosted response may carry unless streamed.
        big = [(f"{n}.png", f"web_resources/img/photo {n}.png", PNG + os.urandom(course_files.MAX_IMAGE_BYTES - 200))
               for n in range(1, 6)]
        refs = "".join(f'<img src="$IMS-CC-FILEBASE$/img/photo%20{n}.png" alt="Photo {n}">' for n in range(1, 6))
        extra = {"wiki_content/welcome.html": f"<html><head><title>Welcome</title></head><body><p>Welcome to cells.</p>{refs}</body></html>",
                 "course_images/index.json": json.dumps([{"name": "course_images/" + name, "path": path} for name, path, _ in big])}
        extra.update({"course_images/" + name: data for name, _, data in big})
        self.upload(package(images=False, extra=extra))
        self.assertEqual(self.rows(), 5)
        response = self.client.get(self.base + "/documents/export?format=hax", buffered=False)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.is_streamed, "sent as a stream, so no single body exceeds the limit")
        self.assertIsNone(response.content_length)
        data = b"".join(response.iter_encoded())
        self.assertGreater(len(data), 4_500_000)
        archive = zipfile.ZipFile(io.BytesIO(data))
        self.assertIsNone(archive.testzip())
        self.assertEqual(len([n for n in archive.namelist() if n.startswith("files/")]), 5)
