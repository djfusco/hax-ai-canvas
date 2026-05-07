"""
pipeline_runner.py
Orchestrates the pipeline steps as subprocesses and streams output via queues.
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Dict, List, Optional

APP_DIR      = Path(__file__).parent.resolve()
PROJECT_ROOT = APP_DIR.parent.resolve()
_IS_WINDOWS  = sys.platform == "win32"

# ── sensitive key filter (never log these) ────────────────────────────────────
_SECRET_RE = re.compile(
    r"(api_key|token|password|secret|key)\s*=\s*\S+",
    re.IGNORECASE,
)

def _scrub(text: str) -> str:
    """Remove potential secret values from log lines."""
    return _SECRET_RE.sub(lambda m: m.group(0).rsplit("=", 1)[0] + "=••••••••", text)


# ── job state ─────────────────────────────────────────────────────────────────

STEPS = [
    {"id": "export",   "label": "Export from Canvas"},
    {"id": "pipeline", "label": "AI Evaluation & Enhancement"},
    {"id": "build",    "label": "Build HAX Site"},
    {"id": "npm",      "label": "Install Site Packages (npm install)"},
    {"id": "serve",    "label": "Start Local Site (npm start)"},
]

class RunState:
    def __init__(self, run_id: str, course_id: str, options: Dict):
        self.run_id    = run_id
        self.course_id = course_id
        self.options   = options
        self.status    = "running"       # running | complete | failed
        self.current_step: Optional[str] = None
        self.step_statuses: Dict[str, str] = {s["id"]: "pending" for s in STEPS}
        self.q: queue.Queue = queue.Queue()
        self.site_dir: Optional[str] = None
        self.site_url: Optional[str] = None
        self.npm_process: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None

    def put(self, msg_type: str, **kwargs):
        self.q.put({"type": msg_type, **kwargs})

    def log(self, text: str, step: Optional[str] = None):
        safe = _scrub(text)
        self.q.put({"type": "log", "step": step or self.current_step, "text": safe})

    def set_step(self, step_id: str, status: str):
        self.current_step = step_id if status == "running" else self.current_step
        self.step_statuses[step_id] = status
        self.q.put({"type": "step", "step": step_id, "status": status})


# ── active jobs ───────────────────────────────────────────────────────────────
_jobs: Dict[str, RunState] = {}


def get_job(run_id: str) -> Optional[RunState]:
    return _jobs.get(run_id)


def list_jobs() -> List[Dict]:
    return [
        {"run_id": j.run_id, "course_id": j.course_id,
         "status": j.status, "site_url": j.site_url}
        for j in _jobs.values()
    ]


# ── subprocess runner ─────────────────────────────────────────────────────────

def _run_step(
    job: RunState,
    step_id: str,
    cmd: List[str],
    cwd: Path = PROJECT_ROOT,
    capture_site_dir: bool = False,
) -> bool:
    """
    Run a subprocess for a pipeline step.
    Streams stdout/stderr to job.q line by line.
    Returns True on success, False on failure.
    """
    job.set_step(step_id, "running")
    job.log(f"▶ {' '.join(str(c) for c in cmd)}", step_id)

    try:
        # PYTHONUNBUFFERED=1 ensures print() output streams line-by-line
        # instead of being held in a buffer (Python buffers when stdout is a pipe)
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(cwd),
            bufsize=1,
            env=env,
            shell=_IS_WINDOWS,  # needed on Windows for npm.cmd and other batch files
        )

        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            job.log(line, step_id)

            # Detect HAX site directory from build_site.py output
            if capture_site_dir and "HAX site ready at:" in line:
                m = re.search(r"HAX site ready at:\s*(.+)", line)
                if m:
                    job.site_dir = m.group(1).strip()

            # Also detect from "Output dir:" line
            if capture_site_dir and "Output dir:" in line:
                m = re.search(r"Output dir:\s*(.+)", line)
                if m and not job.site_dir:
                    job.site_dir = m.group(1).strip()

        proc.wait()

        if proc.returncode == 0:
            job.set_step(step_id, "complete")
            return True
        else:
            job.set_step(step_id, "failed")
            job.log(f"✗ Step failed (exit code {proc.returncode})", step_id)
            return False

    except Exception as exc:
        job.set_step(step_id, "failed")
        job.log(f"✗ Error running step: {exc}", step_id)
        return False


def _find_site_dir(course_id: str) -> Optional[Path]:
    """
    Try to find the HAX site directory for a course.
    build_site.py puts it at ~/.hax-ai/sites/<site_name>/
    We look for any directory under ~/.hax-ai/sites/ that was recently modified.
    """
    hax_base = Path.home() / ".hax-ai" / "sites"
    if not hax_base.exists():
        return None

    # Prefer the most recently modified site dir
    candidates = sorted(
        [d for d in hax_base.iterdir() if d.is_dir() and (d / "site.json").exists()],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


# ── npm start handler ─────────────────────────────────────────────────────────

def _start_npm_serve(job: RunState, site_dir: Path) -> None:
    """Start npm start in site_dir and watch for the serving URL."""
    job.set_step("serve", "running")
    job.log(f"Starting local site at {site_dir} ...", "serve")

    try:
        proc = subprocess.Popen(
            ["npm", "start"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(site_dir),
            bufsize=1,
            shell=_IS_WINDOWS,  # needed on Windows for npm.cmd
        )
        job.npm_process = proc

        # Patterns that indicate the site is ready
        ready_patterns = [
            re.compile(r"(https?://localhost[:\d]*)", re.IGNORECASE),
            re.compile(r"Local:\s+(https?://\S+)", re.IGNORECASE),
            re.compile(r"running at\s+(https?://\S+)", re.IGNORECASE),
            re.compile(r"listening on port (\d+)", re.IGNORECASE),
        ]

        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            job.log(line, "serve")

            # Try to detect URL
            if not job.site_url:
                for pat in ready_patterns:
                    m = pat.search(line)
                    if m:
                        raw = m.group(1)
                        if raw.isdigit():
                            job.site_url = f"http://localhost:{raw}"
                        else:
                            job.site_url = raw
                        job.set_step("serve", "complete")
                        job.put("site_ready", url=job.site_url)
                        job.log(f"✓ Site ready at {job.site_url}", "serve")
                        break

        # If process ends without URL detected
        if proc.returncode is not None and proc.returncode != 0:
            job.set_step("serve", "failed")

    except Exception as exc:
        job.set_step("serve", "failed")
        job.log(f"✗ npm start failed: {exc}", "serve")


# ── main pipeline orchestrator ────────────────────────────────────────────────

def _run_pipeline(job: RunState) -> None:
    """Run all selected pipeline steps. Called in a background thread."""
    opts      = job.options
    course_id = job.course_id
    py        = sys.executable

    job.log(f"═══ Course Builder — Course ID: {course_id} ═══")
    job.log(f"Project root: {PROJECT_ROOT}")

    # ── Step 1: Export ────────────────────────────────────────────────────────
    if opts.get("do_export", True):
        ok = _run_step(
            job, "export",
            [py, str(PROJECT_ROOT / "export_course.py"),
             "--course_id", course_id],
        )
        if not ok:
            job.status = "failed"
            job.put("done", success=False,
                    message="Export from Canvas failed. Check your Canvas URL and token.")
            return
    else:
        job.set_step("export", "skipped")
        job.log("Export skipped (already exported).", "export")

    # ── Step 2: Run pipeline (evaluate + transform + load) ───────────────────
    if opts.get("do_pipeline", True):
        pipeline_cmd = [
            py, str(PROJECT_ROOT / "run_pipeline.py"),
            "--course_id", course_id,
            "--model_provider", opts.get("model_provider", "nebula"),
            "--workers", str(opts.get("workers", 3)),
        ]
        if opts.get("skip_extract", False):
            pipeline_cmd.append("--skip_extract")
        if opts.get("no_ai", False):
            pipeline_cmd.append("--no_ai")

        ok = _run_step(job, "pipeline", pipeline_cmd)
        if not ok:
            job.status = "failed"
            job.put("done", success=False,
                    message="AI pipeline failed. Check the logs above for details.")
            return
    else:
        job.set_step("pipeline", "skipped")

    # ── Step 3: Build HAX site ────────────────────────────────────────────────
    if opts.get("do_build", True):
        ok = _run_step(
            job, "build",
            [py, str(PROJECT_ROOT / "build_site.py"),
             "--course_id", course_id],
            capture_site_dir=True,
        )
        if not ok:
            job.status = "failed"
            job.put("done", success=False,
                    message="HAX site build failed. Check logs above.")
            return
    else:
        job.set_step("build", "skipped")

    # Resolve site directory
    if not job.site_dir:
        found = _find_site_dir(course_id)
        job.site_dir = str(found) if found else None

    if not job.site_dir:
        job.log("⚠ Could not detect HAX site directory.", "build")

    # ── Step 4: npm install ───────────────────────────────────────────────────
    if opts.get("do_npm_install", True) and job.site_dir:
        site_path = Path(job.site_dir)
        node_modules = site_path / "node_modules"
        if node_modules.exists():
            job.set_step("npm", "complete")
            job.log("node_modules already exists — skipping npm install.", "npm")
        else:
            ok = _run_step(job, "npm", ["npm", "install"], cwd=site_path)
            if not ok:
                job.log("⚠ npm install failed. The site may still work if node_modules exist.", "npm")
                # Don't stop — continue to serve
    else:
        job.set_step("npm", "skipped")

    # ── Step 5: npm start ─────────────────────────────────────────────────────
    if opts.get("do_serve", True) and job.site_dir:
        site_path = Path(job.site_dir)
        # Start in a separate thread so it doesn't block
        serve_thread = threading.Thread(
            target=_start_npm_serve, args=(job, site_path), daemon=True
        )
        serve_thread.start()
        # Wait up to 20 seconds for the site URL to appear
        for _ in range(40):
            if job.site_url:
                break
            time.sleep(0.5)
        if not job.site_url:
            job.site_url = "http://localhost:8080"
            job.log("Could not auto-detect site URL — try http://localhost:8080", "serve")
            job.set_step("serve", "complete")
    else:
        job.set_step("serve", "skipped")

    # ── Done ──────────────────────────────────────────────────────────────────
    job.status = "complete"
    job.put("done", success=True, site_dir=job.site_dir, site_url=job.site_url)
    job.log(f"═══ All steps complete! ═══")


def start_run(course_id: str, options: Dict) -> str:
    """Create a new run job and start it in a background thread. Returns run_id."""
    run_id = str(uuid.uuid4())
    job    = RunState(run_id, course_id, options)
    _jobs[run_id] = job

    job.thread = threading.Thread(target=_run_pipeline, args=(job,), daemon=True)
    job.thread.start()
    return run_id


def stop_npm(run_id: str) -> None:
    """Terminate the npm start process for a job if running."""
    job = _jobs.get(run_id)
    if job and job.npm_process:
        try:
            job.npm_process.terminate()
        except Exception:
            pass
