"""
setup_wizard.py
Environment checking and .env configuration for the Course Builder app.
"""

from __future__ import annotations

import importlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

# ── paths ─────────────────────────────────────────────────────────────────────
APP_DIR      = Path(__file__).parent.resolve()
PROJECT_ROOT = APP_DIR.parent.resolve()
ENV_FILE     = PROJECT_ROOT / ".env"
REQ_FILE     = PROJECT_ROOT / "requirements.txt"

# Keys that get masked in the UI by default
_SENSITIVE = re.compile(
    r"(key|token|secret|password|api|auth)", re.IGNORECASE
)

# All .env variables the pipeline needs
REQUIRED_ENV_VARS = [
    {"key": "CANVAS_URL",             "label": "Canvas URL",
     "help": "Your institution's Canvas URL, e.g. https://school.instructure.com",
     "validate": "url"},
    {"key": "CANVAS_TOKEN",           "label": "Canvas API Token",
     "help": "Generate in Canvas → Account → Settings → New Access Token",
     "validate": "nonempty"},
    {"key": "NEBULA_API_KEY",         "label": "NebulaONE API Key",
     "help": "Your NebulaONE subscription key (from your institution's AI portal)",
     "validate": "nonempty", "optional": True},
    {"key": "NEBULA_BASE_URL",        "label": "NebulaONE Base URL",
     "help": "e.g. https://apim-n1ai-use2-b53a23b6a.azure-api.net/anthropic",
     "validate": "url",      "optional": True},
    {"key": "NEBULA_MODEL",           "label": "NebulaONE Model",
     "help": "e.g. claude-haiku-4-5",
     "validate": "none",     "optional": True},
    {"key": "ANTHROPIC_API_KEY",      "label": "Anthropic API Key",
     "help": "From console.anthropic.com — leave blank if using NebulaONE",
     "validate": "none",     "optional": True},
    {"key": "OPENAI_API_KEY",         "label": "OpenAI API Key",
     "help": "From platform.openai.com — leave blank if using NebulaONE",
     "validate": "none",     "optional": True},
    {"key": "GEMINI_API_KEY",         "label": "Gemini API Key",
     "help": "From Google AI Studio — leave blank if using NebulaONE",
     "validate": "none",     "optional": True},
]

REQUIRED_SCRIPTS = [
    "export_course.py",
    "run_pipeline.py",
    "build_site.py",
]


# ── .env helpers ──────────────────────────────────────────────────────────────

def load_env_values() -> Dict[str, str]:
    """Load current .env values as a plain dict."""
    values: Dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                values[k.strip()] = v.strip()
    return values


def save_env_values(new_values: Dict[str, str]) -> None:
    """
    Merge new_values into the existing .env file.
    Existing keys are updated in-place; new keys are appended.
    Comments and formatting are preserved where possible.
    """
    existing_lines = []
    if ENV_FILE.exists():
        existing_lines = ENV_FILE.read_text(encoding="utf-8").splitlines()

    updated_keys: set = set()
    result_lines: List[str] = []

    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, _ = stripped.partition("=")
            k = k.strip()
            if k in new_values:
                result_lines.append(f"{k}={new_values[k]}")
                updated_keys.add(k)
                continue
        result_lines.append(line)

    # Append any new keys not already in file
    for k, v in new_values.items():
        if k not in updated_keys and v:
            result_lines.append(f"{k}={v}")

    ENV_FILE.write_text("\n".join(result_lines) + "\n", encoding="utf-8")


def validate_env_value(key: str, value: str, validate: str) -> Optional[str]:
    """Return error message string, or None if valid."""
    if validate == "nonempty" and not value.strip():
        return "This field is required."
    if validate == "url":
        if value.strip() and not value.strip().startswith(("http://", "https://")):
            return "Must be a URL starting with https://"
    return None


def mask_value(key: str, value: str) -> str:
    """Return masked version of a sensitive value for display."""
    if not value:
        return ""
    if _SENSITIVE.search(key):
        if len(value) <= 8:
            return "••••••••"
        return value[:4] + "••••••••" + value[-4:]
    return value


# ── system checks ─────────────────────────────────────────────────────────────

