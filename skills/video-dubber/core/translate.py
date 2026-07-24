import json
import os
import shutil
import tempfile
import time
from hashlib import sha256
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
    is_non_speech,
    source_hash,
)
from .translation_context import (
    CONTEXT_SCHEMA_VERSION,
    PROMPT_POLICY_VERSION,
    context_hash,
    estimate_timing_risks,
    matching_terms,
    neighbor_context,
    prepare_translation_context,
)


DEFAULT_TIMING_RATES = {
    "cjk_chars_per_second": 4.0,
    "latin_words_per_second": 2.5,
    "punctuation_pause_seconds": 0.12,
    "warning_ratio": 1.25,
    "critical_ratio": 1.6,
}


TRANSLATION_STYLE_RULES = {
    "faithful": (
        "Translation style: faithful. Preserve all meaning, negation, conditions, numbers, named entities, "
        "technical terms, jokes, plot points, and causal relations. Do not omit content just to fit timing. "
        "If the subtitle window is short, keep the full meaning; downstream audio alignment will report or speed-fit timing pressure."
    ),
    "concise": (
        "Translation style: concise. You may make natural, equivalent compression for subtitle readability, "
        "but do not remove critical facts, negation, conditions, numbers, named entities, technical terms, or plot points."
    ),
    "summary": (
        "Translation style: summary. The user explicitly allows summarization. Preserve the main point and important facts, "
        "but shorter paraphrase is allowed."
    ),
}


def translation_style_rule(args):
    style = getattr(args, "translation_style", "faithful") or "faithful"
    return TRANSLATION_STYLE_RULES.get(style, TRANSLATION_STYLE_RULES["faithful"])


def _terms_file_hash(path):
    if not path:
        return sha256(b"").hexdigest()
    terms_path = Path(path)
    if not terms_path.exists():
        return sha256(b"").hexdigest()
    return sha256(terms_path.read_bytes()).hexdigest()


def _translation_cache_identity(subs, args, context):
    """Return all inputs that make a translation cache reusable."""
    provenance = (context.get("provenance") or {}) if isinstance(context, dict) else {}
    return {
        "context_schema_version": context.get("schema_version", CONTEXT_SCHEMA_VERSION),
        "translation_context_hash": context_hash(context),
        "terms_file_hash": _terms_file_hash(getattr(args, "terms_file", None)),
        "prompt_policy_version": provenance.get(
            "prompt_policy_version", PROMPT_POLICY_VERSION
        ),
        "source_hash": source_hash(subs),
        "target_language": args.target_language,
        "translation_style": getattr(args, "translation_style", "faithful"),
        "translation_model": resolve_model_identity(args),
        "content_kind": (
            "source" if getattr(args, "subtitle_mode", "target") == "source" else "translated"
        ),
    }


def _find_config_yaml():
    candidate = Path(__file__).resolve().parent.parent / "model-config.yaml"
    return candidate if candidate.exists() else None


def _resolve_env_value(value):
    if isinstance(value, str) and value.startswith("$"):
        return os.environ.get(value[1:])
    return value


def _model_config_path(args):
    if getattr(args, "model_config", None):
        return Path(args.model_config).expanduser()
    return _find_config_yaml()


def _configured_models(args):
    config_path = _model_config_path(args)
    if not config_path or not config_path.exists():
        return []
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    models = data.get("models") or []
    return models if isinstance(models, list) else []


def resolve_model_identity(args):
    """Resolve the configured model name without requiring request credentials."""
    requested = args.translation_model
    for item in _configured_models(args):
        if not isinstance(item, dict):
            continue
        if requested in {item.get("name"), item.get("model"), item.get("display_name")}:
            return item.get("model") or requested
    return requested


def load_model_config(args):
    models = _configured_models(args)

    if models:
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


def _normalize_cached_strict_sync(translations) -> bool:
    """Force loaded translation cache records onto the strict-sync contract."""
    changed = False
    for item in translations.values():
        if not isinstance(item, dict):
            continue
        display = item.get("display_text", "")
        if item.get("tts_text") != display:
            item["tts_text"] = display
            changed = True
    return changed


def _valid_display_record(item) -> bool:
    return (
        isinstance(item, dict)
        and isinstance(item.get("display_text"), str)
        and bool(item["display_text"].strip())
    )


def _drop_invalid_speech_translations(translations, subs) -> list[int]:
    """Remove invalid speech records so they remain missing and retryable."""
    invalid = []
    for index, sub in enumerate(subs):
        if is_non_speech(get_sub_source_text(sub)):
            continue
        if not _valid_display_record(translations.get(index)):
            translations.pop(index, None)
            invalid.append(index)
    return invalid


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(data, ensure_ascii=False, indent=2)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(serialized)
            temporary = Path(handle.name)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _backup_cache(path):
    path = Path(path)
    if not path.exists():
        return
    backup = path.with_name(path.name + f".{int(time.time())}.bak")
    shutil.copy2(path, backup)


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _update_translation_status(
    args,
    translated,
    total,
    batch_done,
    batch_total,
    model_name,
    timing_summary=None,
):
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
    if timing_summary:
        payload.update(
            {
                "timing_counts": timing_summary["timing_counts"],
                "max_required_speed_ratio": timing_summary["max_required_speed_ratio"],
                "timing_risks_path": timing_summary["timing_risks_path"],
            }
        )
    _write_json(path, payload)


