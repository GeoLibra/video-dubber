#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

from core.job_state import artifact_snapshot, ensure_job_layout, pid_alive, read_progress

def main():
    parser = argparse.ArgumentParser(description="Compact job status: PID + heartbeat + artifact growth.")
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("--stale-sec", type=int, default=300)
    args = parser.parse_args()
    job = Path(args.job_dir).expanduser().resolve()
    ensure_job_layout(job)
    pid_path = job / "job_pid.txt"
    pid = pid_path.read_text(encoding="utf-8").strip() if pid_path.exists() else None
    status_path = job / "pipeline_status.json"
    status = {}
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception as exc:
            status = {"status_parse_error": repr(exc)}
    now = time.time()
    last_seen_age = round(now - status_path.stat().st_mtime, 1) if status_path.exists() else None
    snapshot = artifact_snapshot(job)
    artifact_age = round(now - snapshot["last_artifact_mtime"], 1) if snapshot["last_artifact_mtime"] else None
    heartbeat_stale = bool(last_seen_age is not None and last_seen_age > args.stale_sec)
    artifacts_recent = bool(artifact_age is not None and artifact_age <= args.stale_sec)
    alive = pid_alive(pid) if pid else False
    stalled = bool(heartbeat_stale and not alive and not artifacts_recent)
    progress = read_progress(job)
    structurally_stuck = progress.get("guardian_status") == "structurally_stuck"
    payload = {
        "job_dir": str(job),
        "pid": int(pid) if pid and pid.isdigit() else pid,
        "pid_alive": alive,
        "status": status,
        "progress": progress,
        "last_seen_age_sec": last_seen_age,
        "heartbeat_stale": heartbeat_stale,
        "artifacts_recent": artifacts_recent,
        "stalled": stalled,
        "structurally_stuck": structurally_stuck,
        "guardian_allowed_actions": ["liveness-check", "resume", "mark-structurally-stuck"],
        "chunk_count": snapshot["chunk_count"],
        "last_chunk": snapshot["last_chunk"],
        "last_artifact_age_sec": artifact_age,
        "outputs": snapshot["outputs"],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
