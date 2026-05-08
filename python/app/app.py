"""
app.py — Canvas Course Builder Web App
A friendly local web interface for non-technical instructors.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import webbrowser
import zipfile
from pathlib import Path
from threading import Timer

# ── bootstrap: ensure Flask is available ─────────────────────────────────────
try:
    from flask import (Flask, Response, jsonify, redirect, render_template,
                       request, stream_with_context, url_for)
except ImportError:
    print("\n" + "="*60)
    print("  Flask is not installed.")
    print("  Please run:  pip install flask")
    print("  Then re-run this app.")
    print("="*60 + "\n")
    sys.exit(1)

from setup_wizard import (
    REQUIRED_ENV_VARS, load_env_values, mask_value, run_all_checks,
    save_env_values, validate_env_value, install_packages, PROJECT_ROOT,
    test_canvas_connection, test_ai_connection,
)
from pipeline_runner import STEPS, get_job, start_run, stop_npm

# ── app setup ─────────────────────────────────────────────────────────────────
APP_DIR = Path(__file__).parent.resolve()

app = Flask(
    __name__,
    template_folder=str(APP_DIR / "templates"),
    static_folder=str(APP_DIR / "static"),
)
app.secret_key = os.urandom(24)


# ── helpers ───────────────────────────────────────────────────────────────────

def _report_path(course_id: str, report_type: str) -> Path:
    names = {
        "readiness":  f"course_{course_id}_ai_readiness_report.html",
        "changes":    f"course_{course_id}_ai_changes_report.html",
        "dashboard":  f"course_{course_id}_ai_dashboard.html",
    }
    return PROJECT_ROOT / names.get(report_type, "")


def _create_site_name(course_title: str, course_id: str) -> str:
    """Derive the HAX site directory name from a course title.

    Must mirror the logic in build_site._create_site_name so we resolve to
    the same directory that was created during the pipeline run.
    """
    patterns = [
        r'([A-Z]{2,}\s+\d{3})',
        r'([A-Z]+\d+)',
        r'([A-Z]{2,}\s+[A-Z]{1,})',
    ]
    for pattern in patterns:
        match = re.search(pattern, course_title)
        if match:
            return match.group(1).lower().replace(' ', '')
    return f"course{course_id}"


def _resolve_site_dir(course_id: str) -> Path | None:
    """Return the HAX site directory for a course, or None if it doesn't exist."""
    import sys as _sys
    if str(PROJECT_ROOT) not in _sys.path:
        _sys.path.insert(0, str(PROJECT_ROOT))
    try:
        from extract import get_export_dir, get_course_title
        export_dir = get_export_dir(course_id, str(PROJECT_ROOT / "exports"))
        title = get_course_title(export_dir)
    except Exception:
        title = f"Course {course_id}"
    site_name = _create_site_name(title, course_id)
    site_dir = Path.home() / ".hax-ai" / "sites" / site_name
    return site_dir if site_dir.exists() and (site_dir / "site.json").exists() else None


# Track running site processes: course_id → {"proc": Popen, "url": str}
_site_procs: dict[str, dict] = {}


# ── routes: navigation ─────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Welcome + requirements screen."""
    result = run_all_checks()
    return render_template("index.html",
                           checks=result["checks"],
                           all_ok=result["all_ok"])


@app.route("/setup")
def setup():
    """Configuration wizard for .env values."""
    current = load_env_values()
    fields = []
    for var in REQUIRED_ENV_VARS:
        raw_val = current.get(var["key"], "")
        fields.append({
            **var,
            "value":   raw_val,
            "masked":  mask_value(var["key"], raw_val),
            "present": bool(raw_val),
            "sensitive": any(s in var["key"].upper()
                             for s in ("KEY", "TOKEN", "SECRET", "PASSWORD")),
        })
    return render_template("setup.html", fields=fields)


