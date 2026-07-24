#!/usr/bin/env python3
from __future__ import annotations
import argparse, os, signal, time
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description="Stop a detached video-dubber job by job_pid.txt.")
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("--kill-after-sec", type=float, default=8)
    args = parser.parse_args()
    pid_path = Path(args.job_dir).expanduser().resolve() / "job_pid.txt"
    if not pid_path.exists():
        print("no job_pid.txt")
        return
    pid = int(pid_path.read_text(encoding="utf-8").strip())
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print(f"pid {pid} not running")
        return
    deadline = time.time() + args.kill_after_sec
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            print(f"stopped {pid}")
            return
        time.sleep(0.5)
    try:
        os.kill(pid, signal.SIGKILL)
        print(f"killed {pid}")
    except ProcessLookupError:
        print(f"stopped {pid}")

if __name__ == "__main__":
    main()
