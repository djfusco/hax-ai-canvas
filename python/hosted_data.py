"""Hosted persistence. No service calls are made except explicit DB operations.

Tables deliberately retain JSON TEXT workspace state so MentorStore's revision,
approval, interview and review transitions remain the single source of truth.
The injected connect callable follows psycopg.connect's signature.
"""

from contextlib import contextmanager
import json
from pathlib import PurePosixPath
import re
import time
from urllib.parse import quote, urlsplit
from uuid import uuid4

from cryptography.fernet import Fernet

from mentor_credentials import CredentialVault
from mentor_materials import EXTENSIONS, MAX_FILE_BYTES, ROLES, _name
from mentor_store import (
    MentorStore, WorkspaceConflict, WorkspaceNotFound, _now, _object, _revision,
)


def _identifier(value, label="identifier"):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("Invalid " + label + ".")
    return value


class _SQL:
    """Adapt only our inherited, static SQLite parameterized statements."""

    def __init__(self, connection, locking=False):
        self.connection = connection
        self.locking = locking

    def execute(self, sql, params=()):
        sql = sql.replace("?", "%s")
        if self.locking and sql.startswith(
            ("SELECT state FROM mentor_workspaces", "SELECT owner, state FROM mentor_workspaces")
        ):
            sql += " FOR UPDATE"
        return self.connection.execute(sql, params)


class _Postgres:
    def _configure(self, dsn, connect):
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("A PostgreSQL connection string is required.")
        self._dsn = dsn
        self._connect = connect

    @contextmanager
    def _session(self, locking=False):
        connect = self._connect
        if connect is None:
            import psycopg
            connect = psycopg.connect
        # Keyword overrides also prohibit a DSN from downgrading transport TLS
        # or extending connection attempts indefinitely.
        conn = connect(self._dsn, sslmode="require", connect_timeout=10)
        try:
            yield _SQL(conn, locking)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


class PostgresMentorStore(_Postgres, MentorStore):
    def __init__(self, dsn, connect=None):
        self._configure(dsn, connect)
        with self._session() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS mentor_workspaces ("
                "id TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS mentor_workspaces_owner "
                "ON mentor_workspaces(owner)"
            )

    def _connection(self):
        return self._session()

    def _transaction(self):
        return self._session(locking=True)


class PostgresCredentialVault(_Postgres, CredentialVault):
    def __init__(self, dsn, encryption_key, clock=time.time, connect=None):
        if not encryption_key:
            raise ValueError("A server-managed Fernet encryption key is required.")
        self.cipher = Fernet(
            encryption_key.encode() if isinstance(encryption_key, str) else encryption_key
        )
        self.clock = clock
        self._configure(dsn, connect)
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS hax_credentials ("
                "owner TEXT NOT NULL, provider TEXT NOT NULL, sealed BYTEA NOT NULL, "
                "expires DOUBLE PRECISION NOT NULL, remembered INTEGER NOT NULL, "
                "PRIMARY KEY(owner, provider))"
            )

    @contextmanager
    def _db(self):
        with self._session() as db:
            yield _VaultSQL(db)

    def status(self, owner):
        with self._db() as db:
            db.execute(
                "DELETE FROM credentials WHERE owner=? AND expires <= ?",
                (owner, self.clock()),
            )
            rows = db.execute(
                "SELECT provider, expires, remembered FROM credentials WHERE owner=?",
                (owner,),
            ).fetchall()
        return [
            dict(provider=p, configured=True, expires_at=expiry, remembered=bool(remember))
            for p, expiry, remember in rows
        ]


class _VaultSQL:
    def __init__(self, db):
        self.db = db

    def execute(self, sql, params=()):
        sql = sql.replace("credentials", "hax_credentials")
        # Constructor already uses the fully qualified hosted table name.
        sql = sql.replace("hax_hax_credentials", "hax_credentials")
        if sql.startswith("INSERT OR REPLACE INTO"):
            sql = sql.replace("INSERT OR REPLACE INTO", "INSERT INTO")
            sql += (
                " ON CONFLICT (owner, provider) DO UPDATE SET "
                "sealed=EXCLUDED.sealed, expires=EXCLUDED.expires, "
                "remembered=EXCLUDED.remembered"
            )
        return self.db.execute(sql, params)


