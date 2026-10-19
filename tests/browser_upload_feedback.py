"""Mocked upload UI; no files leave this browser and no cloud jobs are started."""
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
STUB = """window.testTransfer=null;window.HaxHostedUpload={upload(file,intent,options){
return new Promise((resolve,reject)=>{window.testTransfer={file,intent,options,resolve};
options.signal.addEventListener('abort',()=>reject(new DOMException('Stopped','AbortError')));});}};"""
work = {"id": "a" * 32, "title": "Upload fixture", "stage": "brief", "source_type": "blank",
        "revision": 1, "draft_revision": 0, "brief": "", "materials": [], "messages": [], "jobs": [],
        "active_job": None, "direction_revision": None, "draft": None, "artifact": None}
state = {"calls": [], "number": 0, "hold_intent": True, "hold_complete": True, "fail_intent": False}


def reserve(body):
    state["number"] += 1
    ident = f"{state['number']:032x}"
    work.update(stage="uploading", active_job={"id": ident, "kind": "prepare", "status": "running"})
    work["upload"] = {"id": ident, "filename": body["filename"], "size": body["size"],
                      "kind": body["kind"], "status": "awaiting_upload", "cleanup_status": "none"}
    return {"upload_id": ident, "pathname": "raw/fixture/" + ident + "/source.imscc",
            "ticket": "synthetic-ticket", "maximum_size": body["size"]}


