from pathlib import Path

from .lang import slug as lang_slug
from .media import FFMPEG, run, escape_filter_path
from .prereqs import resolve_font


def ass_filter(ass_path, args):
    font_dir = resolve_font(args.font_file).parent
    return f"ass='{escape_filter_path(ass_path)}':fontsdir='{escape_filter_path(font_dir)}'"


def synthesize_videos(video_path, no_vocals_path, tts_audio, ass_path, out_dir, video_duration_s, args):
    suffix = f"{lang_slug(args.target_language)}_{args.subtitle_mode}"
    out_orig = Path(out_dir) / f"output_original_{suffix}.mp4"
    out_cloned = Path(out_dir) / f"output_cloned_{suffix}.mp4"
    vf = ass_filter(ass_path, args)

    if not out_orig.exists():
        run([
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-i", video_path,
            "-vf", vf,
            "-c:v", "libx264",
            "-c:a", "aac", "-b:a", "192k",
            str(out_orig),
        ], "SYNTHESIS")

    if args.tts_engine == "none":
        return str(out_orig), str(out_orig)

    if not out_cloned.exists():
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
                str(out_cloned),
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
                str(out_cloned),
            ]
        run(cmd, "SYNTHESIS")

    return str(out_orig), str(out_cloned)
