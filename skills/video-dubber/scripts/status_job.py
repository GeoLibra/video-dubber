#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os, time
from pathlib import Path

def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False

def main():
    parser = argparse.ArgumentParser(description="Compact job status: PID + heartbeat + artifact growth.")
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("--stale-sec", type=int, default=300)
    args = parser.parse_args()
    job = Path(args.job_dir).expanduser().resolve()
    pid_path = job / "job_pid.txt"
    pid = pid_path.read_text(encoding="utf-8").strip() if pid_path.exists() else None
    status_path = job / "pipeline_status.json"
    status = {}
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception as exc:
            status = {"status_parse_error": repr(exc)}
    artifacts = list(job.glob("chunk_*_qwen3tts_*.wav")) + list(job.glob("output_*.mp4")) + list(job.glob("merged_tts_*.wav"))
    last_mtime = max([p.stat().st_mtime for p in artifacts] or [0])
    now = time.time()
    last_seen_age = round(now - status_path.stat().st_mtime, 1) if status_path.exists() else None
    chunks = sorted(job.glob("chunk_*_qwen3tts_*.wav"))
    payload = {
        "job_dir": str(job),
        "pid": int(pid) if pid and pid.isdigit() else pid,
        "pid_alive": pid_alive(pid) if pid else False,
        "status": status,
        "last_seen_age_sec": last_seen_age,
        "heartbeat_stale": bool(last_seen_age is not None and last_seen_age > args.stale_sec),
        "chunk_count": len(chunks),
        "last_chunk": chunks[-1].name if chunks else None,
        "last_artifact_age_sec": round(now - last_mtime, 1) if last_mtime else None,
        "outputs": [p.name for p in sorted(job.glob("output_*.mp4"))],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
