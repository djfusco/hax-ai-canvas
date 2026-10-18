"""Administrator helper: write a new private env file without printing secrets."""

import argparse
import getpass
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from cryptography.fernet import Fernet
from hosted_config import settings


def create_environment(database_url, origin, blob_token):
    values = {
        "DATABASE_URL": database_url.strip(),
        "HAX_APP_ORIGIN": origin.strip().rstrip("/"),
        "BLOB_READ_WRITE_TOKEN": blob_token.strip(),
        "HAX_MENTOR_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    }
    for key in ("HAX_SESSION_SECRET", "HAX_UPLOAD_SIGNING_SECRET", "HAX_INTERNAL_SECRET",
                "HAX_BETA_ACCESS_CODE", "CRON_SECRET"):
        values[key] = secrets.token_urlsafe(48)
    if any("\n" in value or "\r" in value or "\x00" in value for value in values.values()):
        raise ValueError("Configuration values must be single-line.")
    settings(values)
    return values


def save_environment(path, values):
    """Never overwrite an existing configuration or follow its symlink."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write("# Private hosted configuration. Never commit or paste into chat.\n")
        for name, value in values.items():
            output.write(name + "=" + value + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".env.local")
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        parser.error("Output already exists. Refusing to overwrite configuration.")
    database = getpass.getpass("PostgreSQL DATABASE_URL (hidden): ")
    origin = input("Canonical Vercel HTTPS origin (e.g. https://your-app.vercel.app): ")
    blob = getpass.getpass("Private BLOB_READ_WRITE_TOKEN (hidden): ")
    try:
        values = create_environment(database, origin, blob)
        save_environment(args.output, values)
    except (ValueError, OSError) as exc:
        print("Configuration failed (" + type(exc).__name__ + "). No secret values were printed.")
        return 1
    print("Saved a private environment file with owner-only permissions.")
    print("Transfer its variables securely to Vercel's environment settings; never commit this file.")
    print("The generated beta invitation code is HAX_BETA_ACCESS_CODE. Retrieve/share it privately.")
    print("Required variable names: " + ", ".join(values))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
