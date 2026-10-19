"""Configuration tooling tests use generated fixtures, never real credentials."""
from contextlib import redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import check_hosted, configure_hosted
from hosted_config import settings
from test_hosted_integration import fixture_environment, MemoryBlob


class HostedSetupTests(unittest.TestCase):
    def test_generated_environment_validates_and_secrets_are_distinct(self):
        values = configure_hosted.create_environment(
            "postgresql://fixture", "https://mentor.example/", "fixture-blob",
            "https://ep-fixture.neonauth.us-east-1.aws.neon.tech/neondb/auth")
        self.assertEqual(settings(values).origin, "https://mentor.example")
        names = ("HAX_SESSION_SECRET", "HAX_UPLOAD_SIGNING_SECRET",
                 "HAX_INTERNAL_SECRET", "HAX_BETA_ACCESS_CODE", "CRON_SECRET")
        self.assertEqual(len({values[name] for name in names}), len(names))
        self.assertTrue(all(len(values[name]) >= 32 for name in names))
        self.assertNotIn(values["HAX_SESSION_SECRET"], repr(settings(values)))

    def test_newline_injection_is_rejected(self):
        with self.assertRaises(ValueError):
            configure_hosted.create_environment(
                "postgresql://fixture\nINJECTED=value", "https://mentor.example", "blob")

    def test_private_file_permissions_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".env.local"
            configure_hosted.save_environment(path, {"FIXTURE": "not-a-secret"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                configure_hosted.save_environment(path, {"FIXTURE": "replacement"})
            self.assertIn("FIXTURE=not-a-secret", path.read_text())
            link = Path(temp) / "symlink"
            link.symlink_to(path)
            with self.assertRaises(FileExistsError):
                configure_hosted.save_environment(link, {"FIXTURE": "replacement"})

    def test_env_loader_preserves_existing_environment(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"EXISTING": "kept"}, clear=True):
            path = Path(temp) / "fixture"
            path.write_text("# fixture\nEXISTING=ignored\nNEW=value\n")
            check_hosted.load_environment(path)
            self.assertEqual(os.environ["EXISTING"], "kept")
            self.assertEqual(os.environ["NEW"], "value")

    def preflight(self, extra=(), backend=None, env=None):
        output = io.StringIO()
        values = fixture_environment() if env is None else env
        registry = SimpleNamespace(wf=SimpleNamespace(http_handler=lambda: None))
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, values, clear=True), \
                patch.dict(sys.modules, {"workflows": registry}), \
                patch.object(importlib.util, "find_spec", return_value=True), \
                patch("hosted_backend.HostedBackend", backend or unittest.mock.Mock(
                    side_effect=AssertionError("Offline preflight contacted services"))), \
                redirect_stdout(output):
            code = check_hosted.main(["--env-file", str(Path(temp) / "absent"), *extra])
        for name in ("HAX_SESSION_SECRET", "HAX_UPLOAD_SIGNING_SECRET", "HAX_INTERNAL_SECRET",
                     "HAX_BETA_ACCESS_CODE", "BLOB_READ_WRITE_TOKEN", "HAX_MENTOR_ENCRYPTION_KEY"):
            if name in values:
                self.assertNotIn(values[name], output.getvalue())
        return code, output.getvalue()

    def test_default_preflight_is_offline(self):
        code, output = self.preflight()
        self.assertEqual(code, 0)
        self.assertIn("No cloud resources contacted", output)

    def test_missing_configuration_reports_names_not_values(self):
        code, output = self.preflight(env={})
        self.assertEqual(code, 1)
        self.assertIn("DATABASE_URL", output)

    def test_explicit_live_probe_verifies_and_deletes(self):
        blob = MemoryBlob()
        code, output = self.preflight(["--live"], lambda config: SimpleNamespace(blob=blob))
        self.assertEqual(code, 0, output)
        self.assertEqual([event[0] for event in blob.events], ["persist", "download", "delete"])
        self.assertEqual(blob.objects, {})

    def test_live_probe_reports_retryable_deletion_failure(self):
        blob = MemoryBlob()
        blob.delete_failure = True
        code, output = self.preflight(["--live"], lambda config: SimpleNamespace(blob=blob))
        self.assertEqual(code, 1)
        self.assertIn("Probe cleanup needs attention", output)
