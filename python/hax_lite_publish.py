"""Publish the course site into a person's HAX Lite ("connect and approve").

Implements HAX_LITE_PUBLISH_CONTRACT.md, section 3 option A, from the server:

1. ``discover``: read ``{hax-lite}/.well-known/hax-lite-publish``; require the connect door and
   use only the endpoint addresses it returns (they must be on the same HAX Lite).
2. ``start``: a random state and a PKCE pair, kept on the server; the browser is sent to the
   authorize endpoint with our callback as ``redirect_uri``.
3. ``finish`` (one server request): exchange the approval code for a pass, POST the course-site
   ZIP to the sites endpoint with a fresh Idempotency-Key, then poll the import until it is
   complete, partial or failed.

The pass is a local variable of ``finish`` only: never stored, logged or returned. These calls
send no Origin header; HAX Lite then gates them by the pass alone.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import re
import secrets
import socket
import time
from urllib.parse import quote, urlencode, urlsplit

SLUG = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
STATE = re.compile(r"^[A-Za-z0-9._~-]{8,200}$")
PENDING_SECONDS = 600
POLL_SECONDS = 3
POLL_LIMIT_SECONDS = 240  # stay inside the hosting platform's 300-second request limit
DONE = ("complete", "partial", "failed")
LOCAL_HOSTS = ("localhost", "127.0.0.1")


class PublishError(ValueError):
    """A publish step failed; the message is safe (and meant) to show the person."""


def normalize_address(value):
    """The HAX Lite origin: https only (http only for localhost)."""
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise PublishError("Enter your HAX Lite address, for example https://lite.example.edu.")
    value = value.strip().rstrip("/")
    if "://" not in value:
        value = "https://" + value
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise PublishError("That HAX Lite address is not valid.") from None
    host = (parts.hostname or "").lower()
    if (not host or parts.username or parts.password or parts.query or parts.fragment
            or parts.path not in ("", "/")):
        raise PublishError("Enter only the HAX Lite address, for example https://lite.example.edu.")
    if parts.scheme == "http" and host not in LOCAL_HOSTS:
        raise PublishError("The HAX Lite address must start with https:// (http:// only for localhost).")
    if parts.scheme not in ("http", "https"):
        raise PublishError("The HAX Lite address must start with https://.")
    return f"{parts.scheme}://{host}" + (f":{port}" if port else "")


def check_public(origin, allow_local):
    """Refuse addresses that resolve to private networks (unless running locally)."""
    host = urlsplit(origin).hostname
    if host in LOCAL_HOSTS:
        if allow_local:
            return
        raise PublishError("A localhost HAX Lite can only be used when Course Consultant runs on this computer.")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        raise PublishError(f"Could not find {host}. Check the HAX Lite address.") from None
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if not ip.is_global and not allow_local:
            raise PublishError("That HAX Lite address points to a private network, which is not allowed here.")


def _client(transport=None):
    import httpx
    return httpx.Client(transport=transport, timeout=httpx.Timeout(30, read=120), follow_redirects=False,
                        headers={"User-Agent": "CourseConsultant-HAXLitePublish/1"})


def _error_text(response, fallback):
    """HAX Lite reports every error as JSON {"error": "..."}; show that text."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), str) and body["error"].strip():
        return body["error"].strip()[:500]
    return f"{fallback} (HTTP {response.status_code})."


def discover(origin, transport=None):
    """The HAX Lite's publish description, checked against the contract."""
    import httpx
    try:
        with _client(transport) as client:
            response = client.get(f"{origin}/.well-known/hax-lite-publish")
    except httpx.HTTPError:
        raise PublishError(f"Could not reach {origin}. Check the address and try again.") from None
    if response.status_code == 404:
        raise PublishError(f"{origin} does not accept published sites (publishing is not switched on there).")
    if response.status_code != 200:
        raise PublishError(_error_text(response, f"{origin} did not describe its publishing options"))
    try:
        info = response.json()
    except ValueError:
        info = None
    if not isinstance(info, dict) or info.get("service") != "hax-lite-publish":
        raise PublishError(f"{origin} did not answer as a HAX Lite publishing service.")
    if not (info.get("doors") or {}).get("connect"):
        raise PublishError(f"{origin} has not switched on “connect and approve” publishing. "
                           "Ask its administrator to turn it on.")
    max_bytes = info.get("maxBytes")
    if type(max_bytes) is not int or max_bytes <= 0:
        raise PublishError(f"{origin} did not say how large a site it accepts.")
    endpoints = info.get("endpoints") or {}
    result = {"origin": origin, "maxBytes": max_bytes, "formats": info.get("formats") or []}
    for name in ("authorize", "token", "sites", "imports"):
        value = endpoints.get(name)
        try:
            parts = urlsplit(value) if isinstance(value, str) else None
        except ValueError:
            parts = None
        # The pass and the course are only ever sent to the HAX Lite the person named.
        if not parts or f"{parts.scheme}://{parts.netloc}".lower() != origin.lower():
            raise PublishError(f"{origin} returned a {name} address on a different website, so publishing stopped.")
        result[name] = value
    if "{id}" not in result["imports"]:
        raise PublishError(f"{origin} returned an imports address without a place for the import id.")
    if "application/zip" not in result["formats"]:
        raise PublishError(f"{origin} does not accept ZIP files.")
    return result


