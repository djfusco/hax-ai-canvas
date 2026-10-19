"""Hosted persistence and bounded private source materialization."""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
import zipfile
from pathlib import PurePosixPath

from hosted_config import settings


class HostedBackend:
    def __init__(self, config=None):
        self.config = config or settings()
        from hosted_data import PostgresMentorStore, PostgresCredentialVault, PostgresUploads
        self.store = PostgresMentorStore(self.config.database_url)
        self.vault = PostgresCredentialVault(self.config.database_url, self.config.encryption_key)
        self.uploads = PostgresUploads(self.config.database_url)
        if self.config.auth_mode == "neon":
            from hosted_accounts import PostgresAccounts
            self.accounts = PostgresAccounts(self.config.database_url)
        from hosted_worker import VercelBlobStore
        self.blob = VercelBlobStore()

    @contextmanager
    def materialize(self, workspace):
        """Hydrate a prepared source with stable server-owned item identities."""
        source = workspace.get("source", {})
        pathname = source.get("prepared_blob")
        if not pathname:
            yield workspace
            return
        from course_package import import_course_package
        from imscc_content import prepare_import_content
        with tempfile.TemporaryDirectory(prefix="mentor-hosted-source-") as temp:
            directory = Path(temp)
            package = directory / "course.haxcourse"
            receipt = self.blob.download(pathname, package, max_bytes=64 * 1024 * 1024,
                                         expected_size=source.get("size"))
            if receipt["sha256"] != source["sha256"]:
                raise ValueError("Saved prepared content failed its integrity check.")
            summary = import_course_package(package, directory / "exports",
                                            namespace=source["course_id"])
            prepare_import_content(Path(summary["export_dir"]))
            yield {**workspace, "export_dir": summary["export_dir"]}

    def dispatch_upload(self, upload_id):
        import asyncio
        from vercel import workflow
        from hosted_workflows import prepare_upload
        run = asyncio.run(workflow.start(prepare_upload, upload_id=upload_id))
        return str(getattr(run, "run_id", getattr(run, "id", "")))

    def dispatch_mentor(self, owner, workspace, kind, provider="guided"):
        import asyncio
        from vercel import workflow
        from hosted_workflows import mentor_job
        run = asyncio.run(workflow.start(
            mentor_job, owner=owner, workspace_id=workspace["id"],
            job_id=workspace["active_job"]["id"], kind=kind, provider=provider,
        ))
        return str(getattr(run, "run_id", getattr(run, "id", "")))

    def verified_completion(self, upload_id):
        record = self.uploads.internal_get(upload_id)
        if record["status"] in ("ready", "prepared", "processing"):
            return record
        if record["status"] in ("failed", "canceled"):
            from hosted_worker import cleanup_upload
            cleanup_upload(record, self.uploads, self.blob)
            return record
        if record.get("workflow_id"):
            return record
        actual = self.blob.head(record["pathname"])
        if actual["pathname"] != record["pathname"] or actual["size"] != record["size"]:
            raise ValueError("The uploaded object does not match its authorized size/path.")
        record = self.uploads.completed(upload_id, actual)
        run_id = self.dispatch_upload(upload_id)
        if hasattr(self.uploads, "set_workflow"):
            from mentor_store import WorkspaceConflict
            try:
                self.uploads.set_workflow(upload_id, run_id)
            except WorkspaceConflict:
                pass  # A simultaneously verified callback already registered a run.
        return record

    def stream_artifact(self, workspace):
        """Yield authorized bytes directly; never expose store credentials."""
        from vercel.blob import sync as blob
        with blob.stream_blob(workspace["artifact"]["blob_path"], access="private") as download:
            for chunk in download:
                yield chunk


    def preview_stream(self, workspace, filename):
        parts = PurePosixPath(filename).parts
        if not parts or ".." in parts or filename.startswith("/") or "\\" in filename:
            raise ValueError("Invalid preview path.")
        with tempfile.TemporaryDirectory(prefix="private-course-preview-") as temp:
            package = Path(temp) / "course.zip"
            artifact = workspace["artifact"]
            receipt = self.blob.download(artifact["blob_path"], package,
                                         max_bytes=64 * 1024 * 1024,
                                         expected_size=artifact["size"])
            if receipt["sha256"] != artifact["sha256"]:
                raise ValueError("Artifact integrity verification failed.")
            with zipfile.ZipFile(package) as archive:
                info = archive.getinfo(filename)
                if info.file_size > 10 * 1024 * 1024:
                    raise ValueError("Preview entry is too large. Download the course instead.")
                with archive.open(info) as source:
                    while chunk := source.read(65536):
                        yield chunk


def validate_prepared_result(record, path, receipt):
    """Validate importability after the private result has been persisted."""
    path = Path(path)
    if record["kind"] == "material":
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Prepared material is too large.")
        material = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(material, dict) or material.get("role") != record["role"]
                or not isinstance(material.get("text"), str) or len(material["text"]) > 60000
                or material.get("kind") != "file"):
            raise ValueError("Invalid prepared material.")
        # Only the ingestion contract may cross into the workspace store.
        return {k: material[k] for k in
                ("name", "kind", "role", "text", "warnings", "sha256", "bytes") if k in material}
    from course_package import import_course_package
    from imscc_content import prepare_import_content
    from mentor_service import inventory
    namespace = "imscc-" + record["id"]
    with tempfile.TemporaryDirectory(prefix="verify-prepared-") as temp:
        imported = import_course_package(path, temp, namespace=namespace)
        prepare_import_content(Path(imported["export_dir"]))
        summary = inventory({"id": record["workspace_id"], "title": imported["course_name"],
                             "export_dir": imported["export_dir"], "source_type": "prepared", "materials": []})
    return {"source_type": "prepared", "course_id": namespace,
            "prepared_blob": receipt["pathname"], "sha256": receipt["sha256"],
            "size": receipt["size"], "warnings": imported.get("warnings", []),
            "inventory": summary}
