"""
app.py — Course Consultant web app (Flask).

Hosted on Vercel through server.py; also runs locally with
`python python/app/app.py` for development (http://127.0.0.1:5050/mentor).
"""

from __future__ import annotations

import os
import signal
import sys
import webbrowser
from pathlib import Path
from threading import Timer

from flask import Flask, jsonify, redirect, request
from werkzeug.exceptions import RequestEntityTooLarge

APP_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = APP_DIR.parent.resolve()
for path in (str(PROJECT_ROOT), str(APP_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

app = Flask(__name__, template_folder=str(APP_DIR / "templates"), static_folder=str(APP_DIR / "static"))
app.secret_key = os.urandom(24)
# Course packages arrive in pieces of a few MiB; documents are small.
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024

from mentor_routes import create_mentor_blueprint  # noqa: E402

app.register_blueprint(create_mentor_blueprint(
    lambda: Path(app.config.get("MENTOR_DATA_ROOT",
                               Path(os.environ["HAX_DATA_DIR"]) / "mentor_data"
                               if os.environ.get("HAX_DATA_DIR") else PROJECT_ROOT / "mentor_data"))
))


@app.before_request
def require_local_host():
    from hosted_config import hosted_mode
    if not hosted_mode():
        try:
            from urllib.parse import urlsplit
            host = urlsplit("http://" + request.host).hostname
            if host not in ("127.0.0.1", "localhost", "::1"):
                return jsonify({"error": "This application accepts loopback hostnames only."}), 403
        except ValueError:
            return jsonify({"error": "Invalid local hostname."}), 403


@app.before_request
def protect_unprovisioned_hosted_app():
    from hosted_config import hosted_mode, settings, HostedSetupError
    if not hosted_mode():
        return
    if request.path.startswith("/static/"):
        return
    try:
        config = settings()
    except HostedSetupError as exc:
        return jsonify({"error": str(exc), "code": "HOSTED_SETUP_REQUIRED"}), 503
    if request.method in ("GET", "HEAD") and request.path in ("/", "/mentor", "/mentor/"):
        from urllib.parse import urlsplit
        expected_host = urlsplit(config.origin).netloc.lower().removesuffix(":443")
        incoming_host = request.host.lower().removesuffix(":443")
        if incoming_host != expected_host:
            # Page navigation only: never forward an invitation/OTP POST body,
            # cookies or query parameters between deployment aliases.
            return redirect(config.origin + "/mentor", code=302)


@app.context_processor
def deployment_context():
    from hosted_config import hosted_mode
    return {"is_hosted": hosted_mode()}


@app.errorhandler(RequestEntityTooLarge)
def upload_too_large(exc):
    return jsonify({"error": "The upload is too large."}), 413


@app.route("/")
def index():
    return redirect("/mentor")


def open_browser(port):
    if os.environ.get("HAX_NO_BROWSER") != "1":
        webbrowser.open(f"http://127.0.0.1:{port}/mentor")


if __name__ == "__main__":
    from contextlib import nullcontext
    from werkzeug.serving import make_server
    from hosted_config import hosted_mode
    from local_startup import application_lease

    def terminate(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    port = int(os.environ.get("HAX_PORT", "5050"))
    print(f"\n  Course Consultant\n  Opening http://127.0.0.1:{port}/mentor  (Ctrl+C to stop)\n")
    data_root = Path(os.environ.get("HAX_DATA_DIR", PROJECT_ROOT)) / "mentor_data"
    try:
        with application_lease(data_root) if not hosted_mode() else nullcontext():
            server = make_server("127.0.0.1", port, app, threaded=True)
            try:
                Timer(0.3, open_browser, args=(port,)).start()
                server.serve_forever()
            finally:
                server.server_close()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