def _run(cmd: List[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as exc:
        return 1, str(exc)


def check_python() -> Dict:
    v = sys.version_info
    ok = v >= (3, 10)
    return {
        "ok":      ok,
        "label":   "Python 3.10+",
        "detail":  f"Python {v.major}.{v.minor}.{v.micro} found"
                   if ok else
                   f"Python {v.major}.{v.minor} found — 3.10 or newer required.",
        "fix":     None if ok else
                   "Download Python 3.10+ from https://www.python.org/downloads/",
    }


def check_pip() -> Dict:
    code, out = _run([sys.executable, "-m", "pip", "--version"])
    ok = code == 0
    return {
        "ok":     ok,
        "label":  "pip (Python package installer)",
        "detail": out.split("\n")[0] if ok else "pip not found.",
        "fix":    None if ok else
                  "Run: python -m ensurepip --upgrade",
    }


def check_packages() -> Dict:
    """Check each package listed in requirements.txt."""
    if not REQ_FILE.exists():
        return {"ok": True, "label": "Python packages", "detail": "No requirements.txt found.", "missing": []}

    missing = []
    for line in REQ_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Extract package name (strip version specifiers)
        pkg_name = re.split(r"[>=<!;\[]", line)[0].strip()
        import_name = pkg_name.replace("-", "_").lower()
        # Some packages have different import names
        import_aliases = {
            "pillow":         "PIL",
            "beautifulsoup4": "bs4",
            "python_dotenv":  "dotenv",
            "scikit_learn":   "sklearn",
            "pypdf":          "pypdf",
            "pypdf2":         "PyPDF2",   # Package name lowercases to pypdf2, import is PyPDF2
            "python_docx":    "docx",
            "python_pptx":    "pptx",
            "colorama":       "colorama",
            "uvicorn":        "uvicorn",
            "fastapi":        "fastapi",
        }
        try_name = import_aliases.get(import_name, import_name)
        try:
            importlib.import_module(try_name)
        except ImportError:
            missing.append(pkg_name)

    # Always check Flask separately (it's an app-only dep)
    try:
        importlib.import_module("flask")
    except ImportError:
        missing.append("flask")

    ok = len(missing) == 0
    return {
        "ok":      ok,
        "label":   "Python packages",
        "detail":  "All required packages installed." if ok
                   else f"Missing: {', '.join(missing)}",
        "missing": missing,
        "fix":     None if ok else
                   "Click 'Install Missing Packages' below, or run:\n"
                   f"pip install flask -r {REQ_FILE}",
    }


def check_env_file() -> Dict:
    exists = ENV_FILE.exists()
    return {
        "ok":     exists,
        "label":  ".env configuration file",
        "detail": f"Found at {ENV_FILE}" if exists
                  else "Not yet created — use the Configuration Wizard below.",
        "fix":    None if exists else
                  "Complete the Configuration Wizard on the Setup page.",
    }


def check_env_values() -> Dict:
    values = load_env_values()
    required = [v for v in REQUIRED_ENV_VARS if not v.get("optional")]
    missing = [v["key"] for v in required if not values.get(v["key"])]
    ok = len(missing) == 0
    return {
        "ok":     ok,
        "label":  "Canvas & API credentials",
        "detail": "All required credentials present." if ok
                  else f"Missing: {', '.join(missing)}",
        "fix":    None if ok else
                  "Complete the Configuration Wizard on the Setup page.",
    }


def check_node() -> Dict:
    path = shutil.which("node")
    if not path:
        return {
            "ok": False, "label": "Node.js",
            "detail": "Node.js not found.",
            "fix": "Download Node.js LTS from https://nodejs.org/en/download",
        }
    code, out = _run(["node", "--version"])
    return {
        "ok":     code == 0,
        "label":  "Node.js",
        "detail": f"Node {out} found at {path}",
        "fix":    None,
    }


def check_npm() -> Dict:
    path = shutil.which("npm")
    if not path:
        return {
            "ok": False, "label": "npm",
            "detail": "npm not found. It is included with Node.js.",
            "fix": "Download Node.js LTS (includes npm) from https://nodejs.org/en/download",
        }
    code, out = _run(["npm", "--version"])
    return {
        "ok":     code == 0,
        "label":  "npm (Node package manager)",
        "detail": f"npm {out} found",
        "fix":    None,
    }


def check_scripts() -> Dict:
    missing = [s for s in REQUIRED_SCRIPTS if not (PROJECT_ROOT / s).exists()]
    ok = len(missing) == 0
    return {
        "ok":     ok,
        "label":  "Pipeline scripts",
        "detail": "All pipeline scripts found." if ok
                  else f"Missing scripts: {', '.join(missing)}",
        "fix":    None if ok else
                  f"Make sure these files exist in {PROJECT_ROOT}",
    }


def run_all_checks() -> Dict:
    """Run all checks and return a structured result dict."""
    checks = {
        "python":   check_python(),
        "pip":      check_pip(),
        "packages": check_packages(),
        "env_file": check_env_file(),
        "env_vals": check_env_values(),
        "node":     check_node(),
        "npm":      check_npm(),
        "scripts":  check_scripts(),
    }
    # Required checks that must pass before running
    required_keys = ["python", "pip", "packages", "env_file", "env_vals",
                     "node", "npm", "scripts"]
    all_ok = all(checks[k]["ok"] for k in required_keys)
    return {"checks": checks, "all_ok": all_ok}


# ── connection tests ──────────────────────────────────────────────────────────

def test_canvas_connection() -> dict:
    """Test Canvas API credentials by hitting /api/v1/users/self."""
    import ssl
    import urllib.request
    import urllib.error
    import json as _json

    values = load_env_values()
    url   = values.get("CANVAS_URL", "").rstrip("/")
    token = values.get("CANVAS_TOKEN", "")

    if not url or not token:
        return {"ok": False, "message": "Canvas URL or Token is not configured."}

    # Build an SSL context that uses certifi's CA bundle (fixes macOS Python installs)
    try:
        import certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ssl_ctx = ssl.create_default_context()

    try:
        req = urllib.request.Request(
            f"{url}/api/v1/users/self",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=15, context=ssl_ctx) as resp:
            data = _json.loads(resp.read())
            name = data.get("name", "Unknown")
            return {"ok": True, "message": f"Connected as {name}"}
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return {"ok": False, "message": "Invalid token — Canvas returned 401 Unauthorized."}
        return {"ok": False, "message": f"Canvas returned HTTP {exc.code}."}
    except Exception as exc:
        return {"ok": False, "message": f"Connection failed: {exc}"}


def test_ai_connection(provider: str) -> dict:
    """Test an AI provider by sending a tiny prompt."""
    values = load_env_values()

    try:
        if provider in ("nebula", "anthropic"):
            try:
                from anthropic import Anthropic
            except ImportError:
                return {"ok": False, "message": "anthropic package is not installed."}

            if provider == "nebula":
                key = values.get("NEBULA_API_KEY", "")
                url = values.get("NEBULA_BASE_URL", "")
                model = values.get("NEBULA_MODEL", "claude-haiku-4-5")
                if not key or not url:
                    return {"ok": False, "message": "NebulaONE API Key or Base URL is not configured."}
                client = Anthropic(api_key=key, base_url=url)
            else:
                key = values.get("ANTHROPIC_API_KEY", "")
                model = "claude-haiku-4-5"
                if not key:
                    return {"ok": False, "message": "Anthropic API Key is not configured."}
                client = Anthropic(api_key=key)

            r = client.messages.create(
                model=model,
                max_tokens=16,
                messages=[{"role": "user", "content": "Say hello in 3 words."}],
                timeout=20,
            )
            reply = r.content[0].text.strip()
            return {"ok": True, "message": f"Connected — model replied: \"{reply}\""}

        elif provider == "openai":
            try:
                from openai import OpenAI
            except ImportError:
                return {"ok": False, "message": "openai package is not installed."}

            key = values.get("OPENAI_API_KEY", "")
            if not key:
                return {"ok": False, "message": "OpenAI API Key is not configured."}

            client = OpenAI(api_key=key)
            r = client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=16,
                messages=[{"role": "user", "content": "Say hello in 3 words."}],
                timeout=20,
            )
            reply = r.choices[0].message.content.strip()
            return {"ok": True, "message": f"Connected — model replied: \"{reply}\""}

        elif provider == "gemini":
            try:
                from google import genai as _genai
            except ImportError:
                return {"ok": False, "message": "google-generativeai package is not installed."}

            key = values.get("GEMINI_API_KEY", "")
            if not key:
                return {"ok": False, "message": "Gemini API Key is not configured."}

            client = _genai.Client(api_key=key)
            r = client.models.generate_content(
                model="gemini-2.5-flash",
                contents="Say hello in 3 words.",
            )
            reply = r.text.strip()
            return {"ok": True, "message": f"Connected — model replied: \"{reply}\""}

        else:
            return {"ok": False, "message": f"Unknown provider: {provider}"}

    except Exception as exc:
        msg = str(exc)
        # Trim very long error messages
        if len(msg) > 300:
            msg = msg[:300] + "…"
        return {"ok": False, "message": f"Connection failed: {msg}"}


def install_packages() -> tuple[bool, str]:
    """Attempt pip install of flask + requirements.txt. Returns (success, output)."""
    cmds = [
        [sys.executable, "-m", "pip", "install", "flask"],
    ]
    if REQ_FILE.exists():
        cmds.append([sys.executable, "-m", "pip", "install", "-r", str(REQ_FILE)])

    output = ""
    for cmd in cmds:
        code, out = _run(cmd)
        output += out + "\n"
        if code != 0:
            return False, output
    return True, output