def check_size(info, size):
    if size > info["maxBytes"]:
        raise PublishError(f"The course site is {size / 1048576:.1f} MB, larger than the {info['maxBytes'] / 1048576:.1f} MB "
                           f"this HAX Lite accepts. Ask its administrator to raise the limit.")


def check_slug(slug):
    if not isinstance(slug, str) or not SLUG.match(slug):
        raise PublishError("The site path must start with a lowercase letter, followed by up to 39 lowercase "
                           "letters, digits or hyphens (for example ist-210-fall).")
    return slug


def pkce_pair():
    verifier = secrets.token_urlsafe(48)[:96]  # 43-128 characters from the unreserved set
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def new_state(workspace_id):
    """Random state that also says which course it is for ("<workspace>.<random>")."""
    state = f"{workspace_id}.{secrets.token_urlsafe(32)}"
    assert STATE.match(state)
    return state


def workspace_from_state(state):
    if not isinstance(state, str) or not STATE.match(state) or "." not in state:
        raise PublishError("This approval link is not valid. Start publishing again from Course Consultant.")
    return state.split(".", 1)[0]


def authorize_url(info, redirect_uri, challenge, state):
    query = urlencode({"redirect_uri": redirect_uri, "code_challenge": challenge,
                       "code_challenge_method": "S256", "state": state})
    return info["authorize"] + ("&" if "?" in info["authorize"] else "?") + query


def finish(pending, code, archive, transport=None, sleep=time.sleep, clock=time.monotonic, progress=None):
    """Exchange the code, publish the archive and wait for the import. Returns the final view."""
    import httpx
    if not isinstance(code, str) or not code.strip() or len(code) > 500:
        raise PublishError("HAX Lite did not return an approval code. Start publishing again.")
    check_size(pending, len(archive))
    with _client(transport) as client:
        try:
            response = client.post(pending["exchange_url"], json={
                "grant_type": "authorization_code", "code": code, "code_verifier": pending["verifier"],
                "redirect_uri": pending["redirect_uri"]})
        except httpx.HTTPError:
            raise PublishError("Could not reach HAX Lite to finish the approval. Try again.") from None
        if response.status_code != 200:
            raise PublishError(_error_text(response, "HAX Lite did not accept the approval"))
        try:
            access = response.json().get("access_token")
        except (ValueError, AttributeError):
            access = None
        if not isinstance(access, str) or not access:
            raise PublishError("HAX Lite did not return a publish pass.")
        headers = {"Authorization": "Bearer " + access}
        del access  # the pass lives only in these request headers, for this request
        key = "cc-" + secrets.token_hex(16)  # fresh for this publish; reused only on a retry below
        query = {"slug": pending["slug"]}
        if pending.get("title"):
            query["title"] = pending["title"]
        url = pending["sites_url"] + ("&" if "?" in pending["sites_url"] else "?") + urlencode(query, quote_via=quote)
        response = None
        for attempt in range(2):
            try:
                response = client.post(url, content=archive, headers={
                    **headers, "Content-Type": "application/zip", "Idempotency-Key": key})
            except httpx.TransportError:
                response = None
            if response is not None and response.status_code < 500:
                break
            if attempt == 0:
                sleep(2)  # one retry with the same key returns the original answer, never a second site
        if response is None:
            raise PublishError("Could not reach HAX Lite to upload the course site. Try again.")
        if response.status_code != 202:
            raise PublishError(_error_text(response, "HAX Lite did not accept the course site"))
        view = response.json()
        import_id = ((view or {}).get("import") or {}).get("id")
        if not isinstance(import_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,100}", import_id):
            raise PublishError("HAX Lite accepted the site but did not return an import id.")
        status_url = pending["imports_url"].replace("{id}", quote(import_id, safe=""))
        started = clock()
        while (view.get("import") or {}).get("status") not in DONE:
            if progress:
                progress(view)
            if clock() - started > POLL_LIMIT_SECONDS:
                view["still_running"] = True
                break
            sleep(POLL_SECONDS)
            try:
                response = client.get(status_url, headers=headers)
            except httpx.TransportError:
                continue
            if response.status_code != 200:
                raise PublishError(_error_text(response, "HAX Lite could not report the import"))
            view = response.json()
    return summarize(view)


def summarize(view):
    """What the person needs to see: status, site link and the report."""
    imp = view.get("import") or {}
    site = view.get("site") or {}
    report = imp.get("report") or {}

    def items(value):
        result = []
        for entry in value if isinstance(value, list) else []:
            if isinstance(entry, str):
                result.append(entry[:300])
            elif isinstance(entry, dict):
                result.append("; ".join(f"{k}: {v}" for k, v in entry.items() if isinstance(v, (str, int, float)))[:300])
        return result[:100]

    url = site.get("url") or site.get("defaultUrl")
    if not (isinstance(url, str) and url.startswith(("https://", "http://"))):
        url = None
    return {
        "status": imp.get("status") if isinstance(imp.get("status"), str) else "unknown",
        "still_running": bool(view.get("still_running")),
        "site": {"url": url, "title": site.get("title"), "slug": site.get("slug")},
        "report": {"error": report.get("error") if isinstance(report.get("error"), str) else None,
                   "skipped": items(report.get("skipped")), "warnings": items(report.get("warnings")),
                   "pages": report.get("pagesImported", report.get("pages")),
                   "files": report.get("filesImported", report.get("files"))},
    }
