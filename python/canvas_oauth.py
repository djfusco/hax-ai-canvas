"""Institutional Canvas connection (OAuth2 developer keys).

An administrator lists participating institutions in HAX_CANVAS_INSTITUTIONS,
a JSON array, for example:

  [{"id": "state-u", "name": "State University",
    "base_url": "https://canvas.state.edu",
    "client_id": "170000000000123",
    "client_secret_env": "HAX_CANVAS_SECRET_STATE_U",
    "scopes": ["url:GET|/api/v1/courses", "url:GET|/api/v1/courses/:id"]}]

The client secret itself lives in the named environment variable, never in
the JSON. ``scopes`` is optional; when the institution's developer key
enforces scopes, list the read-only endpoints from canvas_collect.ALLOWED.

The access token is kept only in an encrypted, HttpOnly cookie for one hour
and is never stored server-side.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from urllib.parse import urlencode, urlsplit

TOKEN_COOKIE = "canvas_token"
STATE_COOKIE = "canvas_oauth"
TOKEN_SECONDS = 3600


class CanvasConfigError(ValueError):
    """The institutional Canvas configuration is invalid."""


def institutions(environ=None):
    environ = os.environ if environ is None else environ
    raw = environ.get("HAX_CANVAS_INSTITUTIONS", "").strip()
    if not raw:
        return {}
    try:
        rows = json.loads(raw)
    except ValueError:
        raise CanvasConfigError("HAX_CANVAS_INSTITUTIONS is not valid JSON.") from None
    if not isinstance(rows, list):
        raise CanvasConfigError("HAX_CANVAS_INSTITUTIONS must be a JSON array.")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise CanvasConfigError("Each institution must be an object.")
        ident, name, base_url, client_id = (row.get(k) for k in ("id", "name", "base_url", "client_id"))
        secret_env = row.get("client_secret_env")
        if not isinstance(ident, str) or not re.fullmatch(r"[a-z0-9-]{1,40}", ident):
            raise CanvasConfigError("Institution ids must be lowercase letters, digits or hyphens.")
        if not isinstance(name, str) or not name.strip() or not isinstance(client_id, str) or not client_id.strip():
            raise CanvasConfigError(f"Institution {ident} needs a name and client_id.")
        parsed = urlsplit(base_url if isinstance(base_url, str) else "")
        if parsed.scheme != "https" and parsed.hostname not in ("127.0.0.1", "localhost"):
            raise CanvasConfigError(f"Institution {ident} needs an HTTPS base_url.")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username:
            raise CanvasConfigError(f"Institution {ident}'s base_url must be just the Canvas origin.")
        secret = environ.get(secret_env or "", "")
        if not secret:
            raise CanvasConfigError(f"Set the client secret for {ident} in {secret_env or 'client_secret_env'}.")
        scopes = row.get("scopes", [])
        if not isinstance(scopes, list) or any(not isinstance(s, str) for s in scopes):
            raise CanvasConfigError(f"Institution {ident}'s scopes must be a list of strings.")
        result[ident] = {"id": ident, "name": name.strip(), "base_url": f"{parsed.scheme}://{parsed.netloc}",
                         "client_id": client_id.strip(), "client_secret": secret, "scopes": scopes}
    return result


def authorize_url(institution, redirect_uri, state):
    params = {"client_id": institution["client_id"], "response_type": "code",
              "redirect_uri": redirect_uri, "state": state}
    if institution["scopes"]:
        params["scope"] = " ".join(institution["scopes"])
    return institution["base_url"] + "/login/oauth2/auth?" + urlencode(params)


def exchange_code(institution, code, redirect_uri, *, transport=None):
    import httpx
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=20, transport=transport) as http:
        response = http.post(institution["base_url"] + "/login/oauth2/token", data={
            "grant_type": "authorization_code", "client_id": institution["client_id"],
            "client_secret": institution["client_secret"], "redirect_uri": redirect_uri, "code": code})
    try:
        token = response.json().get("access_token") if response.status_code == 200 else None
    except ValueError:
        token = None
    if not isinstance(token, str) or not token.strip():
        raise ValueError("Canvas did not issue access. Try connecting again.")
    return token.strip()


def revoke(institution, token, *, transport=None):
    """Best effort: ask Canvas to revoke the token."""
    import httpx
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=10, transport=transport) as http:
            http.delete(institution["base_url"] + "/login/oauth2/token",
                        headers={"Authorization": "Bearer " + token})
    except httpx.HTTPError:
        pass


def _fernet(key):
    from cryptography.fernet import Fernet
    material = key if isinstance(key, bytes) else str(key).encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"canvas-token:" + material).digest()))


def seal_token(key, owner, institution_id, token):
    payload = {"owner": owner, "institution": institution_id, "token": token, "expires": time.time() + TOKEN_SECONDS}
    return _fernet(key).encrypt(json.dumps(payload).encode()).decode()


def open_token(key, owner, sealed):
    """Return (institution_id, token) or None if missing, expired or not this user's."""
    from cryptography.fernet import InvalidToken
    if not sealed:
        return None
    try:
        payload = json.loads(_fernet(key).decrypt(sealed.encode(), ttl=TOKEN_SECONDS))
    except (InvalidToken, ValueError):
        return None
    if payload.get("owner") != owner or payload.get("expires", 0) < time.time():
        return None
    return payload.get("institution"), payload.get("token")
