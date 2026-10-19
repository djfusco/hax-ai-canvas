"""Optional: python3 tests/browser_hosted_accounts.py (Playwright + Chromium).

All HTTP requests are intercepted; no credentials, email or cloud calls are used.
"""
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
html = (ROOT / "python/app/templates/mentor.html").read_text().split("{% block scripts %}")[0]
html = re.sub(r"{%.*?%}", "", html, flags=re.S)
html += '<script src="/static/mentor.js"></script>'
javascript = (ROOT / "python/app/static/mentor.js").read_text()
state = {"invited": False, "authenticated": False, "saved": False, "selected": "", "calls": []}


def handle(route):
    request = route.request
    path = urlsplit(request.url).path
    if path == "/mentor":
        return route.fulfill(content_type="text/html", body=html)
    if path == "/static/mentor.js":
        return route.fulfill(content_type="application/javascript", body=javascript)
    assert path.startswith("/mentor/api/"), "Unexpected external request"
    body = request.post_data_json if request.post_data else {}
    state["calls"].append((path, request.method, body))
    csrf = "after-account" if state["authenticated"] else "before-account"
    if request.method != "GET":
        assert request.headers["x-mentor-csrf"] == csrf
    status = 200
    if path.endswith("/session"):
        data = {"csrf": csrf, "hosted_ready": True, "authenticated": state["authenticated"],
                "login_required": not state["authenticated"], "invite_required": not state["invited"],
                "account_required": not state["authenticated"],
                "account": {"email": "alice@example.edu"} if state["authenticated"] else None,
                "credentials": [{"provider": "openai", "expires_at": 2000000000}] if state["saved"] else []}
    elif path.endswith("/login"):
        assert body["code"] == "synthetic-invitation"
        state["invited"] = True
        data = {"account_required": True}
    elif path.endswith("/auth/send-code"):
        assert body["email"] == "alice@example.edu"
        data = {"sent": True}
    elif path.endswith("/auth/verify-code"):
        assert body["code"] == "123456" and body["email"] == "alice@example.edu"
        state["authenticated"] = True
        data = {"authenticated": True}
    elif path.endswith("/auth/logout"):
        state.update(authenticated=False, invited=False)
        data = {"signed_out": True}
    elif path.endswith("/workspaces"):
        assert state["authenticated"]
        data = []
    elif path.endswith("/credentials") and request.method == "POST":
        assert body["api_key"] == "synthetic-provider-key" and body["model"] == ""
        state.update(saved=True, selected="")
        data = {"configured": True}
    elif path.endswith("/credentials/openai") and request.method == "DELETE":
        state.update(saved=False, selected="")
        data = {"ok": True}
    elif path.endswith("/model") and request.method == "PUT":
        assert body["model"] == "future-text" and body["version"] == "fixture-version"
        state["selected"] = body["model"]
        data = {"selected": body["model"]}
    elif "/providers/" in path:
        if not state["saved"]:
            status, data = 400, {"error": "Save a key first."}
        else:
            data = {"models": [{"id": "future-text", "name": "Future model"}],
                    "version": "fixture-version", "selected": state["selected"],
                    "expired": False, "manual_allowed": False, "warning": "Compatibility must be checked."}
    else:
        raise AssertionError("Unexpected app route: " + path)
    route.fulfill(status=status, content_type="application/json", body=json.dumps(data))


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/*", handle)
    page.goto("https://mentor.test/mentor")
    page.wait_for_function("busy === false")
    assert page.locator("#account-login-card").is_visible()
    assert page.locator("#workspace-list").is_disabled()
    page.locator("#mentor-invite-code").fill("synthetic-invitation")
    page.locator("#mentor-login-form button").click()
    page.wait_for_function("busy === false")
    assert page.locator("#workspace-list").is_disabled()
    assert not any(path.endswith("/workspaces") for path, _, _ in state["calls"])
    page.locator("#account-email").fill("alice@example.edu")
    page.locator("#account-code-form button").click()
    page.wait_for_function("busy === false")
    assert "Check your email" in page.locator("#account-code-status").inner_text()
    page.locator("#account-code").fill("123456")
    page.locator("#account-verify-form button").click()
    page.wait_for_function("busy === false")
    assert "alice@example.edu" in page.locator("#account-label").inner_text()
    assert page.locator("#account-code").input_value() == ""
    assert not page.locator("#workspace-list").is_disabled()
    page.locator("#provider-settings summary").click()
    page.locator("#mentor-provider").select_option("openai")
    page.wait_for_function("busy === false")
    page.locator("#mentor-key").fill("synthetic-provider-key")
    page.locator("#credential-form button[type=submit]").click()
    page.wait_for_function("busy === false")
    assert page.locator("#mentor-key").input_value() == ""
    assert page.locator("#mentor-model option[value='future-text']").count() == 1
    page.locator("#mentor-model").select_option("future-text")
    page.locator("button[onclick='saveModel()']").click()
    page.wait_for_function("busy === false")
    assert state["selected"] == "future-text"
    page.locator("button[onclick='refreshModels()']").click()
    page.wait_for_function("busy === false")
    assert page.locator("#mentor-model").input_value() == "future-text"
    page.locator("button[onclick='forgetKey()']").click()
    page.wait_for_function("busy === false")
    assert page.locator("#mentor-model option[value='future-text']").count() == 0
    page.locator("button[onclick='signOutAccount()']").click()
    page.wait_for_function("!document.getElementById('account-login-card').hidden && busy === false")
    assert page.locator("#workspace-list").is_disabled()
    assert page.locator("#account-profile").is_hidden()
    assert not errors, errors
    browser.close()
    print("Mocked Chromium sign-in, invitation gate, CSRF rotation, model selection/refresh, forget-key and logout: passed.")
