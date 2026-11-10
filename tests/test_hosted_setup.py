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
from test_hosted_integration import fixture_environment


class HostedSetupTests(unittest.TestCase):
    def test_generated_environment_validates_and_secrets_are_distinct(self):
        values = configure_hosted.create_environment(
            "postgresql://fixture", "https://mentor.example/",
            "https://ep-fixture.neonauth.us-east-1.aws.neon.tech/neondb/auth")
        self.assertEqual(settings(values).origin, "https://mentor.example")
        names = ("HAX_SESSION_SECRET", "HAX_BETA_ACCESS_CODE")
        self.assertEqual(len({values[name] for name in names}), len(names))
        self.assertTrue(all(len(values[name]) >= 32 for name in names))
        self.assertNotIn(values["HAX_SESSION_SECRET"], repr(settings(values)))

    def test_newline_injection_is_rejected(self):
        with self.assertRaises(ValueError):
            configure_hosted.create_environment(
                "postgresql://fixture\nINJECTED=value", "https://mentor.example")

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
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, values, clear=True), \
                patch.object(importlib.util, "find_spec", return_value=True), \
                patch("hosted_backend.HostedBackend", backend or unittest.mock.Mock(
                    side_effect=AssertionError("Offline preflight contacted services"))), \
                redirect_stdout(output):
            code = check_hosted.main(["--env-file", str(Path(temp) / "absent"), *extra])
        for name in ("HAX_SESSION_SECRET", "HAX_BETA_ACCESS_CODE", "HAX_MENTOR_ENCRYPTION_KEY"):
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

    def test_explicit_live_check_connects_to_the_database(self):
        code, output = self.preflight(["--live"], lambda config: SimpleNamespace())
        self.assertEqual(code, 0, output)
        self.assertIn("PostgreSQL connection and tables: verified.", output)

    def test_live_check_reports_failure_without_details(self):
        def broken(config):
            raise ConnectionError("password=hidden-fixture")
        code, output = self.preflight(["--live"], broken)
        self.assertEqual(code, 1)
        self.assertIn("Database check failed (ConnectionError)", output)
        self.assertNotIn("hidden-fixture", output)


if __name__ == "__main__":
    unittest.main()