def _timing_rates(args):
    rates = dict(DEFAULT_TIMING_RATES)
    configured = getattr(args, "translation_timing", None)
    if isinstance(configured, dict):
        rates.update(configured)
    return rates


def _write_timing_report(subs, translations, out_dir, args, context_path, warnings):
    if not getattr(args, "timing_risk_estimator", True):
        return {
            "translation_context_path": str(context_path) if context_path else None,
            "timing_risks_path": None,
            "warnings": list(warnings),
            "timing_counts": {"normal": 0, "warning": 0, "critical": 0},
            "max_required_speed_ratio": 0.0,
        }
    risks = estimate_timing_risks(
        subs,
        translations,
        args.target_language,
        _timing_rates(args),
    )
    timing_path = Path(out_dir) / f"translation_timing_risks_{lang_slug(args.target_language)}.json"
    _write_json(timing_path, risks)
    counts = {"normal": 0, "warning": 0, "critical": 0}
    for item in risks:
        counts[item["risk"]] += 1
    return {
        "translation_context_path": str(context_path) if context_path else None,
        "timing_risks_path": str(timing_path),
        "warnings": list(warnings),
        "timing_counts": counts,
        "max_required_speed_ratio": max(
            (item["required_speed_ratio"] for item in risks),
            default=0.0,
        ),
    }


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


