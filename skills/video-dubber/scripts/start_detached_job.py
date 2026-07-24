#!/usr/bin/env python3
from __future__ import annotations
import argparse, subprocess, sys
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description="Start a video-dubber command detached and write job_pid.txt.")
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("cmd", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    job = Path(args.job_dir).expanduser().resolve()
    job.mkdir(parents=True, exist_ok=True)
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not cmd:
        cmd = [sys.executable, str(Path(__file__).with_name("resume_job.py")), "--job-dir", str(job)]
    stdout = open(job / "stdout_detached.log", "ab", buffering=0)
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.STDOUT, cwd=str(job), start_new_session=True)
    (job / "job_pid.txt").write_text(str(proc.pid) + "\n", encoding="utf-8")
    print(proc.pid)

if __name__ == "__main__":
    main()
