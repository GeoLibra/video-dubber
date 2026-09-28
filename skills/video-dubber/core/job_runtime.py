"""Small job-runtime utilities for resumable long video-dubber tasks."""

from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_write_text(path, text, encoding="utf-8"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    unique_suffix = f".tmp.{os.getpid()}.{time.time_ns()}.{uuid.uuid4().hex[:8]}"
    tmp = path.with_name(path.name + unique_suffix)
    try:
        tmp.write_text(text, encoding=encoding)
        os.replace(tmp, path)
    except BaseException:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_json(path, payload):
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def merge_status(status_file, status=None, message=None, **extra):
    path = Path(status_file)
    lock_path = path.parent / ".status.lock"
    lock_fd = None
    try:
        lock_fd = open(lock_path, "a")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    except OSError as exc:
        if lock_fd:
            try:
                lock_fd.close()
            except OSError:
                pass
        raise RuntimeError(f"Failed to acquire status lock at {lock_path}: {exc}") from exc

    try:
        current = {}
        if path.exists():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                current = {}
        if status is not None:
            current["status"] = status
        if message is not None:
            current["message"] = message
        current["last_seen"] = utc_now()
        current.update(extra)
        atomic_write_json(path, current)
        return current
    finally:
        if lock_fd:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                lock_fd.close()
            except OSError:
                pass


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default
