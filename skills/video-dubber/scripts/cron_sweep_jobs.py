#!/usr/bin/env python3
"""L1 Scheduled Sweeper and Watchdog for video-dubber jobs.

Scans job directories, monitors liveness, auto-resumes stalled jobs,
marks structurally stuck jobs after retry limits, dispatches pending jobs
respecting concurrency limits (max-parallel), and updates L1 heartbeat.
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
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

from core.job_runtime import atomic_write_json
from core.job_state import (
    append_event,
    artifact_snapshot,
    ensure_job_layout,
    get_registered_batches,
    mark_coordination_root,
    pid_alive,
    read_progress,
    register_batch_path,
    resolve_coordination_root,
    update_progress,
)


class SweepLock:
    """Inter-process advisory lock to ensure only one sweep runs at a time per jobs_dir."""

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self._fd = None

    def __enter__(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = open(self.lock_path, "a")
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self
        except (BlockingIOError, OSError):
            if self._fd:
                try:
                    self._fd.close()
                except OSError:
                    pass
                self._fd = None
            return None

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._fd:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self._fd.close()
            except OSError:
                pass
            self._fd = None


def discover_jobs(root_dir: Path) -> list[Path]:
    """Find all valid video-dubber job directories under root_dir."""
    root = root_dir.expanduser().resolve()
    if not root.exists():
        return []

    # If root itself is a job
    if (root / "job_config.json").exists() or (root / "state" / "progress.json").exists():
        return [root]

    jobs = []
    # Scan children (depth 1) and grandchildren (depth 2, e.g. output/batch_xxx/job_xxx)
    try:
        children = sorted(root.iterdir())
    except (OSError, PermissionError) as exc:
        raise RuntimeError(f"Cannot enumerate directory {root}: {exc}") from exc

    for child in children:
        if not child.is_dir() or child.name.startswith("."):
            continue
        if (child / "job_config.json").exists() or (child / "state" / "progress.json").exists():
            jobs.append(child)
        else:
            try:
                grandchildren = sorted(child.iterdir())
            except (OSError, PermissionError) as exc:
                raise RuntimeError(f"Cannot enumerate directory {child}: {exc}") from exc
            for grandchild in grandchildren:
                if (
                    grandchild.is_dir()
                    and not grandchild.name.startswith(".")
                    and (
                        (grandchild / "job_config.json").exists()
                        or (grandchild / "state" / "progress.json").exists()
                    )
                ):
                    jobs.append(grandchild)

    return sorted(jobs)


def assess_job(job_dir: Path, stale_sec: int) -> dict:
    """Assess current state and liveness of a single job."""
    ensure_job_layout(job_dir)
    progress = read_progress(job_dir)
    status = progress.get("status", "unknown")
    guardian_status = progress.get("guardian_status", "healthy")

    pid_path = job_dir / "job_pid.txt"
    pid = pid_path.read_text(encoding="utf-8").strip() if pid_path.exists() else None
    alive = pid_alive(pid) if pid else False

    status_path = job_dir / "pipeline_status.json"
    now = time.time()
    last_seen_age = round(now - status_path.stat().st_mtime, 1) if status_path.exists() else 999999.0
    snapshot = artifact_snapshot(job_dir)
    artifact_age = (
        round(now - snapshot["last_artifact_mtime"], 1)
        if snapshot.get("last_artifact_mtime")
        else None
    )

    heartbeat_stale = bool(last_seen_age > stale_sec)
    artifacts_recent = bool(artifact_age is not None and artifact_age <= stale_sec)

    # Strict stalled definition: status is running/resuming/launch_failed, process is dead, heartbeat is stale, AND no recent artifacts
    stalled = bool(
        (status in ("running", "resuming") or guardian_status == "launch_failed")
        and not alive
        and (heartbeat_stale or guardian_status == "launch_failed")
        and not artifacts_recent
    )

    return {
        "job_dir": job_dir,
        "status": status,
        "guardian_status": guardian_status,
        "pid": pid,
        "alive": alive,
        "last_seen_age": last_seen_age,
        "heartbeat_stale": heartbeat_stale,
        "artifacts_recent": artifacts_recent,
        "stalled": stalled,
        "progress": progress,
        "snapshot": snapshot,
    }


def count_global_active_jobs(
    coord_root: Path,
    stale_sec: int = 7200,
    exclude_job: Path | None = None,
) -> tuple[list[Path], list[Path], list[Path]]:
    """Return (global_running_jobs, coord_jobs, registered_batches) across coord_root and all registered batches."""
    registered_batches = get_registered_batches(coord_root)
    roots_to_scan = {coord_root}
    roots_to_scan.update(registered_batches)

    coord_jobs = []
    seen_job_paths = set()
    for r in sorted(roots_to_scan):
        for job in discover_jobs(r):
            job_resolved = job.resolve()
            if job_resolved not in seen_job_paths:
                seen_job_paths.add(job_resolved)
                coord_jobs.append(job_resolved)

    global_running_jobs: list[Path] = []
    ex_resolved = exclude_job.resolve() if exclude_job else None
    for job in coord_jobs:
        if ex_resolved and job.resolve() == ex_resolved:
            continue
        info = assess_job(job, stale_sec)
        status = info["status"]
        guardian_status = info["guardian_status"]
        if status in ("completed", "structurally_stuck", "pending"):
            continue
        if guardian_status == "structurally_stuck":
            continue

        # Prioritize physical living process over failed/launch_failed status:
        # If the process is alive (even if marked failed or launch_failed), it is consuming resources!
        if info["alive"]:
            global_running_jobs.append(job)
            continue

        if status == "failed" or guardian_status == "launch_failed":
            continue
        if status in ("running", "resuming") and not info["stalled"]:
            # Startup / handoff grace period occupies a slot
            global_running_jobs.append(job)

    return global_running_jobs, coord_jobs, registered_batches


def sweep_once(
    jobs_dir: Path,
    max_parallel: int = 1,
    stale_sec: int = 7200,
    max_resumes: int = 3,
    heartbeat_file: Path | None = None,
    emergency: bool = False,
    quiet: bool = False,
    coordination_dir: Path | None = None,
    per_batch_quota: bool = False,
) -> dict:
    """Execute a single L1 sweep across all jobs with concurrency control and file locking."""
    jobs_dir = jobs_dir.expanduser().resolve()
    if coordination_dir:
        coord_root = Path(coordination_dir).expanduser().resolve()
        if coord_root.exists() and not coord_root.is_dir():
            raise ValueError(f"Coordination directory '{coord_root}' is a file, must be a directory.")
        coord_root.mkdir(parents=True, exist_ok=True)
        coordination_mode = "explicit_cli"
        is_global_scope = True
    elif per_batch_quota:
        coord_root = jobs_dir
        coordination_mode = "isolated_batch"
        is_global_scope = False
    else:
        coord_root = resolve_coordination_root(jobs_dir)
        if coord_root != jobs_dir or coord_root.name == "output" or (jobs_dir / ".coordination_root").exists():
            coordination_mode = "shared_root"
            is_global_scope = True
        else:
            # Custom directory without coordination configuration
            coordination_mode = "unconfigured_local"
            is_global_scope = False
            if not quiet:
                print(
                    f"[L1 SWEEP] Notice: '{jobs_dir}' has no shared coordination root configured. "
                    f"Running with local batch scope. For global multi-batch concurrency protection, "
                    f"specify --coordination-dir or use batch_submit.py to bind a coordination root.",
                    file=sys.stderr,
                )

    mark_coordination_root(coord_root, jobs_dir)
    lock_path = coord_root / ".sweep.lock"

    with SweepLock(lock_path) as lock:
        if lock is None:
            if not quiet:
                print(f"[L1 SWEEP] Another sweep is currently in progress for {coord_root}. Exiting.", file=sys.stderr)
            return {
                "status": "locked",
                "jobs_dir": str(jobs_dir),
                "coordination_root": str(coord_root),
                "coordination_mode": coordination_mode,
                "is_global_scope": is_global_scope,
                "timestamp": time.time(),
            }

        # 1. Register jobs_dir into coordination registry
        try:
            registered_batches = register_batch_path(coord_root, jobs_dir)
        except Exception as e:
            if not quiet:
                print(
                    f"[L1 SWEEP] Error: Failed to register batch {jobs_dir} into {coord_root}: {e}. "
                    f"Halting sweep to prevent exceeding concurrency quota.",
                    file=sys.stderr,
                )
            return {
                "status": "error",
                "error": f"Failed to register batch in coordination directory: {e}",
                "jobs_dir": str(jobs_dir),
                "coordination_root": str(coord_root),
                "timestamp": time.time(),
            }

        # 2. Discover all jobs across coordination root AND all registered batches
        try:
            global_running_jobs, coord_jobs, _ = count_global_active_jobs(coord_root, stale_sec=stale_sec)
        except Exception as e:
            if not quiet:
                print(
                    f"[L1 SWEEP] Error: Failed to discover active jobs in coordination directory {coord_root}: {e}. "
                    f"Halting sweep to prevent exceeding concurrency quota.",
                    file=sys.stderr,
                )
            return {
                "status": "error",
                "error": f"Failed to discover active jobs in coordination directory: {e}",
                "jobs_dir": str(jobs_dir),
                "coordination_root": str(coord_root),
                "timestamp": time.time(),
            }
        current_active = len(global_running_jobs)
        available_slots = max(0, max_parallel - current_active)

        # 3. Target jobs to manage/dispatch in this sweep
        if jobs_dir == coord_root:
            all_jobs = coord_jobs
        else:
            try:
                all_jobs = discover_jobs(jobs_dir)
            except Exception as e:
                if not quiet:
                    print(
                        f"[L1 SWEEP] Error: Failed to discover jobs in {jobs_dir}: {e}. "
                        f"Halting sweep to prevent exceeding concurrency quota.",
                        file=sys.stderr,
                    )
                return {
                    "status": "error",
                    "error": f"Failed to discover jobs in jobs directory: {e}",
                    "jobs_dir": str(jobs_dir),
                    "coordination_root": str(coord_root),
                    "timestamp": time.time(),
                }

        scripts_dir = Path(__file__).resolve().parent

        running_jobs: list[Path] = []
        pending_jobs: list[Path] = []
        stalled_jobs: list[dict] = []
        resumed_jobs: list[Path] = []
        dispatched_jobs: list[Path] = []
        completed_jobs: list[Path] = []
        structurally_stuck_jobs: list[Path] = []
        failed_jobs: list[Path] = []

        for job in all_jobs:
            info = assess_job(job, stale_sec)
            status = info["status"]
            guardian_status = info["guardian_status"]

            if guardian_status == "structurally_stuck" or status == "structurally_stuck":
                structurally_stuck_jobs.append(job)
            elif status == "completed":
                completed_jobs.append(job)
            elif status == "pending":
                pending_jobs.append(job)
            elif info["alive"]:
                running_jobs.append(job)
            elif info["stalled"] or guardian_status == "launch_failed":
                # Strictly meets stall criteria: heartbeat stale, PID dead, no recent artifacts, or launch failed
                stalled_jobs.append(info)
            elif status == "failed":
                failed_jobs.append(job)
            elif status in ("running", "resuming"):
                # Not alive, but NOT stalled (heartbeat fresh or artifacts recent) -> startup / handoff grace period
                running_jobs.append(job)
            else:
                failed_jobs.append(job)

        # -------------------------------------------------------------
        # Concurrency Gate:
        # Both stalled jobs needing resume AND pending jobs needing dispatch
        # must compete for the available slots.
        # -------------------------------------------------------------
        # 1. First, recover stalled jobs up to available_slots
        for info in stalled_jobs:
            job = info["job_dir"]
            progress = info["progress"]
            stale_count = int(progress.get("stale_count", 0)) + 1
            resume_count = int(progress.get("resume_count", 0))

            if resume_count >= max_resumes or stale_count > max_resumes:
                update_progress(
                    job,
                    status="structurally_stuck",
                    guardian_status="structurally_stuck",
                    stale_count=stale_count,
                )
                append_event(
                    job,
                    "guardian",
                    "warn",
                    "structurally_stuck",
                    f"Max retry limit ({max_resumes}) reached; stopped automatic resume.",
                    stale_count=stale_count,
                    resume_count=resume_count,
                )
                structurally_stuck_jobs.append(job)
                if not quiet:
                    print(f"[L1 SWEEP] Job {job.name} reached max resumes ({max_resumes}). Marked structurally_stuck.")
            elif available_slots > 0:
                # Slot is available: resume this stalled job
                update_progress(
                    job,
                    status="resuming",
                    guardian_status="resumed",
                    stale_count=stale_count,
                    touch_last_seen=True,
                )
                append_event(
                    job,
                    "guardian",
                    "decision",
                    "resume_stalled_job",
                    "L1 sweep detected stalled job; initiating detached resume.",
                    stale_count=stale_count,
                )
                resume_cmd = [
                    sys.executable,
                    str(scripts_dir / "resume_job.py"),
                    "--job-dir",
                    str(job),
                    "--detached",
                ]
                try:
                    subprocess.check_call(resume_cmd)
                    resumed_jobs.append(job)
                    available_slots -= 1
                    current_active += 1
                    if not quiet:
                        print(f"[L1 SWEEP] Job {job.name} was stalled. Auto-resumed (attempt {stale_count}/{max_resumes}).")
                except Exception as exc:
                    append_event(job, "guardian", "error", "resume_failed", f"Failed to resume job: {exc}")
                    update_progress(
                        job,
                        status="failed",
                        guardian_status="launch_failed",
                        touch_last_seen=False,
                    )
                    failed_jobs.append(job)
            else:
                if not quiet:
                    print(f"[L1 SWEEP] Job {job.name} is stalled but no concurrency slot available ({current_active}/{max_parallel}); waiting.")

        # 2. Next, dispatch pending jobs with any remaining available_slots
        if available_slots > 0 and pending_jobs:
            to_dispatch = pending_jobs[:available_slots]
            for job in to_dispatch:
                launch_cmd = [
                    sys.executable,
                    str(scripts_dir / "start_detached_job.py"),
                    "--job-dir",
                    str(job),
                ]
                try:
                    subprocess.check_call(launch_cmd)
                    append_event(
                        job,
                        "guardian",
                        "decision",
                        "dispatched_from_queue",
                        f"L1 sweep dispatched pending job.",
                    )
                    dispatched_jobs.append(job)
                    available_slots -= 1
                    current_active += 1
                    if not quiet:
                        print(f"[L1 SWEEP] Dispatched pending job: {job.name}")
                except Exception as exc:
                    append_event(job, "guardian", "error", "dispatch_failed", f"Failed to dispatch pending job: {exc}")
                    update_progress(
                        job,
                        status="failed",
                        guardian_status="launch_failed",
                        touch_last_seen=False,
                    )

        # 4. Touch Heartbeat File
        if emergency:
            hb_file = coord_root / "heartbeat_emergency.json"
        else:
            hb_file = Path(heartbeat_file) if heartbeat_file else (coord_root / ".l1_heartbeat")
        now_ts = time.time()
        hb_payload = {
            "timestamp": now_ts,
            "iso": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "pid": os.getpid(),
            "mode": "emergency" if emergency else "scheduled",
            "jobs_dir": str(jobs_dir),
            "coordination_root": str(coord_root),
            "coordination_mode": coordination_mode,
            "is_global_scope": is_global_scope,
            "registered_batches_count": len(registered_batches),
            "max_parallel": max_parallel,
            "stats": {
                "total_jobs": len(all_jobs),
                "running": len(running_jobs) + len(resumed_jobs) + len(dispatched_jobs),
                "pending": len(pending_jobs) - len(dispatched_jobs),
                "completed": len(completed_jobs),
                "stalled_unresumed": len(stalled_jobs) - len(resumed_jobs),
                "resumed": len(resumed_jobs),
                "dispatched": len(dispatched_jobs),
                "structurally_stuck": len(structurally_stuck_jobs),
                "failed": len(failed_jobs),
            },
        }
        atomic_write_json(hb_file, hb_payload)

        report = {
            "status": "ok",
            "timestamp": now_ts,
            "jobs_dir": str(jobs_dir),
            "coordination_root": str(coord_root),
            "coordination_mode": coordination_mode,
            "is_global_scope": is_global_scope,
            "registered_batches_count": len(registered_batches),
            "total_jobs": len(all_jobs),
            "running": len(running_jobs),
            "pending": len(pending_jobs) - len(dispatched_jobs),
            "completed": len(completed_jobs),
            "stalled": len(stalled_jobs),
            "resumed": len(resumed_jobs),
            "dispatched": len(dispatched_jobs),
            "structurally_stuck": len(structurally_stuck_jobs),
            "failed": len(failed_jobs),
            "active_running_total": current_active,
            "max_parallel": max_parallel,
            "heartbeat_file": str(hb_file),
        }

        return report


def main():
    parser = argparse.ArgumentParser(
        description="L1 Sweeper & Watchdog for video-dubber. Periodically dispatches and recovers jobs."
    )
    parser.add_argument(
        "--jobs-dir",
        default="output",
        help="Root directory containing jobs or batch subdirectories (default: output).",
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=1,
        help="Maximum concurrent running jobs (default: 1).",
    )
    parser.add_argument(
        "--stale-sec",
        type=int,
        default=7200,
        help="Heartbeat timeout threshold in seconds before marking stalled (default: 7200).",
    )
    parser.add_argument(
        "--max-resumes",
        type=int,
        default=3,
        help="Maximum auto-resume retries before marking structurally_stuck (default: 3).",
    )
    parser.add_argument(
        "--heartbeat-file",
        help="Path to write L1 heartbeat timestamp file (default: <coordination-root>/.l1_heartbeat).",
    )
    parser.add_argument(
        "--coordination-dir",
        default=None,
        help="Root directory for concurrency counting and lock coordination (default: auto-resolved output dir).",
    )
    parser.add_argument(
        "--per-batch-quota",
        action="store_true",
        help="Restrict concurrency accounting and lock strictly to --jobs-dir without coordinating with parent output directory.",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run continuously in background daemon loop.",
    )
    parser.add_argument(
        "--interval-sec",
        type=int,
        default=60,
        help="Interval in seconds between sweeps when in daemon mode (default: 60).",
    )
    parser.add_argument(
        "--emergency",
        action="store_true",
        help="Indicate this sweep was triggered by L0 emergency watchdog.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress human-readable progress prints; output JSON only.",
    )

    args = parser.parse_args()
    jobs_dir = Path(args.jobs_dir)
    hb_file = Path(args.heartbeat_file).expanduser().resolve() if args.heartbeat_file else None
    coord_dir = Path(args.coordination_dir).expanduser().resolve() if args.coordination_dir else None

    if args.daemon:
        if not args.quiet:
            print(f"[L1 DAEMON] Started L1 sweeper daemon (interval={args.interval_sec}s, max_parallel={args.max_parallel})...")
        while True:
            try:
                report = sweep_once(
                    jobs_dir=jobs_dir,
                    max_parallel=args.max_parallel,
                    stale_sec=args.stale_sec,
                    max_resumes=args.max_resumes,
                    heartbeat_file=hb_file,
                    emergency=args.emergency,
                    quiet=args.quiet,
                    coordination_dir=coord_dir,
                    per_batch_quota=args.per_batch_quota,
                )
                if not args.quiet and report.get("status") == "ok":
                    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Active: {report['active_running_total']}/{args.max_parallel}, Pending: {report['pending']}, Completed: {report['completed']}")
            except Exception as exc:
                print(f"[L1 DAEMON ERROR] {exc}", file=sys.stderr)
            time.sleep(args.interval_sec)
    else:
        report = sweep_once(
            jobs_dir=jobs_dir,
            max_parallel=args.max_parallel,
            stale_sec=args.stale_sec,
            max_resumes=args.max_resumes,
            heartbeat_file=hb_file,
            emergency=args.emergency,
            quiet=args.quiet,
            coordination_dir=coord_dir,
            per_batch_quota=args.per_batch_quota,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
