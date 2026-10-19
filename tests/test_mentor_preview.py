import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
import tempfile
import unittest
import zipfile

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from imscc_content import ITEM_PREFIX
from mentor_service import build_artifact
from mentor_preview import (PREVIEW_CSS, PREVIEW_STYLE_HASH, document_policy,
                            response_policy, delivery_headers, preview_content)

PIXEL = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aNioAAAAASUVORK5CYII=")


def fixture_workspace(root):
    files = root / "exports" / "files"
    files.mkdir(parents=True)
    for name, data in (("pixel.png", PIXEL), ("reading.pdf", b"%PDF-1.4\nSynthetic fixture"),
                       ("unused-policy.txt", b"Private source not linked from the draft")):
        (files / name).write_bytes(data)
        (files / (name + ".json")).write_text(json.dumps({"filename": name}))
    return {
        "id": "fixture-course", "title": "Evidence & Inquiry", "revision": 4, "draft_revision": 2,
        "export_dir": str(root / "exports"), "source_type": "imscc",
        "messages": [{"content": "PRIVATE INSTRUCTOR CHAT"}],
        "materials": [{"role": "policy", "text": "PRIVATE POLICY CONTEXT"}],
        "draft": {"mode": "ai", "warnings": [], "items": [
            {"id": "first", "type": "pages", "title": "Welcome",
             "reason": "PRIVATE MODEL RATIONALE",
             "html": '<h1>Welcome</h1><p id="intro">Compare sources carefully.</p>'
                     '<img src="files/pixel.png" alt="Retained diagram">'
                     f'<a href="{ITEM_PREFIX}second#activity">Go to activity</a>'
                     '<a href="#intro">Introduction anchor</a><a href="files/reading.pdf">Reading</a>'},
            {"id": "second", "type": "assignments", "title": "Evidence activity",
             "html": '<h2 id="activity">Compare two sources</h2><p>Explain your reasoning.</p>'},
        ]}}


class StaticPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = fixture_workspace(self.root)
        self.artifact = build_artifact(self.workspace, self.root / "site")
        self.site = Path(self.artifact["preview_dir"])
        self.manifest = json.loads((self.site / "site.json").read_text())
        self.pages = [str(PurePosixPath(item["location"]).with_name("preview.html"))
                      for item in self.manifest["items"]]

    def document(self, path):
        return BeautifulSoup((self.site / path).read_text(), "html.parser")

    def test_overview_and_navigation_follow_the_approved_snapshot(self):
        overview = self.document("index.html")
        self.assertEqual(overview.select_one("h1").get_text(), self.workspace["title"])
        self.assertIn("not published", overview.select_one(".preview-banner").get_text())
        self.assertEqual([a.get_text() for a in overview.select(".preview-item-list a")],
                         ["WelcomePage", "Evidence activityAssignment"])
        self.assertEqual([a["href"] for a in overview.select(".preview-item-list a")], self.pages)
        self.assertEqual([item["id"] for item in self.manifest["items"]], ["first", "second"])
        self.assertNotIn("unused-policy.txt", str(overview))

    def test_previous_next_and_content_links_stay_in_styled_previews(self):
        first, second = self.document(self.pages[0]), self.document(self.pages[1])
        self.assertEqual(first.select_one('a[rel="next"]')["href"], "../../" + self.pages[1])
        self.assertEqual(second.select_one('a[rel="prev"]')["href"], "../../" + self.pages[0])
        self.assertEqual(first.find("a", string="Go to activity")["href"], "../../" + self.pages[1] + "#activity")
        self.assertEqual(first.find("a", string="Introduction anchor")["href"], "#intro")
        self.assertIsNone(first.find("base"))
        self.assertEqual(len(first.find_all("h1")), 1)
        self.assertEqual(first.select_one('.preview-sidebar a[aria-current="page"]')["href"], "../../" + self.pages[0])
        raw = (self.site / self.manifest["items"][0]["location"]).read_text()
        self.assertIn(self.manifest["items"][1]["location"] + "#activity", raw)
        self.assertIn("<h1>Welcome</h1>", raw)

    def test_retained_images_and_file_links_resolve_without_a_base_element(self):
        first = self.document(self.pages[0])
        self.assertEqual(first.select_one("img")["src"], "../../files/pixel.png")
        self.assertEqual(first.find("a", string="Reading")["href"], "../../files/reading.pdf")
        self.assertEqual((self.site / "files/pixel.png").read_bytes(), PIXEL)
        self.assertEqual(delivery_headers("files/reading.pdf")["Content-Disposition"], "attachment")
        self.assertNotIn("Content-Disposition", delivery_headers(self.pages[0]))

    def test_only_the_exact_trusted_stylesheet_is_permitted(self):
        for path in ["index.html", *self.pages]:
            doc = self.document(path)
            css = doc.style.string
            actual_hash = "sha256-" + base64.b64encode(hashlib.sha256(css.encode()).digest()).decode()
            self.assertEqual(actual_hash, PREVIEW_STYLE_HASH)
            self.assertEqual(css, PREVIEW_CSS)
            policy = doc.find("meta", attrs={"http-equiv": "Content-Security-Policy"})["content"]
            self.assertIn(actual_hash, policy)
            self.assertNotIn("'unsafe-inline'", policy)
            self.assertIn("script-src 'none'", policy)
            self.assertIn("base-uri 'none'", policy)
            self.assertIsNone(doc.script)
        self.assertEqual(self.artifact["preview_style_hash"], PREVIEW_STYLE_HASH)
        self.assertIn("sandbox allow-same-origin", response_policy())

    def test_private_conversation_policy_and_rationale_are_not_rendered(self):
        for path in ["index.html", *self.pages]:
            content = (self.site / path).read_text()
            for private in ("PRIVATE INSTRUCTOR CHAT", "PRIVATE POLICY CONTEXT", "PRIVATE MODEL RATIONALE"):
                self.assertNotIn(private, content)
        with zipfile.ZipFile(self.artifact["path"]) as archive:
            self.assertIn("site.json", archive.namelist())
            self.assertIn("approval.json", archive.namelist())
            self.assertEqual(json.loads(archive.read("approval.json"))["draft_revision"], 2)
            self.assertIn(self.pages[0], archive.namelist())

    def test_uploaded_active_styles_scripts_and_unregistered_resources_are_not_enabled(self):
        content, unavailable = preview_content(
            '<style>body{display:none}</style><script>window.compromised=1</script>'
            '<img src="https://tracker.example/image.png"><img src="files/missing.png">'
            '<a href="/mentor/api/session">Not a course resource</a>'
            '<a href="../private.db">Not registered</a>'
            '<a href="//example.edu/resource">Public reference</a>',
            {"second": self.pages[1]}, {"files/pixel.png"}, "../../")
        doc = BeautifulSoup(content, "html.parser")
        self.assertTrue(unavailable)
        self.assertIsNone(doc.script)
        self.assertIsNone(doc.style)
        self.assertTrue(all(not tag.get("src") for tag in doc.find_all("img")))
        self.assertIsNone(doc.find("a", string="Not a course resource").get("href"))
        self.assertIsNone(doc.find("a", string="Not registered").get("href"))
        self.assertEqual(doc.find("a", string="Public reference")["href"], "https://example.edu/resource")

    def test_invalid_policy_metadata_cannot_inject_directives_and_old_build_hashes_work(self):
        self.assertNotIn("evil.example", document_policy("sha256-x'; script-src https://evil.example"))
        old_hash = "sha256-" + base64.b64encode(hashlib.sha256(b"old trusted stylesheet").digest()).decode()
        self.assertIn(old_hash, response_policy(old_hash))
        self.assertNotIn(PREVIEW_STYLE_HASH, response_policy(old_hash))

    def test_titles_are_escaped_and_presentation_does_not_mutate_the_snapshot(self):
        from copy import deepcopy
        workspace = deepcopy(self.workspace)
        workspace["title"] = '</title><script>window.bad=true</script>'
        workspace["draft"]["items"][0]["title"] = '<img src=x onerror="bad()">'
        before = deepcopy(workspace)
        artifact = build_artifact(workspace, self.root / "escaped-site")
        self.assertEqual(workspace, before)
        for path in Path(artifact["preview_dir"]).rglob("*.html"):
            if path.name != "preview.html" and path.name != "index.html":
                continue
            doc = BeautifulSoup(path.read_text(), "html.parser")
            self.assertIsNone(doc.script)
            self.assertFalse(any(tag.has_attr("onerror") for tag in doc.find_all(True)))
