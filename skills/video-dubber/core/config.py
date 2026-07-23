import os
from copy import deepcopy
from pathlib import Path

import yaml


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = SKILL_DIR / "profiles" / "default.yaml"


def _deep_merge(base, overlay):
    """递归合并两个字典，overlay 覆盖 base 的同级 key。"""
    merged = deepcopy(base)
    for key, value in overlay.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def load_defaults():
    """加载默认 YAML 配置。"""
    raw = yaml.safe_load(DEFAULT_PROFILE.read_text(encoding="utf-8")) or {}
    return raw


def load_overlay(path):
    """加载覆盖 YAML。"""
    p = Path(path).expanduser()
    if not p.exists():
        return {}
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return raw


def build_config(cli_args, overlay_path=None):
    """构造最终配置：默认值 → YAML 覆盖 → CLI 参数覆盖。

    返回扁平的 SimpleNamespace 风格对象。
    """
    cfg = load_defaults()
    if overlay_path:
        overlay = load_overlay(overlay_path)
        cfg = _deep_merge(cfg, overlay)

    config = _apply_cli_overrides(cfg, cli_args)
    return config


def _apply_cli_overrides(cfg, args):
    """将 CLI 参数值写入配置结构。args 为 argparse.Namespace。"""
    explicit_options = getattr(args, "_explicit_cli_options", None)

    def cli_value(name, *options):
        value = getattr(args, name, None)
        if explicit_options is not None and not any(option in explicit_options for option in options):
            return None
        return value

    overrides = {
        "general": {
            "target_language": getattr(args, "target_language", None),
            "source_lang": getattr(args, "source_lang", None),
            "subtitle_mode": getattr(args, "subtitle_mode", None),
            "min_free_gb": getattr(args, "min_free_gb", None),
            "font_file": getattr(args, "font_file", None),
        },
        "translation": {
            "model": getattr(args, "translation_model", None),
            "batch_size": getattr(args, "translation_batch_size", None),
            "workers": getattr(args, "translation_workers", None),
            "allow_source_fallback": getattr(args, "allow_source_fallback", None),
            "confirm": getattr(args, "confirm_translation", None),
            "env_file": getattr(args, "env_file", None),
            "model_config": getattr(args, "model_config", None),
            "terms_file": cli_value("terms_file", "--terms-file"),
            "context": cli_value("translation_context", "--translation-context"),
            "context_char_budget": cli_value("context_char_budget", "--context-char-budget"),
            "context_neighbor_lines": cli_value(
                "context_neighbor_lines", "--context-neighbor-lines"
            ),
            "timing_risk_estimator": cli_value(
                "timing_risk_estimator",
                "--timing-risk-estimator",
                "--no-timing-risk-estimator",
            ),
        },
        "download": {
            "format": getattr(args, "download_format", None),
            "sub_langs": getattr(args, "sub_langs", None),
            "ignore_yt_dlp_config": getattr(args, "ignore_yt_dlp_config", None),
            "allow_playlist": getattr(args, "allow_playlist", None),
            "playlist_items": getattr(args, "playlist_items", None),
            "proxy": getattr(args, "proxy", None),
            "concurrent_fragments": getattr(args, "concurrent_fragments", None),
            "external_downloader": getattr(args, "external_downloader", None),
            "cookies_from_browser": getattr(args, "cookies_from_browser", None),
        },
        "asr": {
            "engine": getattr(args, "asr_engine", None),
            "mlx_whisper_model": getattr(args, "mlx_whisper_model", None),
            "whisper_model": getattr(args, "whisper_model", None),
            "qwen3_asr_model": getattr(args, "qwen3_asr_model", None),
            "qwen3_asr_aligner": getattr(args, "qwen3_asr_aligner", None),
            "skip_separation": getattr(args, "skip_separation", None),
        },
        "tts": {
            "engine": getattr(args, "tts_engine", None),
            "qwen3_model": getattr(args, "qwen3_model", None),
            "max_atempo": getattr(args, "max_atempo", None),
            "max_clip_ms": getattr(args, "max_clip_ms", None),
            "max_overhang_ms": getattr(args, "max_overhang_ms", None),
            "hf_offline": getattr(args, "hf_offline", None),
            "no_segments": getattr(args, "no_segments", None),
        },
        "audio": {
            "preserve_gap_audio": getattr(args, "preserve_gap_audio", None),
            "gap_audio_gain_db": getattr(args, "gap_audio_gain_db", None),
            "gap_pad_ms": getattr(args, "gap_pad_ms", None),
        },
        "paths": {
            "tmp_dir": getattr(args, "tmp_dir", None),
            "status": getattr(args, "status", None),
            "log": getattr(args, "log", None),
        },
    }

    for section, keys in overrides.items():
        for key, value in keys.items():
            if value is not None:
                cfg.setdefault(section, {})[key] = value

    return cfg
