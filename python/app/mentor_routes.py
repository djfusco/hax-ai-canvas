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
from urllib.parse import urlsplit
import uuid

from flask import (
    Blueprint, abort, current_app, g, jsonify, render_template, request,
    send_file, send_from_directory,
)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.utils import secure_filename
from mentor_store import WorkspaceConflict, WorkspaceNotFound


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

    def root():
        return Path(root_provider()).resolve()

    def store():
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
                                  if k not in ("path", "preview_dir")}
        return result

    def client(data):
        provider = data.get("provider", "guided")
        if provider == "guided":
            return None
        if provider not in ("nebula", "anthropic", "openai", "gemini"):
            raise ValueError("Unsupported AI provider.")
        from ai_client import AIClient
        values = g.vault.get(g.owner, provider)
        if values is None:
            raise ValueError("Save credentials for the selected provider, or choose guided mode.")
        validate_gateway(provider, values.get("base_url", ""))
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
        # A local SQLite/file identity is intentionally not presented as a
        # production Vercel account/storage implementation.
        if os.environ.get("VERCEL"):
            return jsonify({"error": "Hosted mentor storage, authentication and worker services must be provisioned before deployment.",
                            "code": "HOSTED_SETUP_REQUIRED"}), 503
        from mentor_credentials import CredentialVault, private_key
        key = private_key(root() / ".browser-key")
        signer = URLSafeTimedSerializer(key.decode(), salt="faculty-mentor-browser")
        token = None
        try:
            token = signer.loads(request.cookies.get("mentor_browser", ""), max_age=30 * 86400)
        except (BadSignature, SignatureExpired):
            pass
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token):
            token = secrets.token_hex(32)
        g.mentor_cookie = signer.dumps(token)
        g.owner = hashlib.sha256(token.encode()).hexdigest()
        g.csrf = hmac.new(key, (g.owner + ":csrf").encode(), hashlib.sha256).hexdigest()
        g.vault = CredentialVault(root())
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin")
            if origin and origin != request.host_url.rstrip("/"):
                return jsonify({"error": "Cross-origin mentoring requests are not allowed."}), 403
            if not hmac.compare_digest(request.headers.get("X-Mentor-CSRF", ""), g.csrf):
                return jsonify({"error": "Reload the mentoring page before making changes."}), 403
        if (request.is_json and request.content_length
                and request.content_length > 2 * 1024 * 1024):
            return jsonify({"error": "The mentoring request is too large."}), 413

    @bp.after_request
    def response_security(response):
        if getattr(g, "mentor_cookie", None):
            response.set_cookie("mentor_browser", g.mentor_cookie, max_age=30 * 86400,
                                httponly=True, secure=request.is_secure, samesite="Strict")
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
        return jsonify({"error": str(exc)}), 400

    @bp.route("")
    @bp.route("/")
    def page():
        return render_template("mentor.html")

    @bp.get("/api/session")
    def session_info():
        return jsonify({"csrf": g.csrf, "credentials": g.vault.status(g.owner),
                        "identity": "this-browser", "hosted_ready": False})

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

    @bp.get("/api/workspaces")
    def workspaces():
        return jsonify([public(w) for w in store().list_workspaces(g.owner)])

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
        return jsonify(inventory(store().get(g.owner, workspace_id)))

    @bp.put("/api/workspaces/<workspace_id>/brief")
    def save_brief(workspace_id):
        data = payload()
        if not isinstance(data.get("brief"), str) or len(data["brief"]) > 30000:
            raise ValueError("The design brief must be 30,000 characters or smaller.")
        return jsonify(public(store().save_brief(g.owner, workspace_id, redact(data.get("brief", "")),
                                                 data.get("revision"))))

    @bp.post("/api/workspaces/<workspace_id>/source")
    def source_file(workspace_id):
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

    @bp.post("/api/workspaces/<workspace_id>/canvas")
    def canvas_source(workspace_id):
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
        ai = client(data)
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
        store().add_message(g.owner, workspace_id, "user", message, kind)
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
        ai = client(data)
        try:
            workspace = store().begin_analysis(g.owner, workspace_id, data.get("revision"))
        except Exception:
            if ai is not None:
                ai.close()
            raise
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
        return jsonify(public(store().fail_job(g.owner, workspace_id, data["job_id"],
                                               "Canceled or recovered by the owner.")))

    @bp.post("/api/workspaces/<workspace_id>/build")
    def build(workspace_id):
        workspace = store().begin_build(g.owner, workspace_id, payload().get("draft_revision"))
        threading.Thread(target=run_job, args=(g.owner, workspace, "build", None, root()), daemon=True).start()
        return jsonify(public(workspace)), 202

    @bp.get("/api/workspaces/<workspace_id>/download")
    def download(workspace_id):
        workspace = store().get(g.owner, workspace_id)
        artifact = workspace.get("artifact")
        if not artifact or workspace["stage"] != "built":
            abort(404)
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
        directory = Path(artifact["preview_dir"]).resolve()
        if not directory.is_relative_to(root() / "artifacts"):
            abort(404)
        response = send_from_directory(directory, filename)
        response.headers["Content-Security-Policy"] = "sandbox allow-same-origin; default-src 'none'; img-src 'self'; media-src 'self'; base-uri 'self'"
        return response

    return bp
