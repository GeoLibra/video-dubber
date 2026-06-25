import json
import os
import time
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv
from openai import OpenAI

from .lang import slug as lang_slug, is_compact
from .subtitle import (
    apply_translations_to_subs,
    apply_ass_style,
    get_sub_source_text,
    source_hash,
)


def _find_config_yaml():
    candidate = Path(__file__).resolve().parent.parent / "model-config.yaml"
    return candidate if candidate.exists() else None


def _resolve_env_value(value):
    if isinstance(value, str) and value.startswith("$"):
        return os.environ.get(value[1:])
    return value


def load_model_config(args):
    config_path = None
    if args.model_config:
        config_path = Path(args.model_config).expanduser()
    else:
        config_path = _find_config_yaml()

    if config_path and config_path.exists():
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        models = data.get("models") or []

        for item in models:
            if args.translation_model in {item.get("name"), item.get("model"), item.get("display_name")}:
                api_key = _resolve_env_value(item.get("api_key"))
                if not api_key:
                    raise RuntimeError(f"API key is missing for translation model {args.translation_model}")
                return {
                    "model": item.get("model") or args.translation_model,
                    "api_key": api_key,
                    "api_base": item.get("api_base") or "https://generativelanguage.googleapis.com/v1beta/openai/",
                }

        if args.translation_model == "gemini-3.5-flash":
            for item in models:
                api_key_ref = item.get("api_key")
                api_base = item.get("api_base") or ""
                if isinstance(api_key_ref, str) and api_key_ref.startswith("$"):
                    api_key = _resolve_env_value(api_key_ref)
                    if api_key:
                        return {"model": item.get("model") or args.translation_model, "api_key": api_key, "api_base": api_base}
                if "localhost" in api_base or "127.0.0.1" in api_base:
                    return {"model": item.get("model") or args.translation_model, "api_key": api_key_ref or "", "api_base": api_base}

    if args.translation_model == "gemini-3.5-flash":
        gemini_key = os.environ.get("GEMINI_API_KEY")
        if gemini_key:
            return {"model": args.translation_model, "api_key": gemini_key, "api_base": "https://generativelanguage.googleapis.com/v1beta/openai/"}

    raise RuntimeError(
        "No translation model configured. Set an API key in .env "
        "(e.g., GEMINI_API_KEY, OPENAI_API_KEY, DEEPSEEK_API_KEY, NVIDIA_API_KEY) "
        "or specify --model-config / --translation-model"
    )


