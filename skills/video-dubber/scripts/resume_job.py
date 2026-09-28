#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, subprocess, sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

from core.job_state import append_event, ensure_job_layout, read_progress, update_progress

PAIRED_BOOL_FLAGS = {
    "timing_risk_estimator": ("--timing-risk-estimator", "--no-timing-risk-estimator"),
    "allow_atempo_overflow": ("--allow-atempo-overflow", "--no-atempo-overflow"),
    "embed_cover": ("--embed-cover", "--no-embed-cover"),
    "early_original_output": ("--early-original-output", "--no-early-original-output"),
}

STORE_TRUE_FLAGS = {
    "confirm_translation",
    "allow_source_fallback",
    "auto_transcribe_ref",
    "skip_separation",
    "no_segments",
    "hf_offline",
    "allow_playlist",
    "list_formats",
    "preserve_gap_audio",
    "multi_speaker",
}

SKIP = {"target_slug", "ignore_yt_dlp_config", "coordination_dir"}


def flag_name(key: str) -> str:
    return "--" + key.replace("_", "-")


def build_pipeline_cmd(config: dict, job_dir: Path) -> list[str]:
    """Convert job_config dictionary to valid command line arguments for run_pipeline.py."""
    job = Path(job_dir).resolve()
    cfg = dict(config)
    cfg.setdefault("status", str(job / "pipeline_status.json"))
    cfg.setdefault("log", str(job / "pipeline.log"))

    cmd = [sys.executable, str(Path(__file__).with_name("run_pipeline.py"))]
    for key, value in cfg.items():
        if key in SKIP or value is None:
            continue
        if key in PAIRED_BOOL_FLAGS:
            cmd.append(PAIRED_BOOL_FLAGS[key][0] if value else PAIRED_BOOL_FLAGS[key][1])
        elif key in STORE_TRUE_FLAGS:
            if value:
                cmd.append(flag_name(key))
        else:
            cmd.extend([flag_name(key), str(value)])

    if cfg.get("ignore_yt_dlp_config") is False:
        cmd.append("--use-yt-dlp-config")

    return cmd


def main():
    parser = argparse.ArgumentParser(description="Resume a video-dubber job from job_config.json.")
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("--detached", action="store_true")
    parser.add_argument(
        "--initial",
        action="store_true",
        help="Initial startup from pending/fresh state, not a failure recovery.",
    )
    args = parser.parse_args()
    job = Path(args.job_dir).expanduser().resolve()
    ensure_job_layout(job)
    progress = read_progress(job)

    current_status = progress.get("status")
    existing_resumes = int(progress.get("resume_count", 0))
    existing_total = int(progress.get("total_resumes", 0))
    is_initial = args.initial or (
        current_status in ("pending", "initialized", "created")
        and existing_resumes == 0
        and existing_total == 0
    )

    if is_initial:
        new_resume_count = 0
        new_total_resumes = 0
        new_status = "running"
        event_source = "worker"
        event_level = "info"
        event_name = "initial_start"
        event_desc = "Initial start from job_config.json."
    else:
        new_resume_count = existing_resumes + 1
        new_total_resumes = existing_total + 1
        new_status = "resuming"
        event_source = "guardian" if args.detached else "worker"
        event_level = "decision"
        event_name = "resume_job"
        event_desc = f"Resume from existing job_config.json (attempt {new_resume_count})."

    update_progress(
        job,
        status=new_status,
        resume_count=new_resume_count,
        total_resumes=new_total_resumes,
        guardian_status="healthy" if is_initial else "resuming",
    )
    append_event(
        job,
        event_source,
        event_level,
        event_name,
        event_desc,
        detached=args.detached,
        resume_count=new_resume_count,
    )
    config = json.loads((job / "job_config.json").read_text(encoding="utf-8"))
    cmd = build_pipeline_cmd(config, job)
    if args.detached:
        subprocess.check_call([sys.executable, str(Path(__file__).with_name("start_detached_job.py")), "--job-dir", str(job), "--", *cmd])
    else:
        subprocess.check_call(cmd, cwd=str(job))


if __name__ == "__main__":
    main()
