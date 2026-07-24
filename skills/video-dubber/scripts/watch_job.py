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

from core.job_state import append_event, ensure_job_layout, read_progress, update_progress


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


def main():
    parser = argparse.ArgumentParser(
        description="Guardian loop for a single video-dubber job. It may only liveness-check, resume, or mark structurally stuck."
    )
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("--interval-sec", type=int, default=300)
    parser.add_argument("--stale-sec", type=int, default=7200)
    parser.add_argument("--max-resumes", type=int, default=3)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

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
                stale_count = int(progress.get("stale_count", 0)) + 1
                update_progress(
                    job,
                    touch_last_seen=False,
                    stale_count=stale_count,
                    guardian_status="stalled",
                    last_status_check_age_sec=status.get("last_seen_age_sec"),
                    last_artifact_age_sec=status.get("last_artifact_age_sec"),
                )
                if stale_count >= args.max_resumes:
                    update_progress(job, status="structurally_stuck", guardian_status="structurally_stuck")
                    append_event(job, "guardian", "warn", "structurally_stuck", "Max stale count reached; stopping automatic resumes.")
                    break
                resume_count = int(progress.get("resume_count", 0))
                if resume_count >= args.max_resumes:
                    update_progress(job, status="structurally_stuck", guardian_status="structurally_stuck")
                    append_event(job, "guardian", "warn", "structurally_stuck", "Max resume count reached; stopping automatic resumes.")
                    break
                append_event(job, "guardian", "decision", "resume_stalled_job", "Heartbeat stale, process gone, and artifacts are not recent.")
                resume(job)
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
