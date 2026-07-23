import json
from pathlib import Path

from .lang import slug as lang_slug
from .media import probe_streams


def verify_outputs(
    video_path,
    cloned_path,
    subs,
    tts_report,
    out_dir,
    args,
    translation_context_info=None,
):
    is_subtitle_only = args.tts_engine == "none"
    atempo_ratios = [x.get("atempo_ratio", 1.0) for x in tts_report if "atempo_ratio" in x]
    clipped = [x for x in tts_report if x.get("cropped_ms", 0) > 0]
    warned = [x for x in tts_report if x.get("quality_warning")]
    speed_tiers = {"natural": 0, "notice": 0, "obvious": 0, "extreme": 0}
    speed_notices = []
    abrupt_speed_changes = []
    previous_ratio = 1.0
    for item in tts_report:
        ratio = float(item.get("atempo_ratio", 1.0) or 1.0)
        tier = item.get("speed_tier", "natural")
        speed_tiers[tier] = speed_tiers.get(tier, 0) + 1
        if tier != "natural":
            speed_notices.append({
                "index": item.get("index"),
                "start_ms": item.get("start_ms"),
                "end_ms": item.get("end_ms"),
                "atempo_ratio": ratio,
                "speed_tier": tier,
            })
        if abs(ratio - previous_ratio) >= 0.25:
            abrupt_speed_changes.append({
                "index": item.get("index"),
                "start_ms": item.get("start_ms"),
                "previous_atempo_ratio": previous_ratio,
                "atempo_ratio": ratio,
            })
        previous_ratio = ratio

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
            "tts_speed_tier_counts": speed_tiers,
            "tts_speed_notice_count": len(speed_notices),
            "tts_speed_notices": speed_notices,
            "tts_abrupt_speed_change_count": len(abrupt_speed_changes),
            "tts_abrupt_speed_changes": abrupt_speed_changes,
            "tts_content_policy": "preserve_full_text_never_crop_sentence_end",
        })

    if translation_context_info:
        report.update(
            {
                "translation_context_path": translation_context_info.get(
                    "translation_context_path"
                ),
                "timing_risks_path": translation_context_info.get("timing_risks_path"),
                "warnings": list(translation_context_info.get("warnings", [])),
                "timing_counts": dict(
                    translation_context_info.get(
                        "timing_counts",
                        {"normal": 0, "warning": 0, "critical": 0},
                    )
                ),
                "max_required_speed_ratio": translation_context_info.get(
                    "max_required_speed_ratio", 0.0
                ),
            }
        )

    engine_suffix = "" if is_subtitle_only else f"_{args.tts_engine.replace('-', '')}"
    report_path = Path(out_dir) / (
        f"verification_report_{lang_slug(args.target_language)}_{args.subtitle_mode}{engine_suffix}.json"
    )
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not is_subtitle_only and report.get("tts_errors"):
        raise RuntimeError(f"TTS errors found: {report['tts_errors']}. See {report_path}")
    return str(report_path), report
