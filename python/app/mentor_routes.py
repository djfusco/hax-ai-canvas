"""Course Consultant API (served under /mentor), local and hosted."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from urllib.parse import urlsplit

from flask import (
    Blueprint, abort, current_app, g, render_template, request, Response, jsonify as flask_jsonify,
)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.utils import secure_filename
from mentor_store import WorkspaceConflict, WorkspaceNotFound
from hosted_config import hosted_mode, settings, HostedSetupError
from hosted_accounts import AccountAuthError, NeonAuth, session_cookies, new_session_cookies, COOKIE_NAMES


PUBLIC_FIELDS = ("id", "title", "source_type", "revision", "created_at", "updated_at", "messages", "share")


def create_mentor_blueprint(root_provider):
    bp = Blueprint("mentor", __name__, url_prefix="/mentor")

    def backend():
        if not hasattr(g, "hosted_backend"):
            from hosted_backend import HostedBackend
            factory = current_app.config.get("HOSTED_BACKEND_FACTORY", HostedBackend)
            g.hosted_backend = factory(settings())
        return g.hosted_backend

    def auth():
        if not hasattr(g, "neon_auth"):
            factory = current_app.config.get(
                "HOSTED_AUTH_FACTORY" if hosted_mode() else "LOCAL_AUTH_FACTORY", NeonAuth)
            g.neon_auth = factory(g.auth_url, g.auth_origin)
        return g.neon_auth

    def local_accounts():
        from local_accounts import LocalAccounts
        if not hasattr(g, "local_accounts"):
            g.local_accounts = LocalAccounts(root())
        return g.local_accounts

    def auth_cookies():
        if hosted_mode():
            return session_cookies(request.cookies)
        from local_accounts import LOCAL_SESSION_COOKIE
        return local_accounts().cookies(request.cookies.get(LOCAL_SESSION_COOKIE), g.auth_url)

    def jsonify(value):
        if not hosted_mode():
            return flask_jsonify(value)
        encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False)
        def chunks():
            buffer = ""
            for part in encoder.iterencode(value):
                buffer += part
                while len(buffer) >= 32768:
                    yield buffer[:32768]
                    buffer = buffer[32768:]
            if buffer:
                yield buffer
        return Response(chunks(), content_type="application/json; charset=utf-8")

    def root():
        return Path(root_provider()).resolve()

    def store():
        if hosted_mode():
            return backend().store
        from mentor_store import MentorStore
        return MentorStore(root() / "workspaces.sqlite")

    def payload():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        return data

    def redact(value):
        keys = [saved["api_key"] for item in g.vault.status(g.owner)
                if (saved := g.vault.get(g.owner, item["provider"]))]

        def clean(item):
            if isinstance(item, str):
                for key in keys:
                    item = item.replace(key, "[credential removed]")
                return item
            if isinstance(item, list):
                return [clean(v) for v in item]
            if isinstance(item, dict):
                return {k: clean(v) for k, v in item.items()}
            return item
        return clean(value)

    def public(workspace):
        result = {k: v for k, v in workspace.items() if k in PUBLIC_FIELDS}
        course_summary = (workspace.get("source") or {}).get("course_model")
        if course_summary:
            result["course"] = course_summary
        return result

    def capabilities_for_values(provider, values, selected=None, prefer_catalog=False):
        from model_capabilities import resolve_capabilities, validate_capabilities
        chosen = selected or values.get("model")
        if not chosen:
            return None
        if not prefer_catalog and chosen == values.get("model") and values.get("capabilities"):
            return validate_capabilities(provider, chosen, values["capabilities"])
        record = next((row for row in values.get("catalog", {}).get("models", [])
                       if row.get("id") == chosen), None)
        if record and record.get("capabilities"):
            return validate_capabilities(provider, chosen, record["capabilities"])
        return resolve_capabilities(provider, chosen)

    def provider_values(data):
        provider = data.get("provider", "guided")
        if provider == "guided":
            return provider, None
        if provider not in ("nebula", "anthropic", "openai", "gemini", "openrouter"):
            raise ValueError("Unsupported AI provider.")
        values = g.vault.get(g.owner, provider)
        if values is None:
            raise ValueError("Save credentials for the selected provider, or choose guided mode.")
        validate_gateway(provider, values.get("base_url", ""))
        if not values.get("model"):
            raise ValueError("Fetch the provider's models and save an explicit model selection first.")
        profile = capabilities_for_values(provider, values)
        if profile["status"] == "unsupported":
            raise ValueError("The selected model is not supported for this text workflow. Choose a compatible model.")
        return provider, {**values, "capabilities": profile}

    def client(data, resolved=None):
        from ai_client import AIClient
        provider, values = resolved or provider_values(data)
        if values is None:
            return None
        return AIClient(provider, model=values.get("model") or None,
                        api_key=values["api_key"], base_url=values.get("base_url") or None,
                        isolated_credentials=True, capabilities=values.get("capabilities"))

    def validate_gateway(provider, base_url):
        if provider != "nebula":
            if base_url:
                raise ValueError("Custom endpoints are supported only for institutional Nebula gateways.")
            return
        parsed = urlsplit(str(base_url))
        host = parsed.hostname or ""
        allowed = {h.strip() for h in os.environ.get("HAX_ALLOWED_AI_HOSTS", "").split(",") if h.strip()}
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.query or parsed.fragment
                or not (host.endswith(".azure-api.net") or host in allowed)):
            raise ValueError("Use an HTTPS Azure APIM gateway, or an administrator-allowlisted AI host.")

    @bp.before_request
    def browser_identity():
        from mentor_credentials import CredentialVault, private_key
        if request.endpoint in ("mentor.openrouter_callback", "mentor.canvas_callback", "mentor.hax_preview_file",
                                "mentor.shared_view"):
            # Callbacks reached by cross-site redirects, and signed links opened by
            # people who are not signed in. Callbacks from openrouter.ai do not
            # carry our SameSite=Strict cookies. Never mint or replace an identity
            # here; the page finishes the exchange with a same-site request.
            return
        if hosted_mode():
            try:
                config = settings()
            except HostedSetupError as exc:
                return jsonify({"error": str(exc), "code": "HOSTED_SETUP_REQUIRED"}), 503
            g.hosted_config = config
            g.auth_url, g.auth_origin = config.auth_url, config.origin
            key = config.session_secret.encode()
        else:
            key = private_key(root() / ".browser-key")
            from local_accounts import auth_url
            g.auth_url = auth_url(root())
            g.auth_origin = request.host_url.rstrip("/")
        g.mentor_key = key
        signer = URLSafeTimedSerializer(key.decode(), salt="faculty-mentor-browser")
        identity = None
        try:
            identity = signer.loads(request.cookies.get("mentor_browser", ""), max_age=30 * 86400)
        except (BadSignature, SignatureExpired):
            pass
        token = identity.get("token") if isinstance(identity, dict) else identity
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token):
            token = secrets.token_hex(32)
        g.admitted = not hosted_mode() or (
            isinstance(identity, dict) and identity.get("admitted") is True
        )
        g.mentor_token, g.mentor_signer = token, signer
        g.mentor_cookie = signer.dumps({"token": token, "admitted": g.admitted,
                                        "account": bool(isinstance(identity, dict) and identity.get("account"))}) if (
                                            hosted_mode() or isinstance(identity, dict)) else signer.dumps(token)
        g.owner = hashlib.sha256(token.encode()).hexdigest()
        g.legacy_owner = g.owner
        g.legacy_claimable = g.admitted and isinstance(identity, dict) and not identity.get("account")
        if not hosted_mode():
            g.legacy_claimable = bool(identity) and not (
                isinstance(identity, dict) and identity.get("account"))
        g.account = None
        g.neon_updates = []
        g.account_mode = g.hosted_config.auth_mode == "neon" if hosted_mode() else bool(g.auth_url)
        g.csrf = hmac.new(key, (g.owner + ":csrf").encode(), hashlib.sha256).hexdigest()
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin")
            expected_origin = g.hosted_config.origin if hosted_mode() else request.host_url.rstrip("/")
            if origin and origin != expected_origin:
                return jsonify({"error": "Cross-origin mentoring requests are not allowed."}), 403
            if not hmac.compare_digest(request.headers.get("X-Mentor-CSRF", ""), g.csrf):
                return jsonify({"error": "Reload the mentoring page before making changes."}), 403
        if (request.is_json and request.content_length
                and request.content_length > 2 * 1024 * 1024):
            return jsonify({"error": "The mentoring request is too large."}), 413
        auth_endpoints = ("mentor.auth_send", "mentor.auth_verify", "mentor.auth_logout")
        if g.account_mode and request.endpoint not in auth_endpoints:
            account, updates = auth().session(auth_cookies())
            if hosted_mode():
                g.neon_updates.extend(updates)
            else:
                from local_accounts import LOCAL_SESSION_COOKIE
                token = request.cookies.get(LOCAL_SESSION_COOKIE)
                local_accounts().refresh_session(token, g.auth_url, updates)
            if account:
                g.owner = (backend().accounts.resolve(g.auth_url, account, admit=g.admitted)
                           if hosted_mode() else local_accounts().resolve(g.auth_url, account))
                g.admitted = True
                g.account = account
        g.authenticated = g.admitted and (not g.account_mode or g.account is not None)
        if not g.authenticated:
            if request.endpoint not in ("mentor.page", "mentor.session_info", "mentor.login",
                                        "mentor.local_auth_config", *auth_endpoints):
                return jsonify({"error": "Sign in to your account first.", "code": "LOGIN_REQUIRED"}), 401
        elif hosted_mode():
            g.vault = backend().vault
        else:
            g.vault = CredentialVault(root())

    @bp.after_request
    def response_security(response):
        for name, value, age in getattr(g, "neon_updates", []):
            response.set_cookie(name, value, max_age=age, path="/mentor",
                                httponly=True, secure=True, samesite="Strict")
        if hasattr(g, "local_session_update"):
            from local_accounts import LOCAL_SESSION_COOKIE
            response.set_cookie(LOCAL_SESSION_COOKIE, g.local_session_update,
                                max_age=12 * 3600 if g.local_session_update else 0,
                                path="/mentor", httponly=True, secure=request.is_secure,
                                samesite="Strict")
        if getattr(g, "mentor_cookie", None):
            response.set_cookie("mentor_browser", g.mentor_cookie, max_age=30 * 86400,
                                httponly=True, secure=hosted_mode() or request.is_secure, samesite="Strict")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @bp.errorhandler(WorkspaceNotFound)
    @bp.errorhandler(WorkspaceConflict)
    @bp.errorhandler(ValueError)
    def bad_input(exc):
        from mentor_store import WorkspaceConflict, WorkspaceNotFound
        if isinstance(exc, WorkspaceNotFound):
            return jsonify({"error": "Workspace not found."}), 404
        if isinstance(exc, WorkspaceConflict):
            return jsonify({"error": str(exc)}), 409
        return jsonify({"error": str(exc)}), exc.status if isinstance(exc, AccountAuthError) else 400

    @bp.errorhandler(Exception)
    def hosted_failure(exc):
        from werkzeug.exceptions import HTTPException
        if isinstance(exc, HTTPException):
            return exc
        if not hosted_mode():
            raise exc
        current_app.logger.error("Hosted request failed (%s)", type(exc).__name__)
        return jsonify({"error": "A hosted service is unavailable. Check deployment configuration and retry.",
                        "code": "HOSTED_SERVICE_ERROR"}), 503

    @bp.route("")
    @bp.route("/")
    def page():
        return render_template("consultant.html")

    @bp.get("/import/extension")
    def import_from_extension():
        return render_template("import_extension.html")

    @bp.get("/api/session")
    def session_info():
        return jsonify({"csrf": g.csrf, "credentials": g.vault.status(g.owner) if g.authenticated else [],
                        "identity": "account" if g.account_mode else "this-browser",
                        "account": g.account, "account_required": g.account_mode and g.account is None,
                        "can_claim_browser": bool(g.legacy_claimable),
                        "invite_required": not g.admitted, "hosted_ready": hosted_mode(),
                        "authenticated": g.authenticated, "login_required": not g.authenticated,
                        "local_mode": not hosted_mode(),
                        "local_accounts": not hosted_mode() and g.account_mode,
                        "local_auth_configurable": not hosted_mode() and not g.auth_url,
})

    @bp.post("/api/local-auth/config")
    def local_auth_config():
        if hosted_mode():
            abort(404)
        from local_accounts import configure_auth
        configure_auth(root(), payload().get("auth_url", ""))
        return jsonify({"configured": True})

    @bp.post("/api/login")
    def login():
        if not hosted_mode():
            return jsonify({"authenticated": True})
        from hosted_auth import allow_login_attempt
        data = payload()
        limiter = current_app.config.get("HOSTED_LOGIN_LIMITER", allow_login_attempt)
        peer = request.headers.get("x-vercel-forwarded-for") or request.remote_addr or "unknown"
        if not limiter(g.hosted_config, peer):
            return jsonify({"error": "Too many admission attempts. Please wait five minutes."}), 429
        code = data.get("code", "")
        if not isinstance(code, str) or not hmac.compare_digest(code, g.hosted_config.invite_code):
            return jsonify({"error": "Invalid beta invitation code."}), 401
        g.admitted = True
        g.mentor_cookie = g.mentor_signer.dumps({"token": g.mentor_token, "admitted": True})
        return jsonify({"authenticated": not g.account_mode, "account_required": g.account_mode})

    def account_input():
        if not g.account_mode:
            abort(404)
        data = payload()
        email = data.get("email", "")
        if (not isinstance(email, str) or len(email) > 320
                or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)):
            raise ValueError("Enter a valid email address.")
        if hosted_mode():
            from hosted_auth import allow_login_attempt
            limiter = current_app.config.get("HOSTED_LOGIN_LIMITER", allow_login_attempt)
            peer = request.headers.get("x-vercel-forwarded-for") or request.remote_addr or "unknown"
            allowed = (limiter(g.hosted_config, "email-auth:" + peer)
                       and limiter(g.hosted_config, "email:" + email.casefold()))
        else:
            allowed = (local_accounts().allow_attempt("peer:" + (request.remote_addr or "local"))
                       and local_accounts().allow_attempt("email:" + email.casefold()))
        if not allowed:
            raise AccountAuthError("Too many sign-in attempts. Wait five minutes.", 429)
        return data, email

    @bp.post("/api/auth/send-code")
    def auth_send():
        _, email = account_input()
        auth().call("send", data={"email": email, "type": "sign-in"})
        return jsonify({"sent": True, "message": "Check your email for a sign-in code."})

    @bp.post("/api/auth/verify-code")
    def auth_verify():
        data, email = account_input()
        code = data.get("code", "")
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
            raise ValueError("Enter the six-digit code from your email.")
        _, updates = auth().call("verify", data={"email": email, "otp": code})
        cookies = new_session_cookies(updates)
        if not cookies:
            raise AccountAuthError("The authentication service did not establish a session.", 503)
        # Better Auth sign-in responses can be {token,user}, not {session,user}.
        # Resolve identity from the newly issued opaque cookie, never from a
        # browser assertion or an assumed SDK-normalized response shape.
        account, refreshed = auth().session(cookies)
        if account is None:
            raise AccountAuthError("The new email session could not be verified.", 401)
        claim = data.get("claim_browser") is True
        if claim and not g.legacy_claimable:
            raise WorkspaceConflict("This browser has no claimable legacy identity.")
        if hosted_mode():
            backend().accounts.resolve(g.auth_url, account, admit=g.admitted,
                                       legacy_owner=g.legacy_owner if claim else None)
            g.neon_updates.extend(updates)
            g.neon_updates.extend(refreshed)
        else:
            from local_accounts import LOCAL_SESSION_COOKIE
            local_accounts().resolve(g.auth_url, account,
                                     legacy_owner=g.legacy_owner if claim else None)
            local_accounts().forget_session(request.cookies.get(LOCAL_SESSION_COOKIE))
            g.local_session_update = local_accounts().save_session(g.auth_url, [*updates, *refreshed])
        g.mentor_cookie = g.mentor_signer.dumps(
            {"token": secrets.token_hex(32), "admitted": True, "account": True})
        return jsonify({"authenticated": True})

    @bp.post("/api/auth/logout")
    def auth_logout():
        if not g.account_mode:
            abort(404)
        failed = False
        try:
            auth().call("logout", cookies=auth_cookies(), data={})
        except AccountAuthError:
            failed = True
        if hosted_mode():
            for name in COOKIE_NAMES:
                g.neon_updates.append((name, "", 0))
        else:
            from local_accounts import LOCAL_SESSION_COOKIE
            local_accounts().forget_session(request.cookies.get(LOCAL_SESSION_COOKIE))
            g.local_session_update = ""
        g.mentor_cookie = g.mentor_signer.dumps({"token": secrets.token_hex(32), "admitted": False})
        if failed:
            return jsonify({"signed_out": True, "error": "Signed out here; remote revocation failed. "
                            "Revoke this session in your auth service if the device is shared."}), 503
        return jsonify({"signed_out": True})

    @bp.post("/api/credentials")
    def credentials():
        data = payload()
        provider = data.get("provider", "")
        validate_gateway(provider, data.get("base_url", ""))
        return jsonify(g.vault.save(g.owner, provider, data, remember=data.get("remember") is True))

    # ── OpenRouter one-click connect (OAuth PKCE) ─────────────────────────────
    # https://openrouter.ai/docs/use-cases/oauth-pkce
    OPENROUTER_AUTH = "https://openrouter.ai/auth"
    OPENROUTER_KEYS = "https://openrouter.ai/api/v1/auth/keys"

    def app_origin():
        return g.hosted_config.origin if hosted_mode() else request.host_url.rstrip("/")

    @bp.post("/api/openrouter/start")
    def openrouter_start():
        import base64
        from urllib.parse import urlencode
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        url = OPENROUTER_AUTH + "?" + urlencode({
            "callback_url": app_origin() + "/mentor/openrouter/callback",
            "code_challenge": challenge, "code_challenge_method": "S256"})
        response = jsonify({"url": url})
        response.set_cookie("openrouter_pkce", g.mentor_signer.dumps({"verifier": verifier, "owner": g.owner}),
                            max_age=600, path="/mentor", httponly=True,
                            secure=hosted_mode() or request.is_secure, samesite="Strict")
        return response

    @bp.get("/openrouter/callback")
    def openrouter_callback():
        return render_template("openrouter_callback.html")

    @bp.post("/api/openrouter/exchange")
    def openrouter_exchange():
        import httpx
        code = payload().get("code")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,512}", code):
            raise ValueError("OpenRouter returned an invalid authorization code. Try connecting again.")
        try:
            pending = g.mentor_signer.loads(request.cookies.get("openrouter_pkce", ""), max_age=600)
        except (BadSignature, SignatureExpired):
            pending = None
        if not isinstance(pending, dict) or pending.get("owner") != g.owner:
            raise ValueError("This OpenRouter connection was not started here or has expired. Try connecting again.")
        try:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=15) as http:
                response = http.post(OPENROUTER_KEYS, json={
                    "code": code, "code_verifier": pending["verifier"], "code_challenge_method": "S256"})
            key = response.json().get("key") if response.is_success else None
        except (httpx.HTTPError, ValueError):
            key = None
        if not isinstance(key, str) or not key.strip():
            raise ValueError("OpenRouter did not issue a key. Try connecting again.")
        g.vault.save(g.owner, "openrouter", {"api_key": key.strip()}, remember=True)
        result = jsonify({"ok": True, "provider": "openrouter"})
        result.delete_cookie("openrouter_pkce", path="/mentor")
        return result

    # ── institutional Canvas connection (OAuth2) ──────────────────────────────

    def canvas_institutions():
        from canvas_oauth import CanvasConfigError, institutions
        try:
            return institutions()
        except CanvasConfigError as exc:
            current_app.logger.error("Canvas institution configuration: %s", exc)
            return {}

    def canvas_session():
        """(institution, token) for this user's connected Canvas, or raise."""
        from canvas_oauth import TOKEN_COOKIE, open_token
        opened = open_token(g.mentor_key, g.owner, request.cookies.get(TOKEN_COOKIE, ""))
        configured = canvas_institutions()
        if not opened or opened[0] not in configured:
            raise ValueError("Connect to Canvas first.")
        return configured[opened[0]], opened[1]

    @bp.get("/api/canvas/institutions")
    def canvas_list():
        from canvas_oauth import TOKEN_COOKIE, open_token
        opened = open_token(g.mentor_key, g.owner, request.cookies.get(TOKEN_COOKIE, ""))
        return jsonify({"institutions": [{"id": i["id"], "name": i["name"]} for i in canvas_institutions().values()],
                        "connected": opened[0] if opened else None})

    @bp.post("/api/canvas/connect")
    def canvas_connect():
        from canvas_oauth import STATE_COOKIE, authorize_url
        institution = canvas_institutions().get(payload().get("institution"))
        if not institution:
            raise ValueError("Choose your institution.")
        state = secrets.token_urlsafe(32)
        response = jsonify({"url": authorize_url(institution, app_origin() + "/mentor/canvas/callback", state)})
        response.set_cookie(STATE_COOKIE, g.mentor_signer.dumps({"state": state, "owner": g.owner,
                                                                 "institution": institution["id"]}),
                            max_age=600, path="/mentor", httponly=True,
                            secure=hosted_mode() or request.is_secure, samesite="Strict")
        return response

    @bp.get("/canvas/callback")
    def canvas_callback():
        return render_template("canvas_callback.html")

    @bp.post("/api/canvas/exchange")
    def canvas_exchange():
        from canvas_oauth import STATE_COOKIE, TOKEN_COOKIE, TOKEN_SECONDS, exchange_code, seal_token
        data = payload()
        code, state = data.get("code"), data.get("state")
        try:
            pending = g.mentor_signer.loads(request.cookies.get(STATE_COOKIE, ""), max_age=600)
        except (BadSignature, SignatureExpired):
            pending = None
        if (not isinstance(pending, dict) or pending.get("owner") != g.owner or not isinstance(state, str)
                or not hmac.compare_digest(state, pending.get("state", ""))):
            raise ValueError("This Canvas connection was not started here or has expired. Try again.")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_.~-]{4,512}", code):
            raise ValueError("Canvas returned an invalid authorization code. Try again.")
        institution = canvas_institutions().get(pending.get("institution"))
        if not institution:
            raise ValueError("This institution is no longer configured.")
        token = exchange_code(institution, code, app_origin() + "/mentor/canvas/callback",
                              transport=current_app.config.get("CANVAS_TRANSPORT"))
        response = jsonify({"ok": True, "institution": institution["id"]})
        response.delete_cookie(STATE_COOKIE, path="/mentor")
        response.set_cookie(TOKEN_COOKIE, seal_token(g.mentor_key, g.owner, institution["id"], token),
                            max_age=TOKEN_SECONDS, path="/mentor", httponly=True,
                            secure=hosted_mode() or request.is_secure, samesite="Strict")
        return response

    @bp.get("/api/canvas/courses")
    def canvas_courses():
        from canvas_collect import CanvasClient, CollectError, list_courses
        institution, token = canvas_session()
        canvas = CanvasClient(institution["base_url"], token, transport=current_app.config.get("CANVAS_TRANSPORT"))
        try:
            return jsonify({"courses": list_courses(canvas), "institution": institution["name"]})
        except CollectError as exc:
            return jsonify({"error": str(exc) + (" Reconnect to Canvas." if exc.status == 401 else "")}), 502
        finally:
            canvas.close()

    @bp.post("/api/canvas/disconnect")
    def canvas_disconnect():
        from canvas_oauth import TOKEN_COOKIE, revoke
        try:
            institution, token = canvas_session()
            revoke(institution, token, transport=current_app.config.get("CANVAS_TRANSPORT"))
        except ValueError:
            pass
        response = jsonify({"ok": True})
        response.delete_cookie(TOKEN_COOKIE, path="/mentor")
        return response

    @bp.delete("/api/credentials/<provider>")
    def forget_credentials(provider):
        g.vault.forget(g.owner, provider)
        return jsonify({"ok": True})

    def model_preferences(provider):
        from mentor_credentials import PROVIDERS
        if provider not in PROVIDERS:
            raise ValueError("Unsupported AI provider.")
        saved = g.vault.get(g.owner, provider)
        if not saved:
            raise ValueError("Save a provider key before requesting models.")
        validate_gateway(provider, saved.get("base_url", ""))
        return saved

    def public_models(provider, saved):
        catalog = saved.get("catalog", {})
        return {**catalog, "models": catalog.get("models", []),
                "selected": saved.get("model", ""), "version": saved.get("version"),
                "selected_capabilities": capabilities_for_values(provider, saved),
                "manual_allowed": provider == "nebula",
                "expired": saved.get("catalog_expires", 0) <= time.time()}

    @bp.get("/api/providers/<provider>/models")
    def get_models(provider):
        return jsonify(redact(public_models(provider, model_preferences(provider))))

    @bp.post("/api/providers/<provider>/models/refresh")
    def refresh_models(provider):
        from provider_models import discover_models
        saved = model_preferences(provider)
        if hosted_mode():
            from hosted_auth import allow_login_attempt
            limiter = current_app.config.get("HOSTED_LOGIN_LIMITER", allow_login_attempt)
            if not limiter(g.hosted_config, "models:" + g.owner + ":" + provider):
                return jsonify({"error": "Wait five minutes before refreshing more catalogs."}), 429
        discover = current_app.config.get("MODEL_DISCOVERY", discover_models)
        catalog = redact(discover(provider, saved))
        changes = {"catalog": catalog, "catalog_expires": time.time() + 600}
        if saved.get("model") and any(row.get("id") == saved["model"] for row in catalog.get("models", [])):
            changes["capabilities"] = capabilities_for_values(
                provider, {**saved, "catalog": catalog}, prefer_catalog=True)
        updated = g.vault.update_preferences(g.owner, provider, saved.get("version"), changes)
        return jsonify(public_models(provider, updated))

    @bp.put("/api/providers/<provider>/model")
    def select_model(provider):
        from provider_models import model_id
        data, saved = payload(), model_preferences(provider)
        selected = model_id(data.get("model"))
        manual = data.get("manual") is True
        if manual and provider != "nebula":
            raise ValueError("Manual selection is only available for institutional gateways.")
        if not manual and (saved.get("catalog_expires", 0) <= time.time() or selected not in
                           {row["id"] for row in saved.get("catalog", {}).get("models", [])}):
            raise ValueError("Refresh models and choose an entry from this key's current catalog.")
        profile = capabilities_for_values(provider, saved, selected, prefer_catalog=True)
        if profile["status"] == "unsupported":
            raise ValueError("This model is not supported by the text-generation workflow.")
        g.vault.update_preferences(g.owner, provider, data.get("version"),
                                   {"model": selected, "capabilities": profile})
        return jsonify({"selected": selected, "selected_capabilities": profile})

    @bp.get("/api/workspaces")
    def workspaces():
        values = store().list_workspaces(g.owner)
        if hosted_mode():
            values = [{k: w[k] for k in ("id", "title", "revision", "updated_at")} for w in values]
        else:
            values = [public(w) for w in values]
        return jsonify(values)

    @bp.post("/api/workspaces")
    def create_workspace():
        data = payload()
        title = str(data.get("title", "")).strip()
        if not 1 <= len(title) <= 240:
            raise ValueError("Enter a course title of 1–240 characters.")
        workspace = store().create(g.owner, title, source_type="blank")
        store().add_message(g.owner, workspace["id"], "assistant",
                            "Let's design this together. Add your course/materials, then tell me the audience, "
                            "learning goals, what must stay unchanged, and what you hope to improve.")
        return jsonify(public(store().get(g.owner, workspace["id"]))), 201

    @bp.get("/api/workspaces/<workspace_id>")
    def get_workspace(workspace_id):
        return jsonify(public(store().get(g.owner, workspace_id)))

    @bp.post("/api/workspaces/<workspace_id>/intake/export")
    def intake_export(workspace_id):
        """Import a browser-slimmed Canvas export into the workspace's course model.

        The package is processed in a temporary directory and never stored:
        import, privacy strip and validation all happen before saving.
        """
        from slim_import import MAX_SLIM_BYTES, SlimImportError, import_slim
        from privacy_strip import strip_course
        workspace = store().get(g.owner, workspace_id)
        try:
            revision = int(request.form.get("revision", ""))
        except ValueError:
            raise ValueError("Reload the workspace and try again.") from None
        upload = request.files.get("package")
        upload_id = request.form.get("upload_id")
        if not upload and not upload_id:
            raise ValueError("Choose a Canvas course export to import.")
        instructor = request.form.get("instructor_name", "")
        if len(instructor) > 200:
            raise ValueError("The instructor name is too long.")
        with tempfile.TemporaryDirectory(prefix="slim-intake-") as temp:
            path = Path(temp) / "package.zip"
            if upload_id:
                # Assembled from pieces sent to /intake/chunks (hosting caps request size).
                data = store().take_intake_upload(g.owner, workspace_id, upload_id)
                if len(data) > MAX_SLIM_BYTES:
                    raise ValueError("The slimmed course package exceeds 48 MiB.")
                path.write_bytes(data)
            else:
                count = 0
                with path.open("wb") as output:
                    while chunk := upload.stream.read(1024 * 1024):
                        count += len(chunk)
                        if count > MAX_SLIM_BYTES:
                            raise ValueError("The slimmed course package exceeds 48 MiB.")
                        output.write(chunk)
            try:
                model = import_slim(path)
            except SlimImportError as exc:
                raise ValueError(str(exc)) from None
        model, privacy = strip_course(model, [instructor] if instructor.strip() else ())
        privacy = {key: privacy[key] for key in ("counts", "total", "notes")}
        workspace = store().save_course_model(g.owner, workspace_id, revision, model, privacy)
        return jsonify(public(workspace))

    @bp.post("/api/workspaces/<workspace_id>/intake/canvas")
    def intake_canvas(workspace_id):
        """Read a course through the institutional Canvas connection and import it."""
        from canvas_api_import import BundleError, import_api_bundle
        from canvas_collect import CanvasClient, CollectError, collect
        from privacy_strip import strip_course
        data = payload()
        store().get(g.owner, workspace_id)  # 404 unless this is the caller's workspace
        revision = data.get("revision")
        instructor = data.get("instructor_name", "")
        if not isinstance(instructor, str) or len(instructor) > 200:
            raise ValueError("The instructor name is too long.")
        institution, token = canvas_session()
        canvas = CanvasClient(institution["base_url"], token, transport=current_app.config.get("CANVAS_TRANSPORT"))
        try:
            bundle = collect(canvas, str(data.get("course_id", "")))
        except CollectError as exc:
            return jsonify({"error": str(exc)}), 502
        finally:
            canvas.close()
        try:
            model = import_api_bundle(bundle, source="canvas")
        except BundleError as exc:
            raise ValueError(str(exc)) from None
        model, privacy = strip_course(model, [instructor] if instructor.strip() else ())
        privacy = {key: privacy[key] for key in ("counts", "total", "notes")}
        return jsonify(public(store().save_course_model(g.owner, workspace_id, revision, model, privacy)))

    @bp.post("/api/workspaces/<workspace_id>/intake/chunks")
    def intake_chunk(workspace_id):
        from mentor_store import MAX_INTAKE_CHUNK
        try:
            index, total = int(request.form.get("index", "")), int(request.form.get("total", ""))
        except ValueError:
            raise ValueError("Invalid upload piece.") from None
        piece = request.files.get("chunk")
        if not piece:
            raise ValueError("Invalid upload piece.")
        data = piece.stream.read(MAX_INTAKE_CHUNK + 1)
        store().put_intake_chunk(g.owner, workspace_id, request.form.get("upload_id"), index, total, data)
        return jsonify({"received": index})

    @bp.post("/api/workspaces/<workspace_id>/intake/extension")
    def intake_extension(workspace_id):
        """Import the live course collected by the Course Consultant browser extension.

        The page receives the bundle from the extension and uploads it as a
        multipart file with the instructor's own session; the extension never
        holds Course Consultant credentials.
        """
        from canvas_api_import import MAX_BUNDLE_BYTES, BundleError, import_api_bundle
        from privacy_strip import strip_course
        workspace = store().get(g.owner, workspace_id)
        try:
            revision = int(request.form.get("revision", ""))
        except ValueError:
            raise ValueError("Reload the workspace and try again.") from None
        upload = request.files.get("bundle")
        upload_id = request.form.get("upload_id")
        if not upload and not upload_id:
            raise ValueError("No course was received from the extension.")
        # Large bundles arrive in pieces through /intake/chunks (hosting caps request size).
        data = (store().take_intake_upload(g.owner, workspace_id, upload_id) if upload_id
                else upload.stream.read(MAX_BUNDLE_BYTES + 1))
        if len(data) > MAX_BUNDLE_BYTES:
            raise ValueError("The course from the extension exceeds 16 MiB.")
        try:
            bundle = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ValueError("The extension sent data that is not valid JSON.") from None
        try:
            model = import_api_bundle(bundle)
        except BundleError as exc:
            raise ValueError(str(exc)) from None
        instructor = request.form.get("instructor_name", "")
        if len(instructor) > 200:
            raise ValueError("The instructor name is too long.")
        model, privacy = strip_course(model, [instructor] if instructor.strip() else ())
        privacy = {key: privacy[key] for key in ("counts", "total", "notes")}
        workspace = store().save_course_model(g.owner, workspace_id, revision, model, privacy)
        return jsonify(public(workspace))

    @bp.post("/api/workspaces/<workspace_id>/intake/documents")
    def intake_documents(workspace_id):
        """Start from documents only (e.g. a syllabus), with no Canvas course."""
        import course_model as cm
        from mentor_materials import MaterialError, extract_material
        from privacy_strip import strip_course
        workspace = store().get(g.owner, workspace_id)
        try:
            revision = int(request.form.get("revision", ""))
        except ValueError:
            raise ValueError("Reload the workspace and try again.") from None
        title = request.form.get("title", "").strip()[:cm.MAX_TITLE] or workspace["title"]
        uploads = [f for f in request.files.getlist("documents") if f and f.filename][:10]
        notes = request.form.get("notes", "").strip()
        if not uploads and not notes:
            raise ValueError("Add a syllabus or other course document, or describe the course.")
        model = cm.empty_course("documents", title)
        with tempfile.TemporaryDirectory(prefix="documents-intake-") as temp:
            for index, upload in enumerate(uploads, 1):
                path = Path(temp) / f"document-{index}{Path(secure_filename(upload.filename)).suffix.lower()}"
                count = 0
                with path.open("wb") as output:
                    while chunk := upload.stream.read(65536):
                        count += len(chunk)
                        if count > 10 * 1024 * 1024:
                            raise ValueError("Each document must be 10 MiB or smaller.")
                        output.write(chunk)
                try:
                    material = extract_material(path, upload.filename, "reference")
                except MaterialError as exc:
                    raise ValueError(f"{upload.filename[:100]}: {exc}") from None
                model["materials"].append({"id": f"material-{index}", "name": material["name"][:cm.MAX_TITLE],
                                           "role": "reference", "text": material["text"]})
        if notes:
            if len(notes) > cm.MAX_MATERIAL_CHARS:
                raise ValueError("The course description is too long.")
            model["materials"].append({"id": "material-notes", "name": "Instructor's description",
                                       "role": "reference", "text": notes})
        syllabus = next((m for m in model["materials"] if "syllabus" in m["name"].lower()), None)
        if syllabus:
            from html import escape
            model["course"]["syllabus_html"] = "<pre>" + escape(syllabus["text"][:200000]) + "</pre>"
        from datetime import datetime as _dt, timezone as _tz
        model["provenance"].update(imported_at=_dt.now(_tz.utc).isoformat(timespec="seconds"),
                                   counts={"materials": len(model["materials"])})
        instructor = request.form.get("instructor_name", "")[:200]
        model, privacy = strip_course(model, [instructor] if instructor.strip() else ())
        privacy = {key: privacy[key] for key in ("counts", "total", "notes")}
        workspace = store().save_course_model(g.owner, workspace_id, revision, model, privacy)
        return jsonify(public(workspace))

    @bp.get("/api/workspaces/<workspace_id>/course-map")
    def course_map_view(workspace_id):
        from course_map import build_course_map
        workspace = store().get(g.owner, workspace_id)
        model = store().get_course_model(g.owner, workspace_id)
        if model is None:
            return jsonify({"error": "Import a course first."}), 404
        return jsonify(build_course_map(model, (workspace.get("source") or {}).get("alignment")))

    # ── consultation ──────────────────────────────────────────────────────────

    def consult_view(workspace):
        import consultation as consult
        state = workspace.get("consultation")
        return {
            "topics": [{"id": t, "label": consult.TOPIC_LABELS[t]} for t in consult.TOPICS],
            "state": state,
            "resume": consult.resume_summary(state) if state and state.get("turns") else None,
            "messages": [m for m in workspace["messages"] if m.get("kind") == "consult"],
            "busy": bool(workspace.get("consult_busy")),
        }

    @bp.get("/api/workspaces/<workspace_id>/consult")
    def consult_get(workspace_id):
        return jsonify(consult_view(store().get(g.owner, workspace_id)))

    @bp.post("/api/workspaces/<workspace_id>/consult/settings")
    def consult_settings(workspace_id):
        import consultation as consult
        data = payload()
        workspace = store().get(g.owner, workspace_id)
        state = workspace.get("consultation") or consult.new_state()
        try:
            if "directness" in data:
                state = consult.set_directness(state, data["directness"])
            if "topic" in data:
                state = consult.choose_topic(state, data["topic"])
        except consult.ConsultationError as exc:
            raise ValueError(str(exc)) from None
        return jsonify(consult_view(store().save_consultation(g.owner, workspace_id, state)))

    @bp.post("/api/workspaces/<workspace_id>/consult")
    def consult_turn(workspace_id):
        """One consultation turn, answered within this request."""
        import consultation as consult
        from course_map import build_course_map
        data = payload()
        mode = data.get("mode", "turn")
        if mode not in ("opening", "turn", "advance", "auto"):
            raise ValueError("Unknown consultation mode.")
        if mode == "advance":
            current = store().get(g.owner, workspace_id).get("consultation")
            if not current or not current.get("turns"):
                raise ValueError("Begin the consultation first.")
            try:
                consult.advance(current)  # check there is a next topic before any AI call
            except consult.ConsultationError as exc:
                raise ValueError(str(exc)) from None
        message = None
        if mode == "turn":
            message = str(data.get("message", "")).strip()
            if not 1 <= len(message) <= 6000:
                raise ValueError("Enter a message of 1–6000 characters.")
            message = redact(message)
        resolved = provider_values(data)
        if resolved[1] is None:
            raise ValueError("Choose an AI provider and model for the consultation.")
        workspace = store().begin_consult(g.owner, workspace_id)
        ai = None
        try:
            model = store().get_course_model(g.owner, workspace_id)
            state = workspace.get("consultation") or consult.new_state()
            if mode == "opening" and state.get("turns"):
                raise ValueError("The consultation has already started.")
            if mode == "advance":
                previous = consult.TOPIC_LABELS[state["current_topic"]]
                state = consult.advance(state)
                message = f"I'm done with {previous}. Let's move on to {consult.TOPIC_LABELS[state['current_topic']]}."
            alignment = (workspace.get("source") or {}).get("alignment")
            course_map = build_course_map(model, alignment)
            history = [m for m in workspace["messages"] if m.get("kind") == "consult"]
            ai = client(data, resolved)
            topic = state["current_topic"]
            reply, state = consult.respond(state, history, model, course_map, ai, mode=mode,
                                           message=None if mode in ("advance", "auto") else message)
            if mode == "auto":
                # The advisor finished this topic; move on so the next call covers the next one.
                reply = f"**Quick AI version: {consult.TOPIC_LABELS[topic]}.** " + reply
                if state["topics"][topic]["status"] in ("not_started", "in_progress"):
                    state["topics"][topic]["status"] = "covered"
                try:
                    state = consult.advance({**state, "current_topic": topic})
                except consult.ConsultationError:
                    pass  # every topic is done
        except consult.ConsultationError as exc:
            store().release_consult(g.owner, workspace_id)
            return jsonify({"error": redact(str(exc))[:500]}), 502
        except Exception:
            store().release_consult(g.owner, workspace_id)
            raise
        finally:
            if ai is not None:
                ai.close()
        turn = ([("user", message)] if message else []) + [("assistant", redact(reply))]
        workspace = store().finish_consult(g.owner, workspace_id, state, turn)
        return jsonify(consult_view(workspace))

    # ── improvement plan and revised documents (Phases 2-3) ───────────────────

    def plan_view(workspace):
        import improvement_plan as ip
        consult_state = workspace.get("consultation") or {}
        return {"plan": workspace.get("plan") or ip.new_plan(),
                "proposals": consult_state.get("proposals", []),
                "consulted": bool(consult_state.get("turns")),
                "busy": bool(workspace.get("consult_busy"))}

    def documents_view(workspace):
        import revised_documents as rd
        model = store().get_course_model(g.owner, workspace["id"])
        documents = workspace.get("documents") or []
        return {"documents": documents,
                "targets": rd.targets(model, workspace.get("consultation")) if model else [],
                "items": [{"id": i["id"], "title": i["title"], "kind": i["kind"]} for i in (model or {}).get("items", [])
                          if i["kind"] in rd.REVISABLE],
                "busy": bool(workspace.get("consult_busy"))}

    def ai_step(workspace_id, data, work):
        """Run one AI step under the consultation lease; return the saved workspace."""
        resolved = provider_values(data)
        if resolved[1] is None:
            raise ValueError("Choose an AI provider and model first.")
        workspace = store().begin_consult(g.owner, workspace_id)
        ai = None
        try:
            ai = client(data, resolved)
            fields = work(workspace, ai)
        except ValueError as exc:
            store().release_consult(g.owner, workspace_id)
            return None, (jsonify({"error": redact(str(exc))[:500]}), 502)
        except Exception:
            store().release_consult(g.owner, workspace_id)
            raise
        finally:
            if ai is not None:
                ai.close()
        return store().save_consultant_fields(g.owner, workspace_id, release=True, **fields), None

    def course_inputs(workspace):
        from course_map import build_course_map
        model = store().get_course_model(g.owner, workspace["id"])
        if model is None:
            raise WorkspaceConflict("Import a course first.")
        return model, build_course_map(model, (workspace.get("source") or {}).get("alignment"))

    @bp.post("/api/workspaces/<workspace_id>/consult/proposals/<proposal_id>")
    def decide_proposal(workspace_id, proposal_id):
        import improvement_plan as ip
        data = payload()
        workspace = store().get(g.owner, workspace_id)
        try:
            state = ip.decide(workspace.get("consultation"), proposal_id, data.get("status"),
                              {k: data[k] for k in ("title", "change", "rationale") if k in data})
        except ip.PlanError as exc:
            raise ValueError(str(exc)) from None
        return jsonify(plan_view(store().save_consultant_fields(g.owner, workspace_id, consultation=state)))

    @bp.post("/api/workspaces/<workspace_id>/consult/proposals/accept-all")
    def accept_all_proposals(workspace_id):
        import improvement_plan as ip
        workspace = store().get(g.owner, workspace_id)
        state = ip.accept_all(workspace.get("consultation"))
        return jsonify(plan_view(store().save_consultant_fields(g.owner, workspace_id, consultation=state)))

    @bp.get("/api/workspaces/<workspace_id>/plan")
    def plan_get(workspace_id):
        return jsonify(plan_view(store().get(g.owner, workspace_id)))

    @bp.put("/api/workspaces/<workspace_id>/plan")
    def plan_put(workspace_id):
        import improvement_plan as ip
        workspace = store().get(g.owner, workspace_id)
        try:
            plan = ip.edit_plan(workspace.get("plan"), payload())
        except ip.PlanError as exc:
            raise ValueError(str(exc)) from None
        return jsonify(plan_view(store().save_consultant_fields(g.owner, workspace_id, plan=plan)))

    @bp.post("/api/workspaces/<workspace_id>/plan/restore")
    def plan_restore(workspace_id):
        import improvement_plan as ip
        workspace = store().get(g.owner, workspace_id)
        try:
            plan = ip.restore_plan(workspace.get("plan"), payload().get("version"))
        except ip.PlanError as exc:
            raise ValueError(str(exc)) from None
        return jsonify(plan_view(store().save_consultant_fields(g.owner, workspace_id, plan=plan)))

    @bp.post("/api/workspaces/<workspace_id>/plan/draft")
    def plan_draft(workspace_id):
        import improvement_plan as ip

        def work(workspace, ai):
            model, course_map = course_inputs(workspace)
            return {"plan": ip.draft_plan(workspace.get("plan") or ip.new_plan(), model, course_map,
                                          workspace.get("consultation"), ai)}
        workspace, error = ai_step(workspace_id, payload(), work)
        return error or jsonify(plan_view(workspace))

    @bp.get("/api/workspaces/<workspace_id>/plan/export")
    def plan_export(workspace_id):
        import improvement_plan as ip
        workspace = store().get(g.owner, workspace_id)
        plan = workspace.get("plan") or ip.new_plan()
        title = ((workspace.get("source") or {}).get("course_model") or {}).get("title") or workspace["title"]
        if request.args.get("format") == "docx":
            body = ip.render_docx(title, plan, workspace.get("consultation"))
            return file_response(body, ip.safe_filename("Improvement plan " + title, ".docx"),
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        return file_response(ip.render_html(title, plan, workspace.get("consultation")).encode(),
                        ip.safe_filename("Improvement plan " + title, ".html"), "text/html; charset=utf-8")

    def file_response(body, filename, mimetype):
        from flask import Response
        response = Response(body, mimetype=mimetype)
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
        return response

    @bp.get("/api/workspaces/<workspace_id>/site")
    def site_root(workspace_id):
        from flask import redirect
        return redirect(f"/mentor/api/workspaces/{workspace_id}/site/index.html", code=302)

    @bp.get("/api/workspaces/<workspace_id>/site/<path:path>")
    def site_page(workspace_id, path):
        """Private preview of the whole course site, rendered from current approved documents."""
        import course_site
        from flask import Response
        from mentor_preview import PREVIEW_STYLE_HASH, response_policy
        workspace = store().get(g.owner, workspace_id)
        model = store().get_course_model(g.owner, workspace_id)
        if model is None:
            return jsonify({"error": "Import a course first."}), 404
        title = ((workspace.get("source") or {}).get("course_model") or {}).get("title") or workspace["title"]
        html = course_site.render(title, course_site.build_items(model, workspace.get("documents") or []), path)
        if html is None:
            return jsonify({"error": "That page is not part of the course site."}), 404
        response = Response(html, mimetype="text/html")
        response.headers["Content-Security-Policy"] = response_policy(PREVIEW_STYLE_HASH)
        return response

    # ── HAX-theme preview (signed link, sandboxed third-party scripts) ────────

    def preview_signer():
        from mentor_credentials import private_key
        key = settings().session_secret.encode() if hosted_mode() else private_key(root() / ".browser-key")
        return URLSafeTimedSerializer(key.decode(), salt="hax-preview")

    def preview_origin():
        return settings().origin if hosted_mode() else request.host_url.rstrip("/")

    @bp.post("/api/workspaces/<workspace_id>/hax-preview")
    def hax_preview_link(workspace_id):
        if store().get_course_model(g.owner, workspace_id) is None:
            return jsonify({"error": "Import a course first."}), 404
        token = preview_signer().dumps({"owner": g.owner, "workspace": workspace_id})
        return jsonify({"url": f"{preview_origin()}/mentor/hax/{token}/"})

    @bp.get("/hax/<token>/", defaults={"path": ""})
    @bp.get("/hax/<token>/<path:path>")
    def hax_preview_file(token, path):
        import course_site
        import hax_preview
        from flask import Response
        def respond(body, mimetype, status=200):
            response = Response(body, status=status, mimetype=mimetype)
            response.headers["Content-Security-Policy"] = hax_preview.policy(preview_origin())
            # The sandboxed page has an isolated origin; the signed link is the credential.
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["X-Robots-Tag"] = "noindex"
            return response
        try:
            claim = preview_signer().loads(token, max_age=hax_preview.TOKEN_SECONDS)
        except (BadSignature, SignatureExpired):
            return respond("This preview link has expired. Open the preview again from Course Consultant.",
                           "text/plain", 403)
        workspace = store().get(claim["owner"], claim["workspace"])
        model = store().get_course_model(claim["owner"], claim["workspace"])
        if model is None:
            return respond("Not found.", "text/plain", 404)
        title = ((workspace.get("source") or {}).get("course_model") or {}).get("title") or workspace["title"]
        items = course_site.build_items(model, workspace.get("documents") or [])
        places = course_site.locations(items)
        if path in ("", "index.html") or ("." not in path.rsplit("/", 1)[-1] and not path.startswith("pages/")):
            # The site shell; HAX's router reads page addresses like ".../week-1" itself.
            body, mimetype = hax_preview.index_html(title, f"/mentor/hax/{token}/"), "text/html"
        elif path == "config.js":
            body, mimetype = hax_preview.config_js(), "text/javascript"
        elif path == "site.json":
            body = json.dumps(hax_preview.site_json(claim["workspace"], title, items, places))
            mimetype = "application/json"
        else:
            slug = path.split("/")[1] if path.startswith("pages/") and path.endswith("/index.html") else None
            item = next((i for i in items if slug and places[i["id"]].split("/")[1] == slug), None)
            if item is None:
                return respond("Not found.", "text/plain", 404)
            body, mimetype = hax_preview.page_html(item), "text/html"
        return respond(body, mimetype)

    # ── Read-only share link (signed, revocable) ─────────────────────────────

    SHARE_SECONDS = 90 * 24 * 3600

    def share_signer():
        from mentor_credentials import private_key
        key = settings().session_secret.encode() if hosted_mode() else private_key(root() / ".browser-key")
        return URLSafeTimedSerializer(key.decode(), salt="read-only-share")

    def share_view(workspace):
        share = workspace.get("share") or {}
        if not share.get("enabled"):
            return {"enabled": False}
        token = share_signer().dumps({"owner": g.owner, "workspace": workspace["id"], "v": share["version"]})
        return {"enabled": True, "url": f"{preview_origin()}/mentor/shared/{token}/",
                "days": SHARE_SECONDS // 86400}

    @bp.get("/api/workspaces/<workspace_id>/share")
    def share_status(workspace_id):
        return jsonify(share_view(store().get(g.owner, workspace_id)))

    @bp.post("/api/workspaces/<workspace_id>/share")
    def share_start(workspace_id):
        if store().get_course_model(g.owner, workspace_id) is None:
            return jsonify({"error": "Import a course first."}), 404
        return jsonify(share_view(store().set_share(g.owner, workspace_id, True)))

    @bp.delete("/api/workspaces/<workspace_id>/share")
    def share_stop(workspace_id):
        return jsonify(share_view(store().set_share(g.owner, workspace_id, False)))

    @bp.get("/shared/<token>/", defaults={"path": ""})
    @bp.get("/shared/<token>/<path:path>")
    def shared_view(token, path):
        """What a colleague sees: the plan and the course site, read-only."""
        import course_site
        import improvement_plan as ip
        from flask import Response
        from html import escape
        from mentor_preview import PREVIEW_STYLE_HASH, response_policy

        def respond(body, status=200, policy="default-src 'none'; style-src 'unsafe-inline'"):
            response = Response(body, status=status, mimetype="text/html")
            response.headers["Content-Security-Policy"] = policy
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Cache-Control"] = "no-store"
            return response

        unavailable = "<!doctype html><title>Link not available</title><p>This shared link has been turned off or has expired.</p>"
        try:
            claim = share_signer().loads(token, max_age=SHARE_SECONDS)
            workspace = store().get(claim["owner"], claim["workspace"])
        except (BadSignature, SignatureExpired, KeyError, TypeError, WorkspaceNotFound):
            return respond(unavailable, 404)
        share = workspace.get("share") or {}
        if not share.get("enabled") or share.get("version") != claim.get("v"):
            return respond(unavailable, 404)
        model = store().get_course_model(claim["owner"], claim["workspace"])
        if model is None:
            return respond(unavailable, 404)
        title = ((workspace.get("source") or {}).get("course_model") or {}).get("title") or workspace["title"]
        if path == "plan":
            return respond(ip.render_html(title, workspace.get("plan") or ip.new_plan(), workspace.get("consultation")))
        if path.startswith("site/"):
            html = course_site.render(title, course_site.build_items(model, workspace.get("documents") or []),
                                      path[len("site/"):] or "index.html")
            if html is None:
                return respond("<!doctype html><title>Not found</title><p>That page is not part of the course site.</p>", 404)
            return respond(html, policy=response_policy(PREVIEW_STYLE_HASH))
        if path:
            return respond("<!doctype html><title>Not found</title><p>Not found.</p>", 404)
        approved = [d for d in workspace.get("documents") or [] if d.get("status") == "approved"]
        rows = "".join(f"<li>{escape(d.get('title') or d['kind'])}</li>" for d in approved[:100])
        body = (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
                f"<title>{escape(title)} – shared view</title><style>"
                "body{font:16px/1.5 system-ui,sans-serif;max-width:720px;margin:32px auto;padding:0 16px;color:#1d2330;background:#fff}"
                "a{color:#2459c9}.note{color:#5b6474}"
                "@media (prefers-color-scheme:dark){body{background:#14171c;color:#e7eaf0}a{color:#7ea6ff}.note{color:#a3abb9}}"
                f"</style></head><body><h1>{escape(title)}</h1>"
                "<p class=\"note\">A read-only view shared by the course's instructor from Course Consultant. "
                "It shows the current plan and course site, and updates as the instructor works.</p>"
                "<ul><li><a href=\"plan\">Improvement plan</a></li>"
                "<li><a href=\"site/index.html\">Course site preview</a></li></ul>"
                + (f"<h2>Approved revised documents</h2><ul>{rows}</ul>" if rows else "")
                + "</body></html>")
        return respond(body)

    @bp.get("/api/workspaces/<workspace_id>/documents")
    def documents_get(workspace_id):
        return jsonify(documents_view(store().get(g.owner, workspace_id)))

    @bp.post("/api/workspaces/<workspace_id>/documents/generate")
    def documents_generate(workspace_id):
        import revised_documents as rd
        data = payload()

        def work(workspace, ai):
            model, course_map = course_inputs(workspace)
            document = rd.generate(data.get("kind"), model, course_map, workspace.get("consultation"),
                                   workspace.get("plan"), ai, item_id=data.get("item_id"),
                                   instruction=redact(data["instruction"]) if isinstance(data.get("instruction"), str) else None)
            if data.get("approve") is True:  # Quick AI version: approved on the instructor's behalf
                document.update(status="approved", auto=True)
            if data.get("replace_id"):
                return {"documents": rd.replace(workspace.get("documents"), data["replace_id"], document)}
            return {"documents": rd.add(workspace.get("documents"), document)}
        workspace, error = ai_step(workspace_id, data, work)
        return error or jsonify(documents_view(workspace))

    @bp.get("/api/workspaces/<workspace_id>/estimate")
    def cost_estimate(workspace_id):
        import cost_estimate as ce
        from course_map import build_course_map
        workspace = store().get(g.owner, workspace_id)
        model = store().get_course_model(g.owner, workspace_id)
        if model is None:
            return jsonify({"error": "Import a course first."}), 404
        provider = request.args.get("provider", "")
        saved = g.vault.get(g.owner, provider) if provider else None
        pricing = capabilities = None
        if saved and saved.get("model"):
            row = next((m for m in (saved.get("catalog") or {}).get("models", [])
                        if m.get("id") == saved["model"]), {})
            pricing, capabilities = row.get("pricing"), saved.get("capabilities") or row.get("capabilities")
        try:
            count = int(request.args.get("count", "1"))
        except ValueError:
            count = 1
        course_map = build_course_map(model, (workspace.get("source") or {}).get("alignment"))
        history = [m for m in workspace.get("messages", []) if m.get("kind") == "consult"]
        return jsonify(ce.estimate(request.args.get("action", ""), model=model, course_map=course_map,
                                   consult_state=workspace.get("consultation"), history=history,
                                   plan=workspace.get("plan"), count=count, capabilities=capabilities,
                                   pricing=pricing))

    @bp.get("/api/workspaces/<workspace_id>/accessibility")
    def accessibility_report(workspace_id):
        import accessibility
        workspace = store().get(g.owner, workspace_id)
        model = store().get_course_model(g.owner, workspace_id)
        if model is None:
            return jsonify({"error": "Import a course first."}), 404
        report = accessibility.course_report(model, workspace.get("documents") or [])
        for entry in report["items"]:
            entry["fix_instruction"] = accessibility.fix_instruction(entry)
        return jsonify(report)

    @bp.get("/api/workspaces/<workspace_id>/documents/<document_id>/compare")
    def documents_compare(workspace_id, document_id):
        import revised_documents as rd
        workspace = store().get(g.owner, workspace_id)
        document = next((d for d in workspace.get("documents") or [] if d["id"] == document_id), None)
        model = store().get_course_model(g.owner, workspace_id)
        if document is None or model is None:
            return jsonify({"error": "That document no longer exists."}), 404
        return jsonify(rd.compare(document, model))

    @bp.post("/api/workspaces/<workspace_id>/documents/<document_id>/restore")
    def documents_restore(workspace_id, document_id):
        import revised_documents as rd
        workspace = store().get(g.owner, workspace_id)
        try:
            documents = rd.restore(workspace.get("documents"), document_id, payload().get("version"))
        except rd.DocumentError as exc:
            raise ValueError(str(exc)) from None
        return jsonify(documents_view(store().save_consultant_fields(g.owner, workspace_id, documents=documents)))

    @bp.post("/api/workspaces/<workspace_id>/documents/<document_id>/revise")
    def documents_revise(workspace_id, document_id):
        import revised_documents as rd
        data = payload()

        def work(workspace, ai):
            documents = workspace.get("documents") or []
            document = next((d for d in documents if d["id"] == document_id), None)
            if document is None:
                raise rd.DocumentError("That document no longer exists.")
            model, course_map = course_inputs(workspace)
            revised = rd.revise(document, redact(str(data.get("instruction", ""))), model, course_map,
                                workspace.get("consultation"), workspace.get("plan"), ai)
            return {"documents": rd.replace(documents, document_id, revised)}
        workspace, error = ai_step(workspace_id, data, work)
        return error or jsonify(documents_view(workspace))

    @bp.route("/api/workspaces/<workspace_id>/documents/<document_id>", methods=["PUT", "DELETE"])
    def documents_change(workspace_id, document_id):
        import revised_documents as rd
        workspace = store().get(g.owner, workspace_id)
        try:
            documents = (rd.remove(workspace.get("documents"), document_id) if request.method == "DELETE"
                         else rd.edit(workspace.get("documents"), document_id, payload()))
        except rd.DocumentError as exc:
            raise ValueError(str(exc)) from None
        return jsonify(documents_view(store().save_consultant_fields(g.owner, workspace_id, documents=documents)))

    @bp.get("/api/workspaces/<workspace_id>/documents/<document_id>/export")
    def document_export(workspace_id, document_id):
        import revised_documents as rd
        from improvement_plan import safe_filename
        workspace = store().get(g.owner, workspace_id)
        document = next((d for d in workspace.get("documents") or [] if d["id"] == document_id), None)
        if document is None:
            return jsonify({"error": "That document no longer exists."}), 404
        if request.args.get("format") == "docx":
            return file_response(rd.to_docx(document), safe_filename(document["title"], ".docx"),
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        return file_response(rd.standalone_html(document).encode(), safe_filename(document["title"], ".html"),
                        "text/html; charset=utf-8")

    @bp.get("/api/workspaces/<workspace_id>/documents/export")
    def documents_package(workspace_id):
        import improvement_plan as ip
        import revised_documents as rd
        workspace = store().get(g.owner, workspace_id)
        title = ((workspace.get("source") or {}).get("course_model") or {}).get("title") or workspace["title"]
        documents = workspace.get("documents") or []
        try:
            if request.args.get("format") == "imscc":
                import canvas_package
                model = store().get_course_model(g.owner, workspace_id)
                if model is None:
                    raise ValueError("Import a course first.")
                body, _ = canvas_package.build(title, model, documents)
                return file_response(body, ip.safe_filename(title + " revised for Canvas", ".imscc"),
                                     "application/zip")
            if request.args.get("format") == "hax":
                import course_site
                model = store().get_course_model(g.owner, workspace_id)
                if model is None:
                    raise ValueError("Import a course first.")
                with tempfile.TemporaryDirectory(prefix="hax-package-") as temp:
                    result = course_site.package(workspace_id, title, course_site.build_items(model, documents),
                                                 Path(temp) / "site")
                    body = Path(result["path"]).read_bytes()
                return file_response(body, ip.safe_filename(title + " course site", ".zip"), "application/zip")
            plan = workspace.get("plan") or ip.new_plan()
            plan_files = [("Improvement plan.html", ip.render_html(title, plan, workspace.get("consultation"))),
                          ("Improvement plan.docx", ip.render_docx(title, plan, workspace.get("consultation")))]
            return file_response(rd.zip_package(title, documents, plan_files),
                            ip.safe_filename(title + " revised documents", ".zip"), "application/zip")
        except rd.DocumentError as exc:
            raise ValueError(str(exc)) from None

    @bp.post("/api/workspaces/<workspace_id>/course-map/align")
    def course_map_align(workspace_id):
        from course_map import CourseMapError, build_course_map, propose_alignment
        data = payload()
        resolved = provider_values(data)
        if resolved[1] is None:
            raise ValueError("Choose an AI provider and model to map outcomes.")
        workspace = store().get(g.owner, workspace_id)
        summary = (workspace.get("source") or {}).get("course_model")
        model = store().get_course_model(g.owner, workspace_id)
        if model is None or not summary:
            return jsonify({"error": "Import a course first."}), 404
        ai = client(data, resolved)
        try:
            alignment = propose_alignment(model, ai)
        except CourseMapError as exc:
            return jsonify({"error": str(exc)}), 502
        finally:
            ai.close()
        store().save_course_alignment(g.owner, workspace_id, summary["imported_at"], alignment)
        return jsonify(build_course_map(model, alignment))

    return bp
