"""Deployment configuration. Diagnostics contain names, never secret values."""

from dataclasses import dataclass, field
import os
from urllib.parse import urlsplit

from cryptography.fernet import Fernet


class HostedSetupError(ValueError):
    pass


def hosted_mode():
    if os.environ.get("HAX_EXECUTION_MODE") == "local":
        return False
    return bool(os.environ.get("VERCEL")) or os.environ.get("HAX_HOSTED_ENABLED") == "1"


@dataclass(frozen=True)
class HostedSettings:
    database_url: str = field(repr=False)
    session_secret: str = field(repr=False)
    invite_code: str = field(repr=False)
    encryption_key: str = field(repr=False)
    origin: str
    auth_url: str = ""
    auth_mode: str = "neon"


def settings(environ=None):
    env = os.environ if environ is None else environ
    names = {
        "database_url": "DATABASE_URL", "session_secret": "HAX_SESSION_SECRET",
        "invite_code": "HAX_BETA_ACCESS_CODE", "encryption_key": "HAX_MENTOR_ENCRYPTION_KEY",
        "origin": "HAX_APP_ORIGIN",
    }
    mode = env.get("HAX_AUTH_MODE", "neon")
    if mode not in ("neon", "browser-beta"):
        raise HostedSetupError("HAX_AUTH_MODE must be neon or browser-beta.")
    # The invitation code is optional with email accounts: without it, anyone who verifies their
    # email can sign in. Browser-only mode has no accounts, so it always needs the code.
    required = [name for key, name in names.items() if key != "invite_code" or mode == "browser-beta"]
    if mode == "neon":
        required.append("NEON_AUTH_BASE_URL")
    missing = [name for name in required if not env.get(name)]
    if missing:
        raise HostedSetupError("Configure deployment variables: " + ", ".join(missing))
    # The session secret signs identities and links, so it must be long and random. The invitation
    # code is only a gate that people type (attempts are rate-limited, and accounts still verify their
    # email), so a short, memorable code is allowed.
    for key, minimum in (("session_secret", 32), ("invite_code", 6)):
        value = env.get(names[key], "")
        if key == "invite_code" and not value:
            continue
        if len(value.strip()) < minimum or any(ord(c) < 32 for c in value):
            raise HostedSetupError(f"{names[key]} must contain at least {minimum} printable characters.")
    if env[names["session_secret"]] == env.get(names["invite_code"]):
        raise HostedSetupError("Use different values for the session secret and the invitation code.")
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
    auth_url = env.get("NEON_AUTH_BASE_URL", "") if mode == "neon" else ""
    if auth_url:
        from hosted_accounts import validate_auth_url
        try:
            validate_auth_url(auth_url)
        except ValueError:
            raise HostedSetupError("NEON_AUTH_BASE_URL must be the HTTPS Auth Base URL from Neon.") from None
    return HostedSettings(**{key: env.get(name, "") for key, name in names.items()},
                          auth_url=auth_url, auth_mode=mode)

