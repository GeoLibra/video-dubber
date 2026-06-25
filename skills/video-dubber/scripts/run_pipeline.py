"""视频配音管线入口。

职责：
  - 解析 CLI 参数
  - 加载 YAML 配置（默认值 + 覆盖文件 + CLI 覆盖）
  - 编排各阶段（下载 → ASR → 翻译 → TTS → 合成 → 验证）
  - 写 status.json 和 run.log 供 Agent 轮询
"""

import argparse
import json
import shutil
import sys
import traceback
from pathlib import Path

from dotenv import load_dotenv


SKILL_DIR = Path(__file__).resolve().parents[1]
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))


from core.config import build_config
from core.prereqs import verify_tools, setup_runtime, check_disk, resolve_font
from core.source_loader import download_video, separate_audio, build_yt_dlp_cmd
from core.media import probe_duration, run as media_run
from core.speech import transcribe_audio
from core.lang import normalize_name, slug as lang_slug
from core.subtitle import source_hash, get_sub_source_text
from core.translate import translate_subtitles
from core.audio_builder import prepare_reference_audio, generate_and_merge, add_gap_audio
from core.video_builder import synthesize_videos
from core.verifier import verify_outputs
from core.costs import TaskMeter


FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"


def log(msg, step=None):
    prefix = f"[{step}] " if step else ""
    print(prefix + str(msg), flush=True)


def update_status(status_file, status, msg="", **extra):
    payload = {"status": status, "message": msg, **extra}
    Path(status_file).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def preflight(job_dir, args):
    verify_tools(args.url)
    font = resolve_font(args.font_file)
    if not font.exists():
        raise RuntimeError(f"Subtitle font not found: {font}")
    free_gb = check_disk(job_dir, args.min_free_gb)
    log(f"Preflight OK. Free disk: {free_gb:.2f}GB. Font: {font}", "PREFLIGHT")


