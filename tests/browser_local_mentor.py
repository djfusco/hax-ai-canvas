"""Mocked local UI verification; synthetic inputs only, no external services."""
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sync_playwright, "Optional playwright dependency is not installed")
class LocalMentorBrowserTest(unittest.TestCase):
    def test_local_import_and_site_lifecycle(self):
        templates = Environment(loader=FileSystemLoader(ROOT / "python/app/templates"),
                                autoescape=select_autoescape())
        html = templates.get_template("mentor.html").render(
            is_hosted=False, request=SimpleNamespace(blueprint="mentor", endpoint="mentor.page"),
            url_for=lambda endpoint, **values: "/static/" + values["filename"]
            if endpoint == "static" else "/mentor")
        work = {
            "id": "synthetic-workspace", "title": "Synthetic course", "stage": "brief",
            "revision": 1, "draft_revision": 0, "brief": "", "materials": [], "messages": [],
            "jobs": [], "active_job": None, "draft": None, "artifact": None,
            "source_type": "blank", "direction_revision": None, "build_revision": None,
            "reviewed_draft_revision": None,
        }
        state = {"offset": 0, "size": 0, "calls": [], "chunks": [], "running": False}

        def handle(route):
            req = route.request
            parsed = urlsplit(req.url)
            path = parsed.path
            if path == "/mentor":
                return route.fulfill(content_type="text/html", body=html)
            if path == "/favicon.ico":
                return route.fulfill(status=204)
            if path.startswith("/static/"):
                name = path.removeprefix("/static/")
                self.assertIn(name, ("mentor.js", "style.css"))
                return route.fulfill(
                    content_type="text/css" if name.endswith(".css") else "application/javascript",
                    body=(ROOT / "python/app/static" / name).read_text())
            self.assertTrue(path.startswith("/mentor/api/"), "External request: " + req.url)
            state["calls"].append((req.method, path))
            if req.method != "GET":
                self.assertEqual(req.headers["x-mentor-csrf"], "fixture")
            if req.method == "PUT":
                self.assertEqual(req.headers["content-type"], "application/octet-stream")
                offset = int(parse_qs(parsed.query)["offset"][0])
                self.assertEqual(offset, state["offset"])
                size = len(req.post_data_buffer)
                self.assertLessEqual(size, 8 * 1024 ** 2)
                state["chunks"].append(size)
                state["offset"] += size
                data = {"id": "synthetic-copy", "offset": state["offset"], "status": "uploading"}
            elif path.endswith("/session"):
                data = {"csrf": "fixture", "authenticated": True, "login_required": False,
                        "hosted_ready": False, "local_mode": True, "local_hax": True,
                        "local_accounts": True, "account": {"email": "faculty@example.test"},
                        "credentials": []}
            elif path == "/mentor/api/workspaces":
                data = [work]
            elif path.endswith("/local-uploads"):
                body = req.post_data_json
                self.assertEqual(body["revision"], work["revision"])
                state["size"] = body["size"]
                data = {"id": "synthetic-copy", "offset": 0, "chunk_size": 8 * 1024 ** 2,
                        "filename": body["filename"], "size": body["size"], "status": "uploading"}
            elif path.endswith("/complete"):
                self.assertEqual(state["offset"], state["size"])
                work.update(stage="preparing", active_job={"id": "synthetic-prepare", "kind": "prepare"},
                            upload={"id": "synthetic-copy", "filename": "synthetic.imscc",
                                    "size": state["size"], "status": "processing",
                                    "phase": "inspect", "message": "Inspecting local instructional content."})
                data = work
            elif path.endswith("/inventory"):
                data = {"course_items": 0, "materials": [], "counts": {}, "warnings": [], "limitations": []}
            elif path.endswith("/hax/start"):
                state["running"] = True
                data = {"running": True, "phase": "ready", "url": "http://127.0.0.1:8123/"}
            elif path.endswith("/hax/stop"):
                state["running"] = False
                data = {"running": False, "phase": "stopped", "url": None}
            elif path.endswith("/hax"):
                data = {"running": state["running"], "phase": "ready" if state["running"] else "stopped",
                        "url": "http://127.0.0.1:8123/" if state["running"] else None,
                        "directory": "/synthetic/course/site-v1"}
            elif path == "/mentor/api/workspaces/" + work["id"]:
                data = work
            else:
                self.fail("Unexpected route: " + path)
            route.fulfill(status=202 if path.endswith(("/complete", "/hax/start")) else 200,
                          content_type="application/json", body=json.dumps(deepcopy(data)))

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True, executable_path=os.environ.get("HAX_TEST_CHROMIUM"))
            try:
                page = browser.new_page(viewport={"width": 390, "height": 844})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.route("**/*", handle)
                page.goto("http://127.0.0.1:8765/mentor")
                page.wait_for_function("busy === false")
                self.assertIn("Account-scoped local", page.locator("#workspace-storage-help").inner_text())
                page.locator("#workspace-list").select_option(work["id"])
                page.wait_for_function("busy === false")
                page.locator("#source-file").set_input_files({
                    "name": "synthetic.imscc", "mimeType": "application/zip",
                    "buffer": b"x" * (9 * 1024 ** 2),
                })
                page.locator("#upload-course-button").click()
                page.wait_for_function("busy === false && workspace.active_job?.kind === 'prepare'")
                self.assertEqual(state["chunks"], [8 * 1024 ** 2, 1024 ** 2])
                self.assertIn("locally", page.locator("#upload-feedback-title").inner_text())
                self.assertNotIn("cloud upload", page.locator("#upload-state").inner_text())
                self.assertTrue(page.locator("#approve-build").is_disabled())
                self.assertTrue(page.locator("#build-course").is_disabled())
                # Model/backend approval behavior is covered separately. Supply a built
                # synthetic snapshot here to isolate lifecycle rendering and edit safety.
                report = {
                    "version": 1, "scope": "draft", "mode": "ai",
                    "frameworks": [
                        {"id": "blooms", "title": "Bloom taxonomy", "version": "revised",
                         "url": "https://framework.example.test/blooms"},
                        {"id": "unsafe", "title": "Unsafe reference", "url": "javascript:alert(1)"}],
                    "coverage": {"items_total": 1, "items_evaluated": 1, "truncated_items": ["one"]},
                    "summary": "<img src=x onerror=alert(1)> Literal assessment summary",
                    "warnings": ["Synthetic bounded-excerpt limitation"],
                    "items": [{
                        "item_id": "one", "title": "Saved title", "content_sha256": "a" * 64,
                        "blooms": {"level": 4, "label": "Analyze", "rationale": "Compare evidence."},
                        "aias": {"observed_level": 1, "recommended_level": 2, "label": "AI planning",
                                 "rationale": "Retain student judgment."},
                        "dimensions": {"ai_leverage": {"score": 3, "rationale": "Planning support."}},
                        "evidence": [{"source_id": "course:one", "quote": "<script>Literal evidence</script>"}],
                        "strengths": ["Evidence comparison"], "concerns": [],
                        "recommendations": ["Explain evidence provenance"],
                        "limitations": ["Bounded excerpt only"]}],
                }
                work.update(stage="built", active_job=None, brief="Saved brief", draft_revision=1,
                            reviewed_draft_revision=1, build_revision=1,
                            draft={"summary": "Synthetic draft", "warnings": [],
                                   "assessment_required": True, "assessment_current": True,
                                   "assessment": report, "baseline_assessment": {**report, "scope": "baseline"},
                                   "items": [
                                {"id": "one", "title": "Saved title", "html": "<p>Synthetic lesson</p>"}]},
                            artifact={"hax_site": {"directory": "/synthetic/course/site-v1",
                                                   "draft_revision": 1, "items": 1}})
                work["upload"]["status"] = "ready"
                page.evaluate("async () => { await fetchWorkspace(); await renderWorkspace(); }")
                page.wait_for_function("haxStatus?.phase === 'stopped'")
                self.assertTrue(page.locator("#hax-site-panel").is_visible())
                self.assertTrue(page.locator("#assessment-panel").is_visible())
                self.assertIn("1 of 1", page.locator("#proposed-assessment-content").inner_text())
                self.assertIn("Truncated item excerpts: one", page.locator("#proposed-assessment-content").inner_text())
                self.assertEqual(page.locator("#proposed-assessment-content a").count(), 1)
                self.assertEqual(page.locator("#assessment-panel img, #assessment-panel script").count(), 0)
                self.assertIn("higher AIAS levels are not inherently better",
                              page.locator("#assessment-panel").inner_text())
                self.assertIn("Imported course baseline", page.locator("#baseline-assessment summary").first.inner_text())
                self.assertTrue(page.locator("#open-hax-site").is_hidden())
                self.assertFalse(any(path.endswith("/hax/start") for _, path in state["calls"]))
                page.locator("#design-brief").fill("Unsaved brief")
                page.locator("#draft-title-0").fill("Unsaved title")
                self.assertIn("unsaved edits", page.locator("#assessment-readiness").inner_text())
                self.assertTrue(page.locator("#approve-build").is_disabled())
                page.locator("#mentor-message").fill("Unsent conversation")
                page.locator("#start-hax-site").click()
                page.wait_for_function("haxBusy === false && haxStatus?.running")
                self.assertEqual(page.locator("#open-hax-site").get_attribute("href"), "http://127.0.0.1:8123/")
                self.assertTrue(page.locator("#open-hax-site").is_visible())
                page.locator("#refresh-hax-site").click()
                self.assertEqual(page.locator("#design-brief").input_value(), "Unsaved brief")
                self.assertEqual(page.locator("#draft-title-0").input_value(), "Unsaved title")
                self.assertEqual(page.locator("#mentor-message").input_value(), "Unsent conversation")
                page.locator("#start-hax-site").click()
                page.wait_for_function("haxBusy === false")
                self.assertEqual(state["calls"][-2:], [
                    ("POST", "/mentor/api/workspaces/synthetic-workspace/hax/stop"),
                    ("POST", "/mentor/api/workspaces/synthetic-workspace/hax/start")])
                page.locator("#stop-hax-site").click()
                page.wait_for_function("haxBusy === false && !haxStatus?.running")
                self.assertTrue(page.locator("#open-hax-site").is_hidden())
                self.assertFalse(page.evaluate("document.documentElement.scrollWidth > window.innerWidth"),
                                 page.evaluate("""Array.from(document.querySelectorAll('body *'))
                                 .filter(node => node.getBoundingClientRect().right > innerWidth)
                                 .map(node => [node.tagName, node.id, node.className])"""))
                self.assertEqual(errors, [])
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
