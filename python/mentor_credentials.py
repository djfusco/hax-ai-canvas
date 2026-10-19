"""Local encrypted credential retention for the mentoring workspace.

The browser holds only a signed opaque identity. Provider keys never belong in
cookies, chat transcripts, URLs, or API responses. A private local key file
protects against accidental plaintext disclosure, not a compromised host.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from uuid import uuid4
from contextlib import contextmanager

from cryptography.fernet import Fernet, InvalidToken

PROVIDERS = ("nebula", "anthropic", "openai", "gemini")
SESSION_TTL = 12 * 60 * 60
REMEMBER_TTL = 30 * 24 * 60 * 60


def private_key(path: Path) -> bytes:
    """Create key material once, exclusively, with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        return path.read_bytes()
    key = Fernet.generate_key()
    fd, name = tempfile.mkstemp(prefix=".key-bootstrap-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Publish only the fully written inode; a competing creator wins
            # without exposing an empty key file or rotating an existing key.
            os.link(temporary, path)
            return key
        except FileExistsError:
            return path.read_bytes()
    finally:
        temporary.unlink(missing_ok=True)


class CredentialVault:
    def __init__(self, root: Path, clock=time.time):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "credentials.sqlite"
        configured = os.environ.get("HAX_MENTOR_ENCRYPTION_KEY")
        self.cipher = Fernet(configured.encode() if configured else private_key(self.root / ".vault-key"))
        self.clock = clock
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS credentials (
                owner TEXT NOT NULL, provider TEXT NOT NULL,
                sealed BLOB NOT NULL, expires REAL NOT NULL, remembered INTEGER NOT NULL,
                PRIMARY KEY(owner, provider))""")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def _db(self):
        connection = sqlite3.connect(self.path, timeout=20)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def save(self, owner: str, provider: str, values: dict, remember: bool = False):
        if provider not in PROVIDERS:
            raise ValueError("Unsupported AI provider.")
        key = str(values.get("api_key", "")).strip()
        if not key or len(key) > 2048 or any(ord(c) < 32 for c in key):
            raise ValueError("Enter a valid provider API key.")
        model = str(values.get("model", "")).strip()
        if model:
            from provider_models import model_id
            model_id(model)
        if len(model) > 120 or any(ord(c) < 32 for c in model):
            raise ValueError("Invalid model name.")
        if model and key in model:
            raise ValueError("A model identifier must not contain the provider key.")
        payload = {"api_key": key, "model": model, "base_url": values.get("base_url", ""),
                   "version": uuid4().hex}
        sealed = self.cipher.encrypt(json.dumps(payload).encode())
        expires = self.clock() + (REMEMBER_TTL if remember else SESSION_TTL)
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO credentials VALUES (?, ?, ?, ?, ?)",
                       (owner, provider, sealed, expires, int(remember)))
        return {"provider": provider, "configured": True,
                "expires_at": expires, "remembered": remember}

    def get(self, owner: str, provider: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT sealed, expires FROM credentials WHERE owner=? AND provider=?",
                             (owner, provider)).fetchone()
            if row is None:
                return None
            if row[1] <= self.clock():
                db.execute("DELETE FROM credentials WHERE owner=? AND provider=? AND expires<=?",
                           (owner, provider, self.clock()))
                return None
        try:
            return json.loads(self.cipher.decrypt(row[0]))
        except (InvalidToken, ValueError):
            raise ValueError("Saved credentials could not be unlocked. Re-enter your key.") from None

    def status(self, owner: str) -> list[dict]:
        with self._db() as db:
            db.execute("DELETE FROM credentials WHERE expires <= ?", (self.clock(),))
            rows = db.execute("SELECT provider, expires, remembered FROM credentials WHERE owner=?",
                              (owner,)).fetchall()
        return [{"provider": p, "configured": True, "expires_at": expiry,
                 "remembered": bool(remember)} for p, expiry, remember in rows]

    def forget(self, owner: str, provider: str):
        with self._db() as db:
            db.execute("DELETE FROM credentials WHERE owner=? AND provider=?", (owner, provider))

    def update_preferences(self, owner, provider, expected_version, changes):
        """Compare-and-swap prevents a slow catalog response overwriting a new key."""
        from mentor_store import WorkspaceConflict
        if not isinstance(changes, dict) or set(changes) - {"model", "catalog", "catalog_expires", "capabilities"}:
            raise ValueError("Invalid provider preferences.")
        with self._db() as db:
            row = db.execute("SELECT sealed, expires FROM credentials WHERE owner=? AND provider=?",
                             (owner, provider)).fetchone()
            if row is None or row[1] <= self.clock():
                raise WorkspaceConflict("Credentials expired or were removed.")
            saved = json.loads(self.cipher.decrypt(row[0]))
            if saved.get("version") != expected_version:
                raise WorkspaceConflict("Credentials changed. Refresh models for the new key.")
            if changes.get("model") and saved["api_key"] in changes["model"]:
                raise ValueError("A model identifier must not contain the provider key.")
            if "model" in changes and "capabilities" not in changes:
                saved.pop("capabilities", None)
            if "capabilities" in changes:
                from model_capabilities import validate_capabilities
                changes = {**changes, "capabilities": validate_capabilities(
                    provider, changes.get("model", saved.get("model")), changes["capabilities"])}
            saved.update(changes)
            sealed = self.cipher.encrypt(json.dumps(saved, allow_nan=False).encode())
            cursor = db.execute("UPDATE credentials SET sealed=? WHERE owner=? AND provider=? AND sealed=?",
                                (sealed, owner, provider, row[0]))
            if cursor.rowcount != 1:
                raise WorkspaceConflict("Provider settings changed. Please retry.")
        return saved