class PostgresUploads(_Postgres):
    """Privileged upload ledger; only get/create/cancel accept browser owners.

    Internal mutation methods must never be exposed as ownerless public routes.
    Workers should pass ``attempt=claim['attempts']`` to fence a reclaimed lease.
    """

    def __init__(self, dsn, clock=time.time, connect=None):
        self._configure(dsn, connect)
        self.clock = clock
        self.store = PostgresMentorStore(dsn, connect=connect)
        with self._session() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS hax_uploads ("
                "id TEXT PRIMARY KEY, owner TEXT NOT NULL, workspace_id TEXT NOT NULL, "
                "state TEXT NOT NULL)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS hax_uploads_owner ON hax_uploads(owner)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS hax_uploads_workspace "
                "ON hax_uploads(workspace_id)"
            )
            # Stable owner row serializes intent quotas across different workspaces.
            db.execute(
                "CREATE TABLE IF NOT EXISTS hax_upload_owners (owner TEXT PRIMARY KEY)"
            )

    @staticmethod
    def _load(db, id, owner=None):
        _identifier(id, "upload ID")
        sql = "SELECT state FROM hax_uploads WHERE id = ?"
        params = (id,)
        if owner is not None:
            _identifier(owner, "owner")
            sql += " AND owner = ?"
            params += (owner,)
        row = db.execute(sql + " FOR UPDATE", params).fetchone()
        if row is None:
            raise WorkspaceNotFound("Upload not found.")
        return json.loads(row[0])

    def _write(self, db, record):
        record["updated_at"] = self.clock()
        db.execute(
            "UPDATE hax_uploads SET state = ? WHERE id = ? AND owner = ?",
            (json.dumps(record, allow_nan=False), record["id"], record["owner"]),
        )

    def _workspace(self, db, record):
        return self.store._load(db, record["owner"], record["workspace_id"])

    def _current(self, state, record):
        self.store._exact(state, record["input_revision"])
        self.store._active(state, record["id"], "prepare")

    def _lease(self, state, record, attempt=None):
        self._current(state, record)
        if (record["status"] not in ("processing", "prepared")
                or record["lease_until"] is None
                or record["lease_until"] <= self.clock()
                or record["expires_at"] <= self.clock()
                or (attempt is not None and attempt != record["attempts"])):
            raise WorkspaceConflict("Preparation lease is no longer current.")

    def create(self, owner, workspace_id, revision, filename, size,
               kind="course", role="reference"):
        _identifier(owner, "owner")
        _identifier(workspace_id, "workspace ID")
        _revision(revision)
        filename = _name(filename)
        extension = PurePosixPath(filename).suffix.lower()
        if kind == "course":
            limits = {".imscc": 8 * 1024**3, ".haxcourse": 64 * 1024**2}
            if extension not in limits:
                raise ValueError("Upload an IMSCC or HAXCOURSE package.")
            limit = limits[extension]
        elif kind == "material":
            if extension not in EXTENSIONS or role not in ROLES:
                raise ValueError("Unsupported material type or role.")
            limit = MAX_FILE_BYTES
        else:
            raise ValueError("Invalid upload kind.")
        if type(size) is not int or not 0 < size <= limit:
            raise ValueError("Invalid upload size.")
        if role not in ROLES:
            raise ValueError("Invalid upload role.")
        now = self.clock()
        id = uuid4().hex
        record = dict(
            id=id, owner=owner, workspace_id=workspace_id, input_revision=revision,
            pathname=f"raw/{owner}/{workspace_id}/{id}/source{extension}",
            filename=filename, size=size, kind=kind, role=role,
            status="awaiting_upload", cleanup_status="none",
            expires_at=now + 86400, token_expires_at=now + 3600,
            attempts=0, lease_until=None, prepared=None, sandbox=None,
            created_at=now, updated_at=now,
        )
        with self._session(locking=True) as db:
            db.execute(
                "INSERT INTO hax_upload_owners (owner) VALUES (?) ON CONFLICT DO NOTHING",
                (owner,),
            )
            db.execute(
                "SELECT owner FROM hax_upload_owners WHERE owner = ? FOR UPDATE", (owner,)
            ).fetchone()
            rows = db.execute(
                "SELECT state FROM hax_uploads WHERE owner = ?", (owner,)
            ).fetchall()
            prior = [json.loads(row[0]) for row in rows]
            pending = sum(r["status"] in (
                "awaiting_upload", "uploaded", "processing", "prepared"
            ) and r["expires_at"] > now for r in prior)
            if pending >= 3 or sum(r["created_at"] > now - 3600 for r in prior) >= 20:
                raise WorkspaceConflict("Upload quota exceeded. Try again later.")
            state = self.store._load(db, owner, workspace_id)
            self.store._exact(state, revision)
            self.store._idle(state)
            if kind == "material" and len(state["materials"]) >= 30:
                raise WorkspaceConflict("Use at most 30 supplemental sources in this workspace.")
            job = dict(
                id=id, kind="prepare", revision=revision,
                draft_revision=state["draft_revision"], status="running",
                created_at=_now(), finished_at=None,
            )
            state["active_job"] = job
            state["jobs"].append(job)
            state["stage"] = "uploading"
            self.store._write(db, owner, state)
            db.execute(
                "INSERT INTO hax_uploads (id, owner, workspace_id, state) VALUES (?, ?, ?, ?)",
                (id, owner, workspace_id, json.dumps(record, allow_nan=False)),
            )
        return record

    def get(self, owner, id):
        with self._session() as db:
            return self._load(db, id, owner)

    def internal_get(self, id):
        with self._session() as db:
            return self._load(db, id)

    @staticmethod
    def _blob(blob, pathname, size):
        if not isinstance(blob, dict) or blob.get("pathname") != pathname:
            raise ValueError("Upload receipt does not match the reserved pathname.")
        if type(blob.get("size")) is not int or blob["size"] != size:
            raise ValueError("Upload receipt size does not match.")
        url = blob.get("url")
        if (not isinstance(url, str) or len(url) > 4096
                or any(ord(c) < 33 or ord(c) == 127 for c in url) or "\\" in url):
            raise ValueError("Invalid private Blob receipt.")
        try:
            parsed = urlsplit(url)
            valid = (
                parsed.scheme == "https"
                and re.fullmatch(r"[a-z0-9-]+\.private\.blob\.vercel-storage\.com",
                                 parsed.hostname or "")
                and parsed.netloc == parsed.hostname
                and parsed.path == "/" + quote(pathname, safe="/")
                and not parsed.query and not parsed.fragment
            )
        except ValueError:
            valid = False
        etag = blob.get("etag")
        if not valid or (etag is not None and (
                not isinstance(etag, str) or len(etag) > 256
                or any(ord(c) < 32 for c in etag))):
            raise ValueError("Invalid private Blob receipt.")
        return dict(url=url, pathname=pathname, size=size, etag=etag)

    def completed(self, id, blob):
        with self._session(locking=True) as db:
            record = self._load(db, id)
            receipt = self._blob(blob, record["pathname"], record["size"])
            if record.get("blob") is not None:
                if record["blob"] != receipt:
                    raise WorkspaceConflict("Upload receipt conflicts with the saved receipt.")
                return record
            if record["status"] != "awaiting_upload" or record["expires_at"] <= self.clock():
                raise WorkspaceConflict("Upload is no longer awaiting a receipt.")
            self._current(self._workspace(db, record), record)
            record.update(blob=receipt, status="uploaded")
            self._write(db, record)
        return record

    def claim(self, id, lease_seconds=1200, *, worker_id=None):
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("Invalid preparation lease.")
        if worker_id is not None:
            _identifier(worker_id, "worker ID")
        with self._session(locking=True) as db:
            record = self._load(db, id)
            now = self.clock()
            if (record["status"] not in ("uploaded", "processing", "prepared")
                    or record["expires_at"] <= now):
                return None
            try:
                state = self._workspace(db, record)
                self._current(state, record)
            except (WorkspaceConflict, WorkspaceNotFound):
                return None
            if record["lease_until"] is not None and record["lease_until"] > now:
                if worker_id is not None and record.get("worker_id") == worker_id:
                    return record
                return None
            if record["sandbox"] is not None:
                record.setdefault("previous_sandboxes", []).append(record["sandbox"])
            record.update(
                status="processing", attempts=record["attempts"] + 1,
                lease_until=min(now + lease_seconds, record["expires_at"]),
                prepared=None, sandbox=None, worker_id=worker_id,
            )
            state["stage"] = "preparing"
            self.store._write(db, record["owner"], state)
            self._write(db, record)
        return record

    def save_sandbox(self, id, metadata, *, attempt=None):
        metadata = _object(metadata)
        if "name" in metadata:
            _identifier(metadata["name"], "sandbox name")
        if "phase" in metadata and not isinstance(metadata["phase"], str):
            raise ValueError("Invalid sandbox phase.")
        with self._session(locking=True) as db:
            record = self._load(db, id)
            self._lease(self._workspace(db, record), record, attempt)
            previous = record["sandbox"] or {}
            if (previous.get("name") is not None and metadata.get(
                    "name", previous["name"]) != previous["name"]):
                raise WorkspaceConflict("Sandbox identity cannot change within an attempt.")
            phases = {
                "setup": 0, "download": 1, "locking": 2,
                "prepare": 3, "parse": 3, "complete": 4,
            }
            old_phase = previous.get("phase")
            new_phase = metadata.get("phase", old_phase)
            if (old_phase in phases and new_phase in phases
                    and phases[new_phase] < phases[old_phase]):
                raise WorkspaceConflict("Sandbox phase cannot move backwards.")
            record["sandbox"] = dict(previous, **metadata)
            self._write(db, record)
        return record

    def prepared(self, id, receipt, *, attempt=None):
        receipt = _object(receipt)
        with self._session(locking=True) as db:
            record = self._load(db, id)
            self._lease(self._workspace(db, record), record, attempt)
            expected = (
                f"prepared/{record['owner']}/{record['workspace_id']}/{id}/"
                f"attempt-{record['attempts']}/"
            )
            pathname = receipt.get("pathname")
            if (not isinstance(pathname, str) or not pathname.startswith(expected)
                    or pathname[len(expected):] != (
                        "course.haxcourse" if record["kind"] == "course" else "result.json"
                    )
                    or type(receipt.get("size")) is not int or receipt["size"] <= 0
                    or not isinstance(receipt.get("sha256"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", receipt["sha256"])):
                raise ValueError("Invalid prepared receipt.")
            if record["kind"] == "course":
                if not pathname.endswith(".haxcourse"):
                    raise ValueError("Invalid prepared course package.")
                course_id = "imscc-" + id
                if receipt.get("course_id", course_id) != course_id:
                    raise ValueError("Invalid prepared course ID.")
                receipt["course_id"] = course_id
            elif not pathname.endswith(".json"):
                raise ValueError("Invalid prepared material receipt.")
            if record["prepared"] is not None and record["prepared"] != receipt:
                raise WorkspaceConflict("Prepared receipt already exists.")
            record.update(prepared=receipt, status="prepared", cleanup_status="pending")
            self._write(db, record)
        return record

    def attach(self, id, source, *, attempt=None):
        source = _object(source)
        with self._session(locking=True) as db:
            record = self._load(db, id)
            state = self._workspace(db, record)
            if record["status"] == "ready":
                # An idempotent replay must not overwrite a later user edit.
                return state
            self._lease(state, record, attempt)
            receipt = record["prepared"]
            if record["status"] != "prepared" or not receipt:
                raise WorkspaceConflict("A prepared receipt is required.")
            if record["kind"] == "course":
                expected = dict(
                    source_type="prepared", course_id="imscc-" + id,
                    prepared_blob=receipt["pathname"], sha256=receipt["sha256"],
                    size=receipt["size"],
                )
                if any(source.get(key) != value for key, value in expected.items()):
                    raise ValueError("Prepared source does not match its receipt.")
                source["export_dir"] = None
                state.update(
                    source=source, source_type="prepared",
                    course_id=expected["course_id"], export_dir=None,
                )
            else:
                if len(state["materials"]) >= 30:
                    raise WorkspaceConflict("Use at most 30 supplemental sources in this workspace.")
                source["id"] = uuid4().hex
                state["materials"].append(source)
            self.store._end(state, state["active_job"], "completed")
            self.store._invalidate(state)
            self.store._write(db, record["owner"], state)
            record.update(status="ready", cleanup_status="pending", lease_until=None)
            self._write(db, record)
        return state

    def _end_workspace_job(self, db, record, canceled=False):
        try:
            state = self._workspace(db, record)
        except WorkspaceNotFound:
            return
        active = state["active_job"] or {}
        if active.get("id") == record["id"] and active.get("kind") == "prepare":
            self.store._end(
                state, state["active_job"], "failed",
                "Upload canceled." if canceled else "Upload processing failed.",
            )
            state["stage"] = self.store._resting_stage(state)
            self.store._write(db, record["owner"], state)

    def _terminate(self, id, status, owner=None, attempt=None):
        with self._session(locking=True) as db:
            record = self._load(db, id, owner)
            if attempt is not None and attempt != record["attempts"]:
                raise WorkspaceConflict("Preparation attempt is no longer current.")
            if record["status"] in ("ready", "canceled", "failed"):
                return record
            if (attempt is not None and record["status"] in ("processing", "prepared")
                    and record["lease_until"] <= self.clock()):
                raise WorkspaceConflict("Preparation lease is no longer current.")
            self._end_workspace_job(db, record, canceled=status == "canceled")
            record.update(
                status=status, cleanup_status="pending", lease_until=None,
                error="processing_failed" if status == "failed" else "canceled",
            )
            self._write(db, record)
        return record

    def fail(self, id, code="processing_failed", *, attempt=None):
        # Never persist exception messages or caller-controlled provider responses.
        return self._terminate(id, "failed", attempt=attempt)

    def cancel(self, owner, id):
        _identifier(owner, "owner")
        return self._terminate(id, "canceled", owner)

    def cleanup_due(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Invalid cleanup batch size.")
        now = self.clock()
        with self._session() as db:
            # JSON text predicates avoid coupling inherited state to JSONB. Only
            # lifecycle candidates cross the driver, never the whole ledger.
            rows = db.execute(
                "SELECT state FROM hax_uploads WHERE "
                "((state::jsonb->>'cleanup_status') IN ('pending', 'error') AND "
                "(state::jsonb->>'status') IN ('ready', 'failed', 'canceled')) OR "
                "((state::jsonb->>'expires_at')::double precision <= ? AND "
                "(state::jsonb->>'cleanup_status') != 'deleted') OR "
                "((state::jsonb->>'status') IN ('canceled', 'ready', 'failed') AND "
                "COALESCE((state::jsonb->>'cleanup_checked_at')::double precision, 0) "
                "< (state::jsonb->>'token_expires_at')::double precision) OR "
                "(state::jsonb->>'sandbox_cleanup_status') = 'error' "
                "ORDER BY (state::jsonb->>'updated_at')::double precision LIMIT ?",
                (now, limit),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def record_cleanup(self, id, success, code=None):
        if type(success) is not bool:
            raise ValueError("Invalid cleanup result.")
        with self._session(locking=True) as db:
            record = self._load(db, id)
            if (record["status"] not in ("ready", "failed", "canceled")
                    and record["expires_at"] > self.clock()):
                raise WorkspaceConflict("Raw upload is still needed for preparation.")
            if record["status"] not in ("ready", "failed", "canceled"):
                # Expired abandoned browser uploads must not leave a workspace
                # permanently busy after maintenance removes their raw object.
                self._end_workspace_job(db, record)
                record.update(status="failed", error="processing_failed", lease_until=None)
            record["cleanup_status"] = "deleted" if success else "error"
            record["cleanup_error"] = None if success else "cleanup_failed"
            if success:
                record["cleanup_checked_at"] = self.clock()
            self._write(db, record)
        return record

    def record_sandbox_cleanup(self, id, success):
        if type(success) is not bool:
            raise ValueError("Invalid sandbox cleanup result.")
        with self._session(locking=True) as db:
            record = self._load(db, id)
            if (record["status"] not in ("ready", "failed", "canceled", "expired")
                    and record["expires_at"] > self.clock()):
                raise WorkspaceConflict("Sandbox is still needed for preparation.")
            record["sandbox_cleanup_status"] = "deleted" if success else "error"
            record["sandbox_cleanup_error"] = None if success else "sandbox_cleanup_failed"
            self._write(db, record)
        return record

    def set_workflow(self, id, run_id):
        _identifier(run_id, "workflow run ID")
        with self._session(locking=True) as db:
            record = self._load(db, id)
            if record.get("workflow_id") not in (None, run_id):
                raise WorkspaceConflict("Workflow already registered.")
            record["workflow_id"] = run_id
            self._write(db, record)
        return record
