"""One live mentor process per local data directory."""

from contextlib import contextmanager
import os
from pathlib import Path


@contextmanager
def application_lease(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / ".runtime-lock"
    if path.is_symlink():
        raise RuntimeError("The local application lock cannot be a symbolic link.")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a+b") as stream:
        try:
            if os.name == "nt":
                import msvcrt
                if path.stat().st_size == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("This local data directory is already open. "
                               "Use the running app or stop it before restarting.") from None
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
