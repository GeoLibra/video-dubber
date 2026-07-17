import json
from pathlib import Path

from .lang import slug as lang_slug
from .media import probe_streams


def verify_outputs(video_path, cloned_path, subs, tts_report, out_dir, args):
    is_subtitle_only = args.tts_engine == "none"
    atempo_ratios = [x.get("atempo_ratio", 1.0) for x in tts_report if "atempo_ratio" in x]
    clipped = [x for x in tts_report if x.get("cropped_ms", 0) > 0]
    warned = [x for x in tts_report if x.get("quality_warning")]

    report = {
        "source_video": probe_streams(video_path),
        "cloned_video": probe_streams(cloned_path),
        "target_language": args.target_language,
        "subtitle_mode": args.subtitle_mode,
        "subtitle_count": len(subs),
        "clone_voice": not is_subtitle_only,
        "tts_engine": args.tts_engine,
    }
    if not is_subtitle_only:
        report.update({
            "tts_total": len(tts_report),
            "tts_generated": sum(1 for x in tts_report if "raw_ms" in x),
            "tts_skipped": sum(1 for x in tts_report if x.get("skipped")),
            "tts_errors": sum(1 for x in tts_report if "error" in x),
            "tts_max_atempo_ratio": max(atempo_ratios) if atempo_ratios else 1.0,
            "tts_clipped_count": len(clipped),
            "tts_quality_warning_count": len(warned),
            "tts_quality_warning_indexes": [x["index"] for x in warned[:20]],
        })

    engine_suffix = "" if is_subtitle_only else f"_{args.tts_engine.replace('-', '')}"
    report_path = Path(out_dir) / (
        f"verification_report_{lang_slug(args.target_language)}_{args.subtitle_mode}{engine_suffix}.json"
    )
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not is_subtitle_only and report.get("tts_errors"):
        raise RuntimeError(f"TTS errors found: {report['tts_errors']}. See {report_path}")
    return str(report_path), report
