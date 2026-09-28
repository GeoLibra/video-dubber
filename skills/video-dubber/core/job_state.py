"""State and event-log helpers for resumable video-dubber jobs."""

from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

from .job_runtime import atomic_write_json, atomic_write_text, read_json, utc_now


DEFAULT_PROGRESS = {
    "iteration": 0,
    "status": "initialized",
    "stage": None,
    "max_stage": None,
    "stale_count": 0,
    "resume_count": 0,
    "total_resumes": 0,
    "guardian_status": "healthy",
}


def ensure_job_layout(job_dir):
    job = Path(job_dir).expanduser().resolve()
    state = job / "state"
    logs = job / "logs"
    state.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    progress_path = state / "progress.json"
    if not progress_path.exists():
        atomic_write_json(progress_path, dict(DEFAULT_PROGRESS, last_seen=utc_now()))
    directions_path = state / "directions_tried.json"
    if not directions_path.exists():
        atomic_write_json(directions_path, [])
    task_spec_path = state / "task_spec.md"
    if not task_spec_path.exists():
        atomic_write_text(
            task_spec_path,
            "# Video Dubber Job\n\n"
            "Goal, milestones, and success criteria are inferred from job_config.json.\n",
        )
    return {
        "job": job,
        "state": state,
        "logs": logs,
        "progress": progress_path,
        "directions": directions_path,
        "task_spec": task_spec_path,
        "iteration_log": state / "iteration_log.jsonl",
        "work_log": logs / "work.jsonl",
        "heartbeat_log": logs / "heartbeat.jsonl",
    }


def append_event(job_dir, source, level, event, detail="", log_name=None, **extra):
    paths = ensure_job_layout(job_dir)
    if log_name is None:
        log_name = "heartbeat.jsonl" if source == "guardian" else "work.jsonl"
    path = paths["logs"] / log_name
    payload = {
        "ts": utc_now(),
        "source": source,
        "level": level,
        "event": event,
        "detail": detail,
    }
    payload.update(extra)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return payload


def read_progress(job_dir):
    paths = ensure_job_layout(job_dir)
    progress = read_json(paths["progress"], default={}) or {}
    merged = dict(DEFAULT_PROGRESS)
    merged.update(progress)
    return merged


def update_progress(job_dir, touch_last_seen=True, **updates):
    paths = ensure_job_layout(job_dir)
    lock_path = paths["state"] / ".progress.lock"
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
        raise RuntimeError(f"Failed to acquire progress lock at {lock_path}: {exc}") from exc

    try:
        progress = read_progress(job_dir)
        progress.update(updates)
        if touch_last_seen:
            progress["last_seen"] = utc_now()
        atomic_write_json(paths["progress"], progress)
        return progress
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


def record_decision(job_dir, event, detail, **extra):
    return append_event(job_dir, "worker", "decision", event, detail, **extra)


def artifact_snapshot(job_dir):
    job = Path(job_dir).expanduser().resolve()
    artifacts = (
        list(job.glob("chunk_*_qwen3tts_*.wav"))
        + list(job.glob("output_*.mp4"))
        + list(job.glob("merged_tts_*.wav"))
    )
    last_mtime = max([p.stat().st_mtime for p in artifacts] or [0])
    chunks = sorted(job.glob("chunk_*_qwen3tts_*.wav"))
    return {
        "chunk_count": len(chunks),
        "last_chunk": chunks[-1].name if chunks else None,
        "artifact_count": len(artifacts),
        "last_artifact_mtime": last_mtime or None,
        "outputs": [p.name for p in sorted(job.glob("output_*.mp4"))],
    }


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def mark_coordination_root(coord_root: Path | str, target: Path | str | None = None) -> None:
    """Persist .coordination_root marker in target batch directory pointing to coord_root."""
    try:
        c_root = Path(coord_root).expanduser().resolve()
        if target:
            t = Path(target).expanduser().resolve()
            if t.exists() and t != c_root:
                (t / ".coordination_root").write_text(str(c_root), encoding="utf-8")
    except OSError:
        pass


