"""Vercel Python entrypoint; local desktop operation remains python/app/app.py."""

import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "python/app"), str(ROOT / "python"), str(ROOT)]

from app import app
from flask import Response, jsonify, request
from hosted_config import hosted_mode, settings, HostedSetupError


@app.get("/healthz")
def health():
    try:
        settings()
    except HostedSetupError as exc:
        return jsonify({"configured": False, "error": str(exc)}), 503
    return jsonify({"configured": True, "mode": "hosted", "services_verified": False})


class WorkflowRequest:
    def __init__(self):
        self.method = request.method
        self.url = request.full_path.rstrip("?")
        self.headers = request.headers
        self._body = request.get_data(cache=False)

    async def aiter_bytes(self, chunk_size=None):
        size = chunk_size or 65536
        for index in range(0, len(self._body), size):
            yield self._body[index:index + size]


@app.route("/.well-known/workflow/v1/flow", methods=["POST", "GET", "HEAD", "OPTIONS"])
def workflow_delivery():
    # The official SDK authenticates queue messages. Never replace this handler
    # with a public arbitrary job executor or pass provider credentials into it.
    from workflows import wf
    response = asyncio.run(wf.http_handler(WorkflowRequest()))
    return Response(response.body, status=response.status, headers=response.headers)


@app.get("/.well-known/workflow/v1/manifest.json")
def workflow_manifest():
    from workflows import wf
    response = asyncio.run(wf.manifest_handler(WorkflowRequest()))
    return Response(response.body, status=response.status, headers=response.headers)
