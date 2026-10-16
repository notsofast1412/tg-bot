import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def instance_lock(database_path):
    path = Path(database_path).resolve().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Экземпляр уже запущен для этой базы") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