@app.route("/setup/save", methods=["POST"])
def setup_save():
    """Save .env values from wizard form."""
    new_values = {}
    errors = {}
    for var in REQUIRED_ENV_VARS:
        key      = var["key"]
        value    = request.form.get(key, "").strip()
        # If field was left blank, keep existing value
        if not value:
            existing = load_env_values().get(key, "")
            if existing:
                continue   # keep as-is
        validate = var.get("validate", "none")
        if value:
            err = validate_env_value(key, value, validate)
            if err:
                errors[key] = err
            else:
                new_values[key] = value
        elif validate == "nonempty" and not var.get("optional"):
            errors[key] = "This field is required."

    if errors:
        current = load_env_values()
        fields = []
        for var in REQUIRED_ENV_VARS:
            raw_val = request.form.get(var["key"], "") or current.get(var["key"], "")
            fields.append({
                **var,
                "value":   raw_val,
                "masked":  mask_value(var["key"], raw_val),
                "present": bool(raw_val),
                "sensitive": any(s in var["key"].upper()
                                 for s in ("KEY", "TOKEN", "SECRET", "PASSWORD")),
                "error":  errors.get(var["key"]),
            })
        return render_template("setup.html", fields=fields, errors=errors)

    save_env_values(new_values)
    return redirect(url_for("index"))


@app.route("/setup/save-ajax", methods=["POST"])
def setup_save_ajax():
    """Save .env values via AJAX and return JSON (used by test-connection flow)."""
    new_values = {}
    errors = {}
    for var in REQUIRED_ENV_VARS:
        key   = var["key"]
        value = request.form.get(key, "").strip()
        if not value:
            continue  # keep existing
        validate = var.get("validate", "none")
        err = validate_env_value(key, value, validate)
        if err:
            errors[key] = err
        else:
            new_values[key] = value

    if errors:
        return jsonify({"ok": False, "errors": errors})

    if new_values:
        save_env_values(new_values)
    return jsonify({"ok": True})


@app.route("/install-packages", methods=["POST"])
def install_packages_route():
    """Attempt to pip install missing packages."""
    success, output = install_packages()
    return jsonify({"success": success, "output": output[-2000:]})


@app.route("/run")
def run_page():
    """Main run form."""
    result = run_all_checks()
    return render_template("run.html",
                           all_ok=result["all_ok"],
                           steps=STEPS)


# ── routes: pipeline execution ────────────────────────────────────────────────

@app.route("/run/start", methods=["POST"])
def run_start():
    """Start a new pipeline run. Returns run_id as JSON."""
    data = request.get_json() or request.form.to_dict()

    course_id = str(data.get("course_id", "")).strip()
    if not course_id:
        return jsonify({"error": "Course ID is required."}), 400

    options = {
        "do_export":     data.get("do_export",     "true") in (True, "true", "1", "on"),
        "do_pipeline":   data.get("do_pipeline",   "true") in (True, "true", "1", "on"),
        "do_build":      data.get("do_build",       "true") in (True, "true", "1", "on"),
        "do_npm_install":data.get("do_npm_install","true") in (True, "true", "1", "on"),
        "do_serve":      data.get("do_serve",       "true") in (True, "true", "1", "on"),
        "model_provider":data.get("model_provider", "nebula"),
        "workers":       int(data.get("workers", 3)),
        "no_ai":         data.get("no_ai", "false") in (True, "true", "1", "on"),
        "skip_extract":  False,  # export step controls this
    }

    run_id = start_run(course_id, options)
    return jsonify({"run_id": run_id})


@app.route("/stream/<run_id>")
def stream(run_id: str):
    """Server-Sent Events endpoint — streams log messages to the browser."""
    job = get_job(run_id)
    if not job:
        return "Run not found", 404

    def generate():
        # Send initial step statuses
        for step_id, status in job.step_statuses.items():
            payload = json.dumps({"type": "step", "step": step_id, "status": status})
            yield f"data: {payload}\n\n"

        while True:
            try:
                msg = job.q.get(timeout=30)
            except Exception:
                # Keep-alive ping
                yield "data: {\"type\":\"ping\"}\n\n"
                if job.status in ("complete", "failed"):
                    break
                continue

            yield f"data: {json.dumps(msg)}\n\n"

            if msg.get("type") == "done":
                break

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/status/<run_id>")
def job_status(run_id: str):
    """Return current job status as JSON (for polling fallback)."""
    job = get_job(run_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        "run_id":    job.run_id,
        "status":    job.status,
        "steps":     job.step_statuses,
        "site_dir":  job.site_dir,
        "site_url":  job.site_url,
        "course_id": job.course_id,
    })


# ── routes: results & file access ─────────────────────────────────────────────

@app.route("/results/<run_id>")
def results(run_id: str):
    """Results screen shown after a run completes."""
    job = get_job(run_id)
    if not job:
        return redirect(url_for("run_page"))

    course_id = job.course_id
    readiness_exists  = _report_path(course_id, "readiness").exists()
    changes_exists    = _report_path(course_id, "changes").exists()
    dashboard_exists  = _report_path(course_id, "dashboard").exists()

    return render_template(
        "results.html",
        job=job,
        course_id=course_id,
        readiness_exists=readiness_exists,
        changes_exists=changes_exists,
        dashboard_exists=dashboard_exists,
    )


