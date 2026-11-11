"""Durable private state and a single-writer boundary for portfolio execution."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time
from pathlib import Path

DATA = Path(__file__).resolve().parents[2] / "data"


class ExecutionBusy(RuntimeError):
    pass


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, allow_nan=False, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def append_record(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps(value, allow_nan=False, default=str) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


@contextlib.contextmanager
def execution_lock(path: Path = DATA / "portfolio.lock", *, wait: float = 0):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ExecutionBusy("another portfolio cycle holds the execution lock") from None
                time.sleep(.2)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
