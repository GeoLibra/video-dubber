#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

from core.job_state import (
    append_event,
    ensure_job_layout,
    read_progress,
    register_batch_path,
    resolve_coordination_root,
    update_progress,
)
from scripts.cron_sweep_jobs import SweepLock, count_global_active_jobs


def run_status(job, stale_sec):
    cmd = [
        sys.executable,
        str(Path(__file__).with_name("status_job.py")),
        "--job-dir",
        str(job),
        "--stale-sec",
        str(stale_sec),
    ]
    output = subprocess.check_output(cmd, text=True)
    return json.loads(output)


def resume(job):
    cmd = [
        sys.executable,
        str(Path(__file__).with_name("resume_job.py")),
        "--job-dir",
        str(job),
        "--detached",
    ]
    subprocess.check_call(cmd)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Guardian loop for a single video-dubber job. It may only liveness-check, resume, or mark structurally stuck."
    )
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("--interval-sec", type=int, default=300)
    parser.add_argument("--stale-sec", type=int, default=7200)
    parser.add_argument("--max-resumes", type=int, default=3)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-parallel", type=int, default=1, help="Max parallel running jobs in shared coordination scope.")
    parser.add_argument("--coordination-dir", default=None, help="Explicit coordination directory root.")
    args = parser.parse_args(argv)

    job = Path(args.job_dir).expanduser().resolve()
    ensure_job_layout(job)
    append_event(job, "guardian", "info", "guardian_started", "Guardian started.", interval_sec=args.interval_sec, stale_sec=args.stale_sec)

    while True:
        try:
            status = run_status(job, args.stale_sec)
            append_event(job, "guardian", "info", "liveness_check", "Checked job liveness.", status=status)
            progress = read_progress(job)
            if status.get("structurally_stuck"):
                update_progress(job, status="structurally_stuck", guardian_status="structurally_stuck")
                append_event(job, "guardian", "warn", "structurally_stuck", "Max stale count reached; stopping automatic resumes.")
                break
            if status.get("stalled"):
                coord_root = Path(args.coordination_dir).resolve() if args.coordination_dir else resolve_coordination_root(job)
                with SweepLock(coord_root / ".sweep.lock") as lock:
                    if lock is None:
                        append_event(job, "guardian", "warn", "sweep_locked", "Coordination lock held by another process; deferring resume.")
                        update_progress(
                            job,
                            touch_last_seen=False,
                            guardian_status="concurrency_queued",
                            last_status_check_age_sec=status.get("last_seen_age_sec"),
                            last_artifact_age_sec=status.get("last_artifact_age_sec"),
                        )
                    else:
                        # 1. Re-read status under the lock to prevent duplicate resumes by concurrent guardians
                        status = run_status(job, args.stale_sec)
                        progress = read_progress(job)
                        if status.get("structurally_stuck"):
                            update_progress(job, status="structurally_stuck", guardian_status="structurally_stuck")
                            append_event(job, "guardian", "warn", "structurally_stuck", "Max stale count reached; stopping automatic resumes.")
                            break
                        elif not status.get("stalled"):
                            append_event(job, "guardian", "info", "already_recovering", "Job is no longer stalled after acquiring coordination lock; skipping duplicate resume.")
                            update_progress(
                                job,
                                touch_last_seen=False,
                                guardian_status="healthy" if progress.get("guardian_status") != "resumed" else progress.get("guardian_status"),
                                last_status_check_age_sec=status.get("last_seen_age_sec"),
                                last_artifact_age_sec=status.get("last_artifact_age_sec"),
                            )
                        else:
                            try:
                                register_batch_path(coord_root, job.parent)
                                global_running, _, _ = count_global_active_jobs(coord_root, stale_sec=args.stale_sec, exclude_job=job)
                            except Exception as e:
                                append_event(
                                    job,
                                    "guardian",
                                    "warn",
                                    "registry_error",
                                    f"Failed to access coordination registry in {coord_root}: {e}; deferring resume.",
                                )
                                update_progress(
                                    job,
                                    touch_last_seen=False,
                                    guardian_status="concurrency_queued",
                                    last_status_check_age_sec=status.get("last_seen_age_sec"),
                                    last_artifact_age_sec=status.get("last_artifact_age_sec"),
                                )
                            else:
                                if len(global_running) >= args.max_parallel:
                                    append_event(
                                        job,
                                        "guardian",
                                        "warn",
                                        "concurrency_limit_reached",
                                        f"Global concurrency limit reached ({len(global_running)}/{args.max_parallel} active in {coord_root}); deferring resume.",
                                    )
                                    update_progress(
                                        job,
                                        touch_last_seen=False,
                                        guardian_status="concurrency_queued",
                                        last_status_check_age_sec=status.get("last_seen_age_sec"),
                                        last_artifact_age_sec=status.get("last_artifact_age_sec"),
                                    )
                                else:
                                    stale_count = int(progress.get("stale_count", 0)) + 1
                                    resume_count = int(progress.get("resume_count", 0))
                                    if resume_count >= args.max_resumes or stale_count > args.max_resumes:
                                        update_progress(job, status="structurally_stuck", guardian_status="structurally_stuck")
                                        append_event(job, "guardian", "warn", "structurally_stuck", "Max retry limit reached; stopping automatic resumes.")
                                        break

                                    # Pre-mark status as resuming under the lock to claim concurrency slot immediately
                                    update_progress(
                                        job,
                                        status="resuming",
                                        touch_last_seen=True,
                                        stale_count=stale_count,
                                        guardian_status="resumed",
                                        last_status_check_age_sec=status.get("last_seen_age_sec"),
                                        last_artifact_age_sec=status.get("last_artifact_age_sec"),
                                    )
                                    append_event(job, "guardian", "decision", "resume_stalled_job", "Heartbeat stale, process gone, and concurrency slot available.")
                                    try:
                                        resume(job)
                                    except Exception as launch_err:
                                        append_event(
                                            job,
                                            "guardian",
                                            "error",
                                            "resume_launch_failed",
                                            f"Failed to execute resume: {launch_err}",
                                            error=repr(launch_err),
                                        )
                                        update_progress(
                                            job,
                                            status="failed",
                                            touch_last_seen=False,
                                            guardian_status="launch_failed",
                                            last_status_check_age_sec=status.get("last_seen_age_sec"),
                                            last_artifact_age_sec=status.get("last_artifact_age_sec"),
                                        )
                                        if args.once:
                                            raise
            else:
                update_progress(
                    job,
                    touch_last_seen=False,
                    stale_count=0,
                    guardian_status="healthy",
                    last_status_check_age_sec=status.get("last_seen_age_sec"),
                    last_artifact_age_sec=status.get("last_artifact_age_sec"),
                )
            if args.once:
                break
        except Exception as exc:
            append_event(job, "guardian", "error", "guardian_error", repr(exc))
            if args.once:
                raise
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    main()
