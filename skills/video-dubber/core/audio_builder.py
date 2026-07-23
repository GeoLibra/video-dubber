import hashlib
import json
import os
import time
from pathlib import Path

from .media import FFMPEG, run
from .lang import slug as lang_slug
from .subtitle import normalize_spaces, is_non_speech
from .tts_register import get_engine


def extract_reference_audio_from_subs(subs, vocals_path, out_dir):
    from pydub import AudioSegment

    ref_audio_path = Path(out_dir) / "ref_audio.wav"
    ref_text_path = Path(out_dir) / "ref_text.txt"
    if ref_audio_path.exists() and ref_text_path.exists():
        return str(ref_audio_path), ref_text_path.read_text(encoding="utf-8").strip()

    candidates = []
    for sub in subs:
        duration = (sub.end - sub.start) / 1000.0
        text = _get_text(sub)
        word_count = len(text.split())
        if 3.0 <= duration <= 12.0 and word_count >= 5:
            candidates.append((abs(duration - 8.0), sub))
    if not candidates:
        raise ValueError("No usable subtitle segment for reference audio.")
    _score, ref_sub = sorted(candidates, key=lambda x: x[0])[0]

    start_sec = ref_sub.start / 1000.0
    duration_sec = min((ref_sub.end - ref_sub.start) / 1000.0, 12.0)
    ref_text = _get_text(ref_sub)
    run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-i", vocals_path,
        "-ss", f"{start_sec:.3f}",
        "-t", f"{duration_sec:.3f}",
        "-acodec", "pcm_s16le", "-ar", "24000", "-ac", "1",
        str(ref_audio_path),
    ], "REF_AUDIO")
    ref_text_path.write_text(ref_text, encoding="utf-8")
    return str(ref_audio_path), ref_text


def _get_text(sub):
    return normalize_spaces(getattr(sub, "tts_text", sub.text.split("\\N")[0]))


def _transcribe_reference(ref_audio, args):
    if args.ref_text:
        return args.ref_text.strip()
    if args.ref_text_file:
        return Path(args.ref_text_file).read_text(encoding="utf-8").strip()
    if not args.auto_transcribe_ref:
        raise ValueError("External --ref-audio requires --ref-text, --ref-text-file, or --auto-transcribe-ref.")
    try:
        from .speech import transcribe_audio_riva
        return transcribe_audio_riva(ref_audio, args.source_lang, config_type="text")
    except Exception as exc:
        raise RuntimeError("Reference auto-transcription failed; provide --ref-text.") from exc


def prepare_reference_audio(subs, vocals_path, out_dir, args):
    if args.ref_audio:
        ref_audio_path = Path(out_dir) / "ref_audio.wav"
        run([
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-i", args.ref_audio,
            "-acodec", "pcm_s16le", "-ar", "24000", "-ac", "1",
            str(ref_audio_path),
        ], "REF_AUDIO")
        ref_text = _transcribe_reference(str(ref_audio_path), args)
        (Path(out_dir) / "ref_text.txt").write_text(ref_text, encoding="utf-8")
        return str(ref_audio_path), ref_text
    return extract_reference_audio_from_subs(subs, vocals_path, out_dir)


def _trim_silence(audio):
    from pydub.silence import detect_leading_silence
    start = detect_leading_silence(audio)
    tail = audio[start:]
    end = detect_leading_silence(tail.reverse())
    return tail[:-end] if end else tail


def classify_speed_ratio(ratio):
    ratio = float(ratio or 1.0)
    if ratio <= 1.15:
        return "natural"
    if ratio <= 1.30:
        return "notice"
    if ratio <= 1.50:
        return "obvious"
    return "extreme"


