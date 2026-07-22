"""Reusable context helpers for subtitle translation."""

from __future__ import annotations

import json
import heapq
import re
import shutil
import tempfile
import time
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import httpx
import yaml
from openai import OpenAI

from .lang import slug as lang_slug
from .subtitle import get_sub_source_text
from .subtitle import is_cjk_char
from .subtitle import source_hash as subtitle_source_hash


CONTEXT_SCHEMA_VERSION = 1
PROMPT_POLICY_VERSION = "context-v2"


def estimate_timing_risks(
    subs,
    translations: dict,
    target_language: str,
    rates: dict,
) -> list[dict]:
    """Estimate whether translated speech fits each immutable subtitle window."""
    del target_language  # Text-script counts, rather than labels, handle mixed-language output.
    cjk_rate = float(rates["cjk_chars_per_second"])
    latin_rate = float(rates["latin_words_per_second"])
    punctuation_pause = float(rates["punctuation_pause_seconds"])
    warning_ratio = float(rates["warning_ratio"])
    critical_ratio = float(rates["critical_ratio"])
    if cjk_rate <= 0 or latin_rate <= 0:
        raise ValueError("timing speech rates must be positive")
    if punctuation_pause < 0:
        raise ValueError("punctuation pause must be non-negative")
    if warning_ratio < 1 or critical_ratio < warning_ratio:
        raise ValueError("timing risk ratios must satisfy 1 <= warning <= critical")

    results = []
    for index, sub in enumerate(subs):
        item = translations.get(index, translations.get(str(index), {}))
        if isinstance(item, dict):
            text = str(item.get("display_text") or item.get("tts_text") or "")
        else:
            text = str(item or "")

        cjk_count = sum(1 for char in text if is_cjk_char(char))
        latin_word_count = len(re.findall(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)?", text))
        punctuation_count = len(re.findall(r"[,.!?;:，。！？；：、]", text))
        estimated_s = (
            cjk_count / cjk_rate
            + latin_word_count / latin_rate
            + punctuation_count * punctuation_pause
        )
        window_s = max(0.0, (float(sub.end) - float(sub.start)) / 1000.0)
        denominator = window_s if window_s > 0 else 0.001
        required_ratio = estimated_s / denominator
        if required_ratio <= 1.0:
            risk = "normal"
        elif required_ratio <= warning_ratio:
            risk = "warning"
        else:
            risk = "critical"
        results.append(
            {
                "id": index,
                "window_s": round(window_s, 6),
                "estimated_s": round(estimated_s, 6),
                "required_speed_ratio": round(required_ratio, 6),
                "risk": risk,
            }
        )
    return results


def _normalize_whitespace(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def load_terms(path: str | None) -> list[dict]:
    """Load and normalize terminology records from a JSON or YAML file."""
    if not path:
        return []

    source_path = Path(path)
    if not source_path.exists():
        return []

    suffix = source_path.suffix.lower()
    if suffix == ".json":
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    elif suffix in {".yaml", ".yml"}:
        payload = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    else:
        raise ValueError(f"{source_path}: unsupported terminology extension {suffix!r}")

    terms = payload.get("terms") if isinstance(payload, dict) else payload
    if not isinstance(terms, list):
        raise ValueError(f"{source_path}: item 0: terms must be a list")

    normalized = []
    seen = set()
    for index, item in enumerate(terms):
        if not isinstance(item, dict):
            raise ValueError(f"{source_path}: item {index}: term must be an object")
        source = _normalize_whitespace(item.get("source"))
        target = _normalize_whitespace(item.get("target"))
        if not source:
            raise ValueError(f"{source_path}: item {index}: source is required")
        if not target:
            raise ValueError(f"{source_path}: item {index}: target is required")

        key = (source.casefold(), target.casefold())
        if key in seen:
            continue
        seen.add(key)
        normalized.append(
            {
                "source": source,
                "target": target,
                "note": _normalize_whitespace(item.get("note")),
                "provenance": {"path": str(source_path), "index": index},
            }
        )
    return normalized


def _subtitle_text(sub: object) -> str:
    if isinstance(sub, dict):
        text = sub.get("text", "")
    elif isinstance(sub, str):
        text = sub
    else:
        text = getattr(sub, "text", "")
    return get_sub_source_text(SimpleNamespace(text=str(text)))


def sample_transcript(subs, char_budget: int) -> str:
    """Return a deterministic, evenly distributed transcript sample."""
    budget = max(0, int(char_budget))
    if not budget:
        return ""

    texts = [_subtitle_text(sub) for sub in subs]
    texts = [text for text in texts if text]
    if not texts:
        return ""

    transcript = "\n".join(texts)
    if len(transcript) <= budget:
        return transcript

    size = len(texts)
    core_indexes = list(dict.fromkeys((0, size // 2, size - 1)))
    core_windows = [texts[index] for index in core_indexes]
    core_available = budget - (len(core_windows) - 1)
    if sum(map(len, core_windows)) > core_available:
        if core_available < len(core_windows):
            return "".join(window[:1] for window in core_windows)[:budget]
        lengths = [0] * len(core_windows)
        remaining = core_available
        while remaining and any(
            length < len(window) for length, window in zip(lengths, core_windows)
        ):
            for index, window in enumerate(core_windows):
                if remaining and lengths[index] < len(window):
                    lengths[index] += 1
                    remaining -= 1
        return "\n".join(
            window[:length] for window, length in zip(core_windows, lengths)
        )

    landmarks = sorted(
        set(core_indexes)
        | {
            round((size - 1) / 4),
            round((size - 1) * 3 / 4),
        }
    )
    candidate_indexes = [index for index in landmarks if index not in core_indexes]
    gaps = []
    for left, right in zip(landmarks, landmarks[1:]):
        if right - left > 1:
            heapq.heappush(gaps, (-(right - left), left, right))
    while gaps:
        _negative_width, left, right = heapq.heappop(gaps)
        middle = (left + right) // 2
        candidate_indexes.append(middle)
        if middle - left > 1:
            heapq.heappush(gaps, (-(middle - left), left, middle))
        if right - middle > 1:
            heapq.heappush(gaps, (-(right - middle), middle, right))

    selected = {index: texts[index] for index in core_indexes}
    used = sum(map(len, selected.values())) + len(selected) - 1
    skipped = []
    for index in candidate_indexes:
        cost = len(texts[index]) + 1
        if used + cost <= budget:
            selected[index] = texts[index]
            used += cost
        else:
            skipped.append(index)

    if used < budget and skipped and budget - used > 1:
        index = skipped[0]
        selected[index] = texts[index][: budget - used - 1]

    return "\n".join(selected[index] for index in sorted(selected))


def matching_terms(terms: list[dict], texts: list[str]) -> list[dict]:
    """Keep terms whose source appears in any supplied source subtitle text.

    Unicode case-folding leaves CJK characters intact, so one substring comparison
    handles CJK-only and mixed-script sources while ignoring Latin case.
    """
    joined = "\n".join(str(text or "") for text in texts)
    folded = joined.casefold()
    matches = []
    for term in terms:
        source = str(term.get("source") or "")
        if source and source.casefold() in folded:
            matches.append(term)
    return matches


def context_hash(context: dict) -> str:
    """Return a stable SHA-256 digest for JSON-compatible context data."""
    canonical = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def neighbor_context(subs, first_id: int, last_id: int, count: int) -> dict:
    """Return nearby subtitle records without including the active batch."""
    size = len(subs)
    limit = max(0, int(count))
    if not size or not limit:
        return {"before": [], "after": []}

    first, last = sorted((int(first_id), int(last_id)))
    before_ids = range(max(0, first - limit), min(size, max(0, first)))
    after_ids = range(max(0, last + 1), min(size, max(0, last + 1) + limit))

    return {
        "before": [{"id": index, "text": _subtitle_text(subs[index])} for index in before_ids],
        "after": [{"id": index, "text": _subtitle_text(subs[index])} for index in after_ids],
    }


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
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


def _backup_artifact(path: Path) -> None:
    if path.exists():
        backup = path.with_name(f"{path.name}.{time.time_ns()}.bak")
        shutil.copy2(path, backup)


def _request_context(model_config: dict, prompt: str) -> dict:
    """Request a summary and terminology candidates from the translation model."""
    client = OpenAI(
        api_key=model_config["api_key"],
        base_url=model_config["api_base"],
        http_client=httpx.Client(trust_env=True, http2=False, timeout=90.0),
    )
    try:
        response = client.chat.completions.create(
            model=model_config["model"],
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        return json.loads(response.choices[0].message.content.strip())
    finally:
        client.close()


def _normalize_generated_terms(raw_terms: object, model: str | None) -> list[dict]:
    if raw_terms is None:
        return []
    if not isinstance(raw_terms, list):
        raise ValueError("generated terms must be a list")

    normalized = []
    seen_sources = set()
    for index, item in enumerate(raw_terms):
        if not isinstance(item, dict):
            raise ValueError(f"generated term {index} must be an object")
        for field in ("source", "target"):
            if not isinstance(item.get(field), str):
                raise ValueError(f"generated term {index} {field} must be a string")
        if item.get("note") is not None and not isinstance(item.get("note"), str):
            raise ValueError(f"generated term {index} note must be a string")
        source = _normalize_whitespace(item.get("source"))
        target = _normalize_whitespace(item.get("target"))
        if not source or not target:
            raise ValueError(f"generated term {index} requires source and target")
        key = source.casefold()
        if key in seen_sources:
            continue
        seen_sources.add(key)
        normalized.append(
            {
                "source": source,
                "target": target,
                "note": _normalize_whitespace(item.get("note")),
                "provenance": {"kind": "auto", "model": model, "index": index},
            }
        )
    return normalized


def _merge_terms(auto_terms: list[dict], user_terms: list[dict]) -> list[dict]:
    """Merge terms by case-folded source, with explicit user choices winning."""
    user_sources = {str(term["source"]).casefold() for term in user_terms}
    merged = [
        term for term in auto_terms if str(term.get("source") or "").casefold() not in user_sources
    ]
    merged.extend(user_terms)
    return merged


def _terms_fingerprint(terms: list[dict]) -> str:
    return context_hash({"terms": terms})


def _artifact_matches(artifact: object, expected: dict) -> bool:
    if not isinstance(artifact, dict):
        return False
    required_types = {
        "summary": str,
        "terms": list,
        "provenance": dict,
        "generation_model": (str, type(None)),
    }
    if any(not isinstance(artifact.get(key), value_type) for key, value_type in required_types.items()):
        return False
    if any(artifact.get(key) != value for key, value in expected.items()):
        return False
    for term in artifact["terms"]:
        if not isinstance(term, dict):
            return False
        if not isinstance(term.get("source"), str) or not term["source"].strip():
            return False
        if not isinstance(term.get("target"), str) or not term["target"].strip():
            return False
        if not isinstance(term.get("note"), str):
            return False
        term_provenance = term.get("provenance")
        if not isinstance(term_provenance, dict):
            return False
        if "kind" in term_provenance:
            if (
                term_provenance.get("kind") != "auto"
                or type(term_provenance.get("index")) is not int
                or not isinstance(term_provenance.get("model"), (str, type(None)))
            ):
                return False
        elif (
            not isinstance(term_provenance.get("path"), str)
            or type(term_provenance.get("index")) is not int
        ):
            return False
    provenance = artifact["provenance"]
    return (
        provenance.get("prompt_policy_version") == PROMPT_POLICY_VERSION
        and provenance.get("generation_status") in {"ok", "off"}
        and provenance.get("context_mode") in {"auto", "off"}
        and isinstance(provenance.get("user_terms_hash"), str)
        and type(provenance.get("char_budget")) is int
        and provenance["char_budget"] >= 0
    )


def _context_prompt(target_language: str, transcript_sample: str) -> str:
    return f"""Analyze this complete-video transcript sample before subtitle translation.
Target language: {target_language}
Return strict JSON with this shape:
{{"summary":"concise video summary","terms":[{{"source":"source term","target":"preferred translation","note":"optional guidance"}}]}}

Select only important recurring names, technical terms, and ambiguous phrases.

TRANSCRIPT SAMPLE
{transcript_sample}
"""


def prepare_translation_context(
    subs,
    out_dir: Path,
    args,
    model_config,
    resolved_generation_model: str | None = None,
) -> tuple[dict, Path, list[str]]:
    """Load or generate the reusable whole-video translation context artifact."""
    output_dir = Path(out_dir)
    artifact_path = output_dir / f"translation_context_{lang_slug(args.target_language)}.json"
    user_terms = load_terms(getattr(args, "terms_file", None))
    context_mode = getattr(args, "translation_context", "auto")
    generation_model = None
    if context_mode == "auto":
        generation_model = resolved_generation_model
        if generation_model is None and isinstance(model_config, dict):
            generation_model = model_config.get("model")
    expected = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "source_hash": subtitle_source_hash(subs),
        "target_language": args.target_language,
        "generation_model": generation_model,
    }
    expected_provenance = {
        "prompt_policy_version": PROMPT_POLICY_VERSION,
        "context_mode": context_mode,
        "user_terms_hash": _terms_fingerprint(user_terms),
        "char_budget": max(0, int(getattr(args, "context_char_budget", 8_000) or 0)),
    }

    if artifact_path.exists():
        try:
            cached = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = None
        if _artifact_matches(cached, expected) and all(
            cached["provenance"].get(key) == value for key, value in expected_provenance.items()
        ):
            return cached, artifact_path, []
        _backup_artifact(artifact_path)

    warnings = []
    auto_terms = []
    summary = ""
    generation_status = "off"
    request_model_config = model_config
    if context_mode == "auto" and callable(request_model_config):
        request_model_config = request_model_config()
    if context_mode == "auto" and request_model_config:
        sample = sample_transcript(subs, expected_provenance["char_budget"])
        try:
            generated = _request_context(
                request_model_config,
                _context_prompt(args.target_language, sample),
            )
            if not isinstance(generated, dict):
                raise ValueError("context response must be a JSON object")
            generated_summary = generated.get("summary")
            if not isinstance(generated_summary, str):
                raise ValueError("context summary must be a string")
            normalized_summary = _normalize_whitespace(generated_summary)
            normalized_terms = _normalize_generated_terms(
                generated.get("terms"), generation_model
            )
            summary = normalized_summary
            auto_terms = normalized_terms
            generation_status = "ok"
        except Exception as exc:
            generation_status = "failed"
            warnings.append(f"Automatic translation context generation failed: {exc}")

    artifact = {
        **expected,
        "summary": summary,
        "terms": _merge_terms(auto_terms, user_terms),
        "provenance": {**expected_provenance, "generation_status": generation_status},
    }
    _write_json_atomic(artifact_path, artifact)
    return artifact, artifact_path, warnings
