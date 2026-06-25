#!/usr/bin/env python3
import argparse
import itertools
import json
import re
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_FONT_DIR = SKILL_DIR / "assets" / "fonts"
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"
SR = 24000


def run(cmd):
    subprocess.run([str(x) for x in cmd], check=True)


def probe_duration(path):
    return float(
        subprocess.check_output(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            text=True,
        ).strip()
    )


def ass_ts(ms):
    cs = int(round(ms / 10))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def display_width(ch):
    return 2 if ord(ch) > 127 else 1


def text_width(text):
    return sum(display_width(ch) for ch in text)


def auto_max_width(target_width, font_size, margin_l, max_lines, subtitle_mode):
    usable_width = max(320, target_width - margin_l * 2)
    width = int(usable_width / max(1, font_size * 0.8))
    if subtitle_mode == "bilingual":
        width = min(width, 44)
    else:
        width = min(width, 58)
    if max_lines <= 1:
        width = min(width, 64)
    return max(34, width)


def normalize_subtitle_text(text):
    return re.sub(r"\s+", " ", text or "").strip().replace("{", "（").replace("}", "）")


def semantic_units(text):
    text = normalize_subtitle_text(text)
    if not text:
        return []
    units = []
    current = ""
    for char in text:
        current += char
        if char in "，,、；;：:。！？!?":
            units.append(current.strip())
            current = ""
    if current.strip():
        units.append(current.strip())
    return units or [text]


def join_units(units):
    line = ""
    for unit in units:
        if not line:
            line = unit
        elif re.search(r"[A-Za-z0-9]$", line) and re.search(r"^[A-Za-z0-9]", unit):
            line += " " + unit
        else:
            line += unit
    return line


def balanced_semantic_wrap(text, max_width, max_lines):
    units = semantic_units(text)
    if len(units) <= 1 or max_lines < 2:
        return None
    total = join_units(units)
    best = None
    max_candidate_lines = min(max_lines, len(units))
    for line_count in range(2, max_candidate_lines + 1):
        for splits in itertools.combinations(range(1, len(units)), line_count - 1):
            bounds = (0, *splits, len(units))
            lines = [join_units(units[bounds[i] : bounds[i + 1]]) for i in range(line_count)]
            widths = [text_width(line) for line in lines]
            overflow = sum(max(0, width - max_width) for width in widths)
            balance = max(widths) - min(widths)
            score = (overflow, line_count, max(widths), balance)
            if best is None or score < best[0]:
                best = (score, lines)
    if best and best[0][0] == 0:
        return best[1]
    if best and text_width(total) > max_width:
        return best[1]
    return None


def token_wrap(text, max_width, max_lines):
    text = normalize_subtitle_text(text)
    raw_tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9+_.:/#-]*|[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]+|[^\s]", text)
    tokens = []
    for token in raw_tokens:
        if all(ord(char) > 127 for char in token):
            tokens.extend(token)
        else:
            tokens.append(token)
    lines, line, width = [], "", 0
    for token in tokens:
        token_width = sum(display_width(ch) for ch in token)
        glue = "" if (not line or re.match(r"^[，。！？、；：,.!?;:）)]$", token)) else " "
        if line and width + len(glue) + token_width > max_width and len(lines) < max_lines:
            lines.append(line.rstrip())
            line, width = token, token_width
        else:
            line += glue + token
            width += len(glue) + token_width
    if line:
        lines.append(line.rstrip())
    if len(lines) <= max_lines:
        return r"\N".join(lines)
    return r"\N".join([lines[0], "".join(lines[1:])])


def wrap_text(text, max_width, max_lines):
    text = normalize_subtitle_text(text)
    if text_width(text) <= max_width or max_lines <= 1:
        return text
    semantic = balanced_semantic_wrap(text, max_width, max_lines)
    if semantic:
        fixed = []
        for line in semantic[:max_lines]:
            if text_width(line) > int(max_width * 1.25):
                fixed.extend(token_wrap(line, max_width, max_lines).split(r"\N"))
            else:
                fixed.append(line)
        if len(fixed) <= max_lines:
            return r"\N".join(fixed)
    return token_wrap(text, max_width, max_lines)


def write_ass(groups, out_path, max_width, max_lines, font_name, font_size, margin_v, margin_l, video_width, video_height, subtitle_mode):
    if max_width <= 0:
        max_width = auto_max_width(video_width, font_size, margin_l, max_lines, subtitle_mode)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_width}
