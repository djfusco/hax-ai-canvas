"""Offline DB contract tests. No real cloud/database connections are attempted.

The mock driver executes static statements against temporary SQLite fixtures,
while recording PostgreSQL SQL to assert locking and parameter binding. This is
not a PostgreSQL SQL parser or substitute for opt-in deployment DB integration.
"""

import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unittest

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "tests"))

from hosted_data import PostgresCredentialVault, PostgresMentorStore
from mentor_store import WorkspaceConflict
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
    """Run the SQLite store contract unchanged against the Postgres store."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "postgres-mock.sqlite"
        self.driver = MockDriver(self.path)
        self.store = PostgresMentorStore("postgresql://fixture", connect=self.driver)
        self.owner = "browser-one"
        self.workspace = self.store.create(self.owner, "Evidence and inquiry")
        self.id = self.workspace["id"]

    def other_instance(self):
        return PostgresMentorStore("postgresql://fixture", connect=self.driver)

    def test_driver_parameters_row_locks_and_close(self):
        self.store.set_share(self.owner, self.id, True)
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
            self.store.begin_consult(self.owner, self.id)  # no course imported yet
        connection = self.driver.connections[-1]
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)
        self.assertEqual(self.get(), self.workspace)


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
        actual = self.vault.get("owner-one", "openai")
        self.assertRegex(actual.pop("version"), r"^[a-f0-9]{32}$")
        self.assertEqual(actual, payload)
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
        decoded = json.loads(Fernet(self.key).decrypt(params[2]))
        self.assertRegex(decoded.pop("version"), r"^[a-f0-9]{32}$")
        self.assertEqual(decoded, payload)
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


if __name__ == "__main__":
    unittest.main()
