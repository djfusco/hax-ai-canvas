"""Synthetic-only tests; fake Node CLI/runtime and no provider/network services."""

import copy
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
import local_hax


FAKE_CLI = r"""
const fs = require('fs'), path = require('path');
if (process.env.OPENAI_API_KEY || process.env.NODE_OPTIONS || process.env.VERCEL_TOKEN)
  process.exit(9);
const args = process.argv.slice(2);
if (/^[0-9]/.test(args[1])) process.exit(10);
if (args[0] !== 'site' || !args.includes('--no-extras') ||
    !args.includes('--no-i') || !args.includes('--quiet')) process.exit(8);
const site = path.join(args[args.indexOf('--path')+1], args[1]);
fs.mkdirSync(site, {recursive:true});
fs.mkdirSync(path.join(site,'pages'), {recursive:true});
fs.writeFileSync(path.join(site,'site.json'), JSON.stringify({
 id:'generated-manifest-id', title:'Generated',
 metadata:{theme:{element:'clean-one'},platform:{audience:'expert'},
 build:{version:'26.8.0'},site:{settings:{pathauto:true}}},
 items:[],customScaffoldField:'retain-me'
}));
fs.writeFileSync(path.join(site,'index.html'), '<haxcms-site-builder file="site.json"></haxcms-site-builder>');
fs.writeFileSync(path.join(site,'package.json'), '{"scripts":{"serve":"preserved"}}');
fs.writeFileSync(path.join(site,'.gitignore'), 'preserved');
fs.writeFileSync(path.join(site,'build-haxcms.js'), '// HAX runtime synthetic');
"""

FAKE_RUNTIME = r"""
const fs = require('fs'), path = require('path'), http = require('http');
if (process.env.OPENAI_API_KEY || process.env.NODE_OPTIONS) process.exit(7);
if (fs.existsSync('fail-runtime')) {
 console.error('synthetic-private-output must never reach status');
 process.exit(6);
}
if (fs.existsSync('spawn-child')) {
 const child = require('child_process').spawn(process.execPath,
   ['-e',"process.on('SIGTERM',()=>{});setInterval(()=>{},1000)"],{stdio:'ignore'});
 fs.writeFileSync('child.pid',String(child.pid));
}
const server = http.createServer((req,res) => {
 let target = req.url.split('?')[0].slice(1) || 'index.html';
 try {res.end(fs.readFileSync(path.join(process.cwd(),target)));}
 catch {res.statusCode=404;res.end('missing');}
});
let resolve;
const serverReady = new Promise(r => resolve=r);
server.listen(Number(process.env.PORT || 3000), () => resolve(server.address().port));
module.exports = {server,serverReady};
"""


def workspace():
    return dict(
        id="workspace-one", title="Synthetic course", revision=4, direction_revision=4,
        draft_revision=7, reviewed_draft_revision=7, build_revision=7,
        messages=[{"content": "PRIVATE-CHAT"}],
        materials=[{"name": "PRIVATE-POLICY", "text": "not student facing"}],
        draft={"items": [
            {"id": "one", "title": "First page",
             "html": '<h1>Approved first</h1><a href="$IMSCC_ITEM_REFERENCE$/two#target">Next</a>'
                     '<img src="files/picture.png"><script>PRIVATE-ACTIVE</script>'
                     '<img src="file:///private/test"><a href="../secret.txt">No</a>',
             "source_ids": ["course:one", "PRIVATE-POLICY"], "reason": "PRIVATE-REASON"},
            {"id": "two", "title": "Second page", "html": '<h1 id="target">Approved second</h1>'},
        ]},
    )


