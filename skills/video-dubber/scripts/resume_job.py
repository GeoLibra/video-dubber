#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, subprocess, sys
from pathlib import Path

BOOL_FLAGS = {
    "confirm_translation", "allow_source_fallback", "auto_transcribe_ref", "skip_separation",
    "no_segments", "hf_offline", "allow_playlist", "list_formats", "preserve_gap_audio",
    "allow_atempo_overflow", "early_original_output",
}
SKIP = {"target_slug", "ignore_yt_dlp_config", "early_original_output"}

def flag_name(key): return "--" + key.replace("_", "-")

def main():
    parser = argparse.ArgumentParser(description="Resume a video-dubber job from job_config.json.")
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("--detached", action="store_true")
    args = parser.parse_args()
    job = Path(args.job_dir).expanduser().resolve()
    config = json.loads((job / "job_config.json").read_text(encoding="utf-8"))
    config.setdefault("status", str(job / "pipeline_status.json"))
    config.setdefault("log", str(job / "pipeline.log"))
    cmd = [sys.executable, str(Path(__file__).with_name("run_pipeline.py"))]
    for key, value in config.items():
        if key in SKIP or value is None:
            continue
        if key == "allow_atempo_overflow":
            cmd.append("--allow-atempo-overflow" if value else "--no-atempo-overflow")
        elif key in BOOL_FLAGS:
            if value:
                cmd.append(flag_name(key))
        else:
            cmd.extend([flag_name(key), str(value)])
    if config.get("ignore_yt_dlp_config") is False:
        cmd.append("--use-yt-dlp-config")
    if config.get("early_original_output") is False:
        cmd.append("--no-early-original-output")
    if args.detached:
        subprocess.check_call([sys.executable, str(Path(__file__).with_name("start_detached_job.py")), "--job-dir", str(job), "--", *cmd])
    else:
        subprocess.check_call(cmd, cwd=str(job))

if __name__ == "__main__":
    main()
