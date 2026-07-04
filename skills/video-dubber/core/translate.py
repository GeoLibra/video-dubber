import json
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
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


def _write_json(path, data):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _backup_cache(path):
    path = Path(path)
    if not path.exists():
        return
    backup = path.with_name(path.name + f".{int(time.time())}.bak")
    shutil.copy2(path, backup)


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _update_translation_status(args, translated, total, batch_done, batch_total, model_name):
    status_path = getattr(args, "status", None)
    if not status_path:
        return
    path = Path(status_path)
    payload = {}
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
    payload.update({
        "status": "running",
        "message": "translating",
        "stage": "translation",
        "last_seen": _now(),
        "stage_timeout_min": 15,
        "translation_model": getattr(args, "translation_model", None),
        "resolved_translation_model": model_name,
        "translated": translated,
        "total": total,
        "batch": batch_done,
        "batches": batch_total,
    })
    _write_json(path, payload)


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


def _build_prompt(args, batch, display_rule, user_template):
    lines = "\n".join(f"{idx}|{get_sub_source_text(sub)}" for idx, sub in batch)
    if user_template:
        return user_template.format(
            target_language=args.target_language,
            display_rule=display_rule,
            lines=lines,
        )
    return f"""Translate these subtitle lines into natural spoken {args.target_language}.
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


def _request_batch(model_config, model_messages, prompt):
    client = OpenAI(
        api_key=model_config["api_key"],
        base_url=model_config["api_base"],
        http_client=httpx.Client(trust_env=True, http2=False, timeout=90.0),
    )
    try:
        response = client.chat.completions.create(
            model=model_config["model"],
            messages=model_messages + [{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        data = json.loads(response.choices[0].message.content.strip())
        translations = {}
        for item in data.get("translations", []):
            display = item.get("display_text", "")
            translations[int(item["id"])] = {
                "display_text": display,
                "tts_text": item.get("tts_text") or display,
            }
        return translations
    finally:
        client.close()


def _translate_one_batch(model_config, messages, prompt, batch, batch_number):
    expected = {idx for idx, _sub in batch}
    last_error = None
    for attempt in range(3):
        try:
            result = _request_batch(model_config, messages, prompt)
            missing = sorted(expected - set(result))
            if missing:
                raise RuntimeError(f"model omitted ids {missing[:10]}")
            return result
        except Exception as exc:
            last_error = exc
            print(f"[TRANSLATE] Batch {batch_number} attempt {attempt + 1} failed: {exc}", flush=True)
            time.sleep(2 + attempt * 2)
    raise RuntimeError(f"Batch {batch_number} failed after retries: {last_error}")


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

    translations = {}
    cache_matches = False
    if meta_path.exists() and cache_meta_path.exists():
        cache_meta = json.loads(cache_meta_path.read_text(encoding="utf-8"))
        cache_matches = all(cache_meta.get(k) == v for k, v in expected_cache.items())
        if cache_matches:
            translations = load_translation_cache(meta_path)
            if len(translations) == len(subs) and all(idx in translations for idx in range(len(subs))):
                apply_translations_to_subs(subs, translations, args.subtitle_mode, args.target_language)
                apply_ass_style(subs)
                if not ass_path.exists():
                    subs.save(str(ass_path))
                return str(ass_path), subs, translations
        else:
            _backup_cache(meta_path)
            _backup_cache(cache_meta_path)

    if args.subtitle_mode == "source":
        translations = {
            idx: {"display_text": get_sub_source_text(sub), "tts_text": get_sub_source_text(sub)}
            for idx, sub in enumerate(subs)
        }
        apply_translations_to_subs(subs, translations, args.subtitle_mode, args.target_language)
        apply_ass_style(subs)
        subs.save(str(ass_path))
        _write_json(meta_path, translations)
        _write_json(cache_meta_path, expected_cache)
        return str(ass_path), subs, translations

    if cache_matches and translations:
        print(f"[TRANSLATE] Resuming cache: {len(translations)}/{len(subs)}", flush=True)
    else:
        translations = {}

    try:
        model_config = load_model_config(args)
    except RuntimeError:
        raw_srt_path = Path(out_dir) / "source_raw.srt"
        subs.save(str(raw_srt_path))
        print(f"[TRANSLATE] No API key configured. Saved raw SRT for agent translation: {raw_srt_path}", flush=True)
        return None, None, None

    batch_size = args.translation_batch_size
    workers = max(1, int(getattr(args, "translation_workers", 1) or 1))
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

    missing_items = [(idx, sub) for idx, sub in enumerate(subs) if idx not in translations]
    batches = [missing_items[i : i + batch_size] for i in range(0, len(missing_items), batch_size)]
    total_batches = len(batches)
    _update_translation_status(args, len(translations), len(subs), 0, total_batches, model_config["model"])

    def save_progress(done):
        _write_json(meta_path, {str(k): translations[k] for k in sorted(translations)})
        _write_json(cache_meta_path, expected_cache)
        print(f"[TRANSLATE] batch {done}/{total_batches} saved; total={len(translations)}/{len(subs)}", flush=True)
        _update_translation_status(args, len(translations), len(subs), done, total_batches, model_config["model"])

    if workers == 1:
        for done, batch in enumerate(batches, start=1):
            prompt = _build_prompt(args, batch, display_rule, user_template)
            result = _translate_one_batch(model_config, messages, prompt, batch, done)
            translations.update(result)
            save_progress(done)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {}
            for batch_number, batch in enumerate(batches, start=1):
                prompt = _build_prompt(args, batch, display_rule, user_template)
                future = pool.submit(_translate_one_batch, model_config, messages, prompt, batch, batch_number)
                future_map[future] = batch_number
            done = 0
            for future in as_completed(future_map):
                result = future.result()
                translations.update(result)
                done += 1
                save_progress(done)

    missing = [idx for idx in range(len(subs)) if idx not in translations]
    if missing:
        if not getattr(args, "allow_source_fallback", False):
            raise RuntimeError(f"Missing translations for {len(missing)} subtitles; first ids: {missing[:20]}")
        for idx in missing:
            fallback = get_sub_source_text(subs[idx])
            translations[idx] = {"display_text": fallback, "tts_text": fallback}

    apply_translations_to_subs(subs, translations, args.subtitle_mode, args.target_language)
    apply_ass_style(subs)
    subs.save(str(ass_path))
    _write_json(meta_path, {str(k): translations[k] for k in sorted(translations)})
    _write_json(cache_meta_path, expected_cache)
    return str(ass_path), subs, translations