@unittest.skipUnless(shutil.which("node"), "Node is required for the managed fake CLI")
class LocalHaxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="test-local-hax-")
        self.root = Path(self.temporary.name).resolve()
        self.entry = self.root / "managed/create/dist/create.js"
        self.entry.parent.mkdir(parents=True)
        self.entry.write_text(FAKE_CLI)
        (self.entry.parent.parent / "package.json").write_text(json.dumps(
            {"name": "@haxtheweb/create", "version": "26.8.1"}))
        modules = self.entry.parent.parent / "node_modules"
        runtime = modules / "@haxtheweb/haxcms-nodejs"
        (runtime / "dist").mkdir(parents=True)
        (runtime / "package.json").write_text('{"name":"@haxtheweb/haxcms-nodejs","version":"26.8.1"}')
        (runtime / "dist/app.js").write_text(FAKE_RUNTIME)
        (modules / "dotenv").mkdir()
        (modules / "dotenv/index.js").write_text("module.exports={config:()=>({})};")
        self.environment = patch.dict(os.environ, {
            "HAX_HAX_CLI": str(self.entry), "HAX_NODE": shutil.which("node"),
            "OPENAI_API_KEY": "synthetic-secret-only", "NODE_OPTIONS": "--invalid-synthetic-option",
            "VERCEL_TOKEN": "synthetic-cloud-token",
        })
        self.environment.start()
        self.sites = local_hax.LocalHaxSites(self.root / "sites")
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.sites.shutdown)
        self.destination = self.root / "sites/workspace-one/build-one"

    def build(self, state=None):
        return local_hax.build_local_hax_site(state or workspace(), self.destination)

    def wait_phase(self, owner="owner", phase="ready"):
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            result = self.sites.status(owner, "workspace-one")
            if result["phase"] == phase:
                return result
            if result["phase"] == "failed" and phase != "failed":
                self.fail(result["error"])
            time.sleep(.02)
        self.fail("Timed out waiting for " + phase)

    def test_manifest_runtime_approvals_and_private_content(self):
        original = workspace()
        state = copy.deepcopy(original)
        result = self.build(state)
        self.assertEqual(state, original)
        self.assertEqual(result, {"directory": str(self.destination), "draft_revision": 7, "items": 2})
        manifest = json.loads((self.destination / "site.json").read_text())
        self.assertEqual(manifest["id"], "generated-manifest-id")
        self.assertEqual(manifest["metadata"]["platform"], {"audience": "expert"})
        self.assertEqual(manifest["customScaffoldField"], "retain-me")
        self.assertEqual(manifest["metadata"]["build"]["version"], "26.8.0")
        self.assertEqual(json.loads((self.destination / "package.json").read_text())["scripts"]["serve"],
                         "preserved")
        self.assertEqual((self.destination / ".gitignore").read_text(), "preserved")
        self.assertEqual([item["slug"] for item in manifest["items"]], ["lesson-1", "lesson-2"])
        first = (self.destination / manifest["items"][0]["location"]).read_text()
        self.assertIn('href="lesson-2#target"', first)
        self.assertNotIn("file:", first)
        self.assertNotIn("../secret", first)
        self.assertNotIn("<script", first)
        approval = json.loads((self.destination / "approval.json").read_text())
        self.assertEqual(approval["build_revision"], 7)
        self.assertEqual(approval["pages"][0]["source_ids"], ["course:one"])
        exported = "\n".join(p.read_text() for p in self.destination.rglob("*") if p.is_file())
        self.assertNotIn("PRIVATE-", exported)

    def test_all_stale_gates_reject_before_command_or_output(self):
        for key in ("revision", "direction_revision", "draft_revision",
                    "reviewed_draft_revision", "build_revision"):
            for value in (None, 0, 99, True):
                with self.subTest(key=key, value=value):
                    state = workspace()
                    state[key] = value
                    with patch.object(local_hax, "_popen") as launch:
                        with self.assertRaises(ValueError):
                            self.build(state)
                        launch.assert_not_called()
                    self.assertFalse(self.destination.exists())

    def test_stale_build_job_rejected(self):
        state = workspace()
        state["active_job"] = {"kind": "build", "status": "running", "revision": 4,
                               "draft_revision": 6}
        with self.assertRaises(ValueError):
            self.build(state)

    def test_active_job_and_output_directory_must_match_exact_snapshot(self):
        state = workspace()
        state["active_job"] = {"id": "different-job", "kind": "build", "status": "running",
                               "revision": 4, "draft_revision": 7}
        with self.assertRaises(ValueError):
            self.build(state)
        with self.assertRaises(ValueError):
            local_hax.build_local_hax_site(workspace(), self.root / "sites/wrong/build-one")
        state["active_job"]["id"] = "build-one"
        self.build(state)
        approval = json.loads((self.destination / "approval.json").read_text())
        self.assertEqual(approval["build_id"], "build-one")

    def test_duplicate_or_unsafe_pages_rejected(self):
        state = workspace()
        state["draft"]["items"][1]["id"] = "one"
        with self.assertRaises(ValueError):
            self.build(state)
        self.assertFalse(self.destination.exists())

    def test_numeric_leading_job_id_uses_valid_cli_machine_name(self):
        job_id = "5e45687ba5dd43d78eb1ad894d83508f"
        self.destination = self.root / "sites/workspace-one" / job_id
        state = workspace()
        state["active_job"] = {"id": job_id, "kind": "build", "status": "running",
                               "revision": 4, "draft_revision": 7}
        self.build(state)
        self.assertTrue((self.destination / "site.json").is_file())
        self.assertEqual(json.loads((self.destination / "approval.json").read_text())["build_id"], job_id)

    def test_existing_site_never_overwritten(self):
        self.destination.mkdir(parents=True)
        marker = self.destination / "user-edits.txt"
        marker.write_text("keep")
        with self.assertRaises(ValueError):
            self.build()
        self.assertEqual(marker.read_text(), "keep")

    def test_wrong_managed_cli_version_rejected(self):
        (self.entry.parent.parent / "package.json").write_text(
            '{"name":"@haxtheweb/create","version":"99.0.0"}')
        with self.assertRaisesRegex(RuntimeError, "26.8.1"):
            self.build()
        self.assertFalse(self.destination.exists())

    def test_snapshot_detached_before_progress_callback(self):
        state = workspace()
        def mutate(phase, message):
            state["draft"]["items"][0]["html"] = "<h1>unapproved replacement</h1>"
        local_hax.build_local_hax_site(state, self.destination, mutate)
        manifest = json.loads((self.destination / "site.json").read_text())
        first = (self.destination / manifest["items"][0]["location"]).read_text()
        self.assertIn("Approved first", first)
        self.assertNotIn("unapproved", first)

    def test_only_referenced_safe_assets_copied(self):
        files = self.root / "export/files"
        files.mkdir(parents=True)
        for index, name in enumerate(("picture.png", "private.pdf", "bad.svg")):
            (files / name).write_bytes(b"synthetic-fixture")
            (files / f"{index}.json").write_text(json.dumps({"filename": name}))
        outside = self.root / "outside.png"
        outside.write_bytes(b"private")
        (files / "linked.png").symlink_to(outside)
        (files / "linked.json").write_text('{"filename":"linked.png"}')
        state = workspace()
        state["export_dir"] = str(files.parent)
        state["draft"]["items"][0]["html"] += '<img src="files/linked.png"><img src="files/bad.svg">'
        self.build(state)
        self.assertEqual((self.destination / "files/picture.png").read_bytes(), b"synthetic-fixture")
        self.assertFalse((self.destination / "files/private.pdf").exists())
        self.assertFalse((self.destination / "files/linked.png").exists())
        self.assertFalse((self.destination / "files/bad.svg").exists())
        manifest = json.loads((self.destination / "site.json").read_text())
        first = (self.destination / manifest["items"][0]["location"]).read_text()
        self.assertIn('src="files/picture.png"', first)
        self.assertNotIn("files/linked.png", first)

    def test_immediate_start_actual_port_stop_restart_and_owner_status(self):
        self.build()
        occupied = socket.socket()
        occupied.bind(("127.0.0.1", 0))
        self.addCleanup(occupied.close)
        start = self.sites.start("owner", "workspace-one", "build-one", self.destination)
        self.assertEqual(start["phase"], "starting")
        self.assertFalse(start["running"])
        self.assertIsNone(start["url"])
        ready = self.wait_phase()
        self.assertNotIn(f":{occupied.getsockname()[1]}/", ready["url"])
        self.assertEqual(self.sites.status("other-owner", "workspace-one")["phase"], "stopped")
        self.assertEqual(self.sites.stop("other-owner", "workspace-one")["phase"], "stopped")
        process = self.sites._entries[("owner", "workspace-one")]["process"]
        stopped = self.sites.stop("owner", "workspace-one")
        self.assertEqual(stopped["phase"], "stopped")
        self.assertIsNotNone(process.poll())
        self.sites.start("owner", "workspace-one", "build-one", self.destination)
        self.wait_phase()

    def test_origin_host_and_cross_site_requests_blocked(self):
        self.build()
        self.sites.start("owner", "workspace-one", "build-one", self.destination)
        url = self.wait_phase()["url"]
        for headers, data in (
            ({"Origin": "https://attacker.invalid"}, None),
            ({"Host": "attacker.invalid"}, None),
            ({"Sec-Fetch-Site": "same-site"}, None),
            ({}, b"synthetic"),
        ):
            with self.subTest(headers=headers):
                with self.assertRaises(HTTPError) as caught:
                    urlopen(Request(url, headers=headers, data=data), timeout=2)
                self.assertEqual(caught.exception.code, 403)
        self.assertIn(b"haxcms-site-builder", urlopen(url, timeout=2).read())

    def test_missing_runtime_does_not_report_guessed_success_or_raw_logs(self):
        self.build()
        (self.destination / "fail-runtime").touch()
        self.sites.start("owner", "workspace-one", "build-one", self.destination)
        failed = self.wait_phase(phase="failed")
        self.assertFalse(failed["running"])
        self.assertIsNone(failed["url"])
        self.assertNotIn("synthetic-private-output", failed["error"])

    def test_saved_directory_outside_root_or_wrong_workspace_rejected(self):
        self.build()
        with self.assertRaises(ValueError):
            self.sites.start("owner", "workspace-two", "build-one", self.destination)
        with self.assertRaises(ValueError):
            self.sites.start("owner", "../workspace-one", "build-one", self.destination)
        (self.destination / "approval.json").write_text('{"workspace_id":"another"}')
        with self.assertRaises(ValueError):
            self.sites.start("owner", "workspace-one", "build-one", self.destination)

    def test_multiprocess_lease_prevents_duplicate_site_server(self):
        self.build()
        self.sites.start("owner", "workspace-one", "build-one", self.destination)
        self.wait_phase()
        code = (
            "from local_hax import LocalHaxSites; import sys; "
            "m=LocalHaxSites(sys.argv[1]); "
            "\ntry:\n m._lease('workspace-one'); print('unlocked')"
            "\nexcept ValueError:\n print('blocked')"
        )
        env = local_hax._environment(self.root)
        env["PYTHONPATH"] = str(Path(local_hax.__file__).parent)
        completed = subprocess.run([sys.executable, "-c", code, str(self.root / "sites")],
                                   env=env, capture_output=True, text=True, check=True)
        self.assertEqual(completed.stdout.strip(), "blocked")
        other = local_hax.LocalHaxSites(self.root / "sites")
        self.addCleanup(other.shutdown)
        with self.assertRaisesRegex(ValueError, "another local process"):
            other.start("owner", "workspace-one", "build-one", self.destination)
        self.sites.stop("owner", "workspace-one")
        result = other.start("owner", "workspace-one", "build-one", self.destination)
        self.assertEqual(result["phase"], "starting")

    @unittest.skipIf(os.name == "nt", "POSIX process-group check; Windows taskkill is separate")
    def test_stop_terminates_descendant_that_ignores_term(self):
        self.build()
        (self.destination / "spawn-child").touch()
        self.sites.start("owner", "workspace-one", "build-one", self.destination)
        self.wait_phase()
        pid = int((self.destination / "child.pid").read_text())
        self.sites.stop("owner", "workspace-one")
        completed = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                                   capture_output=True, text=True, check=False)
        # A reparented zombie is no longer executing and will be reaped by init.
        self.assertTrue(not completed.stdout.strip() or
                        completed.stdout.strip().startswith("Z"), completed.stdout)

    def test_concurrent_start_idempotent_and_shutdown_cancels(self):
        self.build()
        results = []
        def start():
            results.append(self.sites.start("owner", "workspace-one", "build-one", self.destination))
        threads = [threading.Thread(target=start) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 8)
        self.wait_phase()
        self.sites.shutdown()
        self.assertEqual(self.sites.status("owner", "workspace-one")["phase"], "stopped")
        with self.assertRaises(RuntimeError):
            self.sites.start("owner", "workspace-one", "build-one", self.destination)

    def test_restart_new_manager_uses_explicit_persisted_directory(self):
        self.build()
        self.sites.shutdown()
        self.sites = local_hax.LocalHaxSites(self.root / "sites")
        self.addCleanup(self.sites.shutdown)
        self.sites.start("owner", "workspace-one", "build-one", self.destination)
        self.wait_phase()

    def test_scaffold_failure_cleans_only_claimed_fresh_directory(self):
        self.entry.write_text("process.exit(1);")
        with self.assertRaises(RuntimeError):
            self.build()
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
