"""Optional real local workflow unittest; never uses user data, keys or live AI.

Run with a Python containing Playwright; --python chooses the backend virtualenv.
The managed CLI must already be installed. No package installation is performed.
An explicit --output must be fresh and retains synthetic screenshots/server logs.
"""

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
import zipfile

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]
OPTIONS = argparse.Namespace(
    cli=None, python=None, output=None, ai_fixture=False, provider="openai",
    browser=Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if sys.platform == "darwin" else None)
PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jP1sAAAAASUVORK5CYII=")
DEEP_TEXT = """() => {
  function text(node) {
    let value=node.nodeType===Node.TEXT_NODE ? node.textContent : '';
    if(node.shadowRoot)value+=text(node.shadowRoot);
    for(const child of node.childNodes || [])value+=text(child);
    return value;
  }
  return text(document.body);
}"""


def synthetic_cartridge():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("imsmanifest.xml", """<manifest identifier="synthetic-e2e"
          xmlns="http://www.imsglobal.org/xsd/imsccv1p1/imscp_v1p1">
          <metadata><title>Synthetic browser course</title></metadata>
          <organizations/><resources>
          <resource identifier="first" type="webcontent" href="wiki_content/first.html">
            <file href="wiki_content/first.html"/></resource>
          <resource identifier="second" type="webcontent" href="wiki_content/second.html">
            <file href="wiki_content/second.html"/></resource>
          <resource identifier="pixel" type="webcontent" href="web_resources/pixel.png">
            <file href="web_resources/pixel.png"/></resource>
          </resources></manifest>""")
        archive.writestr("wiki_content/first.html", """
          <html><head><title>First lesson</title><meta name="identifier" content="first"></head>
          <body><h2>Synthetic approved first lesson</h2>
          <p>Students compare synthetic evidence.</p>
          <img src="$IMS-CC-FILEBASE$/pixel.png" alt="Synthetic course pixel">
          <a href="second.html">Continue to second lesson</a>
          <script>throw new Error('ARCHIVE-SCRIPT-MUST-NOT-RUN')</script></body></html>""")
        archive.writestr("wiki_content/second.html", """
          <html><head><title>Second lesson</title><meta name="identifier" content="second"></head>
          <body><h2>Synthetic approved second lesson</h2><p>Second synthetic lesson.</p>
          <a href="first.html">Back to first lesson</a></body></html>""")
        archive.writestr("web_resources/pixel.png", PIXEL)
        # Stored bytes guarantee two real bounded loopback chunks. This is a
        # functional transfer fixture, not a real-course performance benchmark.
        archive.writestr("web_resources/unsupported-padding.bin", b"\0" * (9 * 1024**2))
    return output.getvalue()


def provider_fixture_model():
    return "claude-sonnet-5-5" if OPTIONS.provider == "anthropic" else "synthetic-browser-model"


def environment(home, cli, node):
    result = {key: os.environ[key] for key in (
        "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL",
    ) if key in os.environ}
    result.update(HOME=str(home), USERPROFILE=str(home), HAX_DATA_DIR=str(home / "app-data"),
                  HAX_EXECUTION_MODE="local", HAX_HAX_CLI=str(cli), HAX_NODE=str(node),
                  HAX_NO_BROWSER="1", PYTHONUNBUFFERED="1", NO_COLOR="1")
    return result