@app.route("/open/report/<report_type>/<course_id>")
def open_report(report_type: str, course_id: str):
    """Open a report HTML file in the user's default browser."""
    path = _report_path(course_id, report_type)
    if path.exists():
        webbrowser.open(path.as_uri())
        return jsonify({"ok": True, "path": str(path)})
    return jsonify({"ok": False, "error": f"Report not found: {path}"}), 404


@app.route("/open/folder/<path:folder>")
def open_folder(folder: str):
    """Open a folder in Finder/Explorer."""
    p = Path("/" + folder) if not folder.startswith("/") else Path(folder)
    try:
        import platform
        if platform.system() == "Darwin":
            subprocess.Popen(["open", str(p)])
        elif platform.system() == "Windows":
            subprocess.Popen(["explorer", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/open/site/<run_id>")
def open_site(run_id: str):
    """Open the HAX site in the browser."""
    job = get_job(run_id)
    if job and job.site_url:
        webbrowser.open(job.site_url)
        return jsonify({"ok": True, "url": job.site_url})
    return jsonify({"ok": False, "error": "Site URL not available"}), 404


@app.route("/stop/<run_id>", methods=["POST"])
def stop_site(run_id: str):
    """Stop the running npm start process."""
    stop_npm(run_id)
    return jsonify({"ok": True})


# ── routes: history ──────────────────────────────────────────────────────────

@app.route("/history")
def history():
    """Show past course runs with links to reports."""
    import sys as _sys
    if str(PROJECT_ROOT) not in _sys.path:
        _sys.path.insert(0, str(PROJECT_ROOT))
    from sqlite_client import SQLiteClient
    db_path = PROJECT_ROOT / "course_pipeline.db"
    courses = []
    if db_path.exists():
        try:
            with SQLiteClient(db_path=str(db_path)) as db:
                courses = db.get_course_list()
        except Exception:
            pass
    # Check which reports / site exist for each course
    for c in courses:
        cid = c["course_id"]
        c["has_readiness"]  = _report_path(cid, "readiness").exists()
        c["has_changes"]    = _report_path(cid, "changes").exists()
        c["has_dashboard"]  = _report_path(cid, "dashboard").exists()
        c["has_site"]       = _resolve_site_dir(cid) is not None
        c["site_running"]   = cid in _site_procs
        # Try to get a better course name from the export directory
        try:
            import sys as _sys
            _sys.path.insert(0, str(PROJECT_ROOT))
            from extract import get_export_dir, get_course_title
            c["course_name"] = get_course_title(get_export_dir(cid))
        except Exception:
            pass  # keep the name from get_course_list
    return render_template("history.html", courses=courses)


# ── routes: launch HAX site ──────────────────────────────────────────────────

@app.route("/launch/<course_id>", methods=["POST"])
def launch_site(course_id: str):
    """Launch the HAX site for a course (npm install if needed, then npm start)."""
    # Already running?
    if course_id in _site_procs:
        url = _site_procs[course_id].get("url", "http://localhost:8080")
        webbrowser.open(url)
        return jsonify({"ok": True, "url": url, "already_running": True})

    site_dir = _resolve_site_dir(course_id)
    if not site_dir:
        return jsonify({"ok": False, "error": "HAX site not found for this course."}), 404

    def _launch():
        _IS_WINDOWS = sys.platform == "win32"
        # npm install if node_modules missing
        if not (site_dir / "node_modules").exists():
            subprocess.run(
                ["npm", "install"], cwd=str(site_dir),
                capture_output=True, shell=_IS_WINDOWS,
            )

        proc = subprocess.Popen(
            ["npm", "start"], cwd=str(site_dir),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, shell=_IS_WINDOWS,
        )
        _site_procs[course_id] = {"proc": proc, "url": None, "dir": str(site_dir)}

        ready_patterns = [
            re.compile(r"(https?://localhost[:\d]*)", re.IGNORECASE),
            re.compile(r"Local:\s+(https?://\S+)", re.IGNORECASE),
            re.compile(r"running at\s+(https?://\S+)", re.IGNORECASE),
            re.compile(r"listening on port (\d+)", re.IGNORECASE),
        ]
        for line in proc.stdout:
            line = line.rstrip()
            if not _site_procs[course_id].get("url"):
                for pat in ready_patterns:
                    m = pat.search(line)
                    if m:
                        raw = m.group(1)
                        url = f"http://localhost:{raw}" if raw.isdigit() else raw
                        _site_procs[course_id]["url"] = url
                        webbrowser.open(url)
                        break

        # Process ended — clean up
        _site_procs.pop(course_id, None)

    t = threading.Thread(target=_launch, daemon=True)
    t.start()

    # Wait briefly for the URL to appear
    import time
    for _ in range(40):
        entry = _site_procs.get(course_id, {})
        if entry.get("url"):
            return jsonify({"ok": True, "url": entry["url"]})
        time.sleep(0.5)

    return jsonify({"ok": True, "url": "http://localhost:8080", "note": "Site starting, URL not yet detected."})


@app.route("/launch/<course_id>/stop", methods=["POST"])
def stop_launched_site(course_id: str):
    """Stop a running HAX site launched from the history page."""
    entry = _site_procs.pop(course_id, None)
    if entry and entry.get("proc"):
        try:
            entry["proc"].terminate()
        except Exception:
            pass
    return jsonify({"ok": True})


@app.route("/launch/<course_id>/status")
def launch_status(course_id: str):
    """Check if a site is currently running."""
    entry = _site_procs.get(course_id)
    if entry:
        return jsonify({"running": True, "url": entry.get("url")})
    return jsonify({"running": False})


# ── routes: share / import ──────────────────────────────────────────────────

@app.route("/export/<course_id>")
def export_course_zip(course_id: str):
    """Download a shareable .zip of a course (export folder + DB rows + reports)."""
    import sys as _sys
    if str(PROJECT_ROOT) not in _sys.path:
        _sys.path.insert(0, str(PROJECT_ROOT))
    from sqlite_client import SQLiteClient

    db_path = PROJECT_ROOT / "course_pipeline.db"
    if not db_path.exists():
        return jsonify({"error": "No database found."}), 404

    # Find the export directory
    exports_base = PROJECT_ROOT / "exports"
    matches = list(exports_base.glob(f"course_{course_id}_*"))
    if not matches:
        return jsonify({"error": f"No export folder for course {course_id}."}), 404
    export_dir = matches[0]

    # Collect DB rows for this course
    with SQLiteClient(db_path=str(db_path)) as db:
        conn = db._conn
        rows = conn.execute(
            "SELECT id, course_id, item_type, title, raw_content, "
            "recommendations, evaluation, ai_enhanced_markdown, status, "
            "created_at, updated_at "
            "FROM course_items WHERE course_id = ?",
            (course_id,),
        ).fetchall()
        db_items = [dict(r) for r in rows]

    # Collect report files
    report_types = ["readiness", "changes", "dashboard"]
    report_files = {}
    for rt in report_types:
        rp = _report_path(course_id, rt)
        if rp.exists():
            report_files[rp.name] = rp

    # Find HAX site directory (if it was built)
    site_dir = _resolve_site_dir(course_id)
    site_name = site_dir.name if site_dir else None

    # Build zip in memory
    buf = io.BytesIO()
    _SKIP_DIRS = {".git", "node_modules", ".cache"}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. manifest.json
        manifest = {
            "course_id": course_id,
            "export_folder_name": export_dir.name,
            "site_name": site_name,
            "db_items": db_items,
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, default=str))

        # 2. export folder
        for file_path in export_dir.rglob("*"):
            if file_path.is_file():
                arcname = f"exports/{export_dir.name}/{file_path.relative_to(export_dir)}"
                zf.write(file_path, arcname)

        # 3. report HTML files
        for rname, rpath in report_files.items():
            zf.write(rpath, f"reports/{rname}")

        # 4. HAX site (excluding node_modules, .git, .cache)
        if site_dir:
            for file_path in site_dir.rglob("*"):
                if file_path.is_file():
                    # Skip if any parent is in _SKIP_DIRS
                    parts = file_path.relative_to(site_dir).parts
                    if any(p in _SKIP_DIRS for p in parts):
                        continue
                    arcname = f"site/{site_dir.name}/{file_path.relative_to(site_dir)}"
                    zf.write(file_path, arcname)

    buf.seek(0)
    safe_name = export_dir.name[:80]
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.zip"'},
    )


