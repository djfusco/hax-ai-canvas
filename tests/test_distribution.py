import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DistributionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is needed to validate launcher helpers")
    def test_code_fingerprint_ignores_private_data_but_detects_source_changes(self):
        script = r"""
const assert = require('assert'), fs = require('fs'), path = require('path');
const {sourceFingerprint,bundledEntry} = require(process.argv[1]);
const dir=process.argv[2];
fs.writeFileSync(path.join(dir,'module.py'),'first');
const first=sourceFingerprint(dir);
fs.mkdirSync(path.join(dir,'mentor_data'));
fs.writeFileSync(path.join(dir,'mentor_data','.vault-key'),'not-a-real-key');
fs.writeFileSync(path.join(dir,'credentials.sqlite'),'private-test-data');
assert.strictEqual(sourceFingerprint(dir),first);
fs.writeFileSync(path.join(dir,'module.py'),'changed');
assert.notStrictEqual(sourceFingerprint(dir),first);
for(const name of ['mentor_data','.env','.vault-key','a.sqlite','a.sqlite-wal',
                   'course_123_ai_readiness_report.html']) assert.strictEqual(bundledEntry(name),false);
"""
        with tempfile.TemporaryDirectory() as temp:
            subprocess.run(["node", "-e", script, str(ROOT / "bin/index.js"), temp], check=True)

    @unittest.skipUnless(shutil.which("npm"), "npm is needed to validate publication exclusions")
    def test_npm_dry_run_does_not_package_credentials_or_course_data(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = json.loads((ROOT / "package.json").read_text())
            package.update(name="mentor-distribution-test", version="0.0.0")
            (root / "package.json").write_text(json.dumps(package))
            shutil.copy2(ROOT / ".npmignore", root / ".npmignore")
            private = root / "python" / "mentor_data"
            private.mkdir(parents=True)
            (private / ".vault-key").write_text("test-only")
            (private / "credentials.sqlite").write_text("test-only")
            (root / "python" / "credentials.sqlite").write_text("test-only")
            (root / "python" / ".env").write_text("test-only")
            (root / "python" / "module.py").write_text("# distribution test\n")
            completed = subprocess.run(
                ["npm", "pack", "--dry-run", "--json", "--ignore-scripts", "--offline"],
                cwd=root, capture_output=True, text=True, check=True,
            )
            paths = [item["path"] for item in json.loads(completed.stdout)[0]["files"]]
            self.assertIn("python/module.py", paths)
            self.assertFalse(any("mentor_data" in p or "sqlite" in p or p.endswith(".env") for p in paths))


if __name__ == "__main__":
    unittest.main()