def load_translation_cache(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        normalized = {}
        for key, value in data.items():
            try:
                normalized[int(key)] = value
            except (TypeError, ValueError):
                normalized[key] = value
        return normalized
    return data


def _load_prompt(language, compact_display):
    prompt_path = Path(__file__).resolve().parent.parent / "instructions" / "translate.yaml"
    if prompt_path.exists():
        import yaml as yl
        raw = yl.safe_load(prompt_path.read_text(encoding="utf-8")) or {}
        display_rule = raw.get("display_rule_compact") if compact_display else raw.get("display_rule")
        system = raw.get("system", "").format(language=language)
        user_template = raw.get("user_template", "")
        return system, display_rule, user_template

    display_rule = (
        "display_text: concise natural subtitle text; punctuation is allowed when it preserves sentence meaning; do not manually insert line breaks."
        if compact_display
        else "display_text: concise readable subtitle text; punctuation is allowed; do not manually insert line breaks."
    )
    return "", display_rule, ""


def translate_subtitles(subs, out_dir, args):
    slug = lang_slug(args.target_language)
    meta_path = Path(out_dir) / f"translations_{slug}.json"
    cache_meta_path = Path(out_dir) / f"translations_{slug}.meta.json"
    ass_path = Path(out_dir) / f"subtitles_{slug}_{args.subtitle_mode}.ass"
    sub_hash = source_hash(subs)
    expected_cache = {
        "source_hash": sub_hash,
        "target_language": args.target_language,
        "translation_model": args.translation_model,
    }

    if meta_path.exists() and cache_meta_path.exists():
        cache_meta = json.loads(cache_meta_path.read_text(encoding="utf-8"))
        if all(cache_meta.get(k) == v for k, v in expected_cache.items()):
            translations = load_translation_cache(meta_path)
            apply_translations_to_subs(subs, translations, args.subtitle_mode, args.target_language)
            apply_ass_style(subs)
            if not ass_path.exists():
                subs.save(str(ass_path))
            return str(ass_path), subs, translations

    if meta_path.exists() and not cache_meta_path.exists():
        legacy_meta = Path(out_dir) / "translations.json"
        if not legacy_meta.exists():
            pass

    if args.subtitle_mode == "source":
        translations = {
            idx: {"display_text": get_sub_source_text(sub), "tts_text": get_sub_source_text(sub)}
            for idx, sub in enumerate(subs)
        }
        apply_translations_to_subs(subs, translations, args.subtitle_mode, args.target_language)
        apply_ass_style(subs)
        subs.save(str(ass_path))
        meta_path.write_text(json.dumps(translations, ensure_ascii=False, indent=2), encoding="utf-8")
        cache_meta_path.write_text(json.dumps(expected_cache, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(ass_path), subs, translations

    try:
        model_config = load_model_config(args)
    except RuntimeError:
        slug = lang_slug(args.target_language)
        raw_srt_path = Path(out_dir) / "source_raw.srt"
        subs.save(str(raw_srt_path))
        print(f"[TRANSLATE] No API key configured. Saved raw SRT for agent translation: {raw_srt_path}", flush=True)
        return None, None, None

    client = OpenAI(
        api_key=model_config["api_key"],
        base_url=model_config["api_base"],
        http_client=httpx.Client(trust_env=True, http2=False, timeout=60.0),
    )

    translations = {}
    batch_size = args.translation_batch_size
    compact_display = is_compact(args.target_language)

    system_prompt, display_rule, user_template = _load_prompt(args.target_language, compact_display)

    if not display_rule:
        display_rule = (
            "display_text: concise natural subtitle text; punctuation is allowed when it preserves sentence meaning; do not manually insert line breaks."
            if compact_display
            else "display_text: concise readable subtitle text; punctuation is allowed; do not manually insert line breaks."
        )

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    for start in range(0, len(subs), batch_size):
        batch = list(enumerate(subs))[start : start + batch_size]
        lines = "\n".join(f"{idx}|{get_sub_source_text(sub)}" for idx, sub in batch)
        prompt = (
            user_template.format(
                target_language=args.target_language,
                display_rule=display_rule,
                lines=lines,
            )
            if user_template
            else f"""Translate these subtitle lines into natural spoken {args.target_language}.
Return strict JSON:
{{"translations":[{{"id":0,"display_text":"subtitle text","tts_text":"natural TTS text"}}]}}

Rules:
- Preserve every id exactly.
- {display_rule}
- tts_text: natural spoken language for voiceover; keep meaning but make it easy to say.
- Keep technical proper nouns in English when appropriate.

Input:
{lines}
"""
        )

        for attempt in range(3):
            try:
                response = client.chat.completions.create(
                    model=model_config["model"],
                    messages=messages + [{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                )
                data = json.loads(response.choices[0].message.content.strip())
                for item in data.get("translations", []):
                    translations[int(item["id"])] = {
                        "display_text": item.get("display_text", ""),
                        "tts_text": item.get("tts_text") or item.get("display_text", ""),
                    }
                break
            except Exception as exc:
                print(f"[TRANSLATE] Batch {start // batch_size + 1} attempt {attempt + 1} failed: {exc}", flush=True)
                time.sleep(2)

        meta_path.write_text(json.dumps(translations, ensure_ascii=False, indent=2), encoding="utf-8")

    client.close()

    for idx, sub in enumerate(subs):
        if idx not in translations:
            fallback = get_sub_source_text(sub)
            translations[idx] = {"display_text": fallback, "tts_text": fallback}

    apply_translations_to_subs(subs, translations, args.subtitle_mode, args.target_language)
    apply_ass_style(subs)
    subs.save(str(ass_path))
    meta_path.write_text(json.dumps(translations, ensure_ascii=False, indent=2), encoding="utf-8")
    cache_meta_path.write_text(json.dumps(expected_cache, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(ass_path), subs, translations
