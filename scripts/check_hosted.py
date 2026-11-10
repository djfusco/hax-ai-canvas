"""Validate deployment configuration; --live also connects to the database."""

import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from hosted_config import settings, HostedSetupError


def load_environment(path):
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.local")
    parser.add_argument("--live", action="store_true",
                        help="Connect to the database and create or verify its tables.")
    args = parser.parse_args(argv)
    load_environment(args.env_file)
    try:
        config = settings()
    except HostedSetupError as exc:
        print(str(exc))
        return 1
    print("Configuration shape: valid. Secret values are not displayed.")
    modules = ("psycopg", "flask", "cryptography")
    import importlib.util
    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    if missing:
        print("Install runtime dependencies; missing modules: " + ", ".join(missing))
        return 1
    if not args.live:
        print("No cloud resources contacted. Re-run with --live to check the database connection.")
        return 0
    from hosted_backend import HostedBackend
    try:
        HostedBackend(config)  # creates or verifies the tables
        print("PostgreSQL connection and tables: verified.")
    except Exception as exc:
        print("Database check failed (" + type(exc).__name__ + "). Inspect the configuration privately.")
        return 1
    return 0

if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
