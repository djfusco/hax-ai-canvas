"""Verified Neon identity with encrypted sessions and ownership stored locally."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

from cryptography.fernet import Fernet, InvalidToken

from hosted_accounts import (
    AccountAuthError, COOKIE_NAMES, new_session_cookies, session_cookies, validate_auth_url,
)
from mentor_credentials import CredentialVault, private_key
from mentor_store import MentorStore, WorkspaceConflict

LOCAL_SESSION_COOKIE = "mentor_account_session"


def auth_url(root):
    configured = os.environ.get("NEON_AUTH_BASE_URL", "")
    if not configured:
        path = Path(root) / "auth-settings.json"
        if path.exists():
            configured = json.loads(path.read_text(encoding="utf-8")).get("auth_url", "")
    return validate_auth_url(configured) if configured else ""


def configure_auth(root, value):
    """The Auth Base URL is public configuration, never an API/admin credential."""
    if not isinstance(value, str):
        raise ValueError("Enter a valid Neon Auth Base URL.")
    if auth_url(root):
        raise WorkspaceConflict("Email authentication is already configured.")
    value = validate_auth_url(value)
    directory = Path(root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "auth-settings.json").open("x", encoding="utf-8") as stream:
        os.chmod(stream.name, 0o600)
        json.dump({"auth_url": value}, stream)


class LocalAccounts:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "accounts.sqlite"
        self.cipher = Fernet(private_key(self.root / ".account-key"))
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS local_accounts "
                       "(subject TEXT PRIMARY KEY, owner TEXT NOT NULL UNIQUE)")
            db.execute("CREATE TABLE IF NOT EXISTS local_claims "
                       "(owner TEXT PRIMARY KEY, subject TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS local_sessions "
                       "(id TEXT PRIMARY KEY, sealed BLOB NOT NULL, expires REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS local_auth_limits "
                       "(id TEXT PRIMARY KEY, window INTEGER NOT NULL, attempts INTEGER NOT NULL)")
        self.path.chmod(0o600)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def allow_attempt(self, label):
        key = hashlib.sha256(label.encode()).hexdigest()
        window = int(time.time() // 300)
        with self._db() as db:
            db.execute("DELETE FROM local_auth_limits WHERE window < ?", (window,))
            db.execute("INSERT INTO local_auth_limits VALUES (?, ?, 1) "
                       "ON CONFLICT(id) DO UPDATE SET attempts=attempts+1", (key, window))
            attempts = db.execute("SELECT attempts FROM local_auth_limits WHERE id=?", (key,)).fetchone()[0]
        return attempts <= 10

    def resolve(self, issuer, identity, *, legacy_owner=None):
        subject = hashlib.sha256((issuer + "\0" + identity["subject"]).encode()).hexdigest()
        MentorStore(self.root / "workspaces.sqlite")
        CredentialVault(self.root)
        with self._db() as db:
            db.execute("ATTACH DATABASE ? AS workspaces", (str(self.root / "workspaces.sqlite"),))
            db.execute("ATTACH DATABASE ? AS vault", (str(self.root / "credentials.sqlite"),))
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO local_accounts VALUES (?, ?) ON CONFLICT(subject) DO NOTHING",
                       (subject, secrets.token_hex(32)))
            owner = db.execute("SELECT owner FROM local_accounts WHERE subject=?", (subject,)).fetchone()[0]
            if legacy_owner:
                if not re.fullmatch(r"[a-f0-9]{64}", legacy_owner):
                    raise WorkspaceConflict("Invalid browser claim.")
                db.execute("INSERT INTO local_claims VALUES (?, ?) ON CONFLICT(owner) DO NOTHING",
                           (legacy_owner, subject))
                claimed = db.execute("SELECT subject FROM local_claims WHERE owner=?", (legacy_owner,)).fetchone()[0]
                if claimed != subject:
                    raise WorkspaceConflict("This browser's work has already been claimed.")
                rows = db.execute("SELECT state FROM workspaces.mentor_workspaces WHERE owner=?",
                                  (legacy_owner,)).fetchall()
                if any(json.loads(row[0]).get("active_job") for row in rows):
                    raise WorkspaceConflict("Finish or cancel local jobs before claiming this browser.")
                legacy_keys = db.execute("SELECT 1 FROM vault.credentials WHERE owner=?", (legacy_owner,)).fetchone()
                target_work = db.execute("SELECT 1 FROM workspaces.mentor_workspaces WHERE owner=?", (owner,)).fetchone()
                target_keys = db.execute("SELECT 1 FROM vault.credentials WHERE owner=?", (owner,)).fetchone()
                if (rows or legacy_keys) and (target_work or target_keys):
                    raise WorkspaceConflict("Automatic account merging is not supported.")
                db.execute("UPDATE workspaces.mentor_workspaces SET owner=? WHERE owner=?", (owner, legacy_owner))
                db.execute("UPDATE vault.credentials SET owner=? WHERE owner=?", (owner, legacy_owner))
        return owner

    @staticmethod
    def _session_id(value):
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
            return None
        return hashlib.sha256(value.encode()).hexdigest()

    def save_session(self, issuer, updates):
        cookies = new_session_cookies(updates)
        if not cookies:
            raise AccountAuthError("The authentication service did not establish a session.", 503)
        token = secrets.token_hex(32)
        ages = [age for _, value, age in updates if value and age is not None and age > 0]
        expires = time.time() + min([12 * 3600, *ages])
        sealed = self.cipher.encrypt(json.dumps({"issuer": issuer, "cookies": cookies}).encode())
        with self._db() as db:
            db.execute("DELETE FROM local_sessions WHERE expires<=?", (time.time(),))
            db.execute("INSERT INTO local_sessions VALUES (?, ?, ?)",
                       (self._session_id(token), sealed, expires))
        return token

    def cookies(self, token, issuer):
        identifier = self._session_id(token)
        if not identifier:
            return ""
        with self._db() as db:
            row = db.execute("SELECT sealed, expires FROM local_sessions WHERE id=?", (identifier,)).fetchone()
        if not row or row[1] <= time.time():
            self.forget_session(token)
            return ""
        try:
            value = json.loads(self.cipher.decrypt(row[0]))
            return value["cookies"] if value["issuer"] == issuer else ""
        except (InvalidToken, ValueError, KeyError):
            self.forget_session(token)
            return ""

    def refresh_session(self, token, issuer, updates):
        if not updates:
            return
        existing = self.cookies(token, issuer)
        from http.cookies import SimpleCookie
        parsed = SimpleCookie()
        parsed.load(existing)
        values = {name: morsel.value for name, morsel in parsed.items() if name in COOKIE_NAMES}
        for name, value, age in updates:
            if age == 0 or not value:
                values.pop(name, None)
            else:
                values[name] = value
        cookies = session_cookies(values)
        if not cookies:
            self.forget_session(token)
            return
        sealed = self.cipher.encrypt(json.dumps({"issuer": issuer, "cookies": cookies}).encode())
        with self._db() as db:
            db.execute("UPDATE local_sessions SET sealed=? WHERE id=?",
                       (sealed, self._session_id(token)))

    def forget_session(self, token):
        identifier = self._session_id(token)
        if identifier:
            with self._db() as db:
                db.execute("DELETE FROM local_sessions WHERE id=?", (identifier,))
