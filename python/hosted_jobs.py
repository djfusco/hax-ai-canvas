"""Testable hosted job operations; Workflow arguments contain IDs, not secrets."""

from pathlib import Path
import tempfile
import time

from hosted_backend import HostedBackend, validate_prepared_result
from mentor_store import WorkspaceConflict


def claim_upload(upload_id, run_id, backend=None):
    service = backend or HostedBackend()
    record = service.uploads.claim(upload_id, lease_seconds=2400, worker_id=run_id)
    return {"attempt": record["attempts"]} if record else None


def start_preparation(upload_id, attempt, backend=None, controller=None):
    service = backend or HostedBackend()
    from hosted_worker import VercelPreparationWorker, SandboxStartError
    worker = controller or VercelPreparationWorker(blob=service.blob)
    record = service.uploads.internal_get(upload_id)
    if record["attempts"] != attempt or record["status"] not in ("processing", "prepared"):
        return {"done": True, "status": record["status"]}
    if (record.get("sandbox") or {}).get("process_id"):
        return {"done": False}
    if not record.get("sandbox"):
        # Reserve the exact cleanup handle BEFORE a remote VM can exist. A
        # cancellation, reclaim, or lost DB acknowledgement after create must
        # not leave an untracked resource. An empty process ID is retryable.
        service.uploads.save_sandbox(upload_id, {
            "name": "hax-" + upload_id + "-" + str(attempt),
            "process_id": "", "phase": "setup", "started_at": time.time(),
        }, attempt=attempt)
    try:
        metadata = worker.start({**record, "sandbox": None})
        service.uploads.save_sandbox(upload_id, metadata, attempt=attempt)
    except SandboxStartError as exc:
        if exc.cleanup_pending:
            service.uploads.save_sandbox(upload_id, exc.sandbox, attempt=attempt)
        raise RuntimeError("Preparation environment could not start.") from None
    return {"done": False}


def _validate_persisted(service, record, local_result, receipt):
    with tempfile.TemporaryDirectory(prefix="persisted-preparation-") as temp:
        saved = Path(temp) / "result"
        actual = service.blob.download(receipt["pathname"], saved,
                                       max_bytes=64 * 1024 * 1024,
                                       expected_size=receipt["size"])
        if actual["sha256"] != receipt["sha256"]:
            raise ValueError("Persisted prepared output failed integrity validation.")
        return validate_prepared_result(record, saved, receipt)


def poll_preparation(upload_id, attempt, backend=None, controller=None):
    service = backend or HostedBackend()
    from hosted_worker import VercelPreparationWorker, cleanup_upload, finish
    worker = controller or VercelPreparationWorker(blob=service.blob)
    record = service.uploads.internal_get(upload_id)
    if record["attempts"] != attempt or record["status"] in ("ready", "failed", "canceled"):
        return {"done": True, "status": record["status"]}
    if record["status"] == "prepared":
        # Recover the precise persist/attach boundary after a Function restart.
        receipt = record["prepared"]
        source = _validate_persisted(service, record, None, receipt)
        service.uploads.attach(upload_id, source, attempt=attempt)
        cleanup_upload(service.uploads.internal_get(upload_id), service.uploads, service.blob)
        return {"done": True, "status": "ready"}
    result = worker.poll(record)
    if not result["done"]:
        service.uploads.save_sandbox(upload_id, result["sandbox"], attempt=attempt)
        return {"done": False}
    if not result.get("success"):
        service.uploads.fail(upload_id, attempt=attempt)
        return {"done": True, "status": "failed"}
    path = Path(result["local_result"])
    try:
        finish(record, path, service.uploads,
               lambda rec, local, receipt: _validate_persisted(service, rec, local, receipt),
               blob=service.blob)
    finally:
        path.unlink(missing_ok=True)
    return {"done": True, "status": "ready"}


def fail_preparation(upload_id, attempt, backend=None):
    service = backend or HostedBackend()
    try:
        service.uploads.fail(upload_id, attempt=attempt)
    except WorkspaceConflict:
        pass
    return {"status": "failed"}


def stop_preparation(upload_id, attempt, backend=None, controller=None):
    service = backend or HostedBackend()
    from hosted_worker import VercelPreparationWorker
    worker = controller or VercelPreparationWorker(blob=service.blob)
    record = service.uploads.internal_get(upload_id)
    if record["attempts"] != attempt:
        return {"status": "superseded"}
    try:
        worker.destroy(record)
        for previous in record.get("previous_sandboxes", []):
            worker.destroy({**record, "sandbox": previous})
    except Exception:
        service.uploads.record_sandbox_cleanup(upload_id, False)
        return {"status": "cleanup_pending"}
    service.uploads.record_sandbox_cleanup(upload_id, True)
    return {"status": "stopped"}


