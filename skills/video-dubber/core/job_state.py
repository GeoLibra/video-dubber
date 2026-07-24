"""State and event-log helpers for resumable video-dubber jobs."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .job_runtime import atomic_write_json, atomic_write_text, read_json, utc_now


DEFAULT_PROGRESS = {
    "iteration": 0,
    "status": "initialized",
    "stage": None,
    "stale_count": 0,
    "resume_count": 0,
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
    progress = read_progress(job_dir)
    progress.update(updates)
    if touch_last_seen:
        progress["last_seen"] = utc_now()
    atomic_write_json(paths["progress"], progress)
    return progress


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
