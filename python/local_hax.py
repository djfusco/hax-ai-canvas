"""Build approved local snapshots with the managed HAX CLI; own only our children.

HAX_HAX_CLI must identify the absolute JS entry of @haxtheweb/create 26.8.1.
The CLI's `serve` currently shells out to unpinned npx and binds all interfaces.
Instead we resolve its installed server and enforce loopback in a child-only
bootstrap, without modifying the package, caller environment, or user's sites.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
from urllib.parse import unquote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from bs4 import BeautifulSoup

from imscc_content import ITEM_PREFIX, URL_ATTRIBUTES, sanitize_import_html


CLI_VERSION = "26.8.1"
_SAFE_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}\Z")
_OPEN = build_opener(ProxyHandler({}))
_PROCESSES = set()
_PROCESS_LOCK = threading.Lock()
_RUNTIME = r"""
const {createRequire} = require('module');
const managed = createRequire(process.argv[1]);
// Never load a user-edited site's dotenv into this controlled runtime.
managed('dotenv').config = () => ({parsed:{}});
const net = require('net');
const original = net.Server.prototype.listen;
net.Server.prototype.listen = function(port, ...args) {
  if (typeof port !== 'number') throw new Error('Unsupported listener');
  const callback = args.find(arg => typeof arg === 'function');
  return original.call(this, {port, host:'127.0.0.1'}, callback);
};
// Protect the JWT-disabled local editor from DNS rebinding and cross-origin
// requests, including another loopback port. Top-level user navigation is safe.
const http = require('http');
const emit = http.Server.prototype.emit;
http.Server.prototype.emit = function(event, req, res, ...rest) {
  if (event === 'request') {
    const port = this.address().port;
    const host = '127.0.0.1:' + port;
    const origin = 'http://' + host;
    const method = req.method;
    const write = !['GET','HEAD','OPTIONS'].includes(method);
    const foreign = req.headers['sec-fetch-site'];
    const navigation = !write && req.headers['sec-fetch-mode'] === 'navigate'
      && req.headers['sec-fetch-user'] === '?1'
      && !req.url.includes('/api/');
    if (req.headers.host !== host ||
        (req.headers.origin && req.headers.origin !== origin) ||
        (write && req.headers.origin !== origin) ||
        (foreign && !['none','same-origin'].includes(foreign) && !navigation)) {
      res.writeHead(403, {'Content-Type':'text/plain'});
      res.end('Local HAX requires same-origin access.');
      return true;
    }
  }
  return emit.call(this, event, req, res, ...rest);
};
const runtime = managed.resolve('@haxtheweb/haxcms-nodejs/package.json');
const path = require('path');
const {server, serverReady} = require(path.join(path.dirname(runtime), 'dist/app.js'));
serverReady.then(port => {
  const address = server.address();
  if (address.address !== '127.0.0.1') process.exit(1);
  console.log('HAX_LOCAL_READY:' + port);
});
"""


def _identifier(value):
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ValueError("Invalid local workspace/build identifier.")
    return value


def _environment(home):
    """Allowlist, rather than trying to enumerate provider/cloud secret names."""
    env = {key: os.environ[key] for key in (
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
        "LANG", "LC_ALL",
    ) if key in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), CI="true",
               NO_COLOR="1", NODE_ENV="test", PORT="0", HOST="127.0.0.1",
               HAXCMS_DISABLE_JWT_CHECKS="true",
               DOTENV_CONFIG_PATH=str(home / "no-dotenv"),
               npm_config_update_notifier="false", npm_config_audit="false")
    return env


def _toolchain():
    entry = Path(os.environ.get("HAX_HAX_CLI", ""))
    if not entry.is_absolute() or not entry.is_file():
        raise RuntimeError("Managed HAX CLI missing. Reinstall the local application.")
    try:
        package = json.loads((entry.parent.parent / "package.json").read_text())
    except (OSError, ValueError):
        raise RuntimeError("Managed HAX CLI package metadata is missing.") from None
    if package.get("name") != "@haxtheweb/create" or package.get("version") != CLI_VERSION:
        raise RuntimeError("Managed HAX CLI must be @haxtheweb/create 26.8.1.")
    node = os.environ.get("HAX_NODE") or shutil.which("node")
    if not node or not Path(node).is_absolute() or not Path(node).is_file():
        raise RuntimeError("Node.js 22 or newer is required.")
    return [node, str(entry.resolve())]


def _popen(command, cwd, home):
    options = dict(cwd=str(cwd), env=_environment(home), stdin=subprocess.DEVNULL,
                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(command, **options)
    process._hax_termination_lock = threading.Lock()
    process._hax_terminated = False
    with _PROCESS_LOCK:
        _PROCESSES.add(process)
    return process


def _terminate(process):
    """Terminate the dedicated process group, including surviving descendants."""
    if process is None:
        return
    with process._hax_termination_lock:
        if process._hax_terminated:
            return
        try:
            _terminate_tree(process)
        finally:
            process._hax_terminated = True
            with _PROCESS_LOCK:
                _PROCESSES.discard(process)


def shutdown_local_builds():
    """Stop only children created by this module, including in-flight scaffolds."""
    with _PROCESS_LOCK:
        processes = list(_PROCESSES)
    for process in processes:
        _terminate(process)


def _terminate_tree(process):
    if os.name == "nt":
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            if process.poll() is None:
                process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        # The parent may have exited while a descendant ignored SIGTERM.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass


def _run(command, cwd, home, timeout=180):
    process = _popen(command, cwd, home)
    # Drain continuously but retain no raw CLI output (may contain user text).
    reader = threading.Thread(target=lambda: _drain(process), daemon=True)
    reader.start()
    try:
        code = process.wait(timeout=timeout)
        if code:
            raise RuntimeError("HAX scaffolding failed. Check the managed CLI installation.")
    except subprocess.TimeoutExpired:
        raise RuntimeError("HAX scaffolding timed out. Retry the local build.") from None
    finally:
        _terminate(process)
        reader.join(timeout=3)


def _drain(process, callback=None):
    # Fixed-sized reads avoid unbounded readline allocation for hostile output.
    pending = b""
    try:
        while True:
            chunk = process.stdout.read1(4096)
            if not chunk:
                break
            pending = (pending + chunk)[-8192:]
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if callback:
                    callback(line.decode("utf-8", "replace"))
    finally:
        process.stdout.close()


def _snapshot(workspace):
    state = copy.deepcopy(workspace)
    revision, draft_revision = state.get("revision"), state.get("draft_revision")
    if (type(revision) is not int or revision < 1
            or type(draft_revision) is not int or draft_revision < 1
            or any(type(state.get(key)) is not int for key in (
                "direction_revision", "reviewed_draft_revision", "build_revision"))
            or state.get("direction_revision") != revision
            or state.get("reviewed_draft_revision") != draft_revision
            or state.get("build_revision") != draft_revision):
        raise ValueError("Approve the current direction and reviewed draft before building.")
    job = state.get("active_job")
    if job is not None and (
        job.get("kind") != "build" or job.get("status") != "running"
        or job.get("revision") != revision or job.get("draft_revision") != draft_revision
    ):
        raise ValueError("The build job is stale.")
    _identifier(state.get("id"))
    items = (state.get("draft") or {}).get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 500:
        raise ValueError("A nonempty approved draft of at most 500 items is required.")
    if state["draft"].get("assessment_required"):
        from mentor_assessment import assessment_current
        if not assessment_current(items, state["draft"].get("assessment")):
            raise ValueError("Approve a draft with a current AI assessment before building.")
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid approved page.")
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ValueError("Invalid or duplicate approved page identifier.")
        seen.add(identifier)
        if (not isinstance(item.get("html"), str) or len(item["html"]) > 2 * 1024**2
                or not isinstance(item.get("title"), str) or not 1 <= len(item["title"]) <= 240):
            raise ValueError("Invalid approved page title/content.")
    return state


def _assets(state, destination):
    """Copy only normalized supported course files, never supplemental materials."""
    from imscc_import import ASSET_EXTENSIONS
    result = set()
    referenced = set()
    for item in state["draft"]["items"]:
        soup = BeautifulSoup(sanitize_import_html(item["html"]), "html.parser")
        for tag in soup.find_all(True):
            for attr in URL_ATTRIBUTES:
                value = urlsplit(str(tag.get(attr, "")))
                if not value.scheme and not value.netloc:
                    referenced.add(unquote(value.path).lstrip("/"))
    export = state.get("export_dir")
    if not export:
        return result
    root = Path(export).resolve() / "files"
    if not root.is_dir() or root.is_symlink():
        return result
    for metadata in sorted(root.glob("*.json")):
        if metadata.is_symlink() or metadata.stat().st_size > 1024 * 1024:
            continue
        value = json.loads(metadata.read_text(encoding="utf-8"))
        name = value.get("filename")
        if (not isinstance(name, str) or Path(name).name != name or "\\" in name
                or name in (".", "..") or Path(name).suffix.lower() not in ASSET_EXTENSIONS):
            continue
        source = root / name
        if ("files/" + name not in referenced or not source.is_file() or source.is_symlink()
                or source.suffix.lower() in (".svg", ".html", ".htm", ".js", ".css")):
            continue
        target = destination / "files" / name
        if target.exists() or target.is_symlink():
            raise ValueError("Course asset conflicts with a scaffold file.")
        target.parent.mkdir(exist_ok=True)
        with source.open("rb") as src, target.open("xb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        result.add("files/" + name)
    return result


def _page_html(content, locations, slugs, assets):
    soup = BeautifulSoup(sanitize_import_html(content), "html.parser")
    for tag in soup.find_all(True):
        for attr in URL_ATTRIBUTES:
            if not tag.has_attr(attr):
                continue
            value = str(tag[attr])
            parts = urlsplit(value)
            if value.startswith(ITEM_PREFIX):
                identifier, sep, fragment = value[len(ITEM_PREFIX):].partition("#")
                # HAX fetches fragments into the root document; navigation must
                # target the manifest slug, not a raw HTML fragment location.
                target = slugs.get(identifier)
                if target:
                    tag[attr] = target + (sep + fragment if sep else "")
                else:
                    del tag[attr]
            elif parts.scheme or value.startswith(("//", "#")):
                continue
            else:
                path = unquote(parts.path).lstrip("/")
                page_id = next((key for key, loc in locations.items() if loc == path), None)
                if page_id:
                    tag[attr] = slugs[page_id] + ("#" + parts.fragment if parts.fragment else "")
                elif path in assets:
                    tag[attr] = path + ("#" + parts.fragment if parts.fragment else "")
                else:
                    # No arbitrary filesystem, archive placeholder or runtime links.
                    del tag[attr]
    return str(soup)


def _validate_site(directory):
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("The saved HAX site directory is unavailable.")
    for name in ("site.json", "index.html", "package.json", "approval.json"):
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("The saved directory is not a managed HAX site.")
    manifest = json.loads((directory / "site.json").read_text())
    if not manifest.get("metadata", {}).get("theme") or not manifest.get("items"):
        raise ValueError("The saved HAX manifest is invalid.")
    if "haxcms-site-builder" not in (directory / "index.html").read_text():
        raise ValueError("The saved HAX runtime is missing.")
    for item in manifest["items"]:
        location = item.get("location", "")
        path = directory / location
        if (not isinstance(location, str) or not location.startswith("pages/")
                or "\\" in location or ".." in Path(location).parts
                or not path.resolve().is_relative_to(directory.resolve())
                or not path.is_file() or path.is_symlink()):
            raise ValueError("The saved HAX page is missing or unsafe.")
    return manifest


def build_local_hax_site(workspace, destination, progress=None):
    """Build a fresh directory from a detached, exactly-approved workspace."""
    state = _snapshot(workspace)  # Before any external command or directory write.
    destination = Path(destination).absolute()
    _identifier(destination.name)
    if (destination.parent.name != state["id"] or
            (state.get("active_job") and state["active_job"].get("id") != destination.name)):
        raise ValueError("The build directory does not match this approved workspace/job.")
    if destination.exists() or destination.is_symlink():
        raise ValueError("A new build directory is required; existing sites are never overwritten.")
    command = _toolchain()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Claim the final path exclusively; scaffold into a private sibling parent so
    # concurrent builds cannot race the CLI into overwriting an existing site.
    destination.mkdir(mode=0o700)
    import tempfile
    try:
        with tempfile.TemporaryDirectory(prefix=".hax-build-", dir=destination.parent) as staging:
            staging = Path(staging)
            home = staging / "home"
            home.mkdir(mode=0o700)
            def report(phase, message):
                if progress:
                    progress(phase, message)
            report("scaffolding", "Creating the managed HAX runtime.")
            scaffold_name = "course-" + destination.name
            _run(command + ["site", scaffold_name, "--y", "--no-i",
                            "--no-extras", "--quiet", "--path", str(staging)], staging, home)
            scaffold = staging / scaffold_name
            manifest = json.loads((scaffold / "site.json").read_text())
            if not manifest.get("metadata", {}).get("theme"):
                raise RuntimeError("The HAX CLI did not create a valid runtime.")
            # Preserve every generated runtime/config file, including dotfiles.
            shutil.copytree(scaffold, destination, dirs_exist_ok=True, symlinks=True)
        report("copying_assets", "Copying supported local course assets.")
        assets = _assets(state, destination)
        report("writing_pages", "Writing exactly the approved student pages.")
        items = state["draft"]["items"]
        identifiers = {item["id"]: "item-" + hashlib.sha256(item["id"].encode()).hexdigest()[:24]
                       for item in items}
        locations = {key: f"pages/{value}/index.html" for key, value in identifiers.items()}
        slugs = {item["id"]: f"lesson-{index + 1}" for index, item in enumerate(items)}
        navigation = []
        for index, item in enumerate(items):
            path = destination / locations[item["id"]]
            path.parent.mkdir(parents=True, exist_ok=False)
            path.write_text(_page_html(item["html"], locations, slugs, assets), encoding="utf-8")
            navigation.append(dict(id=identifiers[item["id"]], title=item["title"],
                                   location=locations[item["id"]], slug=slugs[item["id"]],
                                   order=index, indent=0, parent=None,
                                   metadata={"published": True}))
        manifest["title"] = str(state.get("title") or "Approved course")
        manifest["items"] = navigation
        (destination / "site.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        approval = {
            "workspace_id": state["id"], "input_revision": state["revision"],
            "build_id": destination.name,
            "direction_revision": state["direction_revision"],
            "draft_revision": state["draft_revision"],
            "reviewed_draft_revision": state["reviewed_draft_revision"],
            "build_revision": state["build_revision"],
            "pages": [{"id": item["id"], "hax_id": identifiers[item["id"]],
                       "source_ids": [str(value) for value in item.get("source_ids", [])
                                      if str(value).startswith("course:")]}
                      for item in items],
        }
        (destination / "approval.json").write_text(json.dumps(approval, indent=2), encoding="utf-8")
        report("installing", "Using the already-installed managed HAX server; no site install required.")
        _validate_site(destination)
        return {"directory": str(destination), "draft_revision": state["draft_revision"],
                "items": len(items)}
    except BaseException:
        # Only our exclusive fresh directory is disposable, never a prior site.
        shutil.rmtree(destination)
        raise


class LocalHaxSites:
    """Managed sites-root lifecycle with thread safety and OS-backed process leases.

    Root is the sites directory, not the application data directory. Route callers
    must authenticate and authorize owner/workspace before invoking this manager.
    Loopback HAX is a separate local editor, not a hosted account-security boundary.
    """

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._entries = {}
        self._closed = False

    @staticmethod
    def _view(entry=None):
        if entry is None:
            return dict(running=False, url=None, phase="stopped", error=None, directory=None)
        return {key: entry[key] for key in ("running", "url", "phase", "error", "directory")}

    def status(self, owner, workspace_id):
        with self._lock:
            entry = self._entries.get((owner, workspace_id))
            if entry and entry.get("process") and entry["process"].poll() is not None and entry["running"]:
                entry.update(running=False, url=None, phase="failed",
                             error="The local HAX server stopped. Restart the saved site.")
            return self._view(entry)

    def _lease(self, workspace_id):
        directory = self.root / ".runtime-locks"
        directory.mkdir(mode=0o700, exist_ok=True)
        handle = (directory / (workspace_id + ".lock")).open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                if not handle.read(1):
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            handle.close()
            raise ValueError("This workspace has a HAX server in another local process.") from None
        return handle

    def start(self, owner, workspace_id, build_id, site_dir):
        if not isinstance(owner, str) or not owner:
            raise ValueError("A workspace owner is required.")
        _identifier(workspace_id)
        _identifier(build_id)
        provided = Path(site_dir)
        if provided.is_symlink():
            raise ValueError("The saved HAX directory cannot be a symlink.")
        site = provided.resolve()
        expected = self.root / workspace_id / build_id
        if site != expected or expected.resolve() != expected:
            raise ValueError("The saved HAX directory is outside this managed build.")
        manifest = _validate_site(site)
        approval = json.loads((site / "approval.json").read_text())
        if approval.get("workspace_id") != workspace_id or approval.get("build_id") != build_id:
            raise ValueError("The saved HAX site belongs to another workspace/build.")
        toolchain = _toolchain()
        with self._lock:
            if self._closed:
                raise RuntimeError("The local HAX manager is shutting down.")
            key = (owner, workspace_id)
            old = self._entries.get(key)
            self.status(owner, workspace_id)
            if old and old["phase"] in ("starting", "ready") and old["directory"] == str(site):
                return self.status(owner, workspace_id)
            # One managed listener per workspace; never kill a process we did not launch.
            if old:
                old["cancel"].set()
                _terminate(old.get("process"))
                old["lease"].close()
            # Ownership is also enforced by routes; do not allow concurrent
            # account sessions to share a writable HAX process for one workspace.
            if any(k != key and e["directory"] == str(site)
                   and e["phase"] in ("starting", "ready") for k, e in self._entries.items()):
                raise ValueError("This HAX site is already open in another local session.")
            entry = dict(running=False, url=None, phase="starting", error=None,
                         directory=str(site), process=None, cancel=threading.Event(),
                         lease=self._lease(workspace_id))
            self._entries[key] = entry
            thread = threading.Thread(target=self._start, args=(entry, manifest, toolchain),
                                      daemon=True)
            entry["thread"] = thread
            thread.start()
            return self._view(entry)

    def _start(self, entry, manifest, toolchain):
        import tempfile
        process = None
        try:
            with tempfile.TemporaryDirectory(prefix="hax-local-runtime-") as temporary:
                home = Path(temporary)
                ports = []
                def line(value):
                    match = re.fullmatch(r"HAX_LOCAL_READY:([0-9]{1,5})", value)
                    if match and 0 < int(match[1]) < 65536:
                        ports.append(int(match[1]))
                with self._lock:
                    if entry["cancel"].is_set():
                        return
                    process = _popen([toolchain[0], "-e", _RUNTIME, toolchain[1]],
                                     entry["directory"], home)
                    entry["process"] = process
                reader = threading.Thread(target=_drain, args=(process, line), daemon=True)
                reader.start()
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline and not entry["cancel"].is_set():
                    if process.poll() is not None:
                        raise RuntimeError("HAX server failed to start. Reinstall the managed HAX runtime.")
                    if ports:
                        url = f"http://127.0.0.1:{ports[-1]}/"
                        if self._ready(url, manifest):
                            with self._lock:
                                if not entry["cancel"].is_set():
                                    entry.update(running=True, url=url, phase="ready", error=None)
                            break
                    entry["cancel"].wait(.1)
                else:
                    if not entry["cancel"].is_set():
                        raise RuntimeError("HAX runtime readiness timed out. Check local runtime resources.")
                while not entry["cancel"].wait(.2):
                    if process.poll() is not None:
                        raise RuntimeError("The local HAX server stopped. Restart the saved site.")
        except Exception as exc:
            # Only authored messages, never subprocess/provider error bodies.
            message = str(exc) if type(exc) is RuntimeError else "Unable to start the saved local HAX site."
            with self._lock:
                if not entry["cancel"].is_set():
                    entry.update(running=False, url=None, phase="failed", error=message)
        finally:
            _terminate(process)
            entry["lease"].close()

    @staticmethod
    def _ready(url, manifest):
        try:
            def get(path, limit=4 * 1024 * 1024):
                with _OPEN.open(Request(url + path), timeout=2) as response:
                    if response.geturl().split("/")[2] != url.split("/")[2]:
                        raise ValueError("Unexpected runtime redirect.")
                    return response.read(limit)
            index = get("").decode("utf-8")
            served = json.loads(get("site.json"))
            page = get(manifest["items"][0]["location"])
            # Runtime bootloaders are CDN-backed in the official scaffold.
            runtime = get("build-haxcms.js")
            return ("haxcms-site-builder" in index and served.get("id") == manifest["id"]
                    and served.get("items") == manifest["items"] and bool(page)
                    and b"HAX" in runtime)
        except Exception:
            return False

    def stop(self, owner, workspace_id):
        with self._lock:
            entry = self._entries.get((owner, workspace_id))
            if entry is None:
                return self._view()
            entry["cancel"].set()
            entry.update(running=False, url=None, phase="stopped", error=None)
            process, thread = entry.get("process"), entry.get("thread")
        _terminate(process)
        if thread and thread is not threading.current_thread():
            thread.join(timeout=8)
        return self._view(entry)

    def shutdown(self):
        with self._lock:
            self._closed = True
            keys = list(self._entries)
        for owner, workspace in keys:
            self.stop(owner, workspace)
