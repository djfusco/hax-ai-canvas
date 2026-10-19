"""Optional real-browser test of private Flask previews and offline ZIP pages.

Only a temporary SQLite fixture and synthetic approved content are used.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

from itsdangerous import URLSafeTimedSerializer
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server, WSGIRequestHandler

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "python/app"), str(ROOT / "tests")]
import app as web
from mentor_credentials import private_key
from mentor_store import MentorStore
from mentor_service import build_artifact
from test_mentor_preview import fixture_workspace


class QuietHandler(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


with tempfile.TemporaryDirectory(prefix="student-preview-") as temp, patch.dict(os.environ, {}, clear=True):
    root = Path(temp)
    fixture = fixture_workspace(root)
    # Deliberately untrusted source styles/scripts must never reach the shell.
    fixture["draft"]["items"][0]["html"] += (
        '<script>window.compromised=true</script><style>body{display:none}</style>'
        '<img src="https://external.invalid/tracker.png" alt="External image">')
    token = "f" * 64
    owner = hashlib.sha256(token.encode()).hexdigest()
    cookie = URLSafeTimedSerializer(private_key(root / ".browser-key").decode(),
                                    salt="faculty-mentor-browser").dumps(token)
    store = MentorStore(root / "workspaces.sqlite")
    work = store.create(owner, fixture["title"], source_type="imscc",
                        course_id="fixture", export_dir=fixture["export_dir"])
    work = store.add_material(owner, work["id"], {
        "name": "Private policy", "role": "policy", "kind": "text",
        "text": "PRIVATE POLICY CONTEXT"}, work["revision"])
    work = store.save_brief(owner, work["id"], "Fixture course goals.", work["revision"])
    work = store.begin_interview(owner, work["id"], work["revision"])
    work = store.finish_interview(owner, work["id"], work["active_job"]["id"])
    work = store.approve_direction(owner, work["id"], work["revision"])
    work = store.begin_analysis(owner, work["id"], work["revision"])
    work = store.finish_analysis(owner, work["id"], work["active_job"]["id"], fixture["draft"])
    work = store.begin_review(owner, work["id"], work["draft_revision"])
    work = store.finish_review(owner, work["id"], work["active_job"]["id"], None)
    work = store.approve_build(owner, work["id"], work["draft_revision"])
    work = store.begin_build(owner, work["id"], work["draft_revision"])
    job = work["active_job"]["id"]
    artifact = build_artifact(work, root / "artifacts" / work["id"] / job)
    work = store.finish_build(owner, work["id"], job, artifact)
    old_root = web.app.config.get("MENTOR_DATA_ROOT")
    web.app.config["MENTOR_DATA_ROOT"] = root
    server = make_server("127.0.0.1", 0, web.app, threaded=True, request_handler=QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = "http://127.0.0.1:" + str(server.server_port)
    preview_url = origin + "/mentor/api/workspaces/" + work["id"] + "/preview/index.html"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            for width in (1280, 390):
                context = browser.new_context(viewport={"width": width, "height": 900})
                context.add_cookies([{"name": "mentor_browser", "value": cookie, "url": origin,
                                     "httpOnly": True, "sameSite": "Strict"}])
                external = []
                def guard(route):
                    if route.request.url.startswith(origin + "/"):
                        route.continue_()
                    else:
                        external.append(route.request.url)
                        route.abort()
                context.route("**/*", guard)
                main = context.new_page()
                main.goto(origin + "/mentor")
                main.wait_for_function("busy === false")
                main.locator("#workspace-list").select_option(work["id"])
                main.wait_for_function("busy === false")
                with main.expect_popup() as opened:
                    main.get_by_role("link", name="Preview student view").click()
                preview = opened.value
                preview.wait_for_load_state()
                assert preview.url == preview_url
                assert "not published" in preview.locator(".preview-banner").inner_text()
                assert preview.locator("h1").inner_text() == fixture["title"]
                assert preview.locator(".preview-banner").evaluate(
                    "(node) => getComputedStyle(node).backgroundColor") == "rgb(20, 46, 80)"
                preview.locator(".preview-item-list a").first.click()
                preview.wait_for_load_state()
                first_page = preview.url
                assert first_page.endswith("/preview.html")
                assert preview.locator("h1").inner_text() == "Welcome"
                image = preview.get_by_alt_text("Retained diagram")
                image.wait_for()
                assert image.evaluate("(node) => node.complete && node.naturalWidth === 1")
                assert preview.evaluate("window.compromised === undefined")
                assert "PRIVATE POLICY CONTEXT" not in preview.locator("body").inner_text()
                preview.get_by_role("link", name="Introduction anchor").click()
                assert preview.url == first_page + "#intro"
                preview.get_by_role("link", name="Go to activity").click()
                assert preview.locator("h1").inner_text() == "Evidence activity"
                preview.locator('a[rel="prev"]').click()
                assert preview.locator("h1").inner_text() == "Welcome"
                with preview.expect_download() as download:
                    preview.get_by_role("link", name="Reading", exact=True).click()
                assert download.value.suggested_filename == "reading.pdf"
                assert not preview.evaluate("document.documentElement.scrollWidth > innerWidth")
                assert not external, external
                unauthorized = browser.new_context()
                assert unauthorized.request.get(preview_url).status == 404
                assert unauthorized.request.get(preview_url.replace("index.html", "files/pixel.png")).status == 404
                unauthorized.close()
                context.close()
                print("Private new-tab preview, styling, navigation, image, download and owner isolation passed:", width, "px")
            # Downloaded packages also work as local static files; no publication
            # or server-side credentials are embedded in the generated HTML.
            offline = browser.new_page()
            offline.goto((Path(artifact["preview_dir"]) / "index.html").as_uri())
            offline.locator(".preview-item-list a").first.click()
            assert offline.locator("h1").inner_text() == "Welcome"
            assert offline.get_by_alt_text("Retained diagram").evaluate(
                "(node) => node.complete && node.naturalWidth === 1")
            assert offline.locator(".preview-banner").evaluate(
                "(node) => getComputedStyle(node).backgroundColor") == "rgb(20, 46, 80)"
            browser.close()
            print("Offline static course navigation, image and trusted styles passed.")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        if old_root is None:
            web.app.config.pop("MENTOR_DATA_ROOT", None)
        else:
            web.app.config["MENTOR_DATA_ROOT"] = old_root
