#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pysubs2
from pydub import AudioSegment

from core.audio_builder import _fit_audio_to_duration, classify_speed_ratio


TERMINAL_RE = re.compile(r"[.!?。！？][\"'”’)]*$")


def build_groups(subs, *, assume_single_speaker, min_group_ms, max_group_ms, max_gap_ms):
    if not assume_single_speaker:
        return [[i] for i in range(len(subs))]
    groups, current = [], []
    for i, sub in enumerate(subs):
        if current:
            previous = subs[current[-1]]
            gap_ms = sub.start - previous.end
            proposed_ms = sub.end - subs[current[0]].start
            if gap_ms > max_gap_ms or (
                proposed_ms > max_group_ms
                and previous.end - subs[current[0]].start >= min_group_ms
            ):
                groups.append(current)
                current = []
        current.append(i)
        duration_ms = sub.end - subs[current[0]].start
        if duration_ms >= min_group_ms and TERMINAL_RE.search(sub.text.strip()):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def speed_summary(items):
    counts = {"natural": 0, "notice": 0, "obvious": 0, "extreme": 0}
    notices, abrupt = [], []
    previous = 1.0
    for item in items:
        ratio = float(item.get("atempo_ratio", 1.0) or 1.0)
        tier = item.get("speed_tier", classify_speed_ratio(ratio))
        counts[tier] += 1
        if tier != "natural":
            notices.append({
                "group_index": item["group_index"],
                "source_indexes": item["source_indexes"],
                "start_ms": item["start_ms"],
                "end_ms": item["end_ms"],
                "atempo_ratio": ratio,
                "speed_tier": tier,
            })
        if abs(ratio - previous) >= 0.25:
            abrupt.append({
                "group_index": item["group_index"],
                "start_ms": item["start_ms"],
                "previous_atempo_ratio": previous,
                "atempo_ratio": ratio,
            })
        previous = ratio
    return counts, notices, abrupt


def parse_args():
    parser = argparse.ArgumentParser(
        description="Realign existing raw TTS chunks without re-translating or cropping sentence endings."
    )
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("--source-srt", default="canonical_source.srt")
    parser.add_argument("--translations", required=True)
    parser.add_argument(
        "--translation-text-field", choices=["display_text", "tts_text"], default="display_text",
        help="Text represented by the existing chunks; must match the burned subtitles.",
    )
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--chunks-dir")
    source_group.add_argument(
        "--aligned-audio",
        help="Voice-only aligned track to approximately reconstruct chunks when raw chunks were removed.",
    )
    parser.add_argument(
        "--alignment-report",
        help="Required with --aligned-audio; used to reverse each historical atempo ratio.",
    )
    parser.add_argument("--base-video", required=True)
    parser.add_argument("--raw-audio", default="raw_audio.wav")
    parser.add_argument("--output", required=True)
    parser.add_argument("--merged-audio", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--sample-rate", type=int, default=24000)
    parser.add_argument("--preferred-max-atempo", type=float, default=1.6)
    parser.add_argument("--assume-single-speaker", action="store_true")
    parser.add_argument("--min-group-ms", type=int, default=3200)
    parser.add_argument("--max-group-ms", type=int, default=9000)
    parser.add_argument("--max-gap-ms", type=int, default=300)
    parser.add_argument("--inter-chunk-silence-ms", type=int, default=35)
    parser.add_argument("--gap-audio-gain-db", type=float, default=-6.0)
    parser.add_argument("--gap-pad-ms", type=int, default=60)
    return parser.parse_args()


def reverse_atempo(audio, ratio, tmp_dir):
    if ratio <= 1.01:
        return audio
    src = Path(tmp_dir) / f"reverse_src_{id(audio)}.wav"
    dst = Path(tmp_dir) / f"reverse_dst_{id(audio)}.wav"
    audio.export(src, format="wav")
    try:
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(src), "-filter:a", f"atempo={1.0 / ratio:.6f}", str(dst),
        ], check=True)
        return AudioSegment.from_file(dst)
    finally:
        src.unlink(missing_ok=True)
        dst.unlink(missing_ok=True)


