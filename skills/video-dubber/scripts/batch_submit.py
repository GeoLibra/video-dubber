#!/usr/bin/env python3
"""Batch job submission tool for video-dubber.

Creates job directories for multiple video inputs or URLs with standard
layout and pending state, enabling scheduled dispatch and concurrency control.
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

from core.job_runtime import atomic_write_json
from core.job_state import (
    append_event,
    ensure_job_layout,
    mark_coordination_root,
    pid_alive,
    register_batch_path,
    resolve_coordination_root,
    update_progress,
)
from core.lang import slug as lang_slug


def load_input_list(inputs: list[str] | None, list_file: str | None) -> list[str]:
    """Collect and sanitize all input video paths or URLs."""
    items: list[str] = []
    if inputs:
        items.extend([i.strip() for i in inputs if i.strip()])

    if list_file:
        path = Path(list_file).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Input list file not found: {list_file}")
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#"):
                items.append(line)

    return items


def is_url(target: str) -> bool:
    return target.startswith(("http://", "https://", "www."))


def check_job_not_active(job_dir: Path) -> None:
    """Raise RuntimeError if job_dir has an active process or is currently running/resuming."""
    if not job_dir.exists():
        return

    # Check PID file for live process
    pid_path = job_dir / "job_pid.txt"
    if pid_path.exists():
        try:
            raw_pid = pid_path.read_text(encoding="utf-8").strip()
            if raw_pid and pid_alive(int(raw_pid)):
                raise RuntimeError(
                    f"Cannot overwrite active job in '{job_dir.name}': process {raw_pid} is currently running. "
                    f"Stop the running job before overwriting."
                )
        except ValueError:
            pass

    # Check progress.json state
    prog_file = job_dir / "state" / "progress.json"
    if prog_file.exists():
        try:
            prog_data = json.loads(prog_file.read_text(encoding="utf-8"))
            status = prog_data.get("status")
            if status in ("running", "resuming", "dispatching"):
                raise RuntimeError(
                    f"Cannot overwrite active job in '{job_dir.name}': job status is '{status}'. "
                    f"Stop the job before overwriting."
                )
        except (json.JSONDecodeError, OSError):
            pass


def clean_job_artifacts(job_dir: Path) -> None:
    """Purge old media and cache artifacts to prevent cross-video pollution when overwriting."""
    if not job_dir.exists():
        return
    # Delete top-level files in job directory (media files, logs, json caches), keeping lock file
    for p in job_dir.glob("*"):
        if p.is_file() and p.name != ".job.lock":
            try:
                p.unlink()
            except OSError:
                pass
    # Clean state and logs directories
    for sub in ("state", "logs"):
        d = job_dir / sub
        if d.exists():
            for p in d.glob("*"):
                if p.is_file():
                    try:
                        p.unlink()
                    except OSError:
                        pass


def get_next_job_index(batch_dir: Path) -> int:
    """Find the highest numerical index among existing job_* subdirectories."""
    max_idx = 0
    if not batch_dir.exists():
        return 1
    for p in batch_dir.glob("job_*"):
        if p.is_dir():
            parts = p.name.split("_")
            if len(parts) >= 2 and parts[1].isdigit():
                max_idx = max(max_idx, int(parts[1]))
    return max_idx + 1


def create_batch_jobs(items: list[str], batch_dir: Path, args, overwrite: bool = False) -> list[dict]:
    """Create job directories and job_config.json for each item with mutex safety."""
    batch_dir = batch_dir.expanduser().resolve()
    batch_dir.mkdir(parents=True, exist_ok=True)
    created_jobs = []

    coord_dir_arg = getattr(args, "coordination_dir", None)
    if coord_dir_arg:
        coord_root = Path(coord_dir_arg).expanduser().resolve()
        if coord_root.exists() and not coord_root.is_dir():
            raise ValueError(f"Coordination directory '{coord_root}' is a file, must be a directory.")
    else:
        # Check if batch_dir is inside an "output" directory
        curr = batch_dir
        found_output = None
        while curr != curr.parent:
            if curr.name == "output":
                found_output = curr
                break
            curr = curr.parent

        if found_output:
            coord_root = found_output
        elif batch_dir.parent != batch_dir:
            coord_root = batch_dir.parent
        else:
            coord_root = batch_dir

    coord_root.mkdir(parents=True, exist_ok=True)

    # Acquire batch sweep lock during creation/overwriting to coordinate with schedulers and concurrent submissions
    sweep_lock_file = open(coord_root / ".sweep.lock", "a")
    start_wait = time.time()
    locked = False
    while time.time() - start_wait < 10.0:
        try:
            fcntl.flock(sweep_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
            break
        except (BlockingIOError, OSError):
            time.sleep(0.05)

    if not locked:
        sweep_lock_file.close()
        raise RuntimeError(f"Cannot submit/overwrite batch in {batch_dir}: a sweep scheduler or another batch submission is currently holding lock on {coord_root}.")

    try:
        mark_coordination_root(coord_root, batch_dir)
        register_batch_path(coord_root, batch_dir)

        batch_meta = {
            "batch_dir": str(batch_dir.resolve()),
            "coordination_dir": str(coord_root.resolve()),
            "created_at": time.time(),
            "created_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        atomic_write_json(batch_dir / "batch_meta.json", batch_meta)

        start_index = 1 if overwrite else get_next_job_index(batch_dir)

        for offset, item in enumerate(items):
            job_number = start_index + offset
            job_name = f"job_{job_number:03d}"
            job_dir = batch_dir / job_name

            job_lock_file = None
            if job_dir.exists() and overwrite:
                # 1. Refuse to overwrite if the job is active/running
                check_job_not_active(job_dir)

                # 2. Acquire exclusive job lock during cleanup and reconfiguration
                job_lock_file = open(job_dir / ".job.lock", "w")
                try:
                    fcntl.flock(job_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (BlockingIOError, OSError):
                    job_lock_file.close()
                    raise RuntimeError(f"Cannot overwrite job '{job_name}': directory lock is held by another process.")

                clean_job_artifacts(job_dir)

            try:
                ensure_job_layout(job_dir)

                is_item_url = is_url(item)
                video_input = None if is_item_url else str(Path(item).expanduser().resolve())
                url_input = item if is_item_url else None

                def resolve_opt_path(p: str | Path | None) -> str | None:
                    if not p:
                        return None
                    return str(Path(p).expanduser().resolve())

                job_config = {
                    "url": url_input,
                    "input_video": video_input,
                    "source_srt": resolve_opt_path(getattr(args, "source_srt", None)),
                    "source_lang": args.source_lang,
                    "target_language": args.target_language,
                    "target_slug": lang_slug(args.target_language),
                    "subtitle_mode": args.subtitle_mode,
                    "translation_model": args.translation_model,
                    "translation_style": args.translation_style,
                    "translation_workers": args.translation_workers,
                    "translation_batch_size": args.translation_batch_size,
                    "terms_file": resolve_opt_path(getattr(args, "terms_file", None)),
                    "translation_context": args.translation_context,
                    "context_char_budget": getattr(args, "context_char_budget", 8000),
                    "context_neighbor_lines": getattr(args, "context_neighbor_lines", 2),
                    "timing_risk_estimator": getattr(args, "timing_risk_estimator", True),
                    "allow_source_fallback": getattr(args, "allow_source_fallback", False),
                    "asr_engine": args.asr_engine,
                    "tts_engine": args.tts_engine,
                    "profile": resolve_opt_path(getattr(args, "profile", None)),
                    "env_file": resolve_opt_path(getattr(args, "env_file", None)),
                    "coordination_dir": str(coord_root.resolve()),
                }

                # Write job_config.json
                config_path = job_dir / "job_config.json"
                atomic_write_json(config_path, job_config)

                # Initialize progress.json
                update_progress(
                    job_dir,
                    status="pending",
                    stage="queued",
                    guardian_status="healthy",
                    input_source=item,
                    target_language=args.target_language,
                    batch_dir=str(batch_dir),
                    stale_count=0,
                    resume_count=0,
                )

                # Initialize pipeline_status.json
                status_payload = {
                    "status": "pending",
                    "stage": "queued",
                    "msg": f"Job {job_name} queued in batch.",
                    "pid": None,
                    "input": item,
                    "timestamp": time.time(),
                }
                atomic_write_json(job_dir / "pipeline_status.json", status_payload)

                # Log event
                append_event(
                    job_dir,
                    "worker",
                    "info",
                    "batch_job_queued",
                    f"Job {job_name} queued in batch {batch_dir.name}.",
                    input=item,
                    target_language=args.target_language,
                )

                created_jobs.append({
                    "job_name": job_name,
                    "job_dir": str(job_dir),
                    "input": item,
                })
            finally:
                if job_lock_file:
                    try:
                        fcntl.flock(job_lock_file, fcntl.LOCK_UN)
                    except OSError:
                        pass
                    job_lock_file.close()
    finally:
        try:
            fcntl.flock(sweep_lock_file, fcntl.LOCK_UN)
        except OSError:
            pass
        sweep_lock_file.close()

    return created_jobs


def main():
    parser = argparse.ArgumentParser(
        description="Batch job submission tool for video-dubber. Queues multiple jobs for scheduled dispatch."
    )
    parser.add_argument(
        "--inputs",
        nargs="+",
        help="One or more video paths or video URLs to dub.",
    )
    parser.add_argument(
        "--list-file",
        help="Path to a text file containing video paths or URLs (one per line).",
    )
    parser.add_argument(
        "--batch-dir",
        help="Root directory for the batch. Default: output/batch_<timestamp>_<uuid>",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing job directories starting from job_001 instead of appending.",
    )
    parser.add_argument("--target-language", default="Chinese")
    parser.add_argument("--source-lang", default="en")
    parser.add_argument("--translation-model", default="deepseek")
    parser.add_argument(
        "--translation-style",
        choices=["faithful", "concise", "summary"],
        default="faithful",
    )
    parser.add_argument(
        "--translation-context",
        choices=["auto", "off"],
        default="auto",
    )
    parser.add_argument("--translation-batch-size", type=int, default=25)
    parser.add_argument("--translation-workers", type=int, default=1)
    parser.add_argument("--terms-file", default=None)
    parser.add_argument(
        "--asr-engine",
        choices=["auto", "qwen3-asr-mlx", "mlx-whisper", "whisper", "faster-whisper", "qwen3-asr"],
        default="auto",
    )
    parser.add_argument(
        "--tts-engine",
        default="qwen3-tts",
    )
    parser.add_argument(
        "--subtitle-mode",
        choices=["bilingual", "target", "source"],
        default="target",
    )
    parser.add_argument("--profile", default=None)
    parser.add_argument("--env-file", default=None)
    parser.add_argument(
        "--coordination-dir",
        help="Root directory for multi-batch coordination and global concurrency locking.",
    )
    parser.add_argument(
        "--start",
        action="store_true",
        help="Immediately trigger L1 sweep to start processing with concurrency limits.",
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=1,
        help="Max parallel jobs to run concurrently (default 1 to prevent GPU memory OOM).",
    )

    args = parser.parse_args()

    items = load_input_list(args.inputs, args.list_file)
    if not items:
        parser.error("No valid inputs provided. Use --inputs or --list-file.")

    if args.batch_dir:
        batch_dir = Path(args.batch_dir)
    else:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_suffix = uuid.uuid4().hex[:6]
        batch_dir = Path("output") / f"batch_{timestamp}_{unique_suffix}"

    created = create_batch_jobs(items, batch_dir, args, overwrite=args.overwrite)

    coord_arg = f" --coordination-dir {args.coordination_dir}" if getattr(args, "coordination_dir", None) else ""
    result = {
        "status": "success",
        "batch_dir": str(batch_dir.resolve()),
        "total_queued": len(created),
        "jobs": created,
        "next_step": f"python scripts/cron_sweep_jobs.py --jobs-dir {batch_dir} --max-parallel {args.max_parallel}{coord_arg}",
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.start:
        print(f"\n[BATCH] --start specified: Triggering initial sweep (max_parallel={args.max_parallel})...")
        sweep_cmd = [
            sys.executable,
            str(Path(__file__).with_name("cron_sweep_jobs.py")),
            "--jobs-dir",
            str(batch_dir),
            "--max-parallel",
            str(args.max_parallel),
        ]
        if getattr(args, "coordination_dir", None):
            sweep_cmd.extend(["--coordination-dir", str(args.coordination_dir)])
        subprocess.check_call(sweep_cmd)


if __name__ == "__main__":
    main()
