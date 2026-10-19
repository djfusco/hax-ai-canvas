"""Optional mocked Chromium flow tests. No real accounts, uploads or AI calls."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from jinja2 import Environment, FileSystemLoader, select_autoescape

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
templates = Environment(loader=FileSystemLoader(ROOT / "python/app/templates"), autoescape=select_autoescape())
HTML = templates.get_template("mentor.html").render(
    is_hosted=True, request=SimpleNamespace(blueprint="mentor", endpoint="mentor.page"),
    url_for=lambda endpoint, **values: "/static/" + values["filename"] if endpoint == "static" else "/mentor")


def exercise(browser, mode, viewport):
    state = {"work": None, "key": False, "model": "", "calls": [], "reads": 0}

    def handle(route):
        request = route.request
        path = urlsplit(request.url).path
        if path == "/mentor":
            return route.fulfill(content_type="text/html", body=HTML)
        if path == "/favicon.ico":
            return route.fulfill(status=204)
        if path.startswith("/static/"):
            name = path.removeprefix("/static/")
            assert name in ("mentor.js", "style.css", "hosted-upload.js")
            return route.fulfill(content_type="text/css" if name.endswith(".css") else "application/javascript",
                                 body=(ROOT / "python/app/static" / name).read_text())
        assert path.startswith("/mentor/api/"), "Unexpected external request"
        body = request.post_data_json if request.post_data else {}
        state["calls"].append((path, request.method, body))
        work = state["work"]
        if request.method != "GET":
            assert request.headers["x-mentor-csrf"] == "fixture"
        if path.endswith("/session"):
            data = {"csrf": "fixture", "authenticated": True, "login_required": False,
                    "invite_required": False, "account_required": False, "hosted_ready": True,
                    "account": {"email": "fixture@example.edu"},
                    "credentials": [{"provider": "openai", "expires_at": 2000000000}] if state["key"] else []}
        elif path == "/mentor/api/workspaces" and request.method == "POST":
            work = state["work"] = {
                "id": "a" * 32, "title": body["title"], "stage": "brief", "source_type": "blank",
                "revision": 1, "draft_revision": 0, "brief": "", "materials": [], "messages": [],
                "jobs": [], "active_job": None, "direction_revision": None, "draft": None,
                "reviewed_draft_revision": None, "build_revision": None, "artifact": None}
            data = work
        elif path == "/mentor/api/workspaces":
            data = [] if not work else [{key: work[key] for key in ("id", "title", "stage")}]
        elif path.endswith("/credentials"):
            assert body["api_key"] == "synthetic-key" and body["provider"] == "openai"
            state["key"] = True
            data = {"configured": True}
        elif path.endswith("/model"):
            assert body["model"] == "future-text"
            state["model"] = body["model"]
            data = {"selected": state["model"]}
        elif "/providers/" in path:
            assert state["key"]
            data = {"models": [{"id": "future-text", "name": "Future text"}],
                    "selected": state["model"], "version": "fixture", "expired": False, "warning": ""}
        elif path.endswith("/inventory"):
            data = {"course_items": 0, "materials": [], "counts": {}, "warnings": [], "limitations": []}
        elif path.endswith("/chat"):
            assert body["provider"] == mode
            if mode != "guided":
                assert state["key"] and state["model"] == "future-text"
            if body["kind"] == "interview":
                work["revision"] += 1
                work["brief"] = "Students should compare evidence sources."
                job = {"id": "interview-one", "kind": "interview", "status": "running",
                       "revision": work["revision"]}
                work["jobs"].append(job)
                work.update(active_job=deepcopy(job), stage="interviewing")
                state["reads"] = 0
            else:
                assert work["draft"]
                work["reviewed_draft_revision"] = work["draft_revision"]
                work["jobs"].append({"id": "review-one", "kind": "review", "status": "completed",
                                     "revision": work["revision"]})
            work["messages"].append({"role": "user", "kind": body["kind"], "content": body["message"]})
            data = work
        elif path.endswith("/approve-direction"):
            assert work["active_job"] is None
            assert work["jobs"][0]["status"] == "completed"
            work.update(direction_revision=work["revision"], stage="direction_approved")
            data = work
        elif path.endswith("/analyze"):
            assert work["direction_revision"] == work["revision"]
            work.update(stage="draft", draft_revision=1, draft={
                "summary": "Fixture draft", "warnings": [], "items": [
                    {"id": "one", "title": "Evidence", "html": "<p>Compare evidence.</p>"}]})
            data = work
        elif path.endswith("/approve-build"):
            assert work["reviewed_draft_revision"] == work["draft_revision"]
            work.update(build_revision=work["draft_revision"], stage="build_approved")
            data = work
        elif path.endswith("/build"):
            assert work["build_revision"] == work["draft_revision"]
            work.update(stage="built", artifact={"kind": "approved-course-content"})
            data = work
        elif path == "/mentor/api/workspaces/" + work["id"]:
            if work["active_job"]:
                state["reads"] += 1
                if state["reads"] >= 2:
                    work["jobs"][0]["status"] = "completed"
                    work.update(active_job=None, stage="brief")
                    work["messages"].append({"role": "assistant", "kind": "interview",
                                             "content": "Fixture reply — confirm your learning goals."})
            data = work
        else:
            raise AssertionError("Unexpected route: " + path)
        route.fulfill(content_type="application/json", body=json.dumps(data))

    page = browser.new_page(viewport=viewport)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/*", handle)
    page.goto("https://mentor.test/mentor")
    page.wait_for_function("busy === false")
    assert page.locator("#mentor-provider").input_value() == ""
    assert page.locator("#provider-settings").is_visible()
    assert "Choose or create" in page.locator("#next-step-title").inner_text()
    page.locator("#new-title").fill("Evidence course")
    page.locator("#new-workspace button").click()
    page.wait_for_function("busy === false")
    assert "Configure your mentor" in page.locator("#next-step-title").inner_text()
    assert page.locator("#send-mentor-message").is_disabled()
    page.locator("#next-step-button").click()
    assert page.locator("#mentor-provider").evaluate("(node) => node === document.activeElement")
    page.locator("#mentor-provider").select_option(mode)
    page.wait_for_function("busy === false")
    if mode != "guided":
        page.locator("#mentor-key").fill("synthetic-key")
        page.locator("#save-provider-key").click()
        page.wait_for_function("busy === false")
        assert page.locator("#send-mentor-message").is_disabled()
        page.locator("#mentor-model").select_option("future-text")
        page.locator("#save-model").click()
        page.wait_for_function("busy === false")
        assert "OpenAI · future-text" in page.locator("#mentor-mode").inner_text()
    else:
        assert "no AI requests" in page.locator("#mentor-mode").inner_text()
    assert "Add course content" in page.locator("#next-step-title").inner_text()
    page.locator("#start-blank-course").click()
    assert "Discuss your audience" in page.locator("#next-step-title").inner_text()
    page.locator("#mentor-message").fill("I'd like to discuss this course.")
    page.locator("#send-mentor-message").click()
    page.wait_for_function("busy === false")
    assert "Preparing the mentor reply" in page.locator("#mentor-status").inner_text()
    page.wait_for_function("workspace.active_job === null")
    assert page.locator("#mentor-status").is_hidden()
    assert "approve the direction" in page.locator("#next-step-title").inner_text()
    before = sum(path.endswith("/approve-direction") for path, _, _ in state["calls"])
    page.locator("#next-step-button").click()
    assert sum(path.endswith("/approve-direction") for path, _, _ in state["calls"]) == before
    page.locator("#approve-direction").click()
    page.wait_for_function("busy === false")
    assert "Generate a draft" in page.locator("#next-step-title").inner_text()
    page.locator("#analyze-course").click()
    page.wait_for_function("busy === false")
    assert page.locator("#draft-panel").is_visible()
    assert page.locator("#draft-items details").first.evaluate("(node) => node.open")
    assert page.locator("#approve-build").is_disabled()
    page.locator("#mentor-message").fill("I reviewed the proposed draft.")
    page.locator("#send-mentor-message").click()
    page.wait_for_function("busy === false")
    assert "Approve the exact draft" in page.locator("#next-step-title").inner_text()
    page.locator("#approve-build").click()
    page.wait_for_function("busy === false")
    assert "Build your approved content" in page.locator("#next-step-title").inner_text()
    page.locator("#build-course").click()
    page.wait_for_function("busy === false")
    assert "Your approved content is ready" in page.locator("#next-step-title").inner_text()
    assert page.locator("#artifact-links a").count() == 2
    assert page.locator("#mentor-status").is_hidden()
    assert not any("publish" in path for path, _, _ in state["calls"])
    assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth"), "Horizontal overflow"
    assert not errors, errors
    page.close()
    print("Mocked complete mentor flow passed:", mode, viewport["width"], "px")


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    exercise(browser, "guided", {"width": 1280, "height": 900})
    exercise(browser, "openai", {"width": 390, "height": 844})
    browser.close()