PlayResY: {video_height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,4,0,2,{margin_l},{margin_l},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    rows = []
    for item in groups:
        text = item.get("zh") or item.get("text") or item.get("display_text") or ""
        rows.append(
            f"Dialogue: 0,{ass_ts(int(item['start']))},{ass_ts(int(item['end']))},Default,,0,0,0,,{wrap_text(text, max_width, max_lines)}"
        )
    out_path.write_text(header + "\n".join(rows) + "\n", encoding="utf-8")
    return max_width


def read_wav(path):
    with wave.open(str(path), "rb") as wf:
        channels, rate, width = wf.getnchannels(), wf.getframerate(), wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if channels != 1 or rate != SR or width != 2:
        converted = path.with_suffix(".24k.s16.wav")
        run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", path, "-ar", str(SR), "-ac", "1", "-sample_fmt", "s16", converted])
        return read_wav(converted)
    return np.frombuffer(raw, dtype=np.int16).astype(np.int32)


def write_wav(path, samples):
    samples = np.clip(samples, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(samples.tobytes())


def add_original_audio_in_gaps(tts_path, original_audio_path, groups, out_path, gap_volume, pad_ms):
    tts = read_wav(tts_path)
    original = read_wav(original_audio_path)
    n = max(len(tts), len(original))
    if len(tts) < n:
        tts = np.pad(tts, (0, n - len(tts)))
    if len(original) < n:
        original = np.pad(original, (0, n - len(original)))
    speech_mask = np.zeros(n, dtype=bool)
    pad = int(pad_ms * SR / 1000)
    for item in groups:
        start = max(0, int(int(item["start"]) * SR / 1000) - pad)
        end = min(n, int(int(item["end"]) * SR / 1000) + pad)
        speech_mask[start:end] = True
    merged = tts.copy()
    gap = ~speech_mask
    merged[gap] = (merged[gap] + original[gap] * gap_volume).astype(np.int32)
    write_wav(out_path, merged)


def render_video(raw_video, ass_path, output, audio=None, font_dir=DEFAULT_FONT_DIR, target_width=1920, target_height=1080):
    vf = (
        f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
        f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2,"
        f"ass={ass_path}:fontsdir={font_dir}"
    )
    if audio:
        run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                raw_video,
                "-i",
                audio,
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-vf",
                vf,
                "-t",
                f"{probe_duration(raw_video):.3f}",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                output,
            ]
        )
    else:
        run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                raw_video,
                "-vf",
                vf,
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-c:a",
                "copy",
                "-movflags",
                "+faststart",
                output,
            ]
        )


def main():
    parser = argparse.ArgumentParser(description="Rebuild video-dubber ASS and output videos from grouped subtitles.")
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("--groups-json", default="source_groups_zh.json")
    parser.add_argument("--raw-video", default="raw_video.mp4")
    parser.add_argument("--raw-audio", default="raw_audio.wav")
    parser.add_argument("--tts-audio", default="merged_tts_zh_strict.wav")
    parser.add_argument("--ass-out", default="subtitles_zh_target.ass")
    parser.add_argument("--original-out", default="output_original_zh_target.mp4")
    parser.add_argument("--cloned-out", default="output_cloned_zh_target.mp4")
    parser.add_argument("--max-width", type=int, default=0, help="Display-width units. 0 means auto from resolution/font/margins.")
    parser.add_argument("--max-lines", type=int, default=3)
    parser.add_argument("--subtitle-mode", choices=["target", "bilingual", "source"], default="target")
    parser.add_argument("--target-width", type=int, default=1920)
    parser.add_argument("--target-height", type=int, default=1080)
    parser.add_argument("--font-name", default="Hiragino Sans GB")
    parser.add_argument("--font-size", type=int, default=40)
    parser.add_argument("--margin-v", type=int, default=74)
    parser.add_argument("--margin-l", type=int, default=120)
    parser.add_argument("--preserve-gap-audio", action="store_true")
    parser.add_argument("--gap-volume", type=float, default=0.75)
    parser.add_argument("--gap-pad-ms", type=int, default=60)
    args = parser.parse_args()

    job = Path(args.job_dir)
    groups = json.loads((job / args.groups_json).read_text(encoding="utf-8"))
    ass_path = job / args.ass_out
    effective_max_width = write_ass(
        groups,
        ass_path,
        args.max_width,
        args.max_lines,
        args.font_name,
        args.font_size,
        args.margin_v,
        args.margin_l,
        args.target_width,
        args.target_height,
        args.subtitle_mode,
    )

    raw_video = job / args.raw_video
    render_video(raw_video, ass_path, job / args.original_out, target_width=args.target_width, target_height=args.target_height)

    tts_audio = job / args.tts_audio
    cloned_audio = tts_audio
    if args.preserve_gap_audio and tts_audio.exists() and (job / args.raw_audio).exists():
        cloned_audio = job / f"{tts_audio.stem}.with_gap_original.wav"
        add_original_audio_in_gaps(tts_audio, job / args.raw_audio, groups, cloned_audio, args.gap_volume, args.gap_pad_ms)

    if tts_audio.exists():
        render_video(raw_video, ass_path, job / args.cloned_out, audio=cloned_audio, target_width=args.target_width, target_height=args.target_height)

    lines = [line for line in ass_path.read_text(encoding="utf-8").splitlines() if line.startswith("Dialogue:")]
    print(
        json.dumps(
            {
                "dialogues": len(lines),
                "effective_max_width": effective_max_width,
                "line_breaks": sum("\\N" in line for line in lines),
                "visible_backslash_N": sum("\\\\N" in line for line in lines),
                "ass": str(ass_path),
                "original": str(job / args.original_out),
                "cloned": str(job / args.cloned_out) if tts_audio.exists() else None,
                "gap_audio": str(cloned_audio) if cloned_audio != tts_audio else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
