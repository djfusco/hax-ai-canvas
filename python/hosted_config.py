"""Deployment configuration. Diagnostics contain names, never secret values."""

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlsplit

from cryptography.fernet import Fernet


class HostedSetupError(ValueError):
    pass


def hosted_mode():
    return bool(os.environ.get("VERCEL")) or os.environ.get("HAX_HOSTED_ENABLED") == "1"


@dataclass(frozen=True)
class HostedSettings:
    database_url: str = field(repr=False)
    session_secret: str = field(repr=False)
    upload_secret: str = field(repr=False)
    internal_secret: str = field(repr=False)
    invite_code: str = field(repr=False)
    encryption_key: str = field(repr=False)
    cron_secret: str = field(repr=False)
    origin: str


def settings(environ=None):
    env = os.environ if environ is None else environ
    names = {
        "database_url": "DATABASE_URL", "session_secret": "HAX_SESSION_SECRET",
        "upload_secret": "HAX_UPLOAD_SIGNING_SECRET", "internal_secret": "HAX_INTERNAL_SECRET",
        "invite_code": "HAX_BETA_ACCESS_CODE", "encryption_key": "HAX_MENTOR_ENCRYPTION_KEY",
        "cron_secret": "CRON_SECRET", "origin": "HAX_APP_ORIGIN",
    }
    missing = [name for name in [*names.values(), "BLOB_READ_WRITE_TOKEN"] if not env.get(name)]
    if missing:
        raise HostedSetupError("Configure deployment variables: " + ", ".join(missing))
    for key in ("session_secret", "upload_secret", "internal_secret", "invite_code", "cron_secret"):
        value = env[names[key]]
        if len(value) < 32 or any(ord(c) < 32 for c in value):
            raise HostedSetupError(names[key] + " must contain at least 32 printable characters.")
    distinct = [env[names[k]] for k in
                ("session_secret", "upload_secret", "internal_secret", "invite_code", "cron_secret")]
    if len(set(distinct)) != len(distinct):
        raise HostedSetupError("Use different values for session, upload, internal, invitation and cron secrets.")
    try:
        Fernet(env["HAX_MENTOR_ENCRYPTION_KEY"].encode())
    except (ValueError, TypeError):
        raise HostedSetupError("HAX_MENTOR_ENCRYPTION_KEY must be a valid Fernet key.") from None
    origin = urlsplit(env["HAX_APP_ORIGIN"])
    if (origin.scheme != "https" or not origin.hostname or origin.username or origin.password
            or origin.path or origin.query or origin.fragment or origin.port not in (None, 443)):
        raise HostedSetupError("HAX_APP_ORIGIN must be a canonical HTTPS origin without a trailing slash.")
    if urlsplit(env["DATABASE_URL"]).scheme not in ("postgres", "postgresql"):
        raise HostedSetupError("DATABASE_URL must be a PostgreSQL connection URL.")
    return HostedSettings(**{key: env[name] for key, name in names.items()})


def sign_upload(record, secret):
    value = {
        "v": 1, "upload_id": record["id"], "owner": record["owner"],
        "workspace_id": record["workspace_id"], "pathname": record["pathname"],
        "size": record["size"], "kind": record["kind"],
        "expires_at": int(record["token_expires_at"]),
    }
    segment = base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=")
    signature = base64.urlsafe_b64encode(hmac.new(secret.encode(), segment, hashlib.sha256).digest()).rstrip(b"=")
    return segment.decode() + "." + signature.decode()


def verify_upload(ticket, secret, *, allow_expired=False, now=None):
    try:
        if not isinstance(ticket, str) or len(ticket) > 8192:
            raise ValueError()
        segment, signature = ticket.split(".")
        expected = base64.urlsafe_b64encode(hmac.new(secret.encode(), segment.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        value = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
        current = time.time() if now is None else now
        if (value.get("v") != 1 or type(value.get("expires_at")) not in (int, float)
                or value["expires_at"] > current + 3605
                or value["expires_at"] < current - (86400 if allow_expired else 0)):
            raise ValueError()
        return value
    except (ValueError, TypeError, KeyError):
        raise ValueError("Invalid or expired upload authorization.") from None
