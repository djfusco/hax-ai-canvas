"""Offline DB contract tests. No real cloud/database connections are attempted.

The mock driver executes static statements against temporary SQLite fixtures,
while recording PostgreSQL SQL to assert locking and parameter binding. This is
not a PostgreSQL SQL parser or substitute for opt-in deployment DB integration.
"""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from threading import Barrier
import unittest

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "tests"))

from hosted_data import PostgresCredentialVault, PostgresMentorStore, PostgresUploads
from mentor_store import WorkspaceConflict, WorkspaceNotFound
import test_mentor_store as local_tests


class MockDriver:
    def __init__(self, path):
        self.path = path
        self.calls = []
        self.connections = []
        self.failure = None

    def __call__(self, dsn, **kwargs):
        self.calls.append((dsn, kwargs))
        conn = MockConnection(self)
        self.connections.append(conn)
        return conn


class MockConnection:
    def __init__(self, driver):
        self.driver = driver
        self.sql = []
        self.closed = False
        self.committed = False
        self.rolled_back = False
        self.db = sqlite3.connect(driver.path, timeout=10)
        # Simulates serializable write claims; assert actual row-lock SQL below.
        self.db.execute("BEGIN IMMEDIATE")

    def execute(self, sql, params=()):
        self.sql.append((sql, params))
        if self.driver.failure and self.driver.failure in sql:
            raise RuntimeError("simulated database write failure")
        sqlite_sql = sql.replace("%s", "?").replace(" FOR UPDATE", "")
        sqlite_sql = re.sub(
            r"state::jsonb->>'([a-z_]+)'",
            lambda match: "json_extract(state, '$." + match[1] + "')",
            sqlite_sql,
        ).replace("::double precision", "")
        return self.db.execute(sqlite_sql, params)

    def commit(self):
        self.committed = True
        self.db.commit()

    def rollback(self):
        self.rolled_back = True
        self.db.rollback()

    def close(self):
        self.closed = True
        self.db.close()


