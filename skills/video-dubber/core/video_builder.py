from pathlib import Path

from .lang import slug as lang_slug
import json

from .media import FFMPEG, FFPROBE, run, escape_filter_path
from .prereqs import resolve_font


def _is_current(output, inputs):
    output = Path(output)
    paths = [Path(p) for p in inputs if p]
    return output.exists() and paths and output.stat().st_mtime >= max(p.stat().st_mtime for p in paths)


def ass_filter(ass_path, args):
    font_dir = resolve_font(args.font_file).parent
    return f"ass='{escape_filter_path(ass_path)}':fontsdir='{escape_filter_path(font_dir)}'"


def _tmp_output(path):
    path = Path(path)
    # Keep the media suffix last so FFmpeg can infer the output muxer.
    # "output.mp4.tmp" is not recognized as MP4; "output.tmp.mp4" is.
    return path.with_name(f"{path.stem}.tmp{path.suffix}")


def _replace_tmp(tmp, final):
    tmp = Path(tmp)
    final = Path(final)
    if tmp.exists():
        tmp.replace(final)


def _has_embedded_cover(video_path):
    payload = json.loads(
        run(
            [
                FFPROBE, "-v", "error",
                "-show_entries", "stream=codec_type:stream_disposition=attached_pic",
                "-of", "json", str(video_path),
            ],
            capture=True,
        )
    )
    return any(
        stream.get("codec_type") == "video"
        and stream.get("disposition", {}).get("attached_pic") == 1
        for stream in payload.get("streams", [])
    )


def embed_cover(video_path, out_dir, args, *, name_suffix):
    """Create a reusable JPEG thumbnail and attach it to an MP4 without re-encoding."""
    video_path = Path(video_path)
    cover_path = Path(out_dir) / f"cover_{name_suffix}.jpg"
    source_cover = getattr(args, "cover_image", None)
    if source_cover:
        run(
            [
                FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source_cover), "-frames:v", "1", "-q:v", "2",
                str(_tmp_output(cover_path)),
            ],
            "COVER",
        )
        _replace_tmp(_tmp_output(cover_path), cover_path)
    elif not cover_path.exists():
        run(
            [
                FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{float(getattr(args, 'cover_time_sec', 2.0)):.3f}",
                "-i", str(video_path), "-frames:v", "1", "-q:v", "2",
                str(_tmp_output(cover_path)),
            ],
            "COVER",
        )
        _replace_tmp(_tmp_output(cover_path), cover_path)

    if not _has_embedded_cover(video_path):
        tmp_video = video_path.with_name(f"{video_path.stem}.cover.tmp{video_path.suffix}")
        run(
            [
                FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(video_path), "-i", str(cover_path),
                "-map", "0:v:0", "-map", "0:a?", "-map", "1:v:0",
                "-c", "copy", "-disposition:v:1", "attached_pic",
                "-movflags", "+faststart", str(tmp_video),
            ],
            "COVER",
        )
        _replace_tmp(tmp_video, video_path)
    return str(cover_path)


def synthesize_original_video(video_path, ass_path, out_dir, args):
    suffix = f"{lang_slug(args.target_language)}_{args.subtitle_mode}"
    out_orig = Path(out_dir) / f"output_original_{suffix}.mp4"
    vf = ass_filter(ass_path, args)
    if not _is_current(out_orig, [video_path, ass_path]):
        run([
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-i", video_path,
            "-vf", vf,
            "-c:v", "libx264",
            "-c:a", "aac", "-b:a", "192k",
            str(_tmp_output(out_orig)),
        ], "SYNTHESIS")
        _replace_tmp(_tmp_output(out_orig), out_orig)
    if getattr(args, "embed_cover", True):
        embed_cover(out_orig, out_dir, args, name_suffix=suffix)
    return str(out_orig)


def synthesize_videos(video_path, no_vocals_path, tts_audio, ass_path, out_dir, video_duration_s, args):
    suffix = f"{lang_slug(args.target_language)}_{args.subtitle_mode}"
    out_orig = Path(out_dir) / f"output_original_{suffix}.mp4"
    engine_slug = args.tts_engine.replace("-", "")
    if getattr(args, "multi_speaker", False):
        engine_slug += "_multispeaker"
    out_cloned = Path(out_dir) / f"output_cloned_{suffix}_{engine_slug}.mp4"
    vf = ass_filter(ass_path, args)

    synthesize_original_video(video_path, ass_path, out_dir, args)

    if args.tts_engine == "none":
        return str(out_orig), str(out_orig)

    if not _is_current(out_cloned, [video_path, ass_path, tts_audio, no_vocals_path]):
        if no_vocals_path:
            cmd = [
                FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-i", video_path,
                "-i", no_vocals_path,
                "-i", tts_audio,
                "-filter_complex",
                "[1:a]apad=pad_dur=10,volume=1.0[bgm];"
                "[2:a]apad=pad_dur=10,volume=1.15[tts];"
                "[bgm][tts]amix=inputs=2:duration=longest:dropout_transition=0[aout]",
                "-map", "0:v:0", "-map", "[aout]",
                "-vf", vf,
                "-t", f"{video_duration_s:.3f}",
                "-c:v", "libx264",
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                str(_tmp_output(out_cloned)),
            ]
        else:
            cmd = [
                FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-i", video_path,
                "-i", tts_audio,
                "-filter_complex", "[1:a]apad=pad_dur=10[aout]",
                "-map", "0:v:0", "-map", "[aout]",
                "-vf", vf,
                "-t", f"{video_duration_s:.3f}",
                "-c:v", "libx264",
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                str(_tmp_output(out_cloned)),
            ]
        run(cmd, "SYNTHESIS")
        _replace_tmp(_tmp_output(out_cloned), out_cloned)

    if getattr(args, "embed_cover", True):
        embed_cover(
            out_cloned,
            out_dir,
            args,
            name_suffix=f"{suffix}_{engine_slug}",
        )
    return str(out_orig), str(out_cloned)
