"""Neon OTP/session transport and private account ownership.

REST paths, proxy header and cookie prefix verified against
@neondatabase/auth 0.6.0-beta's framework-independent server transport.
No auth responses, cookies, OTPs or user emails are logged.
"""
from datetime import datetime, timezone
import hashlib
from http.cookies import SimpleCookie
import json
import re
import secrets
from urllib.parse import urlsplit

from hosted_data import _Postgres
from mentor_store import WorkspaceConflict

COOKIE_NAMES = ("__Secure-neon-auth.session_token", "__Secure-neonauth.session_token")


class AccountAuthError(ValueError):
    def __init__(self, message="Sign-in could not be verified. Please retry.", status=400):
        super().__init__(message)
        self.status = status


def validate_auth_url(value):
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and not parsed.username and not parsed.password
                 and parsed.port in (None, 443) and not parsed.query and not parsed.fragment
                 and re.fullmatch(r"ep-[a-z0-9-]+\.neonauth\.[a-z0-9.-]+\.neon\.(tech|build)",
                                  parsed.hostname or "")
                 and re.fullmatch(r"/[A-Za-z0-9_-]+/auth", parsed.path))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("NEON_AUTH_BASE_URL must be the HTTPS Auth Base URL from Neon.")
    return value


def session_cookies(cookies):
    return "; ".join(name + "=" + cookies[name] for name in COOKIE_NAMES
                     if name in cookies and re.fullmatch(r"[A-Za-z0-9_.%~-]{1,4096}", cookies[name]))


def verified_identity(data):
    try:
        user, session = data["user"], data["session"]
        expires = datetime.fromisoformat(session["expiresAt"].replace("Z", "+00:00"))
        if (not isinstance(user["id"], str) or not 1 <= len(user["id"]) <= 256
                or user.get("emailVerified") is not True or user.get("banned")
                or not isinstance(user.get("email"), str) or len(user["email"]) > 320
                or session.get("userId", user["id"]) != user["id"]
                or expires.tzinfo is None or expires <= datetime.now(timezone.utc)):
            raise ValueError()
        return {"subject": user["id"], "email": user["email"]}
    except (KeyError, TypeError, ValueError, AttributeError):
        raise AccountAuthError("A verified, unexpired email session is required.", 401) from None


class NeonAuth:
    def __init__(self, base_url, origin, transport=None):
        self.base_url = validate_auth_url(base_url)
        self.origin = origin
        self.transport = transport

    def call(self, operation, *, cookies="", data=None):
        routes = {"send": ("POST", "email-otp/send-verification-otp"),
                  "verify": ("POST", "sign-in/email-otp"),
                  "session": ("GET", "get-session?disableCookieCache=true"),
                  "logout": ("POST", "sign-out")}
        if operation not in routes:
            raise ValueError("Unsupported authentication operation.")
        import httpx
        method, path = routes[operation]
        headers = {"Origin": self.origin, "Accept": "application/json",
                   "x-neon-auth-middleware": "true", "x-neon-auth-proxy": "flask"}
        if cookies:
            headers["Cookie"] = cookies
        try:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=10,
                              transport=self.transport) as client:
                with client.stream(method, self.base_url + "/" + path, headers=headers,
                                   json=data if method == "POST" else None) as response:
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > 128 * 1024:
                            raise AccountAuthError("Authentication response exceeded its limit.", 503)
                    if operation == "session" and response.status_code == 401:
                        return None, []
                    if not response.is_success:
                        status = 429 if response.status_code == 429 else 503 if response.status_code >= 500 else 400
                        raise AccountAuthError("Authentication unavailable or code rejected. Please retry.", status)
                    result = json.loads(content)
                    if result is not None and not isinstance(result, dict):
                        raise ValueError()
                    accepted = []
                    for header in response.headers.get_list("set-cookie"):
                        parsed = SimpleCookie()
                        parsed.load(header)
                        for name, morsel in parsed.items():
                            if name in COOKIE_NAMES:
                                age = int(morsel["max-age"]) if morsel["max-age"] else None
                                accepted.append((name, morsel.value, None if age is None else min(age, 30 * 86400)))
                    return result, accepted
        except AccountAuthError:
            raise
        except Exception:
            raise AccountAuthError("The authentication service is unavailable. Please retry.", 503) from None

    def session(self, cookies):
        if not cookies:
            return None, []
        data, updates = self.call("session", cookies=cookies)
        try:
            account = verified_identity(data) if data else None
        except AccountAuthError:
            account = None
        if account is None:
            updates.extend((name, "", 0) for name in COOKIE_NAMES)
        return account, updates


