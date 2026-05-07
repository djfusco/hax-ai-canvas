"""
app.py — Canvas Course Builder Web App
A friendly local web interface for non-technical instructors.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import webbrowser
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
        "readiness": f"course_{course_id}_ai_readiness_report.html",
        "changes":   f"course_{course_id}_ai_changes_report.html",
    }
    return PROJECT_ROOT / names.get(report_type, "")


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
    readiness_exists = _report_path(course_id, "readiness").exists()
    changes_exists   = _report_path(course_id, "changes").exists()

    return render_template(
        "results.html",
        job=job,
        course_id=course_id,
        readiness_exists=readiness_exists,
        changes_exists=changes_exists,
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