@app.route("/import")
def import_page():
    """Show the import-course form."""
    return render_template("import_course.html")


@app.route("/import/upload", methods=["POST"])
def import_upload():
    """Handle a .zip upload: extract exports, insert DB rows, copy reports."""
    import sys as _sys
    if str(PROJECT_ROOT) not in _sys.path:
        _sys.path.insert(0, str(PROJECT_ROOT))
    from sqlite_client import SQLiteClient

    f = request.files.get("course_zip")
    if not f or not f.filename.endswith(".zip"):
        return jsonify({"ok": False, "error": "Please upload a .zip file."}), 400

    try:
        zf = zipfile.ZipFile(io.BytesIO(f.read()))
    except zipfile.BadZipFile:
        return jsonify({"ok": False, "error": "Invalid zip file."}), 400

    # Read manifest
    if "manifest.json" not in zf.namelist():
        return jsonify({"ok": False, "error": "Missing manifest.json in zip."}), 400

    manifest = json.loads(zf.read("manifest.json"))
    course_id = str(manifest["course_id"])
    export_folder_name = manifest["export_folder_name"]
    db_items = manifest.get("db_items", [])

    exports_base = PROJECT_ROOT / "exports"
    exports_base.mkdir(parents=True, exist_ok=True)

    # 1. Extract export folder
    export_prefix = f"exports/{export_folder_name}/"
    for name in zf.namelist():
        if name.startswith(export_prefix) and not name.endswith("/"):
            relative = name[len("exports/"):]
            target = exports_base / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(name))

    # 2. Insert DB rows
    db_path = PROJECT_ROOT / "course_pipeline.db"
    with SQLiteClient(db_path=str(db_path)) as db:
        for item in db_items:
            db._conn.execute(
                "INSERT OR REPLACE INTO course_items "
                "(id, course_id, item_type, title, raw_content, "
                "recommendations, evaluation, ai_enhanced_markdown, "
                "status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item["id"], item["course_id"], item["item_type"],
                    item["title"], item.get("raw_content"),
                    item.get("recommendations"), item.get("evaluation"),
                    item.get("ai_enhanced_markdown"), item.get("status", "COMPLETED"),
                    item.get("created_at"), item.get("updated_at"),
                ),
            )
        db._conn.commit()

    # 3. Copy report HTML files
    reports_prefix = "reports/"
    for name in zf.namelist():
        if name.startswith(reports_prefix) and not name.endswith("/"):
            report_name = name[len(reports_prefix):]
            target = PROJECT_ROOT / report_name
            target.write_bytes(zf.read(name))

    # 4. Extract HAX site (if included)
    site_name = manifest.get("site_name")
    if site_name:
        site_prefix = f"site/{site_name}/"
        sites_base = Path.home() / ".hax-ai" / "sites"
        sites_base.mkdir(parents=True, exist_ok=True)
        site_files = 0
        for name in zf.namelist():
            if name.startswith(site_prefix) and not name.endswith("/"):
                relative = name[len(f"site/"):]
                target = sites_base / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(name))
                site_files += 1
    else:
        site_files = 0

    return jsonify({
        "ok": True,
        "course_id": course_id,
        "items_imported": len(db_items),
        "export_folder": export_folder_name,
        "site_imported": site_name if site_files > 0 else None,
    })


