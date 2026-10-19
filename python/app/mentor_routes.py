"""Local mentoring API. Hosted deployment is explicitly gated until provisioned."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import threading
import time
from urllib.parse import urlsplit
import uuid
from contextlib import nullcontext

from flask import (
    Blueprint, abort, current_app, g, jsonify, render_template, request,
    send_file, send_from_directory, Response, jsonify as flask_jsonify,
)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.utils import secure_filename
from mentor_store import WorkspaceConflict, WorkspaceNotFound
from hosted_config import hosted_mode, settings, HostedSetupError, sign_upload, verify_upload
from hosted_accounts import AccountAuthError, NeonAuth, session_cookies, COOKIE_NAMES


def acquire_canvas_course(course_id: str, destination: Path) -> Path:
    """Local-only acquisition; no evaluation, enhancement or build is started."""
    from setup_wizard import load_env_values
    values = {**os.environ, **load_env_values()}
    url, token = values.get("CANVAS_URL", ""), values.get("CANVAS_TOKEN", "")
    parsed = urlsplit(url)
    allowed = {h.strip() for h in os.environ.get("HAX_ALLOWED_CANVAS_HOSTS", "").split(",") if h.strip()}
    host = parsed.hostname or ""
    if not token or not url:
        raise ValueError("Configure this local app's Canvas URL/token on the Configuration page, or import an IMSCC file.")
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.port not in (None, 443)
            or not (host.endswith(".instructure.com") or host in allowed)):
        raise ValueError("Use an HTTPS Canvas institution URL or an administrator-allowlisted Canvas host.")
    from canvasapi import Canvas
    from export_course import export_course
    try:
        ok = export_course(Canvas(url, token), int(course_id), str(destination), token)
        matches = list(destination.glob(f"course_{course_id}_*"))
        if not ok or len(matches) != 1:
            raise ValueError("Canvas export did not produce a complete local source folder.")
        return matches[0]
    except Exception:
        raise ValueError("Canvas acquisition failed. Check the local configuration and course access; no analysis was started.") from None


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
            factory = current_app.config.get("HOSTED_AUTH_FACTORY", NeonAuth)
            g.neon_auth = factory(g.hosted_config.auth_url, g.hosted_config.origin)
        return g.neon_auth

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
        result = {k: v for k, v in workspace.items() if k not in ("owner", "export_dir", "source")}
        if result.get("artifact"):
            result["artifact"] = {k: v for k, v in result["artifact"].items()
                                  if k not in ("path", "preview_dir", "blob_path")}
        if (result.get("draft") is not None and
                request.args.get("draft_revision") == str(result["draft_revision"])):
            result.pop("draft")
            result["keep_draft"] = True
        if hosted_mode():
            latest = next((j for j in reversed(workspace["jobs"]) if j["kind"] == "prepare"), None)
            if latest:
                upload = backend().uploads.get(g.owner, latest["id"])
                result["upload"] = {k: upload.get(k) for k in
                                    ("id", "filename", "size", "status", "cleanup_status", "attempts")}
                result["upload"]["phase"] = (upload.get("sandbox") or {}).get("phase")
        return result

    def with_source(workspace):
        return backend().materialize(workspace) if hosted_mode() else nullcontext(workspace)

    def provider_values(data):
        provider = data.get("provider", "guided")
        if provider == "guided":
            return provider, None
        if provider not in ("nebula", "anthropic", "openai", "gemini"):
            raise ValueError("Unsupported AI provider.")
        values = g.vault.get(g.owner, provider)
        if values is None:
            raise ValueError("Save credentials for the selected provider, or choose guided mode.")
        validate_gateway(provider, values.get("base_url", ""))
        if not values.get("model"):
            raise ValueError("Fetch the provider's models and save an explicit model selection first.")
        return provider, values

    def client(data):
        from ai_client import AIClient
        provider, values = provider_values(data)
        if values is None:
            return None
        return AIClient(provider, model=values.get("model") or None,
                        api_key=values["api_key"], base_url=values.get("base_url") or None,
                        isolated_credentials=True)

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
        if hosted_mode():
            try:
                config = settings()
            except HostedSetupError as exc:
                return jsonify({"error": str(exc), "code": "HOSTED_SETUP_REQUIRED"}), 503
            g.hosted_config = config
            if request.endpoint in ("mentor.upload_callback", "mentor.cleanup_cron"):
                expected = config.internal_secret if request.endpoint == "mentor.upload_callback" else config.cron_secret
                if not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + expected):
                    return jsonify({"error": "Unauthorized."}), 401
                return
            key = config.session_secret.encode()
        else:
            key = private_key(root() / ".browser-key")
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
                                        "account": bool(isinstance(identity, dict) and identity.get("account"))}) if hosted_mode() else signer.dumps(token)
        g.owner = hashlib.sha256(token.encode()).hexdigest()
        g.legacy_owner = g.owner
        g.legacy_claimable = g.admitted and isinstance(identity, dict) and not identity.get("account")
        g.account = None
        g.neon_updates = []
        g.account_mode = hosted_mode() and g.hosted_config.auth_mode == "neon"
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
            account, updates = auth().session(session_cookies(request.cookies))
            g.neon_updates.extend(updates)
            if account:
                g.owner = backend().accounts.resolve(g.hosted_config.auth_url, account, admit=g.admitted)
                g.admitted = True
                g.account = account
        g.authenticated = g.admitted and (not g.account_mode or g.account is not None)
        if hosted_mode() and not g.authenticated:
            if request.endpoint not in ("mentor.page", "mentor.session_info", "mentor.login", *auth_endpoints):
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
        return render_template("mentor.html")

    @bp.get("/api/session")
    def session_info():
        return jsonify({"csrf": g.csrf, "credentials": g.vault.status(g.owner) if g.authenticated else [],
                        "identity": "account" if g.account_mode else "this-browser",
                        "account": g.account, "account_required": g.account_mode and g.account is None,
                        "can_claim_browser": bool(g.legacy_claimable),
                        "invite_required": not g.admitted, "hosted_ready": hosted_mode(),
                        "authenticated": g.authenticated, "login_required": not g.authenticated,
                        "maximum_upload_bytes": 8 * 1024**3 if hosted_mode() else 256 * 1024**2})

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
        if not hosted_mode() or not g.account_mode:
            abort(404)
        data = payload()
        email = data.get("email", "")
        if (not isinstance(email, str) or len(email) > 320
                or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)):
            raise ValueError("Enter a valid email address.")
        from hosted_auth import allow_login_attempt
        limiter = current_app.config.get("HOSTED_LOGIN_LIMITER", allow_login_attempt)
        peer = request.headers.get("x-vercel-forwarded-for") or request.remote_addr or "unknown"
        if not limiter(g.hosted_config, "email-auth:" + peer) or not limiter(
                g.hosted_config, "email:" + email.casefold()):
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
        if not any(name in COOKIE_NAMES and value for name, value, _ in updates):
            raise AccountAuthError("The authentication service did not establish a session.", 503)
        # Better Auth sign-in responses can be {token,user}, not {session,user}.
        # Resolve identity from the newly issued opaque cookie, never from a
        # browser assertion or an assumed SDK-normalized response shape.
        account, refreshed = auth().session(session_cookies(
            {name: value for name, value, age in updates if age != 0}))
        if account is None:
            raise AccountAuthError("The new email session could not be verified.", 401)
        claim = data.get("claim_browser") is True
        if claim and not g.legacy_claimable:
            raise WorkspaceConflict("This browser has no claimable legacy identity.")
        backend().accounts.resolve(g.hosted_config.auth_url, account, admit=g.admitted,
                                   legacy_owner=g.legacy_owner if claim else None)
        g.neon_updates.extend(updates)
        g.neon_updates.extend(refreshed)
        g.mentor_cookie = g.mentor_signer.dumps(
            {"token": secrets.token_hex(32), "admitted": True, "account": True})
        return jsonify({"authenticated": True})

    @bp.post("/api/auth/logout")
    def auth_logout():
        if not hosted_mode() or not g.account_mode:
            abort(404)
        failed = False
        try:
            auth().call("logout", cookies=session_cookies(request.cookies), data={})
        except AccountAuthError:
            failed = True
        for name in COOKIE_NAMES:
            g.neon_updates.append((name, "", 0))
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
        updated = g.vault.update_preferences(g.owner, provider, saved.get("version"),
                    {"catalog": catalog, "catalog_expires": time.time() + 600})
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
        g.vault.update_preferences(g.owner, provider, data.get("version"), {"model": selected})
        return jsonify({"selected": selected})

    @bp.get("/api/workspaces")
    def workspaces():
        values = store().list_workspaces(g.owner)
        if hosted_mode():
            values = [{k: w[k] for k in ("id", "title", "stage", "revision", "draft_revision", "updated_at")} for w in values]
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

    @bp.get("/api/workspaces/<workspace_id>/inventory")
    def get_inventory(workspace_id):
        from mentor_service import inventory
        workspace = store().get(g.owner, workspace_id)
        if hosted_mode() and workspace.get("source", {}).get("inventory"):
            info = dict(workspace["source"]["inventory"])
            info["materials"] = [{k: m.get(k) for k in ("id", "name", "role", "kind", "warnings")}
                                 for m in workspace["materials"]]
            return jsonify(info)
        with with_source(workspace) as hydrated:
            return jsonify(inventory(hydrated))

    @bp.put("/api/workspaces/<workspace_id>/brief")
    def save_brief(workspace_id):
        data = payload()
        if not isinstance(data.get("brief"), str) or len(data["brief"]) > 30000:
            raise ValueError("The design brief must be 30,000 characters or smaller.")
        return jsonify(public(store().save_brief(g.owner, workspace_id, redact(data.get("brief", "")),
                                                 data.get("revision"))))

    @bp.post("/api/workspaces/<workspace_id>/source")
    def source_file(workspace_id):
        if hosted_mode():
            return jsonify({"error": "Use the private multipart upload flow for hosted course files."}), 400
        workspace = store().get(g.owner, workspace_id)
        revision = int(request.form.get("revision", 0))
        if revision != workspace["revision"] or workspace.get("active_job"):
            return jsonify({"error": "Workspace changed or is processing. Refresh before importing."}), 409
        upload = request.files.get("file")
        if not upload or not upload.filename:
            raise ValueError("Choose an IMSCC or prepared .haxcourse file.")
        suffix = Path(upload.filename).suffix.lower()
        if suffix not in (".imscc", ".haxcourse"):
            raise ValueError("Choose an IMSCC or prepared .haxcourse file.")
        from imscc_import import import_imscc
        from imscc_content import prepare_import_content
        from course_package import import_course_package
        base = root() / "courses" / workspace_id / uuid.uuid4().hex
        result = None
        try:
            with tempfile.TemporaryDirectory(prefix="mentor-source-") as temp:
                path = Path(temp) / (secure_filename(upload.filename) or "course" + suffix)
                count = 0
                with path.open("wb") as output:
                    while chunk := upload.stream.read(1024 * 1024):
                        count += len(chunk)
                        if count > (64 if suffix == ".haxcourse" else 256) * 1024 * 1024:
                            raise ValueError("File too large for browser import. Use the local preparation tool.")
                        output.write(chunk)
                loader = import_course_package if suffix == ".haxcourse" else import_imscc
                result = loader(path, base)
                prepare_import_content(Path(result["export_dir"]))
                for folder in ("pages", "assignments", "quizzes", "discussions", "syllabus"):
                    for record in (Path(result["export_dir"]) / folder).glob("*.json"):
                        if record.stat().st_size > 2 * 1024 * 1024:
                            raise ValueError("An instructional item exceeds the 2 MiB mentoring review limit.")
                        clean = redact(json.loads(record.read_text(encoding="utf-8")))
                        record.write_text(json.dumps(clean, ensure_ascii=False), encoding="utf-8")
            from mentor_service import inventory
            candidate = {**workspace, "export_dir": result["export_dir"], "source_type": "imscc"}
            inventory(candidate)  # validate review bounds before attaching
            workspace = store().set_source(g.owner, workspace_id, {
                "source_type": "prepared" if suffix == ".haxcourse" else "imscc",
                "course_id": result["course_id"], "export_dir": result["export_dir"],
                "warnings": result.get("warnings", []),
            }, revision)
            return jsonify(public(workspace))
        except Exception:
            shutil.rmtree(base, ignore_errors=True)
            raise

    @bp.post("/api/workspaces/<workspace_id>/uploads")
    def create_upload(workspace_id):
        if not hosted_mode():
            return jsonify({"error": "Direct Blob uploads are enabled only in hosted mode."}), 400
        data = payload()
        record = backend().uploads.create(
            g.owner, workspace_id, data.get("revision"), data.get("filename"),
            data.get("size"), kind=data.get("kind", "course"), role=data.get("role", "reference"),
        )
        return jsonify({
            "upload_id": record["id"], "pathname": record["pathname"],
            "ticket": sign_upload(record, g.hosted_config.upload_secret),
            "maximum_size": record["size"], "expires_at": int(record["token_expires_at"]),
        }), 201

    @bp.get("/api/uploads/<upload_id>")
    def upload_status(upload_id):
        if not hosted_mode():
            abort(404)
        record = backend().uploads.get(g.owner, upload_id)
        return jsonify({k: record.get(k) for k in
                        ("id", "workspace_id", "filename", "size", "status", "cleanup_status", "attempts")})

    @bp.post("/api/uploads/<upload_id>/complete")
    def complete_upload(upload_id):
        if not hosted_mode():
            abort(404)
        backend().uploads.get(g.owner, upload_id)
        backend().verified_completion(upload_id)
        return jsonify({"accepted": True}), 202

    @bp.post("/internal/upload-complete")
    def upload_callback():
        if not hosted_mode():
            abort(404)
        data = payload()
        signed = verify_upload(data.get("ticket"), g.hosted_config.upload_secret, allow_expired=True)
        record = backend().uploads.internal_get(signed["upload_id"])
        if any(record[key] != signed[claim] for key, claim in (
            ("owner", "owner"), ("workspace_id", "workspace_id"),
            ("pathname", "pathname"), ("size", "size"), ("kind", "kind"),
        )):
            raise ValueError("Callback does not match its persisted intent.")
        backend().verified_completion(record["id"])
        return jsonify({"accepted": True}), 202

    @bp.get("/internal/cleanup")
    def cleanup_cron():
        if not hosted_mode():
            abort(404)
        from hosted_jobs import cleanup_batch
        return jsonify(cleanup_batch(backend=backend()))

    @bp.post("/api/workspaces/<workspace_id>/canvas")
    def canvas_source(workspace_id):
        if hosted_mode():
            return jsonify({"error": "Hosted beta uses course-file uploads; local shared Canvas credentials are disabled."}), 400
        data = payload()
        workspace = store().get(g.owner, workspace_id)
        course_id = str(data.get("course_id", ""))
        if not re.fullmatch(r"[0-9]{1,20}", course_id):
            raise ValueError("Enter a numeric Canvas course ID.")
        if data.get("revision") != workspace["revision"] or workspace.get("active_job"):
            return jsonify({"error": "Workspace changed or is processing. Refresh first."}), 409
        base = root() / "courses" / workspace_id / uuid.uuid4().hex
        base.mkdir(parents=True, exist_ok=False)
        try:
            exported = acquire_canvas_course(course_id, base)
            from mentor_service import inventory
            inventory({**workspace, "source_type": "api", "export_dir": str(exported)})
            return jsonify(public(store().set_source(g.owner, workspace_id, {
                "source_type": "api", "course_id": course_id, "export_dir": str(exported),
            }, workspace["revision"])))
        except Exception:
            shutil.rmtree(base, ignore_errors=True)
            raise

    @bp.post("/api/workspaces/<workspace_id>/materials")
    def add_material(workspace_id):
        workspace = store().get(g.owner, workspace_id)
        if workspace.get("active_job"):
            return jsonify({"error": "Wait for the active job before adding sources."}), 409
        if len(workspace["materials"]) >= 30:
            raise ValueError("Use at most 30 supplemental sources in this workspace.")
        from mentor_materials import extract_material, fetch_url_material, text_material
        if request.is_json:
            data = payload()
            revision, role = data.get("revision"), data.get("role", "reference")
            if data.get("url"):
                material = fetch_url_material(data["url"], role)
            else:
                material = text_material(data.get("text", ""), data.get("name", "Instructor notes"), role)
        else:
            if hosted_mode():
                return jsonify({"error": "Use the private multipart upload flow for hosted documents."}), 400
            revision = int(request.form.get("revision", 0))
            role = request.form.get("role", "reference")
            upload = request.files.get("file")
            if not upload or not upload.filename:
                raise ValueError("Choose a supplemental file.")
            with tempfile.TemporaryDirectory(prefix="mentor-material-") as temp:
                path = Path(temp) / (secure_filename(upload.filename) or "material.txt")
                count = 0
                with path.open("wb") as output:
                    while chunk := upload.stream.read(65536):
                        count += len(chunk)
                        if count > 10 * 1024 * 1024:
                            raise ValueError("Supplemental files must be 10 MiB or smaller.")
                        output.write(chunk)
                material = extract_material(path, upload.filename, role)
        if len(workspace["materials"]) >= 30:
            raise ValueError("Use at most 30 supplemental sources in this workspace.")
        return jsonify(public(store().add_material(g.owner, workspace_id, redact(material), revision)))

    @bp.delete("/api/workspaces/<workspace_id>/materials/<material_id>")
    def remove_material(workspace_id, material_id):
        return jsonify(public(store().remove_material(g.owner, workspace_id, material_id,
                                                       payload().get("revision"))))

    @bp.post("/api/workspaces/<workspace_id>/chat")
    def chat(workspace_id):
        from mentor_service import interview, review_draft
        data = payload()
        workspace = store().get(g.owner, workspace_id)
        if workspace.get("active_job"):
            return jsonify({"error": "Wait for the active job before continuing the conversation."}), 409
        message = str(data.get("message", "")).strip()
        if not 1 <= len(message) <= 6000:
            raise ValueError("Enter a message of 1–6000 characters.")
        message = redact(message)
        chosen, selected_values = provider_values(data)
        ai = None if hosted_mode() else client(data)
        kind = "review" if data.get("kind") == "review" else "interview"
        try:
            if kind == "review":
                workspace = store().begin_review(g.owner, workspace_id, workspace["draft_revision"])
            else:
                brief = workspace.get("brief", "") + "\nInstructor feedback: " + message
                if len(brief) > 30000:
                    raise ValueError("Refine the design brief before adding more interview feedback.")
                workspace = store().save_brief(g.owner, workspace_id, brief.strip(), workspace["revision"])
                workspace = store().begin_interview(g.owner, workspace_id, workspace["revision"])
        except Exception:
            if ai is not None:
                ai.close()
            raise
        job_id = workspace["active_job"]["id"]
        if selected_values:
            workspace = store().pin_provider(g.owner, workspace_id, job_id, chosen,
                                              selected_values["model"], selected_values.get("version"))
        store().add_message(g.owner, workspace_id, "user", message, kind)
        if hosted_mode():
            try:
                backend().dispatch_mentor(g.owner, workspace, kind, chosen)
            except Exception:
                store().fail_job(g.owner, workspace_id, job_id, "Could not queue mentor discussion.")
                return jsonify({"error": "Could not queue the discussion. Your feedback is saved; retry."}), 503
            return jsonify(public(store().get(g.owner, workspace_id))), 202
        try:
            if kind == "review":
                reply, revised = review_draft(workspace, message, ai)
                store().finish_review(g.owner, workspace_id, job_id, revised)
            else:
                reply = interview(store().get(g.owner, workspace_id), message, ai)
                store().finish_interview(g.owner, workspace_id, job_id)
        except Exception:
            try:
                store().fail_job(g.owner, workspace_id, job_id, "Mentor response failed.")
            except WorkspaceConflict:
                pass
            # Never return provider exception text, which can contain credentials.
            return jsonify({"error": "Mentor response failed. Your feedback was saved; check provider settings and retry."}), 502
        finally:
            if ai is not None:
                ai.close()
        store().add_message(g.owner, workspace_id, "assistant", redact(reply), kind)
        return jsonify(public(store().get(g.owner, workspace_id)))

    @bp.post("/api/workspaces/<workspace_id>/approve-direction")
    def approve_direction(workspace_id):
        workspace = store().get(g.owner, workspace_id)
        if not any(j.get("kind") == "interview" and j.get("status") == "completed"
                   and j.get("revision") == workspace["revision"] for j in workspace["jobs"]):
            raise ValueError("Complete a mentor interview for this input revision before approving the direction.")
        return jsonify(public(store().approve_direction(g.owner, workspace_id, payload().get("revision"))))

    def run_job(owner, workspace, kind, ai, data_root):
        from mentor_store import MentorStore
        from mentor_service import build_artifact, generate_draft
        database = MentorStore(data_root / "workspaces.sqlite")
        job_id = workspace["active_job"]["id"]
        try:
            if kind == "analysis":
                draft = generate_draft(workspace, ai)
                database.finish_analysis(owner, workspace["id"], job_id, draft)
            else:
                artifact = build_artifact(workspace, data_root / "artifacts" / workspace["id"] / job_id)
                database.finish_build(owner, workspace["id"], job_id, artifact)
        except Exception:
            try:
                database.fail_job(owner, workspace["id"], job_id,
                                  "Processing failed. Check the input/provider and retry; no approval was granted.")
            except WorkspaceConflict:
                pass  # Canceled/recovered jobs cannot commit late results.
        finally:
            if ai is not None:
                ai.close()

    @bp.post("/api/workspaces/<workspace_id>/analyze")
    def analyze(workspace_id):
        data = payload()
        chosen, selected_values = provider_values(data)
        ai = None if hosted_mode() else client(data)
        try:
            workspace = store().begin_analysis(g.owner, workspace_id, data.get("revision"))
        except Exception:
            if ai is not None:
                ai.close()
            raise
        if hosted_mode():
            try:
                if selected_values:
                    workspace = store().pin_provider(g.owner, workspace_id,
                        workspace["active_job"]["id"], chosen, selected_values["model"],
                        selected_values.get("version"))
                backend().dispatch_mentor(g.owner, workspace, "analysis", chosen)
            except Exception:
                store().fail_job(g.owner, workspace_id, workspace["active_job"]["id"], "Could not queue analysis.")
                return jsonify({"error": "Could not queue analysis. Please retry."}), 503
        else:
            if selected_values:
                workspace = store().pin_provider(g.owner, workspace_id,
                    workspace["active_job"]["id"], chosen, selected_values["model"],
                    selected_values.get("version"))
            threading.Thread(target=run_job, args=(g.owner, workspace, "analysis", ai, root()), daemon=True).start()
        return jsonify(public(workspace)), 202

    @bp.put("/api/workspaces/<workspace_id>/draft")
    def edit_draft(workspace_id):
        from mentor_service import validate_manual_draft
        data = payload()
        workspace = store().get(g.owner, workspace_id)
        if not workspace.get("draft"):
            raise ValueError("Generate a draft first.")
        draft = validate_manual_draft(workspace["draft"], redact(data.get("draft")))
        return jsonify(public(store().save_draft(g.owner, workspace_id, draft, data.get("draft_revision"))))

    @bp.post("/api/workspaces/<workspace_id>/approve-build")
    def approve_build(workspace_id):
        workspace = store().get(g.owner, workspace_id)
        return jsonify(public(store().approve_build(g.owner, workspace_id,
                                                     payload().get("draft_revision"))))

    @bp.post("/api/workspaces/<workspace_id>/cancel-job")
    def cancel_job(workspace_id):
        workspace = store().get(g.owner, workspace_id)
        data = payload()
        if not workspace.get("active_job") or workspace["active_job"]["id"] != data.get("job_id"):
            return jsonify({"error": "The job is no longer active."}), 409
        if hosted_mode() and workspace["active_job"]["kind"] == "prepare":
            backend().uploads.cancel(g.owner, data["job_id"])
            return jsonify(public(store().get(g.owner, workspace_id)))
        return jsonify(public(store().fail_job(g.owner, workspace_id, data["job_id"],
                                               "Canceled or recovered by the owner.")))

    @bp.post("/api/workspaces/<workspace_id>/build")
    def build(workspace_id):
        workspace = store().begin_build(g.owner, workspace_id, payload().get("draft_revision"))
        if hosted_mode():
            try:
                backend().dispatch_mentor(g.owner, workspace, "build")
            except Exception:
                store().fail_job(g.owner, workspace_id, workspace["active_job"]["id"], "Could not queue build.")
                return jsonify({"error": "Could not queue the build. Please retry."}), 503
        else:
            threading.Thread(target=run_job, args=(g.owner, workspace, "build", None, root()), daemon=True).start()
        return jsonify(public(workspace)), 202

    @bp.get("/api/workspaces/<workspace_id>/download")
    def download(workspace_id):
        workspace = store().get(g.owner, workspace_id)
        artifact = workspace.get("artifact")
        if not artifact or workspace["stage"] != "built":
            abort(404)
        if hosted_mode():
            return Response(backend().stream_artifact(workspace), content_type="application/zip",
                            headers={"Content-Disposition": 'attachment; filename="approved-course.zip"'})
        path = Path(artifact["path"]).resolve()
        if not path.is_relative_to(root() / "artifacts"):
            abort(404)
        return send_file(path, as_attachment=True, download_name="approved-course.zip")

    @bp.get("/api/workspaces/<workspace_id>/preview/<path:filename>")
    def preview(workspace_id, filename):
        workspace = store().get(g.owner, workspace_id)
        artifact = workspace.get("artifact")
        if not artifact or workspace["stage"] != "built":
            abort(404)
        if hosted_mode():
            import mimetypes
            response = Response(backend().preview_stream(workspace, filename),
                                content_type=mimetypes.guess_type(filename)[0] or "application/octet-stream")
            response.headers["Content-Security-Policy"] = "sandbox allow-same-origin; default-src 'none'; img-src 'self'; media-src 'self'; base-uri 'self'"
            return response
        directory = Path(artifact["preview_dir"]).resolve()
        if not directory.is_relative_to(root() / "artifacts"):
            abort(404)
        response = send_from_directory(directory, filename)
        response.headers["Content-Security-Policy"] = "sandbox allow-same-origin; default-src 'none'; img-src 'self'; media-src 'self'; base-uri 'self'"
        return response

    return bp
