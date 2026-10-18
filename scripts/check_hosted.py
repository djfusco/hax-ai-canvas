"""Validate deployment configuration; --live probes DB/private Blob explicitly."""

import argparse
import os
from pathlib import Path
import sys
import tempfile
import uuid

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
                        help="Explicitly initialize DB tables and PUT/GET/DELETE a tiny private Blob probe.")
    args = parser.parse_args(argv)
    load_environment(args.env_file)
    try:
        config = settings()
    except HostedSetupError as exc:
        print(str(exc))
        return 1
    print("Configuration shape: valid. Secret values are not displayed.")
    modules = ("vercel", "psycopg", "flask", "cryptography")
    import importlib.util
    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    if missing:
        print("Install hosted runtime dependencies; missing modules: " + ", ".join(missing))
        return 1
    from workflows import wf
    if not callable(getattr(wf, "http_handler", None)):
        print("Published Workflow registry has no protocol handler.")
        return 1
    print("Published Workflow registry: imported with its protocol handler.")
    if not args.live:
        print("No cloud resources contacted. Re-run with --live only after configuring your project.")
        print("A deployed upload/preparation smoke test is still required.")
        return 0
    from hosted_backend import HostedBackend
    probe = "prepared/preflight/" + uuid.uuid4().hex + "/probe.txt"
    service = None
    failure = False
    try:
        service = HostedBackend(config)
        with tempfile.TemporaryDirectory(prefix="hax-hosted-probe-") as temp:
            source, downloaded = Path(temp) / "source", Path(temp) / "download"
            source.write_bytes(b"private-hosted-preflight")
            receipt = service.blob.put_file(probe, source)
            actual = service.blob.download(probe, downloaded, 1024, receipt["size"])
            if actual["sha256"] != receipt["sha256"]:
                raise ValueError("Probe integrity check failed.")
        print("PostgreSQL tables and private Blob PUT/GET integrity: verified.")
    except Exception as exc:
        print("Live service preflight failed (" + type(exc).__name__ + "). Inspect provider configuration privately.")
        failure = True
    finally:
        if service is not None:
            try:
                service.blob.delete(probe)
                print("Private probe deletion accepted.")
            except Exception:
                print("Probe cleanup needs attention. Private object pathname: " + probe)
                failure = True
    print("Workflow queue/Sandbox execution still require the deployed UI smoke test.")
    return int(failure)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