class PostgresAccounts(_Postgres):
    def __init__(self, dsn, connect=None):
        self._configure(dsn, connect)
        with self._session() as db:
            db.execute("CREATE TABLE IF NOT EXISTS hax_accounts ("
                       "subject TEXT PRIMARY KEY, owner TEXT NOT NULL UNIQUE, admitted INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS hax_browser_claims ("
                       "owner TEXT PRIMARY KEY, subject TEXT NOT NULL)")

    @staticmethod
    def subject(issuer, identity):
        return hashlib.sha256((issuer + "\0" + identity["subject"]).encode()).hexdigest()

    def resolve(self, issuer, identity, *, admit=False, legacy_owner=None):
        subject = self.subject(issuer, identity)
        with self._session(locking=True) as db:
            db.execute("INSERT INTO hax_accounts VALUES (?,?,?) ON CONFLICT(subject) DO NOTHING",
                       (subject, secrets.token_hex(32), int(admit)))
            owner, admitted = db.execute("SELECT owner, admitted FROM hax_accounts WHERE subject=? FOR UPDATE",
                                         (subject,)).fetchone()
            if not admitted and not admit:
                raise AccountAuthError("Enter the beta invitation code before your first sign-in.", 403)
            if admit and not admitted:
                db.execute("UPDATE hax_accounts SET admitted=1 WHERE subject=?", (subject,))
            if legacy_owner:
                self._claim(db, subject, owner, legacy_owner)
            return owner

    def _claim(self, db, subject, owner, legacy):
        if not re.fullmatch(r"[a-f0-9]{64}", legacy):
            raise WorkspaceConflict("Invalid browser claim.")
        db.execute("INSERT INTO hax_browser_claims VALUES (?,?) ON CONFLICT(owner) DO NOTHING",
                   (legacy, subject))
        claimed = db.execute("SELECT subject FROM hax_browser_claims WHERE owner=? FOR UPDATE",
                             (legacy,)).fetchone()[0]
        if claimed != subject:
            raise WorkspaceConflict("This browser's legacy work has already been claimed.")
        # Same lock/order as upload admission; old deployments cannot race a
        # legacy upload reservation through the ownership migration.
        db.execute("INSERT INTO hax_upload_owners VALUES (?) ON CONFLICT DO NOTHING", (legacy,))
        db.execute("SELECT owner FROM hax_upload_owners WHERE owner=? FOR UPDATE", (legacy,)).fetchone()
        rows = db.execute("SELECT state FROM mentor_workspaces WHERE owner=?", (legacy,)).fetchall()
        uploads = db.execute("SELECT state FROM hax_uploads WHERE owner=? FOR UPDATE", (legacy,)).fetchall()
        if any(json.loads(row[0]).get("active_job") for row in rows):
            raise WorkspaceConflict("Finish or cancel legacy jobs before claiming this browser.")
        if any(json.loads(row[0])["status"] not in ("ready", "failed", "canceled") for row in uploads):
            raise WorkspaceConflict("Finish or cancel legacy uploads before claiming this browser.")
        if db.execute("SELECT state FROM mentor_workspaces WHERE owner=?", (owner,)).fetchone() or \
                db.execute("SELECT provider FROM hax_credentials WHERE owner=?", (owner,)).fetchone():
            if rows or uploads or db.execute("SELECT provider FROM hax_credentials WHERE owner=?", (legacy,)).fetchone():
                raise WorkspaceConflict("Automatic account merging is not supported. Keep the existing work separate.")
        db.execute("UPDATE mentor_workspaces SET owner=? WHERE owner=?", (owner, legacy))
        db.execute("UPDATE hax_credentials SET owner=? WHERE owner=?", (owner, legacy))
        for row in uploads:
            record = json.loads(row[0])
            record["owner"] = owner
            # Registered raw/prepared paths remain unchanged; never copy raw data.
            db.execute("UPDATE hax_uploads SET owner=?, state=? WHERE id=? AND owner=?",
                       (owner, json.dumps(record), record["id"], legacy))