# ── routes: checks API ─────────────────────────────────────────────────────────

@app.route("/api/checks")
def api_checks():
    """Return current system checks as JSON (used by AJAX refresh)."""
    result = run_all_checks()
    return jsonify(result)


@app.route("/api/test-connection", methods=["POST"])
def api_test_connection():
    """Test a Canvas or AI provider connection using saved .env credentials."""
    data = request.get_json() or {}
    provider = data.get("provider", "").strip().lower()

    if provider == "canvas":
        result = test_canvas_connection()
    elif provider in ("nebula", "anthropic", "openai", "gemini"):
        result = test_ai_connection(provider)
    else:
        result = {"ok": False, "message": f"Unknown provider: {provider}"}

    return jsonify(result)


# ── main ──────────────────────────────────────────────────────────────────────

def open_browser():
    webbrowser.open("http://127.0.0.1:5050")


if __name__ == "__main__":
    print("\n" + "="*60)
    print("  Canvas Course Builder")
    print("  Opening in your browser at http://127.0.0.1:5050")
    print("  Press Ctrl+C to stop the app.")
    print("="*60 + "\n")

    # Open browser after a short delay (gives Flask time to start)
    Timer(1.5, open_browser).start()

    app.run(
        host="127.0.0.1",
        port=5050,
        debug=False,
        threaded=True,
        use_reloader=False,
    )