def register_batch_path(coord_root: Path | str, batch_dir: Path | str) -> list[Path]:
    """Register a batch or jobs directory in the coordination directory registry.

    Ensures that when an independent coordination directory is used (outside the
    batch's parent tree), sweepers discover and count all active jobs across all
    registered batches, preventing concurrent execution from exceeding max_parallel.
    Returns the list of all currently active registered batch directories.
    """
    c_root = Path(coord_root).expanduser().resolve()
    b_dir = Path(batch_dir).expanduser().resolve()
    c_root.mkdir(parents=True, exist_ok=True)
    registry_file = c_root / "registered_batches.json"
    lock_file_path = c_root / ".registry.lock"

    lock_fd = None
    try:
        lock_fd = open(lock_file_path, "a")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    except OSError as exc:
        if lock_fd:
            try:
                lock_fd.close()
            except OSError:
                pass
        raise RuntimeError(f"Failed to acquire registry lock at {lock_file_path}: {exc}") from exc

    try:
        data: dict = {"batches": {}}
        if registry_file.is_file():
            try:
                loaded = json.loads(registry_file.read_text(encoding="utf-8"))
                if not (isinstance(loaded, dict) and "batches" in loaded and isinstance(loaded["batches"], dict)):
                    raise ValueError(f"Corrupt registry format in {registry_file}: expected 'batches' dict")
                data = loaded
            except Exception as exc:
                raise RuntimeError(f"Failed to read coordination registry at {registry_file}: {exc}") from exc

        now = time.time()
        data["batches"][str(b_dir)] = {
            "last_seen": now,
        }

        # Clean up batches that no longer exist on disk
        pruned = {}
        active_paths: list[Path] = []
        for path_str, meta in data["batches"].items():
            p = Path(path_str)
            if p.exists():
                pruned[path_str] = meta
                active_paths.append(p)

        data["batches"] = pruned
        atomic_write_json(registry_file, data)

        return active_paths
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


def get_registered_batches(coord_root: Path | str) -> list[Path]:
    """Retrieve all existing registered batch directories from coord_root."""
    c_root = Path(coord_root).expanduser().resolve()
    registry_file = c_root / "registered_batches.json"
    if not registry_file.is_file():
        return []
    lock_file_path = c_root / ".registry.lock"
    lock_fd = None
    try:
        lock_fd = open(lock_file_path, "a")
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
    except OSError as exc:
        if lock_fd:
            try:
                lock_fd.close()
            except OSError:
                pass
        raise RuntimeError(f"Failed to acquire shared registry lock at {lock_file_path}: {exc}") from exc

    try:
        loaded = json.loads(registry_file.read_text(encoding="utf-8"))
        if not (isinstance(loaded, dict) and "batches" in loaded and isinstance(loaded["batches"], dict)):
            raise ValueError(f"Corrupt registry format in {registry_file}: expected 'batches' dict")
        batches: list[Path] = []
        for path_str in loaded["batches"]:
            p = Path(path_str)
            if p.exists():
                batches.append(p)
        return batches
    except Exception as exc:
        raise RuntimeError(f"Failed to read coordination registry at {registry_file}: {exc}") from exc
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


def resolve_coordination_root(path: Path | str) -> Path:
    """Resolve the canonical shared coordination and lock root for jobs_dir.

    Ensures that sweeping the root output dir (e.g. output) or specific custom
    batches (e.g. client_alpha, client_beta) under a shared parent directory
    or custom coordination directory share the same coordination scope,
    concurrency quota accounting, and .sweep.lock to avoid memory exhaustion
    from running multiple MLX models concurrently.
    """
    target = Path(path).expanduser().resolve()

    # 1. Check for explicit persistent marker .coordination_root in target
    marker = target / ".coordination_root"
    if marker.is_file():
        try:
            root_str = marker.read_text(encoding="utf-8").strip()
            if root_str:
                root_path = Path(root_str).expanduser().resolve()
                if root_path.exists():
                    return root_path
        except OSError:
            pass

    # 2. Check batch_meta.json in target
    meta_file = target / "batch_meta.json"
    if meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if meta.get("coordination_dir"):
                c_path = Path(meta["coordination_dir"]).expanduser().resolve()
                if c_path.exists():
                    return c_path
        except (json.JSONDecodeError, OSError):
            pass

    # 3. Check job_config.json in target or target/job_*
    job_cfg = target / "job_config.json"
    if job_cfg.is_file():
        try:
            cfg = json.loads(job_cfg.read_text(encoding="utf-8"))
            if cfg.get("coordination_dir"):
                c_path = Path(cfg["coordination_dir"]).expanduser().resolve()
                if c_path.exists():
                    return c_path
        except (json.JSONDecodeError, OSError):
            pass
    elif target.is_dir():
        try:
            for child_job in target.glob("job_*"):
                child_cfg = child_job / "job_config.json"
                if child_cfg.is_file():
                    try:
                        cfg = json.loads(child_cfg.read_text(encoding="utf-8"))
                        if cfg.get("coordination_dir"):
                            c_path = Path(cfg["coordination_dir"]).expanduser().resolve()
                            if c_path.exists():
                                return c_path
                    except (json.JSONDecodeError, OSError):
                        pass
                    break
        except OSError:
            pass

    # 4. Walk up to find if target or any ancestor is named "output"
    curr = target
    while curr != curr.parent:
        if curr.name == "output":
            return curr
        curr = curr.parent

    # 5. If target is a single job directory (has job_config.json or state/progress.json)
    if (target / "job_config.json").exists() or (target / "state" / "progress.json").exists():
        parent = target.parent
        if parent != target:
            return resolve_coordination_root(parent)
        return target

    # 6. If target is a batch directory (name starts with "batch" or "my_batch")
    if (target.name.startswith("batch") or target.name.startswith("my_batch")) and target.parent != target:
        return target.parent

    return target
