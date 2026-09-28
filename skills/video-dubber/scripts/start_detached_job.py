#!/usr/bin/env python3
from __future__ import annotations
import argparse, fcntl, os, subprocess, sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

from core.job_state import append_event, ensure_job_layout, pid_alive, read_progress, update_progress


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Start a video-dubber command detached and write job_pid.txt.")
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("cmd", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    job = Path(args.job_dir).expanduser().resolve()
    job.mkdir(parents=True, exist_ok=True)
    ensure_job_layout(job)

    # Cross-process lock to prevent concurrent launches for the same job
    lock_file = open(job / ".job.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        print(f"[START_DETACHED] Job {job.name} launch is locked by another process; skipping.", file=sys.stderr)
        return

    try:
        # Check if an existing process is already running for this job
        pid_path = job / "job_pid.txt"
        if pid_path.exists():
            try:
                existing_pid = int(pid_path.read_text(encoding="utf-8").strip())
                if pid_alive(existing_pid):
                    print(f"[START_DETACHED] Job {job.name} already running with PID {existing_pid}; skipping.", file=sys.stderr)
                    print(existing_pid)
                    return
            except Exception:
                pass

        cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
        if not cmd:
            progress = read_progress(job)
            is_fresh = (
                progress.get("status") in ("pending", "initialized", "created")
                and int(progress.get("resume_count", 0)) == 0
                and int(progress.get("total_resumes", 0)) == 0
            )
            cmd = [
                sys.executable,
                str(Path(__file__).with_name("resume_job.py")),
                "--job-dir",
                str(job),
            ]
            if is_fresh:
                cmd.append("--initial")
        stdout = open(job / "stdout_detached.log", "ab", buffering=0)
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.STDOUT, cwd=str(job), start_new_session=True)
        finally:
            try:
                stdout.close()
            except Exception:
                pass

        try:
            (job / "job_pid.txt").write_text(str(proc.pid) + "\n", encoding="utf-8")
            update_progress(job, status="running", pid=proc.pid, guardian_status="healthy")
            append_event(job, "worker", "info", "detached_started", "Started detached job.", pid=proc.pid, command=cmd)
            print(proc.pid)
        except Exception:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
            except Exception:
                pass
            raise
    finally:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        except OSError:
            pass
        lock_file.close()


if __name__ == "__main__":
    main()
