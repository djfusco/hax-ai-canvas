"""Vercel Python entrypoint; local development runs python/app/app.py."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "python/app"), str(ROOT / "python"), str(ROOT)]

from app import app  # noqa: E402
from flask import jsonify  # noqa: E402
from hosted_config import settings, HostedSetupError  # noqa: E402


@app.get("/healthz")
def health():
    try:
        settings()
    except HostedSetupError as exc:
        return jsonify({"configured": False, "error": str(exc)}), 503
    return jsonify({"configured": True, "mode": "hosted"})