def handle(route):
    req = route.request
    path = urlsplit(req.url).path
    if path == "/mentor":
        return route.fulfill(content_type="text/html", body=HTML)
    if path == "/static/hosted-upload.js":
        return route.fulfill(content_type="application/javascript", body=STUB)
    if path.startswith("/static/"):
        name = path.removeprefix("/static/")
        assert name in ("mentor.js", "style.css")
        return route.fulfill(content_type="text/css" if name.endswith(".css") else "application/javascript",
                             body=(ROOT / "python/app/static" / name).read_text())
    if path == "/favicon.ico":
        return route.fulfill(status=204)
    assert path.startswith("/mentor/api/"), "Unexpected external request"
    body = req.post_data_json if req.post_data else {}
    state["calls"].append((path, req.method, body))
    if req.method != "GET":
        assert req.headers["x-mentor-csrf"] == "fixture"
    if path.endswith("/session"):
        data = {"csrf": "fixture", "authenticated": True, "login_required": False,
                "hosted_ready": True, "credentials": []}
    elif path == "/mentor/api/workspaces":
        data = [{"id": work["id"], "title": work["title"], "stage": work["stage"]}]
    elif path.endswith("/uploads"):
        if state.pop("fail_intent", False):
            return route.fulfill(status=503, content_type="application/json", body='{"error":"Synthetic authorization failure."}')
        if state.pop("hold_intent", False):
            state["pending_intent"] = (route, body)
            return
        data = reserve(body)
    elif path.endswith("/complete"):
        assert body == {}, "Browser receipts must not be trusted"
        if state.pop("hold_complete", False):
            state["pending_complete"] = route
            return
        work["upload"]["status"] = "uploaded"
        data = {"accepted": True}
    elif path.endswith("/cancel-job"):
        assert body["job_id"] == work["active_job"]["id"]
        work["upload"].update(status="canceled", cleanup_status="pending")
        work.update(active_job=None, stage="brief")
        data = work
    elif path.endswith("/inventory"):
        data = {"course_items": 12 if work["source_type"] == "prepared" else 0,
                "materials": [], "counts": {}, "warnings": [], "limitations": []}
    elif path == "/mentor/api/workspaces/" + work["id"]:
        data = work
    else:
        raise AssertionError(path)
    route.fulfill(content_type="application/json", body=json.dumps(deepcopy(data)))


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/*", handle)
    page.goto("https://mentor.test/mentor")
    page.wait_for_function("busy === false")
    page.locator("#workspace-list").select_option(work["id"])
    page.wait_for_function("busy === false")
    page.evaluate("""const input=document.getElementById('source-file');const data=new DataTransfer();
data.items.add(new File([new Uint8Array(24000000)],'small-course.imscc'));
input.files=data.files;""")
    before = len(state["calls"])
    page.locator("#upload-course-button").click()
    page.wait_for_function("document.getElementById('upload-feedback').hidden === false")
    assert "Starting" in page.locator("#upload-feedback-title").inner_text()
    assert "24.0 MB" in page.locator("#upload-feedback-file").inner_text()
    assert page.locator("#upload-course-button").is_disabled()
    assert "No file bytes" in page.locator("#upload-feedback-status").inner_text()
    assert page.locator("#hosted-upload-progress").get_attribute("value") is None
    assert page.evaluate("window.testTransfer === null")
    page.evaluate("uploadFeedback.changedAt -= 16000; renderUploadFeedback()")
    assert "Still waiting" in page.locator("#upload-feedback-status").inner_text()
    route, body = state.pop("pending_intent")
    route.fulfill(content_type="application/json", body=json.dumps(reserve(body)))
    page.wait_for_function("window.testTransfer !== null")
    assert all(path.endswith("/uploads") for path, _, _ in state["calls"][before:]), "Transfer must start without inventory/workspace waits"
    page.evaluate("window.testTransfer.options.onProgress({percentage:50,loaded:12000000,total:24000000})")
    assert page.locator("#hosted-upload-progress").get_attribute("value") == "50"
    assert "12.0 MB of 24.0 MB" in page.locator("#upload-feedback-bytes").inner_text()
    page.evaluate("window.testTransfer.resolve({untrusted:'receipt'})")
    page.wait_for_function("document.getElementById('upload-feedback-title').textContent.includes('confirming')")
    assert "not the same as preparation" in page.locator("#upload-feedback-status").inner_text()
    work["upload"]["status"] = "uploaded"
    state.pop("pending_complete").fulfill(content_type="application/json", body='{"accepted":true}')
    page.wait_for_function("busy === false")
    assert "preparation queued" in page.locator("#upload-feedback-title").inner_text()
    assert page.locator("#hosted-upload-progress").get_attribute("value") is None
    assert "return to this workspace" in page.locator("#upload-feedback-help").inner_text()
    for phase, text in (("setup", "secure processing"), ("download", "Loading the archive"), ("prepare", "Reading course")):
        work["upload"].update(status="processing", phase=phase)
        page.wait_for_function("(text)=>document.getElementById('upload-feedback-title').textContent.includes(text)", arg=text)
    work["upload"].update(status="ready", cleanup_status="deleted")
    work.update(active_job=None, stage="brief", source_type="prepared", revision=2)
    page.wait_for_function("document.getElementById('upload-feedback-title').textContent.includes('has been prepared')")
    assert page.locator("#upload-feedback").get_attribute("data-tone") == "success"
    assert page.locator("#mentor-status").is_hidden()
    page.evaluate("window.testTransfer=null")
    page.locator("#upload-course-button").click()
    page.wait_for_function("window.testTransfer !== null")
    page.locator("#abort-upload").click()
    page.wait_for_function("busy === false")
    assert "canceled" in page.locator("#upload-feedback-title").inner_text()
    state["fail_intent"] = True
    page.locator("#upload-course-button").click()
    page.wait_for_function("busy === false")
    assert "needs your attention" in page.locator("#upload-feedback-title").inner_text()
    page.locator("#check-upload-progress").click()
    page.wait_for_function("uploadChecking === false")
    assert "No new upload" in page.locator("#upload-feedback-title").inner_text()
    assert page.locator("#upload-course-button").is_enabled()
    assert not errors, errors
    browser.close()
    print("Delayed 24 MB start, direct transfer, confirmation, preparation phases, cancellation and recovery feedback passed.")