def _build_prompt(args, batch, display_rule, user_template, context, subs):
    lines = "\n".join(f"{idx}|{get_sub_source_text(sub)}" for idx, sub in batch)
    active_ids = ", ".join(str(idx) for idx, _sub in batch)
    active_texts = [get_sub_source_text(sub) for _idx, sub in batch]
    terms = matching_terms(context.get("terms", []), active_texts)
    terminology = "\n".join(
        f"{term['source']} => {term['target']}"
        + (f" ({term['note']})" if term.get("note") else "")
        for term in terms
    ) or "(none)"
    first_id = min(idx for idx, _sub in batch)
    last_id = max(idx for idx, _sub in batch)
    neighbors = neighbor_context(
        subs,
        first_id,
        last_id,
        getattr(args, "context_neighbor_lines", 2),
    )
    reference_lines = "\n".join(
        f"{item['id']}|{item['text']}"
        for item in neighbors["before"] + neighbors["after"]
    ) or "(none)"
    timing_budgets = "\n".join(
        f"{idx}|{max(0, int(sub.end) - int(sub.start))}ms" for idx, sub in batch
    )
    format_values = {
        "target_language": args.target_language,
        "display_rule": display_rule,
        "translation_style_rule": translation_style_rule(args),
        "video_context": context.get("summary") or "(none)",
        "terminology": terminology,
        "reference_lines": reference_lines,
        "timing_budgets": timing_budgets,
        "active_ids": active_ids,
        "lines": lines,
    }
    if user_template:
        return user_template.format(**format_values)
    return f"""Translate these subtitle lines into natural spoken {args.target_language}.
Return strict JSON:
{{"translations":[{{"id":0,"display_text":"subtitle text"}}]}}

Rules:
- Preserve every id exactly.
- {display_rule}
- {format_values['translation_style_rule']}
- Keep technical proper nouns in English when appropriate.

=== VIDEO CONTEXT ===
{format_values['video_context']}

=== TERMINOLOGY ===
{terminology}

=== REFERENCE ONLY ===
Do not translate or return reference ids.
{reference_lines}

=== TIMING BUDGET ===
Each translation must fit its existing subtitle window.
{timing_budgets}

=== TRANSLATE THESE IDS ===
Return translations only for ids: {active_ids}.
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
                "tts_text": display,
            }
        return translations
    finally:
        client.close()


def _translate_one_batch(model_config, messages, prompt, batch, batch_number):
    expected = {idx for idx, _sub in batch}
    speech_ids = {
        idx for idx, sub in batch if not is_non_speech(get_sub_source_text(sub))
    }
    last_error = None
    for attempt in range(3):
        try:
            result = _request_batch(model_config, messages, prompt)
            invalid = {
                idx
                for idx in speech_ids & set(result)
                if not _valid_display_record(result[idx])
            }
            missing = sorted((expected - set(result)) | invalid)
            if missing:
                raise RuntimeError(
                    f"model omitted ids or returned invalid display_text {missing[:10]}"
                )
            unexpected = sorted(set(result) - expected)
            if unexpected:
                raise RuntimeError(f"model returned unexpected ids {unexpected[:10]}")
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
    resolved_model = resolve_model_identity(args)
    model_config = None
    model_config_error = None
    model_config_loaded = args.subtitle_mode == "source"

    def get_model_config():
        nonlocal model_config, model_config_error, model_config_loaded
        if not model_config_loaded:
            model_config_loaded = True
            try:
                model_config = load_model_config(args)
            except RuntimeError as exc:
                model_config_error = exc
        return model_config

    context, context_path, context_warnings = prepare_translation_context(
        subs,
        Path(out_dir),
        args,
        get_model_config if args.subtitle_mode != "source" else None,
        resolved_model if args.subtitle_mode != "source" else None,
    )
    expected_cache = _translation_cache_identity(subs, args, context)

    translations = {}
    cache_matches = False
    cache_pair_was_orphaned = meta_path.exists() != cache_meta_path.exists()
    if cache_pair_was_orphaned:
        _backup_cache(meta_path)
        _backup_cache(cache_meta_path)
        _write_json(meta_path, {})
        _write_json(cache_meta_path, {})

    if not cache_pair_was_orphaned and meta_path.exists() and cache_meta_path.exists():
        cache_meta = json.loads(cache_meta_path.read_text(encoding="utf-8"))
        cache_matches = all(cache_meta.get(k) == v for k, v in expected_cache.items())
        if cache_matches:
            translations = load_translation_cache(meta_path)
            cache_changed = bool(
                args.subtitle_mode != "source"
                and _drop_invalid_speech_translations(translations, subs)
            )
            if args.subtitle_mode != "source" and _normalize_cached_strict_sync(translations):
                cache_changed = True
            if cache_changed:
                _write_json(meta_path, {str(k): translations[k] for k in sorted(translations)})
            if len(translations) == len(subs) and all(idx in translations for idx in range(len(subs))):
                apply_translations_to_subs(subs, translations, args.subtitle_mode, args.target_language)
                apply_ass_style(subs)
                if not ass_path.exists():
                    subs.save(str(ass_path))
                context_info = _write_timing_report(
                    subs,
                    translations,
                    out_dir,
                    args,
                    context_path,
                    context_warnings,
                )
                _update_translation_status(
                    args,
                    len(translations),
                    len(subs),
                    0,
                    0,
                    model_config["model"] if model_config else resolved_model,
                    context_info,
                )
                return str(ass_path), subs, translations, context_info
        else:
            _backup_cache(meta_path)
            _backup_cache(cache_meta_path)
            _write_json(meta_path, {})

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
        context_info = _write_timing_report(
            subs,
            translations,
            out_dir,
            args,
            context_path,
            context_warnings,
        )
        _update_translation_status(
            args,
            len(translations),
            len(subs),
            0,
            0,
            resolved_model,
            context_info,
        )
        return str(ass_path), subs, translations, context_info

    if cache_matches and translations:
        print(f"[TRANSLATE] Resuming cache: {len(translations)}/{len(subs)}", flush=True)
    else:
        translations = {}

    if model_config is None:
        get_model_config()

    if model_config_error is not None:
        raw_srt_path = Path(out_dir) / "source_raw.srt"
        subs.save(str(raw_srt_path))
        _write_json(cache_meta_path, expected_cache)
        print(f"[TRANSLATE] No API key configured. Saved raw SRT for agent translation: {raw_srt_path}", flush=True)
        return None, None, None, {
            "translation_context_path": str(context_path),
            "timing_risks_path": None,
            "warnings": context_warnings,
            "timing_counts": {"normal": 0, "warning": 0, "critical": 0},
            "max_required_speed_ratio": 0.0,
        }

    for warning in context_warnings:
        print(f"[TRANSLATE] WARNING: {warning}", flush=True)
    print(f"[TRANSLATE] Context artifact: {context_path}", flush=True)

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
    messages.append({"role": "system", "content": translation_style_rule(args)})
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
            prompt = _build_prompt(args, batch, display_rule, user_template, context, subs)
            result = _translate_one_batch(model_config, messages, prompt, batch, done)
            translations.update(result)
            save_progress(done)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {}
            for batch_number, batch in enumerate(batches, start=1):
                prompt = _build_prompt(args, batch, display_rule, user_template, context, subs)
                future = pool.submit(_translate_one_batch, model_config, messages, prompt, batch, batch_number)
                future_map[future] = batch_number
            done = 0
            for future in as_completed(future_map):
                result = future.result()
                translations.update(result)
                done += 1
                save_progress(done)

    _drop_invalid_speech_translations(translations, subs)
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
    context_info = _write_timing_report(
        subs,
        translations,
        out_dir,
        args,
        context_path,
        context_warnings,
    )
    _update_translation_status(
        args,
        len(translations),
        len(subs),
        total_batches,
        total_batches,
        model_config["model"],
        context_info,
    )
    return str(ass_path), subs, translations, context_info
