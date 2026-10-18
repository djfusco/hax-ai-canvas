"""Pinned Vercel Python Workflow entrypoints; only small IDs enter run logs."""

from vercel import workflow

wf = workflow.Workflows()


@wf.step(max_retries=2)
async def reserve(upload_id: str, run_id: str):
    from hosted_jobs import claim_upload
    return claim_upload(upload_id, run_id)


@wf.step(max_retries=2)
async def start(upload_id: str, attempt: int):
    from hosted_jobs import start_preparation
    try:
        return start_preparation(upload_id, attempt)
    except Exception:
        raise workflow.RetryableError("Preparation environment temporarily unavailable.", retry_after="10 seconds") from None


@wf.step(max_retries=2)
async def poll(upload_id: str, attempt: int):
    from hosted_jobs import poll_preparation
    try:
        return poll_preparation(upload_id, attempt)
    except Exception:
        raise workflow.RetryableError("Preparation step temporarily unavailable.", retry_after="10 seconds") from None


@wf.step(max_retries=2)
async def fail(upload_id: str, attempt: int):
    from hosted_jobs import fail_preparation
    return fail_preparation(upload_id, attempt)


@wf.step(max_retries=2)
async def stop(upload_id: str, attempt: int):
    from hosted_jobs import stop_preparation
    return stop_preparation(upload_id, attempt)


@wf.workflow
async def prepare_upload(*, upload_id: str):
    claim = await reserve(upload_id, workflow.get_workflow_metadata().run_id)
    if claim is None:
        return {"status": "claimed_elsewhere_or_terminal"}
    attempt = claim["attempt"]
    try:
        initial = await start(upload_id, attempt)
        if initial.get("done"):
            return {"status": initial["status"]}
        for _ in range(90):
            status = await poll(upload_id, attempt)
            if status["done"]:
                return {"status": status["status"]}
            await workflow.sleep("15 seconds")
        return await fail(upload_id, attempt)
    except Exception:
        return await fail(upload_id, attempt)
    finally:
        await stop(upload_id, attempt)


@wf.step(max_retries=1)
async def mentor_step(owner: str, workspace_id: str, job_id: str, kind: str, provider: str):
    from hosted_jobs import execute_mentor
    return execute_mentor(owner, workspace_id, job_id, kind, provider)


@wf.workflow
async def mentor_job(*, owner: str, workspace_id: str, job_id: str, kind: str, provider: str):
    return await mentor_step(owner, workspace_id, job_id, kind, provider)


@wf.step(max_retries=2)
async def cleanup_step():
    from hosted_jobs import cleanup_batch
    return cleanup_batch()


@wf.workflow
async def cleanup_raw_uploads():
    return await cleanup_step()