def write_job_config(job_dir, args):
    from core.source_loader import write_job_config as _write
    return _write(job_dir, args)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url")
    parser.add_argument("--input-video")
    parser.add_argument("--source-srt")
    parser.add_argument("--status", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--model-config", default=None)
    parser.add_argument("--profile", default=None, help="Path to YAML profile override.")
    parser.add_argument("--target-language", default="Chinese")
    parser.add_argument("--source-lang", default="en")
    parser.add_argument("--translation-model", default="gemini-3.5-flash")
    parser.add_argument("--translation-batch-size", type=int, default=25)
    parser.add_argument("--confirm-translation", action="store_true")
    parser.add_argument("--subtitle-mode", choices=["bilingual", "target", "source"], default="target")
    parser.add_argument("--ref-audio")
    parser.add_argument("--ref-text")
    parser.add_argument("--ref-text-file")
    parser.add_argument("--auto-transcribe-ref", action="store_true")
    parser.add_argument("--whisper-model")
    parser.add_argument("--tts-engine", choices=["f5-mlx", "none"], default="f5-mlx")
    parser.add_argument("--skip-separation", action="store_true")
    parser.add_argument("--no-segments", action="store_true")
    parser.add_argument("--hf-offline", action="store_true")
    parser.add_argument("--tmp-dir")
    parser.add_argument("--font-file")
    parser.add_argument("--min-free-gb", type=float, default=2.0)
    parser.add_argument("--max-atempo", type=float, default=1.6)
    parser.add_argument("--max-clip-ms", type=int, default=80)
    parser.add_argument("--max-overhang-ms", type=int, default=450)
    parser.add_argument(
        "--download-format",
        default="bv*[height<=1080]+ba/best[height<=1080]/bv*+ba/best",
    )
    parser.add_argument("--sub-langs", default="en.*,en")
    parser.add_argument(
        "--cookies-from-browser",
        choices=["chrome", "firefox", "safari", "edge", "opera", "brave", "chromium", "vivaldi"],
    )
    parser.add_argument(
        "--use-yt-dlp-config",
        dest="ignore_yt_dlp_config",
        action="store_false",
    )
    parser.set_defaults(ignore_yt_dlp_config=True)
    parser.add_argument("--allow-playlist", action="store_true")
    parser.add_argument("--playlist-items")
    parser.add_argument("--list-formats", action="store_true")
    parser.add_argument("--proxy")
    parser.add_argument("--concurrent-fragments", type=int)
    parser.add_argument("--external-downloader")
    parser.add_argument("--preserve-gap-audio", action="store_true")
    parser.add_argument("--gap-audio-gain-db", type=float, default=-6.0)
    parser.add_argument("--gap-pad-ms", type=int, default=60)
    parser.add_argument("--cost-out", default=None, help="Path to write cost summary JSON.")
    args = parser.parse_args()
    args.target_language = normalize_name(args.target_language)
    if args.max_atempo < 1.0:
        parser.error("--max-atempo must be >= 1.0.")
    if args.max_clip_ms < 0 or args.max_overhang_ms < 0:
        parser.error("--max-clip-ms and --max-overhang-ms must be >= 0.")
    if args.list_formats and not args.url:
        parser.error("--list-formats requires --url.")
    if not args.url and not args.input_video:
        parser.error("Provide --url or --input-video.")
    return args


def main():
    args = parse_args()

    if args.env_file and Path(args.env_file).exists():
        load_dotenv(args.env_file)
    elif Path(".env").exists():
        load_dotenv(".env")

    config = build_config(args, overlay_path=args.profile)

    job_dir = Path(args.status).resolve().parent
    job_dir.mkdir(parents=True, exist_ok=True)

    tmp_dir = setup_runtime(job_dir, tmp_dir=args.tmp_dir)

    sys.stdout = open(args.log, "a", buffering=1, encoding="utf-8")
    sys.stderr = sys.stdout

    meter = TaskMeter()

    try:
        meter.phase_start("preflight")
        update_status(args.status, "running", "preflight", stage="preflight")
        preflight(job_dir, args)
        job_config_path = write_job_config(job_dir, args)
        meter.phase_end()

        if args.list_formats:
            update_status(args.status, "running", "listing formats", stage="download_formats")
            cmd = build_yt_dlp_cmd(args.url, Path(job_dir) / "raw_video.mp4", args, list_formats=True, config=config)
            media_run(cmd, "DOWNLOAD")
            update_status(args.status, "completed", "Listed formats", job_config=job_config_path)
            return

        meter.phase_start("download")
        update_status(args.status, "running", "downloading", stage="download")
        video_path, audio_path = download_video(args.url, job_dir, args, args.input_video, config=config)
        video_duration_s = probe_duration(video_path)
        meter.log_audio(video_duration_s / 60.0)
        meter.phase_end()

        if args.tts_engine == "none":
            vocals_path, no_vocals_path = audio_path, None
            log("Subtitle-only mode: skip audio separation and TTS preparation.", "AUDIO")
        else:
            meter.phase_start("audio_separation")
            update_status(args.status, "running", "separating audio", stage="audio_separation")
            vocals_path, no_vocals_path = separate_audio(audio_path, job_dir, args.skip_separation)
            meter.phase_end()

        meter.phase_start("asr")
        update_status(args.status, "running", "transcribing", stage="asr")
        subs = transcribe_audio(audio_path, job_dir, args)
        meter.phase_end()

        if not args.confirm_translation:
            total_chars = sum(len(get_sub_source_text(sub)) for sub in subs)
            total_subs = len(subs)
            est_tokens = int(total_chars * 1.3)
            log(
                f"Translation token estimate: ~{est_tokens} input tokens, {total_subs} subtitles. "
                f"Re-run with --confirm-translation to proceed, or remove API keys to use agent translation.",
                "TRANSLATE",
            )
            update_status(
                args.status, "confirm_translation",
                f"Estimated ~{est_tokens} input tokens for {total_subs} subtitles. "
                f"Re-run with --confirm-translation to proceed, or remove API keys to translate manually.",
                estimated_tokens=est_tokens,
                subtitle_count=total_subs,
            )
            sys.exit(0)

        meter.phase_start("translation")
        update_status(args.status, "running", "translating", stage="translation")
        ass_path, subs_translated, _translations = translate_subtitles(subs, job_dir, args)
        if ass_path is None:
            slug = lang_slug(args.target_language)
            meta_path = job_dir / f"translations_{slug}.json"
            raw_srt_path = job_dir / "source_raw.srt"
            sub_hash = source_hash(subs)
            cache_meta_path = job_dir / f"translations_{slug}.meta.json"
            expected_cache = {
                "source_hash": sub_hash,
                "target_language": args.target_language,
                "translation_model": args.translation_model,
            }
            cache_meta_path.write_text(json.dumps(expected_cache, ensure_ascii=False, indent=2), encoding="utf-8")
            update_status(
                args.status, "awaiting_translation",
                f"No API key configured. Raw SRT saved to {raw_srt_path}. "
                f"Translate and save results to {meta_path}, then re-run.",
                raw_srt=str(raw_srt_path),
                translations_cache=str(meta_path),
            )
            log(
                f"No translation model configured.\n"
                f"  1. Read source subtitles: {raw_srt_path}\n"
                f"  2. Translate each line to {args.target_language}\n"
                f"  3. Write JSON to: {meta_path}\n"
                f"     Format: {{\"0\": {{\"display_text\": \"...\", \"tts_text\": \"...\"}}, ...}}\n"
                f"  4. Re-run the same command to continue.",
                "TRANSLATE",
            )
            sys.exit(0)
        meter.phase_end()

        if args.tts_engine == "none":
            tts_audio, tts_report = None, []
            log("Subtitle-only mode: skip reference audio and voice cloning.", "TTS")
        else:
            meter.phase_start("reference")
            update_status(args.status, "running", "preparing reference audio", stage="reference")
            ref_audio_path, ref_text = prepare_reference_audio(subs, vocals_path, job_dir, args)
            log(f"Reference text: {ref_text}", "REF_TEXT")
            meter.phase_end()

            meter.phase_start("tts")
            update_status(args.status, "running", "generating aligned tts", stage="tts")
            tts_audio, tts_report = generate_and_merge(
                subs_translated, job_dir, ref_audio_path, ref_text, video_duration_s, args
            )
            tts_chars = sum(len(item.get("text", "")) for item in tts_report)
            meter.log_tts(tts_chars)
            meter.phase_end()

        if args.preserve_gap_audio and not no_vocals_path:
            tts_audio = add_gap_audio(tts_audio, audio_path, subs_translated, job_dir, video_duration_s, args)

        meter.phase_start("synthesis")
        update_status(args.status, "running", "synthesizing videos", stage="synthesis")
        out_orig, out_cloned = synthesize_videos(
            video_path, no_vocals_path, tts_audio, ass_path, job_dir, video_duration_s, args
        )
        meter.phase_end()

        meter.phase_start("verify")
        update_status(args.status, "running", "verifying outputs", stage="verify")
        verification_path, verification = verify_outputs(
            video_path, out_cloned, subs_translated, tts_report, job_dir, args
        )
        meter.phase_end()

        cost_summary = meter.report()
        if args.cost_out:
            Path(args.cost_out).write_text(json.dumps(cost_summary, ensure_ascii=False, indent=2), encoding="utf-8")
        verification["cost"] = cost_summary
        verification_path_final = Path(verification_path)
        verification_path_final.write_text(json.dumps(verification, ensure_ascii=False, indent=2), encoding="utf-8")

        update_status(
            args.status,
            "completed",
            "Done",
            original_video=out_orig,
            cloned_video=out_cloned,
            job_config=job_config_path,
            verification_report=verification_path,
            verification=verification,
            cost=cost_summary,
        )
        log(f"Done\nOriginal: {out_orig}\nCloned: {out_cloned}\nVerification: {verification_path}", "DONE")
    except Exception as exc:
        log(str(exc), "ERROR")
        update_status(args.status, "failed", str(exc), stage="error")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
