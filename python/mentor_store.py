"""Durable browser-owner-scoped mentor state; no provider credentials belong here.

All state transitions use SQLite write transactions, including revision checks.
Connections are per operation, so an instance can be shared by worker threads.
Payloads must be JSON objects and may not contain credential-named fields.
Free text is instructor content: callers must never put credentials into it.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4


class WorkspaceNotFound(LookupError):
    """The workspace does not exist or does not belong to this browser owner."""


class WorkspaceConflict(RuntimeError):
    """A revision, approval or workflow precondition was not satisfied."""


SOURCE_TYPES = frozenset(("blank", "api", "imscc", "prepared"))
_SECRET_FIELDS = frozenset((
    "apikey", "token", "accesstoken", "refreshtoken", "canvastoken",
    "password", "secret", "clientsecret", "authorization", "cookie",
    "cookies", "credentials", "privatekey", "providerkey",
    "openaiapikey", "anthropicapikey", "encryptionkey",
))


def _now():
    return datetime.now(timezone.utc).isoformat()


def _text(value, label, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError("Invalid " + label + ".")
    return value


def _revision(value):
    if type(value) is not int or value < 0:
        raise ValueError("Invalid revision.")
    return value


def _object(value):
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object.")

    def check(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("JSON object keys must be strings.")
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                if normalized in _SECRET_FIELDS or normalized.endswith(
                    ("apikey", "accesstoken", "refreshtoken", "password", "secret")
                ):
                    raise ValueError("Credential fields are not workspace data.")
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValueError("Expected JSON data.")

    try:
        check(value)
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, OverflowError, RecursionError, ValueError):
        raise ValueError("Invalid JSON workspace data or credential field.") from None


class MentorStore:
    def __init__(self, db_path):
        self.db_path = str(Path(db_path))
        if str(db_path) == ":memory:":
            raise ValueError("A durable database path is required.")
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS mentor_workspaces ("
                "id TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS mentor_workspaces_owner "
                "ON mentor_workspaces(owner)"
            )

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def _decode(encoded):
        state = json.loads(encoded)
        state.setdefault("reviewed_draft_revision", None)
        return state

    @staticmethod
    def _load(conn, owner, workspace_id):
        _text(owner, "owner")
        _text(workspace_id, "workspace ID")
        row = conn.execute(
            "SELECT state FROM mentor_workspaces WHERE owner = ? AND id = ?",
            (owner, workspace_id),
        ).fetchone()
        if row is None:
            raise WorkspaceNotFound("Workspace not found.")
        return MentorStore._decode(row[0])

    @staticmethod
    def _write(conn, owner, state):
        state["updated_at"] = _now()
        encoded = json.dumps(state, allow_nan=False)
        conn.execute(
            "UPDATE mentor_workspaces SET state = ? WHERE owner = ? AND id = ?",
            (encoded, owner, state["id"]),
        )

    def _change(self, owner, workspace_id, transition):
        with self._transaction() as conn:
            state = self._load(conn, owner, workspace_id)
            transition(state)
            self._write(conn, owner, state)
        return state

    @staticmethod
    def _idle(state):
        if state["active_job"] is not None:
            raise WorkspaceConflict("A job is already running.")

    @staticmethod
    def _exact(state, revision, key="revision"):
        if state[key] != _revision(revision):
            raise WorkspaceConflict("Workspace revision is stale.")

    @staticmethod
    def _invalidate(state):
        state["revision"] += 1
        state.update(direction_revision=None, draft=None, build_revision=None,
                     reviewed_draft_revision=None,
                     artifact=None, stage="brief")

    @staticmethod
    def _resting_stage(state):
        if state["artifact"] is not None:
            return "built"
        if state["build_revision"] is not None:
            return "build_approved"
        if state["draft"] is not None:
            return "draft"
        if state["direction_revision"] == state["revision"]:
            return "direction_approved"
        return "brief"

    def create(self, owner, title, source_type="blank", course_id=None, export_dir=None):
        _text(owner, "owner")
        _text(title, "title")
        if not isinstance(source_type, str) or source_type not in SOURCE_TYPES:
            raise ValueError("Invalid source type.")
        for value in (course_id, export_dir):
            if value is not None:
                _text(value, "source metadata")
        now = _now()
        state = dict(
            id=uuid4().hex, title=title, source_type=source_type,
            course_id=course_id, export_dir=export_dir, source={},
            brief="", revision=1, stage="brief", materials=[], messages=[],
            draft=None, draft_revision=0, direction_revision=None,
            build_revision=None, reviewed_draft_revision=None,
            active_job=None, artifact=None, jobs=[],
            created_at=now, updated_at=now,
        )
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO mentor_workspaces (id, owner, state) VALUES (?, ?, ?)",
                (state["id"], owner, json.dumps(state)),
            )
        return state

    def list_workspaces(self, owner):
        _text(owner, "owner")
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT state FROM mentor_workspaces WHERE owner = ?", (owner,)
            ).fetchall()
        return sorted((self._decode(row[0]) for row in rows),
                      key=lambda state: (state["updated_at"], state["id"]), reverse=True)

    def get(self, owner, id):
        with self._connection() as conn:
            return self._load(conn, owner, id)

    def _edit_input(self, owner, id, revision, edit):
        def transition(state):
            self._exact(state, revision)
            self._idle(state)
            edit(state)
            self._invalidate(state)
        return self._change(owner, id, transition)

    def set_source(self, owner, id, source, revision):
        source = _object(source)
        if "source_type" in source:
            if not isinstance(source["source_type"], str) or source["source_type"] not in SOURCE_TYPES:
                raise ValueError("Invalid source type.")
        for key in ("course_id", "export_dir"):
            if source.get(key) is not None:
                _text(source[key], "source metadata")

        def edit(state):
            state["source"] = source
            for key in ("source_type", "course_id", "export_dir"):
                if key in source:
                    state[key] = source[key]
        return self._edit_input(owner, id, revision, edit)

    def save_brief(self, owner, id, brief, revision):
        _text(brief, "brief", empty=True)
        return self._edit_input(owner, id, revision,
                                lambda state: state.update(brief=brief))

    def add_material(self, owner, id, material, revision):
        material = _object(material)
        material.setdefault("id", uuid4().hex)
        _text(material["id"], "material ID")

        def edit(state):
            if any(item["id"] == material["id"] for item in state["materials"]):
                raise WorkspaceConflict("Material already exists.")
            state["materials"].append(material)
        return self._edit_input(owner, id, revision, edit)

    def remove_material(self, owner, id, material_id, revision):
        _text(material_id, "material ID")

        def edit(state):
            remaining = [item for item in state["materials"] if item["id"] != material_id]
            if len(remaining) == len(state["materials"]):
                raise WorkspaceNotFound("Material not found.")
            state["materials"] = remaining
        return self._edit_input(owner, id, revision, edit)

    def add_message(self, owner, id, role, content, kind="interview"):
        if role not in ("user", "assistant", "instructor", "system"):
            raise ValueError("Invalid message role.")
        _text(content, "message content")
        if kind not in ("interview", "review"):
            raise ValueError("Invalid message kind.")
        message = dict(id=uuid4().hex, role=role, content=content, kind=kind, created_at=_now())
        return self._change(owner, id, lambda state: state["messages"].append(message))

    def approve_direction(self, owner, id, revision):
        def transition(state):
            self._exact(state, revision)
            self._idle(state)
            if not state["brief"].strip():
                raise WorkspaceConflict("A design brief is required.")
            state["direction_revision"] = state["revision"]
            state["stage"] = self._resting_stage(state)
        return self._change(owner, id, transition)

    def _begin(self, owner, id, revision, kind):
        def transition(state):
            self._exact(state, revision, "draft_revision" if kind in ("build", "review") else "revision")
            self._idle(state)
            if kind != "interview" and state["direction_revision"] != state["revision"]:
                raise WorkspaceConflict("Approve the current design direction first.")
            if kind == "review" and state["draft"] is None:
                raise WorkspaceConflict("Analyze the current direction first.")
            if kind == "build" and (
                state["draft"] is None or state["build_revision"] != state["draft_revision"]
                or state["reviewed_draft_revision"] != state["draft_revision"]
            ):
                raise WorkspaceConflict("Approve the current draft first.")
            job = dict(id=uuid4().hex, kind=kind, revision=state["revision"],
                       draft_revision=state["draft_revision"], status="running",
                       created_at=_now(), finished_at=None)
            state["active_job"] = job
            state["jobs"].append(job)
            if kind != "interview":
                state["artifact"] = None
            if kind in ("analysis", "review"):
                state["build_revision"] = None
            if kind == "review":
                state["reviewed_draft_revision"] = None
            state["stage"] = {
                "analysis": "analyzing", "build": "building",
                "review": "reviewing", "interview": "interviewing",
            }[kind]
        return self._change(owner, id, transition)

    def begin_analysis(self, owner, id, revision):
        return self._begin(owner, id, revision, "analysis")

    def begin_interview(self, owner, id, revision):
        return self._begin(owner, id, revision, "interview")

    def finish_interview(self, owner, id, job_id):
        def transition(state):
            job = self._active(state, job_id, "interview")
            self._end(state, job, "completed")
            state["stage"] = self._resting_stage(state)
        return self._change(owner, id, transition)

    def begin_review(self, owner, id, draft_revision):
        return self._begin(owner, id, draft_revision, "review")

    def finish_review(self, owner, id, job_id, draft_or_none):
        draft = None if draft_or_none is None else _object(draft_or_none)
        if draft is not None and not draft:
            raise ValueError("A nonempty draft is required.")

        def transition(state):
            job = self._active(state, job_id, "review")
            if state["direction_revision"] != state["revision"] or state["draft"] is None:
                raise WorkspaceConflict("Review is not current for this direction.")
            self._end(state, job, "completed")
            if draft is not None:
                state["draft"] = draft
                state["draft_revision"] += 1
            state.update(reviewed_draft_revision=state["draft_revision"],
                         build_revision=None, artifact=None, stage="draft")
        return self._change(owner, id, transition)

    @staticmethod
    def _active(state, job_id, kind=None):
        _text(job_id, "job ID")
        job = state["active_job"]
        if (job is None or job["id"] != job_id or job["status"] != "running"
                or (kind is not None and job["kind"] != kind)
                or job["revision"] != state["revision"]
                or job["draft_revision"] != state["draft_revision"]):
            raise WorkspaceConflict("Job is not active for the current workspace.")
        return job

    @staticmethod
    def _end(state, job, status, error=None):
        # JSON decoding does not preserve aliasing between active_job and jobs.
        for entry in state["jobs"]:
            if entry["id"] == job["id"]:
                entry.update(status=status, finished_at=_now())
                if error is not None:
                    entry["error"] = error
                break
        state["active_job"] = None

    def finish_analysis(self, owner, id, job_id, draft):
        draft = _object(draft)
        if not draft:
            raise ValueError("A nonempty draft is required.")

        def transition(state):
            job = self._active(state, job_id, "analysis")
            self._end(state, job, "completed")
            state.update(draft=draft, draft_revision=state["draft_revision"] + 1,
                         build_revision=None, reviewed_draft_revision=None,
                         artifact=None, stage="draft")
        return self._change(owner, id, transition)

    def fail_job(self, owner, id, job_id, error):
        _text(error, "job error")

        def transition(state):
            job = self._active(state, job_id)
            # Exception strings may include credentials, prompts or upstream bodies.
            self._end(state, job, "failed", "Job failed. Retry after checking server diagnostics.")
            state["stage"] = self._resting_stage(state)
        return self._change(owner, id, transition)

    def save_draft(self, owner, id, draft, draft_revision):
        draft = _object(draft)
        if not draft:
            raise ValueError("A nonempty draft is required.")

        def transition(state):
            self._exact(state, draft_revision, "draft_revision")
            self._idle(state)
            if state["draft"] is None or state["direction_revision"] != state["revision"]:
                raise WorkspaceConflict("Analyze the current direction first.")
            state.update(draft=draft, draft_revision=state["draft_revision"] + 1,
                         build_revision=None, reviewed_draft_revision=None,
                         artifact=None, stage="draft")
        return self._change(owner, id, transition)

    def approve_build(self, owner, id, draft_revision):
        def transition(state):
            self._exact(state, draft_revision, "draft_revision")
            self._idle(state)
            if state["draft"] is None or state["direction_revision"] != state["revision"]:
                raise WorkspaceConflict("A current analyzed draft is required.")
            if state["reviewed_draft_revision"] != state["draft_revision"]:
                raise WorkspaceConflict("Complete the final review of the current draft first.")
            state["build_revision"] = state["draft_revision"]
            state["stage"] = self._resting_stage(state)
        return self._change(owner, id, transition)

    def begin_build(self, owner, id, draft_revision):
        return self._begin(owner, id, draft_revision, "build")

    def finish_build(self, owner, id, job_id, artifact):
        artifact = _object(artifact)
        if not artifact:
            raise ValueError("A nonempty artifact is required.")

        def transition(state):
            job = self._active(state, job_id, "build")
            if (state["direction_revision"] != state["revision"]
                    or state["build_revision"] != state["draft_revision"]
                    or state["reviewed_draft_revision"] != state["draft_revision"]):
                raise WorkspaceConflict("Build approval is stale.")
            self._end(state, job, "completed")
            state.update(artifact=artifact, stage="built")
        return self._change(owner, id, transition)

    def recover_interrupted_jobs(self):
        """Startup-only maintenance: fail interrupted jobs; never call on live workers.

        This privileged operation spans owners and must not be a public API route.
        Construction never silently changes job status.
        """
        recovered = []
        with self._transaction() as conn:
            rows = conn.execute("SELECT owner, state FROM mentor_workspaces").fetchall()
            for owner, encoded in rows:
                state = self._decode(encoded)
                if state["active_job"] is not None:
                    self._end(state, state["active_job"], "failed",
                              "Job interrupted by application restart.")
                    state["stage"] = self._resting_stage(state)
                    self._write(conn, owner, state)
                    recovered.append(state)
        return recovered
