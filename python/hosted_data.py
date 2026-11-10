"""Hosted persistence. No service calls are made except explicit DB operations.

Tables deliberately retain JSON TEXT workspace state so MentorStore's revision,
approval, interview and review transitions remain the single source of truth.
The injected connect callable follows psycopg.connect's signature.
"""

from contextlib import contextmanager
import re
import time

from cryptography.fernet import Fernet

from mentor_credentials import CredentialVault
from mentor_store import (
    COURSE_MODEL_TABLE, INTAKE_CHUNK_TABLE, MentorStore,
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
            conn.execute(COURSE_MODEL_TABLE)
            conn.execute(INTAKE_CHUNK_TABLE)

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