def serve(root):
    """Child-only entry: real main app with private config, no reloader/browser."""
    sys.path[:0] = [str(ROOT / "python/app"), str(ROOT / "python")]
    if OPTIONS.ai_fixture:
        install_provider_http_fixture(root)
    import app as web
    from werkzeug.serving import WSGIRequestHandler, make_server

    class QuietHandler(WSGIRequestHandler):
        def log(self, type, message, *args):
            pass

    data = root / "mentor-data"
    data.mkdir(mode=0o700)
    web.app.config.update(TESTING=True, MENTOR_DATA_ROOT=data, LOCAL_HAX_ENABLED=True)
    server = make_server("127.0.0.1", 0, web.app, threaded=True, request_handler=QuietHandler)
    (root / "server.json").write_text(json.dumps({"url": f"http://127.0.0.1:{server.server_port}"}))

    def stop(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        web.shutdown_local_services()
        server.server_close()


def install_provider_http_fixture(root):
    """Child-local in-memory HTTP transport; unexpected traffic fails closed."""
    import httpx
    real_client = httpx.Client
    calls, lock = [], threading.Lock()

    def transport(request):
        anthropic = OPTIONS.provider == "anthropic"
        expected_host = "api.anthropic.com" if anthropic else "api.openai.com"
        if request.url.host != expected_host:
            raise AssertionError("Unexpected external HTTP request in synthetic fixture")
        credential = request.headers.get("x-api-key") if anthropic else request.headers.get("authorization")
        expected = "synthetic-browser-only-provider-key" if anthropic else "Bearer synthetic-browser-only-provider-key"
        if credential != expected:
            raise AssertionError("Unexpected synthetic provider identity")
        if request.method == "GET" and request.url.path == "/v1/models":
            result = {"object": "list", "data": [
                {"id": provider_fixture_model(), "object": "model", "owned_by": "synthetic"}]}
            kind = "models"
        elif request.method == "POST" and request.url.path == (
                "/v1/messages" if anthropic else "/v1/chat/completions"):
            body = json.loads(request.content)
            if body["model"] != provider_fixture_model():
                raise AssertionError("The selected model was not pinned into inference")
            if anthropic and (body.get("output_config") != {"effort": "medium"}
                              or not 8192 <= body["max_tokens"] <= 16384):
                raise AssertionError("The bounded reasoning profile was not applied")
            prompt = body["messages"][-1]["content"]
            if prompt.startswith("Assess this one item in the explicit local review scope"):
                payload = json.loads(prompt.split("\n", 1)[1].split("\nReturn exactly", 1)[0])
                identifier = payload["item"]["id"]
                sources = payload["evidence_sources"]
                if not {"policy", "student"}.issubset({entry["role"] for entry in sources}):
                    raise AssertionError("Supplemental evidence roles were not provided to assessment")
                current = next(entry for entry in sources if entry["source_id"] == "course:" + identifier)
                dimensions = ("ai_leverage", "delegation_risk", "authenticity",
                              "pedagogical_quality", "transparency", "alignment")
                data = {
                    "item_id": identifier,
                    "blooms": {"level": 4, "rationale": "Synthetic comparison activity fixture classification."},
                    "aias": {"observed_level": None, "recommended_level": 3,
                             "rationale": "Synthetic suggestion only; current AI-use conditions are unstated."},
                    "dimensions": {name: {"score": None if name == "alignment" else 6,
                                          "rationale": "Synthetic fixture judgment; instructor verification required."}
                                   for name in dimensions},
                    "strengths": ["Visible instructional content is supplied for this synthetic fixture."],
                    "concerns": ["Success criteria require instructor verification."],
                    "recommendations": ["Confirm criteria and adapt the controlled comparison activity."],
                    "evidence": [{"source_id": current["source_id"], "quote": current["text"][:200]}],
                    "limitations": ["Synthetic fixture only; no measured outcomes or established AI conditions."],
                }
                if not any(call["kind"].startswith("assessment_") for call in calls):
                    time.sleep(1.2)  # Exercise visible waiting feedback without a paid request.
                kind = "assessment_" + payload["scope"]
            elif "Create a concrete draft following" in prompt:
                records = re.findall(r"SOURCE course:([^\s]+) \([^,\n]+, ([^\n)]+)\):", prompt)
                chosen = next((identifier for identifier, title in records if title == "First lesson"), None)
                known = [chosen] if chosen else []
                proposals = [{
                    "id": known[0] if known else "", "title": "First lesson", "type": "pages",
                    "html": "<h2>Synthetic approved first lesson</h2><p>AI-fixture comparison activity.</p>",
                    "reason": "Synthetic test proposal based on the reviewed teaching direction.",
                    "source_ids": ["course:" + known[0]] if known else ["instructor-brief"],
                }]
                if not known:
                    proposals.append({
                        "id": "", "title": "Second lesson", "type": "pages",
                        "html": "<h2>Synthetic approved second lesson</h2><p>Synthetic reflection task.</p>",
                        "reason": "Synthetic test continuation.", "source_ids": ["instructor-brief"],
                    })
                data = {"summary": "Synthetic provider draft for review.", "items": proposals,
                        "warnings": ["Synthetic fixture output, not an evaluation of a real course."]}
                kind = "draft"
            elif "CURRENT DRAFT" in prompt:
                data = {"reply": "Synthetic final-review discussion; approval remains a separate action.",
                        "items": []}
                kind = "review"
            elif "RECENT CONVERSATION" in prompt:
                data = {"reply": "Synthetic mentor response: confirm the comparison activity and constraints."}
                kind = "interview"
            else:
                raise AssertionError("Unknown inference prompt requires an explicit fixture schema")
            result = {
                "id": "synthetic-completion", "object": "chat.completion", "created": 1,
                "model": provider_fixture_model(),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": json.dumps(data)}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
            if anthropic:
                result = {
                    "id": "msg_synthetic", "type": "message", "role": "assistant",
                    "model": provider_fixture_model(), "stop_reason": "end_turn", "stop_sequence": None,
                    "content": [
                        {"type": "thinking", "thinking": "PRIVATE-THINKING-NOT-ANSWER", "signature": "fixture"},
                        {"type": "text", "text": json.dumps(data)}],
                    "usage": {"input_tokens": 1, "output_tokens": 10},
                }
        else:
            raise AssertionError("Unexpected provider HTTP endpoint in synthetic fixture")
        with lock:
            calls.append({"kind": kind, "method": request.method, "path": request.url.path})
            (root / "provider-fixture-calls.json").write_text(json.dumps(calls, indent=2))
        return httpx.Response(200, json=result, request=request)

    class FixtureClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(transport)
            super().__init__(*args, **kwargs)
    httpx.Client = FixtureClient


@unittest.skipUnless(sync_playwright, "Optional Playwright Python package is required")
class LocalEndToEndTest(unittest.TestCase):
    def test_real_chunked_course_guided_build_and_hax_lifecycle(self):
        self.exercise("guided")

    def test_ai_imported_course_with_supplements_and_model_selection(self):
        if not OPTIONS.ai_fixture:
            self.skipTest("Use --ai-fixture for synthetic OpenAI HTTP coverage")
        self.exercise("ai-imported")

    def test_ai_blank_course_with_supplements_and_model_selection(self):
        if not OPTIONS.ai_fixture:
            self.skipTest("Use --ai-fixture for synthetic OpenAI HTTP coverage")
        self.exercise("ai-blank")

    def exercise(self, mode):
        cli = (OPTIONS.cli or ROOT / "node_modules/@haxtheweb/create/dist/create.js").resolve()
        node = shutil.which("node")
        backend_python = OPTIONS.python or Path("/tmp/hax-hosted-runtime-check/bin/python")
        if not cli.is_file() or not node or not backend_python.is_file():
            self.skipTest("Managed HAX CLI, Node and backend virtualenv are required")
        if OPTIONS.output:
            root = (OPTIONS.output / mode).resolve()
            root.mkdir(mode=0o700, parents=True, exist_ok=False)
        else:
            temporary = tempfile.TemporaryDirectory(prefix="hax-local-e2e-")
            self.addCleanup(temporary.cleanup)
            root = Path(temporary.name).resolve()
        home = root / "isolated-home"
        home.mkdir(mode=0o700)
        log = (root / "flask.log").open("wb")
        self.addCleanup(log.close)
        process = subprocess.Popen(
            [str(backend_python), str(Path(__file__).resolve()), "--serve", str(root)]
            + (["--ai-fixture", "--provider", OPTIONS.provider] if mode != "guided" else []),
            cwd=root, env=environment(home, cli, node), stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=os.name != "nt")

        def stop_backend():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    if os.name != "nt":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                    process.wait(timeout=5)
        self.addCleanup(stop_backend)
        deadline = time.monotonic() + 30
        while not (root / "server.json").exists():
            if process.poll() is not None:
                self.fail("Real backend exited: " + (root / "flask.log").read_text()[-4000:])
            if time.monotonic() > deadline:
                self.fail("Real backend startup timed out")
            time.sleep(.1)
        url = json.loads((root / "server.json").read_text())["url"]
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True, executable_path=str(OPTIONS.browser) if OPTIONS.browser else None)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                external_writes = []
                def guard_external_writes(route):
                    target = urlsplit(route.request.url)
                    if (target.scheme in ("http", "https") and target.hostname != "127.0.0.1"
                            and route.request.method not in ("GET", "HEAD")):
                        external_writes.append((route.request.method, target.netloc))
                        route.abort()
                    else:
                        route.continue_()
                context.route("**/*", guard_external_writes)
                page = context.new_page()
                errors, chunks, external = [], [], set()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("response", lambda response: chunks.append(response.json()["offset"])
                        if response.request.method == "PUT"
                        and "/api/local-uploads/" in response.url else None)
                page.goto(url + "/mentor")
                page.wait_for_function("busy === false")
                session = page.request.get(url + "/mentor/api/session").json()
                self.assertTrue(session["local_hax"])
                self.assertFalse(session["local_accounts"])
                self.assertEqual(session["identity"], "this-browser")
                csrf = session["csrf"]

                def api(path, method="GET", body=None, status=200):
                    response = page.request.fetch(url + path, method=method, data=body,
                                                  headers={"X-Mentor-CSRF": csrf,
                                                           "Origin": url})
                    self.assertEqual(response.status, status, response.text())
                    return response.json()

                page.locator("#new-title").fill("Synthetic real end-to-end course")
                page.locator("#new-workspace button").click()
                page.wait_for_function("busy === false && workspace !== null")
                workspace_id = page.evaluate("workspace.id")
                base = "/mentor/api/workspaces/" + workspace_id
                # Server must reject early approval/build, independent of disabled UI.
                api(base + "/build", "POST", {"draft_revision": 0}, status=409)
                page.locator("#mentor-provider").select_option("guided" if mode == "guided" else OPTIONS.provider)
                page.wait_for_function("busy === false")
                if mode != "guided":
                    page.locator("#mentor-key").fill("synthetic-browser-only-provider-key")
                    page.locator("#save-provider-key").click()
                    page.wait_for_function("busy === false && modelsLoadingProvider === ''")
                    page.locator("#mentor-model").select_option(provider_fixture_model())
                    page.locator("#save-model").click()
                    page.wait_for_function("busy === false && mentorMode().ready")
                    self.assertIn(provider_fixture_model(), page.locator("#mentor-mode").inner_text())
                if mode == "ai-blank":
                    page.locator("#start-blank-course").click()
                    self.assertEqual(api(base + "/inventory")["course_items"], 0)
                else:
                    page.locator("#source-file").set_input_files({
                        "name": "synthetic.imscc", "mimeType": "application/zip",
                        "buffer": synthetic_cartridge(),
                    })
                    page.locator("#upload-course-button").click()
                    page.wait_for_function(
                        "busy === false && workspace.source_type === 'imscc' && workspace.active_job === null",
                        timeout=90000)
                    self.assertEqual(len(chunks), 2)
                    self.assertEqual(chunks[0], 8 * 1024**2)
                    chunks[1] -= chunks[0]
                    self.assertLess(chunks[1], 2 * 1024**2)
                    self.assertEqual(api(base + "/inventory")["course_items"], 2)
                if mode != "guided":
                    page.locator("#supplemental-sources").evaluate("(node) => node.open = true")
                    page.locator("#material-role").select_option("policy")
                    page.locator("#material-file").set_input_files({
                        "name": "synthetic-policy.txt", "mimeType": "text/plain",
                        "buffer": b"SYNTHETIC-POLICY-ONLY: private instructor reference, never publish verbatim.",
                    })
                    page.locator("#upload-material-button").click()
                    page.wait_for_function("busy === false && workspace.materials.length === 1")
                    page.locator("#material-role").select_option("student")
                    page.locator("#material-text").fill(
                        "Synthetic student background: compare controlled evidence and reflect on limitations.")
                    page.locator("#material-text-form button").click()
                    page.wait_for_function("busy === false && workspace.materials.length === 2")
                self.assertTrue(page.locator("#approve-build").is_disabled())
                self.assertTrue(page.locator("#build-course").is_disabled())
                page.locator("#mentor-message").fill(
                    "PRIVATE-INSTRUCTOR-DISCUSSION: preserve these synthetic instructional pages and compare evidence.")
                page.locator("#send-mentor-message").click()
                page.wait_for_function("busy === false && workspace.active_job === null")
                page.locator("#approve-direction").click()
                page.wait_for_function("busy === false && workspace.direction_revision === workspace.revision")
                page.locator("#analyze-course").click()
                self.assertTrue(page.locator("#analysis-feedback").is_visible())
                page.wait_for_function("busy === false && workspace.draft !== null && workspace.active_job === null",
                                       timeout=90000)
                if mode != "guided":
                    initial_draft = api(base)["draft"]
                    self.assertTrue(initial_draft["assessment_required"])
                    self.assertTrue(initial_draft["assessment_current"])
                    self.assertEqual(initial_draft["assessment"]["model"], provider_fixture_model())
                    self.assertEqual(initial_draft["assessment"]["coverage"]["items_evaluated"], 2)
                    self.assertTrue(page.locator("#assessment-panel").is_visible())
                    self.assertTrue(page.locator("#proposed-assessment").is_visible())
                    if mode == "ai-imported":
                        self.assertEqual(initial_draft["baseline_assessment"]["coverage"]["items_evaluated"], 2)
                        self.assertFalse(page.locator("#baseline-assessment").is_hidden())
                    else:
                        self.assertNotIn("baseline_assessment", initial_draft)
                if mode == "ai-blank":
                    second_id = page.evaluate("workspace.draft.items[1].id")
                    page.locator("#draft-html-0").fill(
                        '<h2>Synthetic approved first lesson</h2><p>Instructor-reviewed synthetic activity.</p>'
                        f'<a href="$IMSCC_ITEM_REFERENCE$/{second_id}">Continue to second lesson</a>')
                    page.locator("#save-draft").click()
                    page.wait_for_function("busy === false && workspace.build_revision === null")
                    self.assertFalse(api(base)["draft"]["assessment_current"])
                    self.assertTrue(page.locator("#approve-build").is_disabled())
                draft_revision = page.evaluate("workspace.draft_revision")
                api(base + "/approve-build", "POST", {"draft_revision": draft_revision}, status=409)
                self.assertTrue(page.locator("#approve-build").is_disabled())
                page.locator("#mentor-message").fill("I reviewed these exact synthetic pages.")
                page.locator("#send-mentor-message").click()
                page.wait_for_function(
                    "busy === false && workspace.reviewed_draft_revision === workspace.draft_revision")
                draft_revision = page.evaluate("workspace.draft_revision")
                if mode != "guided":
                    assessed = api(base)["draft"]
                    self.assertTrue(assessed["assessment_current"])
                    by_id = {item["item_id"]: item for item in assessed["assessment"]["items"]}
                    for item in assessed["items"]:
                        self.assertEqual(by_id[item["id"]]["content_sha256"],
                                         hashlib.sha256(item["html"].encode()).hexdigest())
                        self.assertIsNone(by_id[item["id"]]["dimensions"]["alignment"]["score"])
                        self.assertEqual(by_id[item["id"]]["aias"]["recommended_level"], 3)
                    page.locator("#proposed-assessment-content details").first.evaluate(
                        "(node) => node.open = true")
                    report_text = page.locator("#proposed-assessment-content").inner_text()
                    self.assertIn("Bloom's taxonomy: Analyze · level 4.", report_text)
                    self.assertIn("recommended 3 · AI Collaboration", report_text)
                    self.assertIn("Alignment (application-defined heuristic): not assessed.", report_text)
                    self.assertIn("AI leverage (application-defined heuristic): 6.", report_text)
                    self.assertIn("Evidence from assessed excerpts", report_text)
                    page.locator("#assessment-panel").screenshot(
                        path=str(root / "assessment-detail.png"))
                    page.screenshot(path=str(root / "mentor-assessment.png"), full_page=True)
                page.locator("#approve-build").click()
                page.wait_for_function("busy === false && workspace.build_revision === workspace.draft_revision")
                page.locator("#build-course").click()
                page.wait_for_function(
                    "busy === false && (workspace.stage === 'built' || "
                    "(workspace.active_job === null && workspace.jobs.at(-1)?.status === 'failed'))",
                    timeout=120000)
                completed_build = api(base)
                self.assertEqual(completed_build["stage"], "built", completed_build["jobs"][-1])
                self.assertTrue(completed_build["artifact"]["hax_site"])
                page.wait_for_function("haxStatus?.running === true && haxStatus?.phase === 'ready'",
                                       timeout=60000)
                built = api(base)
                hax = api(base + "/hax")
                self.assertNotEqual(hax["url"].split(":")[-1], url.split(":")[-1])
                self.assertEqual(built["artifact"]["hax_site"]["draft_revision"], draft_revision)
                directory = Path(hax["directory"])
                self.assertTrue(directory.is_relative_to(root / "mentor-data/sites" / workspace_id))
                manifest = json.loads((directory / "site.json").read_text())
                self.assertEqual(len(manifest["items"]), 2)
                first_slug = next(item["slug"] for item in manifest["items"] if item["title"] == "First lesson")
                second_slug = next(item["slug"] for item in manifest["items"] if item["title"] == "Second lesson")
                self.assertEqual(json.loads((directory / "approval.json").read_text())["build_revision"],
                                 draft_revision)
                exported = [(directory / "site.json"), (directory / "approval.json")]
                exported.extend(directory / item["location"] for item in manifest["items"])
                for path in exported:
                    self.assertNotIn("PRIVATE-INSTRUCTOR-DISCUSSION", path.read_text())
                    self.assertNotIn("ARCHIVE-SCRIPT-MUST-NOT-RUN", path.read_text())
                    self.assertNotIn("SYNTHETIC-POLICY-ONLY", path.read_text())
                    self.assertNotIn("synthetic-browser-only-provider-key", path.read_text())
                page.screenshot(path=str(root / "mentor-built.png"), full_page=True)
                hax_page = context.new_page()
                hax_page.on("pageerror", lambda error: errors.append(str(error)))
                hax_page.on("request", lambda req: external.add(req.url.split("/")[2])
                            if req.url.startswith("https://") else None)
                hax_page.goto(hax["url"] + first_slug)
                hax_page.wait_for_function(
                    DEEP_TEXT.replace("return text(document.body);",
                                      "return text(document.body).includes('Synthetic approved first lesson');"),
                    timeout=90000)
                hax_page.wait_for_function(
                    DEEP_TEXT.replace("return text(document.body);",
                                      "return text(document.body).includes('Second lesson') && "
                                      "(!document.querySelector('#loading') || document.querySelector('#loading').hidden);"),
                    timeout=90000)
                if mode != "ai-blank":
                    self.assertTrue(hax_page.get_by_alt_text("Synthetic course pixel").evaluate(
                        "(img) => img.complete && img.naturalWidth > 0"))
                hax_page.screenshot(path=str(root / "hax-first-page.png"), full_page=True)
                hax_page.get_by_text("Continue to second lesson", exact=True).click()
                hax_page.wait_for_function(
                    DEEP_TEXT.replace("return text(document.body);",
                                      "return text(document.body).includes('Synthetic approved second lesson');"),
                    timeout=45000)
                self.assertIn(second_slug, hax_page.url)
                hax_page.screenshot(path=str(root / "hax-second-page.png"), full_page=True)
                hax_page.close()
                second_workspace = api("/mentor/api/workspaces", "POST",
                                       {"title": "Separate synthetic course"}, status=201)
                self.assertIsNone(second_workspace["draft"])
                self.assertIsNone(second_workspace["artifact"])
                self.assertEqual({w["id"] for w in api("/mentor/api/workspaces")},
                                 {workspace_id, second_workspace["id"]})
                page.reload()
                page.wait_for_function("busy === false")
                page.locator("#workspace-list").select_option(workspace_id)
                page.wait_for_function("busy === false && workspace.stage === 'built'")
                page.locator("#stop-hax-site").click()
                page.wait_for_function("haxBusy === false && haxStatus?.phase === 'stopped'")
                self.assertFalse(api(base + "/hax")["running"])
                page.reload()
                page.wait_for_function("busy === false")
                page.locator("#workspace-list").select_option(workspace_id)
                page.wait_for_function("busy === false && workspace.stage === 'built'")
                self.assertFalse(api(base + "/hax")["running"])
                page.locator("#start-hax-site").click()
                page.wait_for_function("haxStatus?.running === true && haxStatus?.phase === 'ready'",
                                       timeout=60000)
                restarted = api(base + "/hax")
                self.assertEqual(restarted["directory"], hax["directory"])
                self.assertEqual(api(base)["draft_revision"], draft_revision)
                page.locator("#stop-hax-site").click()
                page.wait_for_function("haxBusy === false && haxStatus?.phase === 'stopped'")
                self.assertEqual(errors, [])
                self.assertEqual(external_writes, [])
                if mode != "guided":
                    calls = json.loads((root / "provider-fixture-calls.json").read_text())
                    self.assertTrue({"models", "interview", "draft", "review"}.issubset(
                        {call["kind"] for call in calls}))
                    kinds = [call["kind"] for call in calls]
                    self.assertEqual(kinds.count("assessment_baseline"), 2 if mode == "ai-imported" else 0)
                    self.assertEqual(kinds.count("assessment_draft"), 2 if mode == "ai-imported" else 4)
                    self.assertTrue(all(call["path"] in ("/v1/models", "/v1/chat/completions", "/v1/messages")
                                        for call in calls))
                    self.assertNotIn("PRIVATE-THINKING-NOT-ANSWER", json.dumps(api(base)))
                print(json.dumps({"result": "passed", "chunks": chunks, "workspace": workspace_id,
                                  "mode": mode,
                                  "hax_url": hax["url"], "restart_url": restarted["url"],
                                  "external_browser_hosts": sorted(external),
                                  "evidence_directory": str(root)}), flush=True)
            except BaseException:
                if "page" in locals():
                    try:
                        page.screenshot(path=str(root / "mentor-failure.png"), full_page=True)
                    except Exception:
                        pass
                raise
            finally:
                browser.close()


def main():
    global OPTIONS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--browser", type=Path,
                        default=OPTIONS.browser)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--cli", type=Path)
    parser.add_argument("--ai-fixture", action="store_true",
                        help="Also run imported/blank AI paths through synthetic provider HTTP transport")
    parser.add_argument("--provider", choices=("openai", "anthropic"), default="openai")
    OPTIONS = parser.parse_args()
    if OPTIONS.serve:
        serve(OPTIONS.serve.resolve())
    else:
        if OPTIONS.output:
            OPTIONS.output.resolve().mkdir(mode=0o700, parents=True, exist_ok=False)
        unittest.main(argv=[sys.argv[0], "-v"])


if __name__ == "__main__":
    main()