def _fit_audio_to_duration(audio, target_ms, tmp_dir, args):
    from pydub import AudioSegment

    target_ms = max(0, int(target_ms))
    stats = {"fit_policy": "guarded", "target_ms": target_ms}
    if target_ms == 0:
        stats["skipped"] = True
        return AudioSegment.silent(duration=0, frame_rate=24000), stats

    original_ms = len(audio)
    audio = _trim_silence(audio).set_frame_rate(24000).set_channels(1)
    stats["raw_ms"] = original_ms
    stats["trimmed_ms"] = len(audio)

    if len(audio) > target_ms + 40:
        needed_ratio = len(audio) / target_ms
        allow_overflow = getattr(args, "allow_atempo_overflow", True)
        ratio = needed_ratio if allow_overflow else min(needed_ratio, args.max_atempo)
        stats["needed_atempo_ratio"] = round(needed_ratio, 4)
        stats["preferred_max_atempo"] = args.max_atempo
        stats["speed_tier"] = classify_speed_ratio(needed_ratio)
        if needed_ratio > args.max_atempo:
            stats["speed_notice"] = "preferred_atempo_threshold_exceeded"
        if 1.01 <= ratio:
            src = Path(tmp_dir) / f"fit_src_{time.time_ns()}.wav"
            dst = Path(tmp_dir) / f"fit_dst_{time.time_ns()}.wav"
            audio.export(src, format="wav")
            try:
                run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                     "-i", str(src),
                     "-filter:a", f"atempo={ratio:.6f}",
                     str(dst)])
                audio = AudioSegment.from_file(dst)
            finally:
                src.unlink(missing_ok=True)
                dst.unlink(missing_ok=True)
            stats["atempo_ratio"] = round(ratio, 4)
            stats["after_atempo_ms"] = len(audio)

        if len(audio) > target_ms:
            overhang_ms = len(audio) - target_ms
            stats["overhang_ms"] = overhang_ms
            stats["quality_warning"] = "tts_longer_than_window_kept_to_avoid_cutting_sentence"

    if len(audio) < target_ms:
        pad_ms = target_ms - len(audio)
        audio += AudioSegment.silent(duration=pad_ms, frame_rate=24000)
        stats["padded_ms"] = pad_ms

    fade_ms = min(8, max(0, len(audio) // 8))
    if fade_ms:
        audio = audio.fade_in(fade_ms).fade_out(fade_ms)
    stats["final_ms"] = len(audio)
    return audio, stats


def generate_and_merge(subs, out_dir, ref_audio_path, ref_text, video_duration_s, args):
    from pydub import AudioSegment

    slug = lang_slug(args.target_language)
    engine_slug = args.tts_engine.replace("-", "")
    resolved_model = None
    if args.tts_engine == "qwen3-tts":
        from .tts_qwen3_mlx import resolve_model_path
        resolved_model = resolve_model_path(
            getattr(args, "qwen3_model", None), hf_offline=args.hf_offline
        )
    merged_wav = Path(out_dir) / f"merged_tts_{slug}_{engine_slug}.wav"
    report_path = Path(out_dir) / f"tts_alignment_report_{slug}_{engine_slug}.json"
    report_meta_path = Path(out_dir) / f"tts_alignment_report_{slug}_{engine_slug}.meta.json"
    partial_report_path = Path(out_dir) / f"tts_alignment_partial_{slug}_{engine_slug}.json"

    if args.tts_engine == "none":
        report = []
        report_path.write_text(json.dumps(report), encoding="utf-8")
        report_meta_path.write_text(json.dumps({}), encoding="utf-8")
        timeline = AudioSegment.silent(duration=int(round(video_duration_s * 1000)), frame_rate=24000)
        timeline.export(merged_wav, format="wav")
        return str(merged_wav), report

    tts_hash = hashlib.sha256(
        "\n".join(normalize_spaces(getattr(sub, "tts_text", sub.text)) for sub in subs).encode("utf-8")
    ).hexdigest()
    expected_meta = {
        "tts_hash": tts_hash,
        "target_language": args.target_language,
        "tts_engine": args.tts_engine,
        "tts_model": resolved_model,
        "ref_text_hash": hashlib.sha256(ref_text.encode("utf-8")).hexdigest(),
        "ref_audio_hash": hashlib.sha256(Path(ref_audio_path).read_bytes()).hexdigest(),
        "video_duration_ms": int(round(video_duration_s * 1000)),
        "max_atempo": args.max_atempo,
        "allow_atempo_overflow": getattr(args, "allow_atempo_overflow", True),
        "max_clip_ms": args.max_clip_ms,
        "max_overhang_ms": args.max_overhang_ms,
    }
    if merged_wav.exists() and report_path.exists() and report_meta_path.exists():
        report_meta = json.loads(report_meta_path.read_text(encoding="utf-8"))
        if all(report_meta.get(k) == v for k, v in expected_meta.items()):
            return str(merged_wav), json.loads(report_path.read_text(encoding="utf-8"))

    tmp_dir = Path(os.environ.get("TMPDIR", out_dir))
    video_ms = int(round(video_duration_s * 1000))
    timeline = AudioSegment.silent(duration=video_ms, frame_rate=24000)
    report = []
    engine = get_engine(args.tts_engine)
    cache_key = hashlib.sha256(
        json.dumps(expected_meta, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]

    for idx, sub in enumerate(subs):
        tts_text = _get_text(sub)
        target_ms = max(0, min(sub.end, video_ms) - max(sub.start, 0))
        item = {
            "index": idx, "start_ms": sub.start, "end_ms": sub.end,
            "target_ms": target_ms, "text": tts_text,
        }
        if target_ms <= 0 or is_non_speech(tts_text):
            item["skipped"] = True
            report.append(item)
            continue

        chunk_wav = Path(out_dir) / f"chunk_{slug}_{engine_slug}_{cache_key}_{idx:04d}.wav"
        try:
            engine.synthesize(
                tts_text, ref_audio_path, ref_text, str(chunk_wav),
                hf_offline=args.hf_offline,
                target_language=args.target_language,
                model_path=resolved_model,
            )
            chunk_audio = AudioSegment.from_file(chunk_wav)
            item["raw_ms"] = len(chunk_audio)
            aligned, fit_stats = _fit_audio_to_duration(chunk_audio, target_ms, tmp_dir, args)
            item.update(fit_stats)
            timeline = timeline.overlay(aligned, position=sub.start)
            if not args.no_segments:
                aligned_path = Path(out_dir) / (
                    f"chunk_{slug}_{engine_slug}_{cache_key}_{idx:04d}_aligned.wav"
                )
                try:
                    aligned.export(aligned_path, format="wav")
                    item["segment_path"] = str(aligned_path)
                except Exception as exc:
                    item["segment_write_warning"] = repr(exc)
        except Exception as exc:
            item["error"] = repr(exc)
        report.append(item)
        partial_report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    timeline.export(merged_wav, format="wav")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report_meta_path.write_text(json.dumps(expected_meta, ensure_ascii=False, indent=2), encoding="utf-8")
    partial_report_path.unlink(missing_ok=True)
    if args.no_segments:
        for chunk_path in Path(out_dir).glob(f"chunk_{slug}_{engine_slug}_{cache_key}_*.wav"):
            chunk_path.unlink(missing_ok=True)
    return str(merged_wav), report


def add_gap_audio(tts_audio, raw_audio, subs, out_dir, video_duration_s, args):
    from pydub import AudioSegment

    engine_slug = args.tts_engine.replace("-", "")
    suffix = f"{lang_slug(args.target_language)}_{args.subtitle_mode}_{engine_slug}"
    out_path = Path(out_dir) / f"merged_tts_{suffix}.with_gap_original.wav"
    inputs = [Path(tts_audio), Path(raw_audio)]
    if out_path.exists() and out_path.stat().st_mtime >= max(p.stat().st_mtime for p in inputs):
        return str(out_path)

    total_ms = int(round(video_duration_s * 1000))
    tts = AudioSegment.from_file(tts_audio).set_frame_rate(24000).set_channels(1)
    original = AudioSegment.from_file(raw_audio).set_frame_rate(24000).set_channels(1)
    if len(tts) < total_ms:
        tts += AudioSegment.silent(duration=total_ms - len(tts), frame_rate=24000)
    if len(original) < total_ms:
        original += AudioSegment.silent(duration=total_ms - len(original), frame_rate=24000)

    gap_bed = AudioSegment.silent(duration=total_ms, frame_rate=24000)
    pad = args.gap_pad_ms
    intervals = []
    for sub in subs:
        display_text = normalize_spaces(getattr(sub, "display_text", sub.text))
        tts_txt = normalize_spaces(getattr(sub, "tts_text", display_text))
        if not display_text or is_non_speech(tts_txt):
            continue
        intervals.append((max(0, sub.start - pad), min(total_ms, sub.end + pad)))
    intervals.sort()
    cursor = 0
    for start, end in intervals:
        if start > cursor:
            gap_bed = gap_bed.overlay(original[cursor:start].apply_gain(args.gap_audio_gain_db), position=cursor)
        cursor = max(cursor, end)
    if cursor < total_ms:
        gap_bed = gap_bed.overlay(original[cursor:total_ms].apply_gain(args.gap_audio_gain_db), position=cursor)

    gap_bed.overlay(tts[:total_ms], position=0).export(out_path, format="wav")
    return str(out_path)