def main():
    args = parse_args()
    job = Path(args.job_dir)
    subs = pysubs2.load(str(job / args.source_srt), encoding="utf-8")
    translations = json.loads((job / args.translations).read_text(encoding="utf-8"))
    if args.aligned_audio and not args.alignment_report:
        raise ValueError("--alignment-report is required with --aligned-audio")
    chunks = job / args.chunks_dir if args.chunks_dir else None
    aligned_track = None
    historical_items = None
    historical_cropped = []
    if args.aligned_audio:
        aligned_track = AudioSegment.from_file(job / args.aligned_audio)
        aligned_track = aligned_track.set_frame_rate(args.sample_rate).set_channels(1)
        historical_report = json.loads((job / args.alignment_report).read_text(encoding="utf-8"))
        historical_items = historical_report if isinstance(historical_report, list) else historical_report["items"]
        historical_cropped = [
            {"index": x.get("index"), "cropped_ms": x.get("cropped_ms", 0)}
            for x in historical_items if x.get("cropped_ms", 0) > 0
        ]
    base_video = job / args.base_video
    groups = build_groups(
        subs,
        assume_single_speaker=args.assume_single_speaker,
        min_group_ms=args.min_group_ms,
        max_group_ms=args.max_group_ms,
        max_gap_ms=args.max_gap_ms,
    )
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(base_video)],
        check=True, capture_output=True, text=True,
    )
    duration_ms = round(float(json.loads(probe.stdout)["format"]["duration"]) * 1000)
    voice = AudioSegment.silent(duration=duration_ms, frame_rate=args.sample_rate).set_channels(1)
    fit_args = SimpleNamespace(
        max_atempo=args.preferred_max_atempo,
        allow_atempo_overflow=True,
        max_clip_ms=0,
        max_overhang_ms=0,
    )
    items = []
    with tempfile.TemporaryDirectory(dir=job) as tmp_dir:
        for group_index, indexes in enumerate(groups):
            start_ms = subs[indexes[0]].start
            end_ms = subs[indexes[-1]].end
            target_ms = end_ms - start_ms
            combined = AudioSegment.silent(duration=0, frame_rate=args.sample_rate).set_channels(1)
            texts = []
            for offset, index in enumerate(indexes):
                text_entry = translations[str(index)]
                text = (
                    text_entry.get(args.translation_text_field)
                    or text_entry.get("display_text")
                    or text_entry.get("tts_text")
                    or ""
                ).strip()
                texts.append(text)
                if chunks:
                    chunk = AudioSegment.from_file(chunks / f"{index:04d}.wav")
                    chunk = chunk.set_frame_rate(args.sample_rate).set_channels(1)
                else:
                    chunk = aligned_track[subs[index].start:subs[index].end]
                    ratio = float(historical_items[index].get("atempo_ratio", 1.0) or 1.0)
                    chunk = reverse_atempo(chunk, ratio, tmp_dir)
                if offset:
                    combined += AudioSegment.silent(
                        duration=args.inter_chunk_silence_ms, frame_rate=args.sample_rate
                    )
                combined += chunk
            aligned, fit_stats = _fit_audio_to_duration(combined, target_ms, tmp_dir, fit_args)
            voice = voice.overlay(aligned, position=start_ms)
            item = {
                "group_index": group_index,
                "source_indexes": indexes,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "target_ms": target_ms,
                "text": " ".join(texts),
                **fit_stats,
            }
            item["speed_tier"] = classify_speed_ratio(item.get("atempo_ratio", 1.0))
            item["cropped_ms"] = 0
            items.append(item)

    raw = AudioSegment.from_file(job / args.raw_audio)
    raw = raw.set_frame_rate(args.sample_rate).set_channels(1)[:duration_ms]
    background = AudioSegment.silent(duration=duration_ms, frame_rate=args.sample_rate).set_channels(1)
    cursor = 0
    for group in groups:
        start_ms = subs[group[0]].start
        end_ms = subs[group[-1]].end
        gap_end = max(cursor, start_ms - args.gap_pad_ms)
        if gap_end > cursor:
            background = background.overlay(
                raw[cursor:gap_end] + args.gap_audio_gain_db, position=cursor
            )
        cursor = max(cursor, end_ms + args.gap_pad_ms)
    if cursor < duration_ms:
        background = background.overlay(
            raw[cursor:duration_ms] + args.gap_audio_gain_db, position=cursor
        )
    merged = voice.overlay(background)
    merged_path = job / args.merged_audio
    merged.export(merged_path, format="wav")

    counts, notices, abrupt = speed_summary(items)
    report = {
        "policy": "preserve_full_text_never_crop_sentence_end",
        "translation_text_field": args.translation_text_field,
        "chunk_source": "raw_chunks" if chunks else "reconstructed_from_aligned_track",
        "historical_crop_warning": bool(historical_cropped),
        "historical_cropped_segments": historical_cropped,
        "content_completeness": (
            "verified_from_raw_chunks" if chunks
            else "best_effort_historical_crop_cannot_be_recovered"
        ),
        "assume_single_speaker": args.assume_single_speaker,
        "source_segment_count": len(subs),
        "group_count": len(groups),
        "errors": 0,
        "cropped_count": 0,
        "max_atempo_ratio": max((x.get("atempo_ratio", 1.0) for x in items), default=1.0),
        "speed_tier_counts": counts,
        "speed_notice_count": len(notices),
        "speed_notices": notices,
        "abrupt_speed_change_count": len(abrupt),
        "abrupt_speed_changes": abrupt,
        "items": items,
    }
    report_path = job / args.report
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(base_video), "-i", str(merged_path),
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-shortest", str(job / args.output),
    ], check=True)
    print(json.dumps({
        "output": str(job / args.output),
        "report": str(report_path),
        "groups": len(groups),
        "max_atempo_ratio": report["max_atempo_ratio"],
        "speed_tier_counts": counts,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