class PostgresInheritedStoreTests(local_tests.MentorStoreTests):
    """Run all existing revision/interview/review/build contracts unchanged."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "postgres-mock.sqlite"
        self.driver = MockDriver(self.path)
        self.store = PostgresMentorStore("postgresql://fixture", connect=self.driver)
        self.owner = "browser-one"
        self.workspace = self.store.create(self.owner, "Evidence and inquiry")
        self.id = self.workspace["id"]

    def test_driver_parameters_row_locks_and_close(self):
        self.store.save_brief(self.owner, self.id, "Brief", 1)
        connection = self.driver.connections[-1]
        select = [sql for sql, _ in connection.sql if sql.startswith("SELECT state")]
        self.assertTrue(select[0].endswith("FOR UPDATE"))
        self.assertIn("owner = %s AND id = %s", select[0])
        self.assertTrue(connection.committed)
        self.assertTrue(all(conn.closed for conn in self.driver.connections))
        self.assertTrue(all(
            kwargs == dict(sslmode="require", connect_timeout=10)
            for _, kwargs in self.driver.calls
        ))

    def test_failed_transition_rolls_back_and_closes(self):
        with self.assertRaises(WorkspaceConflict):
            self.store.save_brief(self.owner, self.id, "Stale", 0)
        connection = self.driver.connections[-1]
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)
        self.assertEqual(self.get(), self.workspace)

    def test_two_postgres_instances_allow_only_one_job(self):
        other = PostgresMentorStore("postgresql://fixture", connect=self.driver)
        barrier = Barrier(2)

        def claim(store):
            barrier.wait(timeout=5)
            try:
                return store.begin_interview(self.owner, self.id, 1)
            except WorkspaceConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, (self.store, other)))
        self.assertEqual(sum(result is not None for result in results), 1)
        selects = [
            sql for conn in self.driver.connections for sql, _ in conn.sql
            if sql.startswith("SELECT state") and sql.endswith("FOR UPDATE")
        ]
        self.assertGreaterEqual(len(selects), 2)


class HostedFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "postgres-mock.sqlite"
        self.driver = MockDriver(self.path)
        self.now = 1_800_000_000.0
        self.clock = lambda: self.now

    def tearDown(self):
        self.assertTrue(all(conn.closed for conn in self.driver.connections))


class PostgresVaultTests(HostedFixture):
    def setUp(self):
        super().setUp()
        self.key = Fernet.generate_key()
        self.vault = PostgresCredentialVault(
            "postgresql://fixture", self.key, self.clock, self.driver
        )

    def test_key_is_required_without_opening_connection(self):
        before = len(self.driver.calls)
        with self.assertRaises(ValueError):
            PostgresCredentialVault("postgresql://fixture", None, connect=self.driver)
        self.assertEqual(len(self.driver.calls), before)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.path])

    def test_save_get_status_and_forget_preserve_local_contract(self):
        payload = dict(api_key="offline-fixture-key", model="fixture-model", base_url="")
        saved = self.vault.save("owner-one", "openai", payload, remember=True)
        self.assertTrue(saved["remembered"])
        self.assertEqual(self.vault.get("owner-one", "openai"), payload)
        self.assertIsNone(self.vault.get("owner-two", "openai"))
        status = self.vault.status("owner-one")
        self.assertEqual(status, [saved])
        self.assertNotIn("offline-fixture-key", json.dumps(status))
        statements = [
            (sql, params) for conn in self.driver.connections for sql, params in conn.sql
            if sql.startswith("INSERT INTO hax_credentials")
        ]
        sql, params = statements[0]
        self.assertIn("ON CONFLICT (owner, provider) DO UPDATE", sql)
        self.assertNotIn(b"offline-fixture-key", params[2])
        self.assertEqual(json.loads(Fernet(self.key).decrypt(params[2])), payload)
        self.assertNotIn("offline-fixture-key", sql)
        self.vault.forget("owner-two", "openai")
        self.assertIsNotNone(self.vault.get("owner-one", "openai"))
        self.vault.forget("owner-one", "openai")
        self.assertEqual(self.vault.status("owner-one"), [])

    def test_expiry_delete_is_conditional_and_owner_scoped(self):
        self.vault.save("owner-one", "openai", dict(api_key="fixture"))
        self.vault.save("owner-two", "openai", dict(api_key="fixture"))
        self.now += 43201
        self.assertIsNone(self.vault.get("owner-one", "openai"))
        delete_sql, params = next(
            (sql, params) for sql, params in self.driver.connections[-1].sql
            if sql.startswith("DELETE")
        )
        self.assertIn("owner=%s AND provider=%s AND expires<=%s", delete_sql)
        self.assertEqual(params[:2], ("owner-one", "openai"))
        self.vault.status("owner-one")
        self.assertIn("owner=%s", self.driver.connections[-1].sql[0][0])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT owner FROM hax_credentials").fetchall(),
                             [("owner-two",)])

    def test_invalid_provider_and_corrupt_cipher_are_safe(self):
        with self.assertRaises(ValueError):
            self.vault.save("owner", "unsupported", dict(api_key="fixture"))
        self.vault.save("owner", "openai", dict(api_key="fixture"))
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE hax_credentials SET sealed=?", (b"invalid",))
        with self.assertRaisesRegex(ValueError, "could not be unlocked"):
            self.vault.get("owner", "openai")

    def test_owner_is_parameterized_not_interpolated(self):
        owner = "'; DROP TABLE hax_credentials; --"
        self.vault.save(owner, "openai", dict(api_key="fixture"))
        self.assertIsNotNone(self.vault.get(owner, "openai"))
        self.assertEqual(self.vault.status("another-owner"), [])
        for conn in self.driver.connections:
            for sql, _ in conn.sql:
                self.assertNotIn(owner, sql)


class PostgresUploadsTests(HostedFixture):
    def setUp(self):
        super().setUp()
        self.uploads = PostgresUploads("postgresql://fixture", self.clock, self.driver)
        self.store = self.uploads.store
        self.owner = "owner-one"
        self.workspace = self.store.create(self.owner, "Upload fixture")
        self.id = self.workspace["id"]

    def create(self, **kwargs):
        return self.uploads.create(
            self.owner, self.id, 1, kwargs.pop("filename", "course.imscc"),
            kwargs.pop("size", 1024), **kwargs,
        )

    def blob(self, record):
        pathname = record["pathname"]
        return dict(
            url="https://fixture.private.blob.vercel-storage.com/" + pathname,
            pathname=pathname, size=record["size"], etag="fixture-etag",
        )

    def claim(self, **kwargs):
        record = self.create(**kwargs)
        self.uploads.completed(record["id"], self.blob(record))
        return self.uploads.claim(record["id"])

    def receipt(self, record):
        extension = "course.haxcourse" if record["kind"] == "course" else "result.json"
        return dict(
            pathname=(f"prepared/{self.owner}/{self.id}/{record['id']}/"
                      f"attempt-{record['attempts']}/{extension}"),
            size=100, sha256="a" * 64,
        )

    def source(self, record):
        receipt = record["prepared"]
        return dict(
            source_type="prepared", course_id="imscc-" + record["id"],
            prepared_blob=receipt["pathname"], sha256=receipt["sha256"],
            size=receipt["size"],
        )

    def prepare(self):
        record = self.claim()
        return self.uploads.prepared(
            record["id"], self.receipt(record), attempt=record["attempts"]
        )

    def test_create_reserves_atomic_workspace_job_and_server_path(self):
        record = self.create(filename="../../Course.IMSCC")
        self.assertEqual(record["filename"], "Course.IMSCC")
        self.assertRegex(record["id"], r"^[a-f0-9]{32}$")
        self.assertEqual(
            record["pathname"],
            f"raw/{self.owner}/{self.id}/{record['id']}/source.imscc",
        )
        self.assertEqual(record["token_expires_at"], self.now + 3600)
        self.assertEqual(record["expires_at"], self.now + 86400)
        self.assertEqual(record["status"], "awaiting_upload")
        state = self.store.get(self.owner, self.id)
        self.assertEqual(state["active_job"]["id"], record["id"])
        self.assertEqual(state["active_job"]["kind"], "prepare")
        self.assertEqual(state["stage"], "uploading")
        self.assertEqual(state["revision"], 1)
        with self.assertRaises(WorkspaceConflict):
            self.create()
        with self.assertRaises(WorkspaceConflict):
            self.store.save_brief(self.owner, self.id, "Cannot edit while preparing", 1)
        self.assertEqual(self.uploads.get(self.owner, record["id"]), record)
        with self.assertRaises(WorkspaceNotFound):
            self.uploads.get("other-owner", record["id"])

    def test_limits_and_identifiers_reject_without_intent(self):
        for kwargs in (
            dict(filename="course.zip"), dict(size=8 * 1024**3 + 1),
            dict(filename="course.haxcourse", size=64 * 1024**2 + 1),
            dict(filename="notes.txt", kind="material", size=10 * 1024**2 + 1),
            dict(filename="notes.exe", kind="material"), dict(role="admin"),
            dict(size=True), dict(size=0), dict(kind="arbitrary"),
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.create(**kwargs)
        with self.assertRaises(ValueError):
            self.uploads.create("../bad", self.id, 1, "c.imscc", 1)
        with self.assertRaises(ValueError):
            self.uploads.create("x" * 129, self.id, 1, "c.imscc", 1)
        self.assertIsNone(self.store.get(self.owner, self.id)["active_job"])

    def test_wrong_revision_rolls_back_quota_and_workspace(self):
        with self.assertRaises(WorkspaceConflict):
            self.uploads.create(self.owner, self.id, 0, "course.imscc", 1)
        self.assertIsNone(self.store.get(self.owner, self.id)["active_job"])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM hax_uploads").fetchone()[0], 0)

    def test_material_intent_respects_existing_supplemental_source_limit(self):
        state = self.workspace
        for index in range(30):
            state = self.store.add_material(self.owner, self.id,
                                            {"name": str(index), "text": "Fixture"},
                                            state["revision"])
        with self.assertRaisesRegex(WorkspaceConflict, "30 supplemental"):
            self.uploads.create(self.owner, self.id, state["revision"], "notes.txt", 1,
                                kind="material")
        self.assertIsNone(self.store.get(self.owner, self.id)["active_job"])

    def test_owner_pending_and_hourly_quota(self):
        records = []
        for index in range(3):
            state = self.store.create(self.owner, str(index))
            records.append(self.uploads.create(
                self.owner, state["id"], 1, "course.imscc", 1
            ))
        with self.assertRaisesRegex(WorkspaceConflict, "quota"):
            self.create()
        for record in records:
            self.uploads.cancel(self.owner, record["id"])
        for _ in range(17):
            record = self.create()
            self.uploads.cancel(self.owner, record["id"])
        with self.assertRaisesRegex(WorkspaceConflict, "quota"):
            self.create()
        self.now += 3601
        self.assertEqual(self.create()["status"], "awaiting_upload")
        locks = [
            sql for conn in self.driver.connections for sql, _ in conn.sql
            if sql.startswith("SELECT owner FROM hax_upload_owners")
        ]
        self.assertTrue(all(sql.endswith("FOR UPDATE") for sql in locks))

    def test_duplicate_create_and_claim_are_serialized(self):
        barrier = Barrier(2)

        def create(_):
            barrier.wait(timeout=5)
            try:
                return self.create()
            except WorkspaceConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(create, range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)
        record = next(r for r in results if r is not None)
        self.uploads.completed(record["id"], self.blob(record))
        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(
                self.uploads.claim, (record["id"], record["id"])
            ))
        self.assertEqual(sum(r is not None for r in claims), 1)
        self.assertEqual(next(r for r in claims if r)["attempts"], 1)

    def test_worker_claim_retry_is_idempotent_but_different_run_cannot_claim(self):
        record = self.create()
        self.uploads.completed(record["id"], self.blob(record))
        claimed = self.uploads.claim(record["id"], worker_id="run-one")
        retried = self.uploads.claim(record["id"], worker_id="run-one")
        self.assertEqual(retried, claimed)
        self.assertEqual(retried["attempts"], 1)
        self.assertIsNone(self.uploads.claim(record["id"], worker_id="run-two"))
        self.assertIsNone(self.uploads.claim(record["id"]))
        self.now += 1201
        reclaimed = self.uploads.claim(record["id"], worker_id="run-two")
        self.assertEqual(reclaimed["attempts"], 2)
        self.assertEqual(reclaimed["worker_id"], "run-two")
        self.assertIsNone(self.uploads.claim(record["id"], worker_id="run-one"))

    def test_complete_validates_private_receipt_and_replays(self):
        record = self.create()
        receipt = self.blob(record)
        for changes in (
            dict(url="https://evil.example/" + record["pathname"]),
            dict(url=receipt["url"] + "?secret=x"),
            dict(url=receipt["url"] + "\n"),
            dict(url=receipt["url"].replace("/raw/", "/wrong/")),
            dict(url=receipt["url"].replace("https://", "https://user@")),
            dict(pathname="raw/other/source.imscc"), dict(size=100),
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.uploads.completed(record["id"], dict(receipt, **changes))
        self.assertEqual(
            self.uploads.completed(record["id"], receipt)["status"], "uploaded"
        )
        self.assertEqual(
            self.uploads.completed(record["id"], receipt)["status"], "uploaded"
        )
        with self.assertRaises(WorkspaceConflict):
            self.uploads.completed(record["id"], dict(receipt, etag="changed"))

    def test_lease_reclaim_fences_old_attempt_and_prepared_path(self):
        first = self.claim()
        self.assertIsNone(self.uploads.claim(first["id"]))
        self.now += 1201
        second = self.uploads.claim(first["id"])
        self.assertEqual(second["attempts"], 2)
        for operation in (
            lambda: self.uploads.save_sandbox(first["id"], {"id": "old"}, attempt=1),
            lambda: self.uploads.prepared(first["id"], self.receipt(first), attempt=1),
            lambda: self.uploads.fail(first["id"], attempt=1),
            lambda: self.uploads.attach(first["id"], {}, attempt=1),
        ):
            with self.assertRaises(WorkspaceConflict):
                operation()
        with self.assertRaises(ValueError):
            self.uploads.prepared(second["id"], self.receipt(first), attempt=2)
        self.uploads.save_sandbox(second["id"], {"id": "new"}, attempt=2)
        self.assertEqual(
            self.uploads.prepared(second["id"], self.receipt(second), attempt=2)["status"],
            "prepared",
        )

    def test_reclaim_prepared_resets_receipt_and_preserves_old_sandbox_for_cleanup(self):
        first = self.claim()
        self.uploads.save_sandbox(first["id"], {"name": "sandbox-one"}, attempt=1)
        self.uploads.prepared(first["id"], self.receipt(first), attempt=1)
        self.now += 1201
        with self.assertRaises(WorkspaceConflict):
            self.uploads.fail(first["id"], attempt=1)
        second = self.uploads.claim(first["id"])
        self.assertIsNone(second["prepared"])
        self.assertIsNone(second["sandbox"])
        self.assertEqual(second["previous_sandboxes"], [{"name": "sandbox-one"}])
        self.uploads.save_sandbox(second["id"], {"name": "sandbox-two"}, attempt=2)
        record = self.uploads.prepared(second["id"], self.receipt(second), attempt=2)
        self.assertIn("/attempt-2/", record["prepared"]["pathname"])

    def test_sandbox_identity_and_phase_cannot_regress_within_attempt(self):
        record = self.claim()
        id = record["id"]
        self.uploads.save_sandbox(id, {"name": "sandbox-one", "phase": "setup"}, attempt=1)
        self.uploads.save_sandbox(id, {"phase": "download"}, attempt=1)
        self.uploads.save_sandbox(id, {"phase": "locking"}, attempt=1)
        with self.assertRaises(WorkspaceConflict):
            self.uploads.save_sandbox(id, {"phase": "download"}, attempt=1)
        self.uploads.save_sandbox(id, {"phase": "prepare"}, attempt=1)
        self.uploads.save_sandbox(id, {"phase": "parse", "polls": 2}, attempt=1)
        with self.assertRaises(WorkspaceConflict):
            self.uploads.save_sandbox(id, {"phase": "download"}, attempt=1)
        with self.assertRaises(WorkspaceConflict):
            self.uploads.save_sandbox(id, {"name": "sandbox-two"}, attempt=1)
        saved = self.uploads.save_sandbox(id, {"phase": "complete"}, attempt=1)
        self.assertEqual(saved["sandbox"]["name"], "sandbox-one")
        self.assertEqual(saved["sandbox"]["phase"], "complete")
        self.assertEqual(saved["sandbox"]["polls"], 2)

    def test_prepared_then_attach_then_cleanup_is_atomic_and_durable(self):
        record = self.prepare()
        self.assertEqual(record["cleanup_status"], "pending")
        self.assertNotIn(record["id"], [r["id"] for r in self.uploads.cleanup_due()])
        with self.assertRaises(WorkspaceConflict):
            self.uploads.record_cleanup(record["id"], True)
        source = self.source(record)
        with self.assertRaises(ValueError):
            self.uploads.attach(record["id"], dict(source, sha256="b" * 64), attempt=1)
        state = self.uploads.attach(record["id"], source, attempt=1)
        self.assertEqual(state["revision"], 2)
        self.assertEqual(state["source_type"], "prepared")
        self.assertEqual(state["course_id"], "imscc-" + record["id"])
        self.assertIsNone(state["export_dir"])
        self.assertIsNone(state["active_job"])
        self.assertEqual(state["jobs"][-1]["status"], "completed")
        for key in ("draft", "direction_revision", "build_revision",
                    "reviewed_draft_revision", "artifact"):
            self.assertIsNone(state[key])
        self.assertEqual(self.uploads.attach(record["id"], source, attempt=1), state)
        self.uploads.record_cleanup(record["id"], True)
        saved = self.uploads.internal_get(record["id"])
        self.assertEqual(saved["status"], "ready")
        self.assertEqual(saved["cleanup_status"], "deleted")
        self.assertEqual(saved["prepared"], record["prepared"])
        # A browser upload token may still be valid: reconciliation must retry
        # deletion if a late upload overwrites a previously deleted raw object.
        self.assertIn(record["id"], [r["id"] for r in self.uploads.cleanup_due()])
        self.now += 3601
        # A final reconciliation is needed even when the token has expired,
        # because its last write may have followed the previous deletion.
        self.assertIn(record["id"], [r["id"] for r in self.uploads.cleanup_due()])
        self.uploads.record_cleanup(record["id"], True)
        self.assertNotIn(record["id"], [r["id"] for r in self.uploads.cleanup_due()])
        self.now += 86400
        self.assertNotIn(record["id"], [r["id"] for r in self.uploads.cleanup_due()])

    def test_attach_write_failure_rolls_back_both_records(self):
        record = self.prepare()
        before = self.store.get(self.owner, self.id)
        self.driver.failure = "UPDATE hax_uploads"
        with self.assertRaises(RuntimeError):
            self.uploads.attach(record["id"], self.source(record), attempt=1)
        self.driver.failure = None
        self.assertEqual(self.store.get(self.owner, self.id), before)
        self.assertEqual(self.uploads.internal_get(record["id"])["status"], "prepared")

    def test_material_attach_preserves_extracted_evidence(self):
        record = self.claim(filename="policy.txt", kind="material", role="policy")
        record = self.uploads.prepared(record["id"], self.receipt(record), attempt=1)
        material = dict(
            name="Policy", kind="file", role="policy", text="Extracted evidence",
            warnings=[], bytes=100, sha256="b" * 64,
        )
        state = self.uploads.attach(record["id"], material, attempt=1)
        self.assertEqual(state["source_type"], "blank")
        self.assertEqual(state["materials"][0]["text"], "Extracted evidence")
        self.assertRegex(state["materials"][0]["id"], r"^[a-f0-9]{32}$")
        self.assertEqual(state["revision"], 2)

    def test_cancel_and_stale_completion_do_not_attach_or_end_other_job(self):
        record = self.claim()
        canceled = self.uploads.cancel(self.owner, record["id"])
        self.assertEqual(canceled["status"], "canceled")
        state = self.store.begin_interview(self.owner, self.id, 1)
        new_job = state["active_job"]["id"]
        self.uploads.fail(record["id"], "potential sensitive detail", attempt=1)
        self.assertEqual(self.store.get(self.owner, self.id)["active_job"]["id"], new_job)
        with self.assertRaises(WorkspaceConflict):
            self.uploads.prepared(record["id"], self.receipt(record), attempt=1)
        self.assertIsNone(self.uploads.claim(record["id"]))
        with self.assertRaises(WorkspaceNotFound):
            self.uploads.cancel("wrong-owner", record["id"])

    def test_workspace_revision_and_job_mismatch_rejects_attachment(self):
        record = self.prepare()
        with sqlite3.connect(self.path) as db:
            state = self.store._decode(db.execute(
                "SELECT state FROM mentor_workspaces WHERE id=?", (self.id,)
            ).fetchone()[0])
            state["revision"] += 1
            db.execute("UPDATE mentor_workspaces SET state=? WHERE id=?",
                       (json.dumps(state), self.id))
        with self.assertRaises(WorkspaceConflict):
            self.uploads.attach(record["id"], self.source(record), attempt=1)
        self.assertEqual(self.uploads.internal_get(record["id"])["status"], "prepared")

    def test_expiry_and_failure_cleanup_preserve_safe_status(self):
        record = self.create()
        self.now += 86401
        self.assertIn(record["id"], [r["id"] for r in self.uploads.cleanup_due()])
        self.assertIsNone(self.uploads.claim(record["id"]))
        with self.assertRaises(WorkspaceConflict):
            self.uploads.completed(record["id"], self.blob(record))
        failed = self.uploads.fail(record["id"], "unsafe provider detail")
        self.assertEqual(failed["error"], "processing_failed")
        result = self.uploads.record_cleanup(record["id"], False, "unsafe provider detail")
        self.assertEqual(result["cleanup_error"], "cleanup_failed")
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("unsafe provider detail", json.dumps(result))
        self.assertEqual(self.uploads.record_cleanup(record["id"], True)["status"], "failed")

    def test_expired_abandoned_upload_cleanup_releases_workspace(self):
        record = self.create()
        self.now += 86401
        cleaned = self.uploads.record_cleanup(record["id"], True)
        self.assertEqual(cleaned["status"], "failed")
        self.assertEqual(cleaned["cleanup_status"], "deleted")
        state = self.store.get(self.owner, self.id)
        self.assertIsNone(state["active_job"])
        self.assertEqual(state["stage"], "brief")
        self.assertEqual(state["jobs"][-1]["status"], "failed")
        self.store.save_brief(self.owner, self.id, "Editable again", 1)

    def test_sandbox_cleanup_retries_even_after_raw_deletion_is_reconciled(self):
        record = self.claim()
        with self.assertRaises(WorkspaceConflict):
            self.uploads.record_sandbox_cleanup(record["id"], True)
        self.uploads.cancel(self.owner, record["id"])
        self.now += 3601
        self.uploads.record_cleanup(record["id"], True)
        self.assertNotIn(record["id"], [r["id"] for r in self.uploads.cleanup_due()])
        failed = self.uploads.record_sandbox_cleanup(record["id"], False)
        self.assertEqual(failed["sandbox_cleanup_status"], "error")
        self.assertEqual(failed["sandbox_cleanup_error"], "sandbox_cleanup_failed")
        self.assertEqual(failed["status"], "canceled")
        self.assertIn(record["id"], [r["id"] for r in self.uploads.cleanup_due()])
        deleted = self.uploads.record_sandbox_cleanup(record["id"], True)
        self.assertEqual(deleted["sandbox_cleanup_status"], "deleted")
        self.assertIsNone(deleted["sandbox_cleanup_error"])
        self.assertNotIn(record["id"], [r["id"] for r in self.uploads.cleanup_due()])
        with self.assertRaises(ValueError):
            self.uploads.record_sandbox_cleanup(record["id"], "unsafe error details")

    def test_expired_sandbox_cleanup_is_recordable(self):
        record = self.claim()
        self.now += 86401
        result = self.uploads.record_sandbox_cleanup(record["id"], False)
        self.assertEqual(result["sandbox_cleanup_status"], "error")
        self.assertIn(record["id"], [r["id"] for r in self.uploads.cleanup_due()])

    def test_schema_and_metadata_contain_no_credentials(self):
        record = self.claim()
        with self.assertRaises(ValueError):
            self.uploads.save_sandbox(record["id"], {"token": "fixture"}, attempt=1)
        with self.assertRaises(ValueError):
            self.uploads.prepared(record["id"], {"api_key": "fixture"}, attempt=1)
        self.uploads.set_workflow(record["id"], "workflow-run-id")
        with self.assertRaises(WorkspaceConflict):
            self.uploads.set_workflow(record["id"], "different-run")
        ddl = [
            sql for conn in self.driver.connections for sql, _ in conn.sql
            if sql.startswith("CREATE")
        ]
        self.assertFalse(any("sealed" in sql or "secret" in sql for sql in ddl))


if __name__ == "__main__":
    unittest.main()
