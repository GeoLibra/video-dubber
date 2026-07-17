import json
import os
import re
import shutil
from pathlib import Path

import pysubs2

from .media import FFMPEG, FFPROBE, run
from .subtitle import normalize_spaces, source_hash
from .lang import slug as lang_slug


def build_yt_dlp_cmd(url, out_path, args, list_formats=False, config=None):
    cfg = config or {}
    d = cfg.get("download", {})

    cmd = ["yt-dlp"]
    cookies = args.cookies_from_browser if args.cookies_from_browser else d.get("cookies_from_browser")
    proxy = args.proxy if args.proxy else d.get("proxy")
    cf = args.concurrent_fragments if args.concurrent_fragments else d.get("concurrent_fragments")
    ext = args.external_downloader if args.external_downloader else d.get("external_downloader")
    playlist_items = args.playlist_items if args.playlist_items else d.get("playlist_items")
    allow_playlist = args.allow_playlist if args.allow_playlist else d.get("allow_playlist", False)
    ignore_config = args.ignore_yt_dlp_config if hasattr(args, "ignore_yt_dlp_config") else d.get("ignore_yt_dlp_config", True)
    fmt = args.download_format if args.download_format else d.get("format", "bv*[height<=1080]+ba/best[height<=1080]/bv*+ba/best")
    sub_langs = args.sub_langs if args.sub_langs else d.get("sub_langs", "en.*,en")

    if ignore_config:
        cmd.append("--ignore-config")
    if cookies:
        cmd.extend(["--cookies-from-browser", cookies])
    if proxy:
        cmd.extend(["--proxy", proxy])
    if cf:
        cmd.extend(["--concurrent-fragments", str(cf)])
    if ext:
        cmd.extend(["--downloader", ext])
    if playlist_items:
        cmd.extend(["--playlist-items", playlist_items])
    elif not allow_playlist:
        cmd.append("--no-playlist")

    if list_formats:
        cmd.extend(["-F", url])
        return cmd

    cmd.extend([
        "--format", fmt,
        "--merge-output-format", "mp4",
        "--write-subs", "--write-auto-subs",
        "--sub-langs", sub_langs,
        "--convert-subs", "srt",
        "--output", str(out_path),
        url,
    ])
    return cmd


def download_video(url, out_dir, args, input_video=None, config=None):
    video_path = Path(out_dir) / "raw_video.mp4"
    audio_path = Path(out_dir) / "raw_audio.wav"

    if input_video:
        video_path = Path(input_video)
    elif not video_path.exists():
        run(build_yt_dlp_cmd(url, video_path, args, config=config), "DOWNLOAD")

    if not audio_path.exists():
        run([
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(video_path),
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            str(audio_path),
        ], "AUDIO")
    return str(video_path), str(audio_path)


def subtitle_quality_score(path):
    try:
        subs = pysubs2.load(str(path), encoding="utf-8")
    except Exception:
        return (10_000, 10_000, path.name)
    if not subs:
        return (9_000, 0, path.name)

    sample = subs[: min(len(subs), 300)]
    overlaps = 0
    duplicates = 0
    empties = 0
    prev_end = -1
    prev_text = ""
    for sub in sample:
        text = normalize_spaces(re.sub(r"\{[^}]*\}", "", sub.text.replace("\\N", " "))).lower()
        if not text:
            empties += 1
        if prev_end >= 0 and sub.start < prev_end - 50:
            overlaps += 1
        if text and text == prev_text:
            duplicates += 1
        prev_end = max(prev_end, sub.end)
        prev_text = text

    name = path.name.lower()
    language_penalty = 0 if any(token in name for token in (".en", "-en", "_en")) else 2
    return (overlaps * 20 + duplicates * 5 + empties * 3 + language_penalty, -len(subs), path.name)


def find_platform_subtitle(out_dir):
    out_dir = Path(out_dir)
    candidates = [
        path for path in sorted(out_dir.glob("*.srt"))
        if path.name != "raw_audio.srt" and not path.name.startswith("subtitles_")
    ]
    if not candidates:
        return None
    return min(candidates, key=subtitle_quality_score)


def separate_audio(audio_path, out_dir, skip=False):
    if skip or not shutil.which("audio-separator"):
        return audio_path, None

    import glob
    vocals = glob.glob(os.path.join(out_dir, "*Vocals*model_bs_roformer*.flac"))
    no_vocals = glob.glob(os.path.join(out_dir, "*Instrumental*model_bs_roformer*.flac"))
    if vocals and no_vocals:
        return vocals[0], no_vocals[0]

    run([
        "audio-separator", audio_path,
        "--model_filename", "model_bs_roformer_ep_317_sdr_12.9755.ckpt",
        "--output_dir", out_dir,
        "--output_format", "flac",
    ], "ROFORMER")

    vocals = glob.glob(os.path.join(out_dir, "*Vocals*model_bs_roformer*.flac"))
    no_vocals = glob.glob(os.path.join(out_dir, "*Instrumental*model_bs_roformer*.flac"))
    return vocals[0], no_vocals[0] if no_vocals else None


def write_job_config(job_dir, args, config=None):
    from .lang import slug as lang_slug
    payload = {
        "url": args.url,
        "input_video": args.input_video,
        "source_srt": args.source_srt,
        "source_lang": args.source_lang if args.source_lang else (config or {}).get("general", {}).get("source_lang", "en-US"),
        "target_language": args.target_language,
        "target_slug": lang_slug(args.target_language),
        "subtitle_mode": args.subtitle_mode,
        "translation_model": args.translation_model,
        "translation_workers": getattr(args, "translation_workers", 1),
        "allow_source_fallback": getattr(args, "allow_source_fallback", False),
        "model_config": args.model_config,
        "tts_engine": args.tts_engine,
        "qwen3_model": getattr(args, "qwen3_model", None),
        "ref_audio": args.ref_audio,
        "no_segments": args.no_segments,
        "max_atempo": args.max_atempo,
        "max_clip_ms": args.max_clip_ms,
        "max_overhang_ms": args.max_overhang_ms,
        "download_format": args.download_format,
        "cookies_from_browser": args.cookies_from_browser,
        "sub_langs": args.sub_langs,
        "ignore_yt_dlp_config": args.ignore_yt_dlp_config,
        "allow_playlist": args.allow_playlist,
        "playlist_items": args.playlist_items,
        "proxy": args.proxy,
        "concurrent_fragments": args.concurrent_fragments,
        "external_downloader": args.external_downloader,
        "list_formats": args.list_formats,
        "preserve_gap_audio": args.preserve_gap_audio,
        "gap_audio_gain_db": args.gap_audio_gain_db,
        "gap_pad_ms": args.gap_pad_ms,
    }
    path = Path(job_dir) / "job_config.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)