def cleanup_batch(backend=None, controller=None, limit=50):
    service = backend or HostedBackend()
    from hosted_worker import cleanup_upload
    done, pending = 0, 0
    for record in service.uploads.cleanup_due(limit=limit):
        result = cleanup_upload(record, service.uploads, service.blob)
        if result.get("deleted"):
            done += 1
        elif result.get("pending"):
            pending += 1
        if ((record.get("sandbox") or record.get("previous_sandboxes"))
                and record.get("sandbox_cleanup_status") != "deleted"):
            outcome = stop_preparation(record["id"], record["attempts"], service, controller)
            pending += outcome.get("status") == "cleanup_pending"
    return {"raw_deleted": done, "cleanup_pending": pending}


def _redact(service, owner, value):
    keys = [saved["api_key"] for item in service.vault.status(owner)
            if (saved := service.vault.get(owner, item["provider"]))]
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


def execute_mentor(owner, workspace_id, job_id, kind, provider="guided", backend=None):
    service = backend or HostedBackend()
    from mentor_service import generate_draft, interview, review_draft, build_artifact
    from ai_client import AIClient
    workspace = service.store.get(owner, workspace_id)
    active = workspace.get("active_job")
    if not active or active["id"] != job_id or active["kind"] != kind:
        return {"status": "already_finished"}
    ai = None
    try:
        if provider != "guided" and kind != "build":
            if provider not in ("nebula", "anthropic", "openai", "gemini"):
                raise ValueError("Unsupported provider.")
            saved = service.vault.get(owner, provider)
            if (not saved or not active.get("model") or active.get("provider") != provider
                    or active.get("credential_version") != saved.get("version")):
                raise ValueError("Provider credentials expired or were removed.")
            ai = AIClient(provider, api_key=saved["api_key"], model=active["model"],
                          base_url=saved.get("base_url") or None, isolated_credentials=True,
                          capabilities=active.get("model_capabilities"))
        with service.materialize(workspace) as hydrated:
            hydrated = _redact(service, owner, hydrated)
            if kind == "analysis":
                draft = _redact(service, owner, generate_draft(hydrated, ai))
                service.store.finish_analysis(owner, workspace_id, job_id, draft)
            elif kind in ("interview", "review"):
                message = next(m["content"] for m in reversed(workspace["messages"])
                               if m["role"] == "user" and m["kind"] == kind)
                if kind == "interview":
                    reply = interview(hydrated, message, ai)
                    service.store.finish_interview(owner, workspace_id, job_id)
                else:
                    reply, revised = review_draft(hydrated, message, ai)
                    service.store.finish_review(owner, workspace_id, job_id,
                                                _redact(service, owner, revised))
                service.store.add_message(owner, workspace_id, "assistant",
                                          _redact(service, owner, reply), kind)
            elif kind == "build":
                with tempfile.TemporaryDirectory(prefix="hosted-build-") as temp:
                    artifact = build_artifact(hydrated, Path(temp) / "site")
                    if Path(artifact["path"]).stat().st_size > 64 * 1024 * 1024:
                        raise ValueError("Built artifact exceeds the 64 MiB beta limit.")
                    path = f"artifacts/{owner}/{workspace_id}/{job_id}/course.zip"
                    receipt = service.blob.put_file(path, artifact["path"])
                    service.store.finish_build(owner, workspace_id, job_id, {
                        "blob_path": path, "size": receipt["size"], "sha256": receipt["sha256"],
                        "items": artifact["items"], "draft_revision": workspace["draft_revision"],
                        "kind": artifact["kind"],
                        "preview_style_hash": artifact.get("preview_style_hash"),
                    })
            else:
                raise ValueError("Unsupported mentor job.")
        return {"status": "completed"}
    except Exception as exc:
        from ai_client import AIResponseError
        from mentor_assessment import AssessmentError
        from model_capabilities import ModelCapabilityError
        message = (_redact(service, owner, str(exc))[:500]
                   if isinstance(exc, (AIResponseError, AssessmentError, ModelCapabilityError))
                   else None)
        try:
            service.store.fail_job(owner, workspace_id, job_id, "Hosted processing failed.",
                                   public_message=message)
        except WorkspaceConflict:
            pass
        return {"status": "failed"}
    finally:
        if ai is not None:
            ai.close()
