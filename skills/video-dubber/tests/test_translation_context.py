from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.translation_context as translation_context
import core.translate as translate
from core.translation_context import (
    CONTEXT_SCHEMA_VERSION,
    PROMPT_POLICY_VERSION,
    context_hash,
    estimate_timing_risks,
    load_terms,
    matching_terms,
    neighbor_context,
    prepare_translation_context,
    sample_transcript,
)
from core.subtitle import source_hash
from core.translate import _build_prompt, _load_prompt, _request_batch


def _sub(text, start=0, end=1000):
    return SimpleNamespace(text=text, start=start, end=end)


def _context_args(terms_file=None, **overrides):
    values = {
        "target_language": "Chinese",
        "terms_file": str(terms_file) if terms_file else None,
        "translation_context": "auto",
        "context_char_budget": 10_000,
        "context_neighbor_lines": 2,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_load_terms_normalizes_json_records_and_removes_duplicate_pairs(tmp_path):
    path = tmp_path / "terms.json"
    path.write_text(
        json.dumps(
            {
                "terms": [
                    {"source": "  Deep   Learning ", "target": " 深度 学习 ", "note": "  preferred  "},
                    {"source": "deep learning", "target": "深度 学习", "note": "duplicate"},
                ]
            }
        ),
        encoding="utf-8",
    )

    assert load_terms(str(path)) == [
        {
            "source": "Deep Learning",
            "target": "深度 学习",
            "note": "preferred",
            "provenance": {"path": str(path), "index": 0},
        }
    ]


def test_load_terms_accepts_yaml_terms(tmp_path):
    path = tmp_path / "terms.yaml"
    path.write_text(
        "terms:\n"
        "  - source: model\n"
        "    target: 模型\n"
        "    note: keep concise\n",
        encoding="utf-8",
    )

    assert load_terms(str(path)) == [
        {
            "source": "model",
            "target": "模型",
            "note": "keep concise",
            "provenance": {"path": str(path), "index": 0},
        }
    ]


@pytest.mark.parametrize(
    ("record", "field"),
    [
        ({"target": "目标"}, "source"),
        ({"source": "source"}, "target"),
        ({"source": "  ", "target": "target"}, "source"),
        ({"source": "source", "target": "\t"}, "target"),
    ],
)
def test_load_terms_rejects_missing_or_blank_required_fields(tmp_path, record, field):
    path = tmp_path / "terms.json"
    path.write_text(json.dumps({"terms": [record]}), encoding="utf-8")

    with pytest.raises(ValueError, match=rf"{path}.*item 0.*{field}"):
        load_terms(str(path))


def test_load_terms_rejects_unsupported_files_and_returns_empty_for_absent_file(tmp_path):
    unsupported = tmp_path / "terms.txt"
    unsupported.write_text("source: hello", encoding="utf-8")

    with pytest.raises(ValueError, match=rf"{unsupported}.*unsupported"):
        load_terms(str(unsupported))

    assert load_terms(str(tmp_path / "absent.json")) == []
    assert load_terms(None) == []


def test_load_terms_rejects_a_non_list_terms_value_with_source_path_and_index(tmp_path):
    path = tmp_path / "terms.json"
    path.write_text(json.dumps({"terms": {"source": "one", "target": "一"}}), encoding="utf-8")

    with pytest.raises(ValueError, match=rf"{path}.*item 0"):
        load_terms(str(path))


def test_context_hash_is_stable_for_equivalent_nested_contexts():
    left = {"terms": [{"source": "model", "target": "模型"}], "sample": "hello"}
    right = {"sample": "hello", "terms": [{"target": "模型", "source": "model"}]}

    assert context_hash(left) == context_hash(right)
    assert len(context_hash(left)) == 64


def test_sample_transcript_deterministically_represents_beginning_middle_and_end_within_budget():
    subs = [
        SimpleNamespace(text="BEGIN"),
        SimpleNamespace(text="one"),
        SimpleNamespace(text="MIDDLE"),
        SimpleNamespace(text="two"),
        SimpleNamespace(text="END"),
    ]

    first = sample_transcript(subs, char_budget=16)
    second = sample_transcript(subs, char_budget=16)

    assert first == second
    assert len(first) <= 16
    assert "BEGIN" in first
    assert "MIDDLE" in first
    assert "END" in first


def test_sample_transcript_evenly_covers_long_transcript_and_materially_uses_budget():
    subs = [
        SimpleNamespace(text=f"MARKER-{index:03d}-" + "x" * 18)
        for index in range(101)
    ]

    first = sample_transcript(subs, char_budget=400)
    second = sample_transcript(subs, char_budget=400)

    assert first == second
    assert 320 <= len(first) <= 400
    for index in (0, 25, 50, 75, 100):
        assert f"MARKER-{index:03d}-" in first


def test_matching_terms_handles_latin_casefolding_and_cjk_substrings():
    terms = [
        {"source": "Deep Learning", "target": "深度学习"},
        {"source": "人工智能", "target": "AI"},
        {"source": "missing", "target": "缺失"},
    ]

    assert matching_terms(terms, ["DEEP learning is useful", "人工智能正在改变世界"]) == terms[:2]


def test_matching_terms_casefolds_latin_segments_in_mixed_script_sources():
    terms = [{"source": "OpenAI模型", "target": "OpenAI model"}]

    assert matching_terms(terms, ["openai模型正在改变世界"]) == terms


def test_neighbor_context_excludes_the_active_batch_and_respects_bounds():
    subs = [SimpleNamespace(text=str(index)) for index in range(6)]

    assert neighbor_context(subs, first_id=2, last_id=3, count=2) == {
        "before": [{"id": 0, "text": "0"}, {"id": 1, "text": "1"}],
        "after": [{"id": 4, "text": "4"}, {"id": 5, "text": "5"}],
    }
    assert neighbor_context(subs, first_id=0, last_id=1, count=10) == {
        "before": [],
        "after": [
            {"id": 2, "text": "2"},
            {"id": 3, "text": "3"},
            {"id": 4, "text": "4"},
            {"id": 5, "text": "5"},
        ],
    }


def test_prepare_translation_context_generates_complete_artifact_from_full_transcript(
    tmp_path, monkeypatch
):
    subs = [
        _sub("BEGIN topic", 0, 1000),
        _sub("MIDDLE detail", 1000, 2200),
        _sub("END conclusion", 2200, 3000),
    ]
    seen = {}

    def fake_request(model_config, prompt):
        seen["model_config"] = model_config
        seen["prompt"] = prompt
        return {
            "summary": "A concise video summary.",
            "terms": [
                {"source": "topic", "target": "主题", "note": "Use consistently."}
            ],
        }

    monkeypatch.setattr(translation_context, "_request_context", fake_request)
    model_config = {"model": "fake-context-model", "api_key": "unused", "api_base": "unused"}

    context, artifact_path, warnings = prepare_translation_context(
        subs, tmp_path, _context_args(), model_config
    )

    assert warnings == []
    assert artifact_path == tmp_path / "translation_context_zh.json"
    assert json.loads(artifact_path.read_text(encoding="utf-8")) == context
    assert context["schema_version"] == CONTEXT_SCHEMA_VERSION == 1
    assert context["source_hash"] == source_hash(subs)
    assert context["target_language"] == "Chinese"
    assert context["summary"] == "A concise video summary."
    assert context["terms"] == [
        {
            "source": "topic",
            "target": "主题",
            "note": "Use consistently.",
            "provenance": {"kind": "auto", "model": "fake-context-model", "index": 0},
        }
    ]
    assert context["provenance"]["prompt_policy_version"] == PROMPT_POLICY_VERSION == "context-v2"
    assert context["generation_model"] == "fake-context-model"
    assert "BEGIN topic" in seen["prompt"]
    assert "MIDDLE detail" in seen["prompt"]
    assert "END conclusion" in seen["prompt"]


def test_prepare_translation_context_reuses_matching_artifact(tmp_path, monkeypatch):
    subs = [_sub("one", 0, 500), _sub("two", 500, 1000)]
    calls = []

    def fake_request(_model_config, _prompt):
        calls.append(True)
        return {"summary": "cached summary", "terms": []}

    monkeypatch.setattr(translation_context, "_request_context", fake_request)
    args = _context_args()
    model_config = {"model": "fake-model", "api_key": "unused", "api_base": "unused"}

    first, artifact_path, _warnings = prepare_translation_context(subs, tmp_path, args, model_config)
    second, second_path, warnings = prepare_translation_context(subs, tmp_path, args, model_config)

    assert second == first
    assert second_path == artifact_path
    assert warnings == []
    assert calls == [True]


@pytest.mark.parametrize(
    "initial_payload",
    [
        "not json",
        json.dumps(
            {
                "schema_version": 999,
                "source_hash": "stale",
                "target_language": "Chinese",
                "summary": "stale",
                "terms": [],
                "provenance": {},
                "generation_model": "fake-model",
            }
        ),
    ],
)
def test_prepare_translation_context_backs_up_corrupt_or_incompatible_artifacts(
    tmp_path, monkeypatch, initial_payload
):
    artifact_path = tmp_path / "translation_context_zh.json"
    artifact_path.write_text(initial_payload, encoding="utf-8")
    monkeypatch.setattr(
        translation_context,
        "_request_context",
        lambda _config, _prompt: {"summary": "fresh", "terms": []},
    )

    context, returned_path, _warnings = prepare_translation_context(
        [_sub("fresh source")],
        tmp_path,
        _context_args(),
        {"model": "fake-model", "api_key": "unused", "api_base": "unused"},
    )

    assert returned_path == artifact_path
    assert context["summary"] == "fresh"
    backups = list(tmp_path.glob("translation_context_zh.json.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == initial_payload


def test_prepare_translation_context_merges_terms_with_user_source_winning(tmp_path, monkeypatch):
    terms_file = tmp_path / "terms.json"
    terms_file.write_text(
        json.dumps(
            {
                "terms": [
                    {"source": "OpenAI", "target": "开放人工智能", "note": "User choice"},
                    {"source": "model", "target": "模型", "note": "User-only"},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        translation_context,
        "_request_context",
        lambda _config, _prompt: {
            "summary": "summary",
            "terms": [
                {"source": "openai", "target": "OpenAI", "note": "Auto choice"},
                {"source": "API", "target": "接口", "note": "Auto-only"},
            ],
        },
    )

    context, _path, warnings = prepare_translation_context(
        [_sub("OpenAI model API")],
        tmp_path,
        _context_args(terms_file),
        {"model": "fake-model", "api_key": "unused", "api_base": "unused"},
    )

    assert warnings == []
    assert [(term["source"], term["target"]) for term in context["terms"]] == [
        ("API", "接口"),
        ("OpenAI", "开放人工智能"),
        ("model", "模型"),
    ]
    assert context["terms"][1]["provenance"] == {"path": str(terms_file), "index": 0}


def test_prepare_translation_context_failure_keeps_user_terms_and_returns_warning(
    tmp_path, monkeypatch
):
    terms_file = tmp_path / "terms.yaml"
    terms_file.write_text(
        "terms:\n  - source: model\n    target: 模型\n    note: preferred\n",
        encoding="utf-8",
    )

    def fail_request(_model_config, _prompt):
        raise RuntimeError("simulated context failure")

    monkeypatch.setattr(translation_context, "_request_context", fail_request)

    context, artifact_path, warnings = prepare_translation_context(
        [_sub("A model")],
        tmp_path,
        _context_args(terms_file),
        {"model": "fake-model", "api_key": "unused", "api_base": "unused"},
    )

    assert context["summary"] == ""
    assert context["terms"] == load_terms(str(terms_file))
    assert context["generation_model"] == "fake-model"
    assert json.loads(artifact_path.read_text(encoding="utf-8")) == context
    assert len(warnings) == 1
    assert "simulated context failure" in warnings[0]


@pytest.mark.parametrize(
    "malformed_terms",
    [
        "not-a-list",
        [
            {"source": "valid auto", "target": "有效自动"},
            {"source": "bad auto", "target": 42},
        ],
        [{"source": ["bad"], "target": "坏"}],
        [{"source": "bad", "target": "坏", "note": 42}],
    ],
)
def test_malformed_generated_terms_publish_no_auto_summary_or_auto_terms(
    tmp_path, monkeypatch, malformed_terms
):
    terms_file = tmp_path / "terms.json"
    terms_file.write_text(
        json.dumps({"terms": [{"source": "user", "target": "用户"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        translation_context,
        "_request_context",
        lambda *_args: {
            "summary": "must not be published",
            "terms": malformed_terms,
        },
    )

    context, artifact_path, warnings = prepare_translation_context(
        [_sub("source")],
        tmp_path,
        _context_args(terms_file),
        {"model": "fake-model", "api_key": "unused", "api_base": "unused"},
    )

    assert context["summary"] == ""
    assert context["terms"] == load_terms(str(terms_file))
    assert context["provenance"]["generation_status"] == "failed"
    assert warnings and "Automatic translation context generation failed" in warnings[0]
    assert json.loads(artifact_path.read_text(encoding="utf-8")) == context


def test_build_prompt_includes_context_matched_terms_neighbors_timing_and_active_payload_only():
    subs = [
        _sub("before zero", 0, 600),
        _sub("before one", 600, 1300),
        _sub("OpenAI builds a model", 1300, 2600),
        _sub("active second", 2600, 3500),
        _sub("after four", 3500, 4100),
        _sub("after five", 4100, 5000),
        _sub("too far away", 5000, 6000),
    ]
    context = {
        "summary": "A video about practical AI systems.",
        "terms": [
            {"source": "OpenAI", "target": "OpenAI", "note": "Keep in English."},
            {"source": "model", "target": "模型", "note": "Preferred technical term."},
            {"source": "unmatched", "target": "不应出现", "note": "Not in this batch."},
        ],
    }
    args = _context_args(context_neighbor_lines=2)
    _system, display_rule, user_template = _load_prompt("Chinese", compact_display=True)

    prompt = _build_prompt(
        args,
        [(2, subs[2]), (3, subs[3])],
        display_rule,
        user_template,
        context,
        subs,
    )

    assert "VIDEO CONTEXT" in prompt
    assert "A video about practical AI systems." in prompt
    assert "TERMINOLOGY" in prompt
    assert "OpenAI => OpenAI" in prompt
    assert "model => 模型" in prompt
    assert "unmatched" not in prompt
    assert "REFERENCE ONLY" in prompt
    assert "0|before zero" in prompt
    assert "1|before one" in prompt
    assert "4|after four" in prompt
    assert "5|after five" in prompt
    assert "6|too far away" not in prompt
    assert "TIMING BUDGET" in prompt
    assert "2|1300ms" in prompt
    assert "3|900ms" in prompt
    assert "TRANSLATE THESE IDS" in prompt
    active_payload = prompt.split("TRANSLATE THESE IDS", 1)[1]
    assert "2|OpenAI builds a model" in active_payload
    assert "3|active second" in active_payload
    assert '"id":2' not in active_payload
    assert "Return translations only for ids: 2, 3." in prompt
    assert "Do not translate or return reference ids." in prompt


def test_request_batch_discards_model_tts_text_for_strict_sync(monkeypatch):
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps(
                        {
                            "translations": [
                                {
                                    "id": 7,
                                    "display_text": "同步文本",
                                    "tts_text": "divergent spoken text",
                                }
                            ]
                        }
                    )
                )
            )
        ]
    )

    class FakeCompletions:
        def create(self, **_kwargs):
            return response

    class FakeClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

        def close(self):
            pass

    monkeypatch.setattr("core.translate.OpenAI", FakeClient)

    result = _request_batch(
        {"model": "fake", "api_key": "unused", "api_base": "https://invalid.example"},
        [],
        "prompt",
    )

    assert result == {7: {"display_text": "同步文本", "tts_text": "同步文本"}}


@pytest.mark.parametrize("invalid_display", [None, "", "   ", 42])
def test_translate_one_batch_retries_invalid_speech_display_text(
    monkeypatch, invalid_display
):
    responses = iter(
        [
            {0: {"display_text": invalid_display, "tts_text": invalid_display}},
            {0: {"display_text": "fresh translation", "tts_text": "fresh translation"}},
        ]
    )
    calls = []

    def fake_request(*_args):
        calls.append(True)
        return next(responses)

    monkeypatch.setattr(translate, "_request_batch", fake_request)
    monkeypatch.setattr(translate.time, "sleep", lambda *_args: None)

    result = translate._translate_one_batch(
        {"model": "fake"},
        [],
        "prompt",
        [(0, _sub("spoken source"))],
        1,
    )

    assert calls == [True, True]
    assert result == {
        0: {"display_text": "fresh translation", "tts_text": "fresh translation"}
    }


class _SavableSubs(list):
    def save(self, path):
        Path(path).write_text("saved", encoding="utf-8")


def _translation_args(**overrides):
    values = {
        "target_language": "Chinese",
        "subtitle_mode": "target",
        "translation_model": "fake-model",
        "translation_batch_size": 25,
        "translation_workers": 1,
        "allow_source_fallback": False,
        "status": None,
        "terms_file": None,
        "translation_context": "off",
        "context_char_budget": 10_000,
        "context_neighbor_lines": 2,
        "translation_timing": {
            "cjk_chars_per_second": 4.0,
            "latin_words_per_second": 2.0,
            "punctuation_pause_seconds": 0.1,
            "warning_ratio": 1.25,
            "critical_ratio": 1.6,
        },
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _write_translation_cache(out_dir, subs, translations, args):
    (out_dir / "translations_zh.json").write_text(
        json.dumps({str(key): value for key, value in translations.items()}),
        encoding="utf-8",
    )
    (out_dir / "translations_zh.meta.json").write_text(
        json.dumps(
            translate._translation_cache_identity(
                subs,
                args,
                {
                    "schema_version": CONTEXT_SCHEMA_VERSION,
                    "source_hash": source_hash(subs),
                    "target_language": args.target_language,
                    "generation_model": None,
                    "summary": "",
                    "terms": [],
                    "provenance": {
                        "prompt_policy_version": PROMPT_POLICY_VERSION,
                        "context_mode": "off",
                        "user_terms_hash": context_hash({"terms": []}),
                        "char_budget": 10_000,
                        "generation_status": "off",
                    },
                },
            )
        ),
        encoding="utf-8",
    )


def test_translation_cache_identity_changes_for_every_context_input(tmp_path):
    terms_file = tmp_path / "terms.json"
    terms_file.write_bytes(b'{"terms": [{"source": "API", "target": "interface"}]}')
    args = _translation_args(terms_file=str(terms_file))
    subs = [_sub("source", 0, 1000)]
    context = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "summary": "original summary",
        "terms": [{"source": "API", "target": "interface"}],
        "provenance": {"prompt_policy_version": PROMPT_POLICY_VERSION},
    }

    baseline = translate._translation_cache_identity(subs, args, context)
    assert baseline == translate._translation_cache_identity(subs, args, context)

    changed_context = {**context, "summary": "changed summary"}
    assert translate._translation_cache_identity(subs, args, changed_context) != baseline

    changed_schema = {**context, "schema_version": CONTEXT_SCHEMA_VERSION + 1}
    assert translate._translation_cache_identity(subs, args, changed_schema) != baseline

    changed_policy = {
        **context,
        "provenance": {"prompt_policy_version": "changed-policy"},
    }
    assert translate._translation_cache_identity(subs, args, changed_policy) != baseline

    assert translate._translation_cache_identity([_sub("changed")], args, context) != baseline
    assert translate._translation_cache_identity(
        subs, _translation_args(terms_file=str(terms_file), target_language="Japanese"), context
    ) != baseline
    assert translate._translation_cache_identity(
        subs, _translation_args(terms_file=str(terms_file), translation_model="other-model"), context
    ) != baseline

    terms_file.write_bytes(b'{"terms": [{"source": "API", "target": "API"}]}')
    assert translate._translation_cache_identity(subs, args, context) != baseline


@pytest.mark.parametrize(
    ("cached_mode", "requested_mode"),
    [("source", "target"), ("target", "source")],
)
def test_translation_cache_never_crosses_source_translated_boundary(
    tmp_path, monkeypatch, cached_mode, requested_mode
):
    source_text = "canonical source"
    cached_text = source_text if cached_mode == "source" else "cached translation"
    cached_subs = _SavableSubs([_sub(source_text, 0, 1000)])
    cached_args = _translation_args(subtitle_mode=cached_mode)
    _write_translation_cache(
        tmp_path,
        cached_subs,
        {0: {"display_text": cached_text, "tts_text": cached_text}},
        cached_args,
    )

    applied = []
    monkeypatch.setattr(
        translate,
        "apply_translations_to_subs",
        lambda _subs, translations, mode, _language: applied.append(
            (json.loads(json.dumps(translations)), mode)
        ),
    )
    monkeypatch.setattr(translate, "apply_ass_style", lambda *_args: None)
    if requested_mode == "target":
        monkeypatch.setattr(
            translate,
            "load_model_config",
            lambda _args: (_ for _ in ()).throw(RuntimeError("credentials unavailable")),
        )

    requested_subs = _SavableSubs([_sub(source_text, 0, 1000)])
    result = translate.translate_subtitles(
        requested_subs,
        tmp_path,
        _translation_args(subtitle_mode=requested_mode),
    )

    if requested_mode == "target":
        assert result[:3] == (None, None, None)
        assert applied == []
    else:
        assert result[2] == {
            0: {"display_text": source_text, "tts_text": source_text}
        }
        assert applied == [
            ({"0": {"display_text": source_text, "tts_text": source_text}}, "source")
        ]


def test_complete_generated_context_and_translation_cache_survive_missing_credentials(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "models.yaml"
    config_path.write_text(
        "models:\n"
        "  - name: fixture-alias\n"
        "    model: resolved-fixture-model\n"
        "    api_key: $FIXTURE_TRANSLATION_KEY\n"
        "    api_base: https://unused.invalid/v1\n",
        encoding="utf-8",
    )
    args = _translation_args(
        translation_model="fixture-alias",
        translation_context="auto",
        model_config=str(config_path),
    )
    context_calls = []
    translation_calls = []
    monkeypatch.setenv("FIXTURE_TRANSLATION_KEY", "secret")
    monkeypatch.setattr(
        translation_context,
        "_request_context",
        lambda *_args: context_calls.append(True)
        or {"summary": "cached context", "terms": []},
    )
    monkeypatch.setattr(
        translate,
        "_translate_one_batch",
        lambda _config, _messages, _prompt, batch, _number: translation_calls.append(True)
        or {
            idx: {"display_text": "cached translation", "tts_text": "cached translation"}
            for idx, _sub_item in batch
        },
    )
    monkeypatch.setattr(translate, "apply_translations_to_subs", lambda *_args: None)
    monkeypatch.setattr(translate, "apply_ass_style", lambda *_args: None)

    first = translate.translate_subtitles(
        _SavableSubs([_sub("canonical source", 0, 1000)]), tmp_path, args
    )
    monkeypatch.delenv("FIXTURE_TRANSLATION_KEY")
    second = translate.translate_subtitles(
        _SavableSubs([_sub("canonical source", 0, 1000)]), tmp_path, args
    )

    assert first[2] == second[2] == {
        0: {"display_text": "cached translation", "tts_text": "cached translation"}
    }
    assert second[3]["translation_context_path"] == str(
        tmp_path / "translation_context_zh.json"
    )
    assert context_calls == [True]
    assert translation_calls == [True]
    assert not list(tmp_path.glob("*.bak"))
    assert not (tmp_path / "source_raw.srt").exists()


@pytest.mark.parametrize(
    "writer_name",
    ["translate", "translation_context"],
)
def test_json_atomic_writers_use_unique_same_directory_temp_files(
    tmp_path, monkeypatch, writer_name
):
    destination = tmp_path / f"{writer_name}.json"
    writer = (
        translate._write_json
        if writer_name == "translate"
        else translation_context._write_json_atomic
    )
    barrier = threading.Barrier(2)
    real_replace = Path.replace
    temporary_paths = []
    paths_lock = threading.Lock()

    def synchronized_replace(source, target):
        with paths_lock:
            temporary_paths.append(source)
        barrier.wait(timeout=5)
        return real_replace(source, target)

    monkeypatch.setattr(Path, "replace", synchronized_replace)
    payloads = [{"writer": 1}, {"writer": 2}]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(writer, destination, payload) for payload in payloads]
        for future in futures:
            future.result(timeout=10)

    assert len(set(temporary_paths)) == 2
    assert all(path.parent == destination.parent for path in temporary_paths)
    assert json.loads(destination.read_text(encoding="utf-8")) in payloads
    assert not any(path.exists() for path in temporary_paths)


@pytest.mark.parametrize(
    ("target_language", "text"),
    [
        ("Chinese", "天地玄黄"),
        ("English", "one two"),
        ("Japanese", "あいうえ"),
        ("Korean", "가나다라"),
    ],
)
def test_estimate_timing_risks_supports_multilingual_text_without_mutating_timestamps(
    target_language, text
):
    subs = [_sub("source", 250, 2250)]
    before = [(sub.start, sub.end) for sub in subs]
    rates = {
        "cjk_chars_per_second": 4.0,
        "latin_words_per_second": 2.0,
        "punctuation_pause_seconds": 0.1,
        "warning_ratio": 1.25,
        "critical_ratio": 1.6,
    }

    risks = estimate_timing_risks(
        subs,
        {0: {"display_text": text, "tts_text": text}},
        target_language,
        rates,
    )

    assert [(sub.start, sub.end) for sub in subs] == before
    assert len(risks) == 1
    assert set(risks[0]) == {
        "id",
        "window_s",
        "estimated_s",
        "required_speed_ratio",
        "risk",
    }
    assert risks[0]["id"] == 0
    assert risks[0]["window_s"] == 2.0
    assert risks[0]["estimated_s"] == pytest.approx(1.0)
    assert risks[0]["required_speed_ratio"] == pytest.approx(0.5)
    assert risks[0]["risk"] == "normal"


@pytest.mark.parametrize(
    ("word_count", "expected_risk"),
    [(4, "normal"), (5, "warning"), (6, "critical")],
)
def test_estimate_timing_risks_classifies_configured_thresholds(word_count, expected_risk):
    rates = {
        "cjk_chars_per_second": 4.0,
        "latin_words_per_second": 2.0,
        "punctuation_pause_seconds": 0.0,
        "warning_ratio": 1.25,
        "critical_ratio": 1.6,
    }

    [result] = estimate_timing_risks(
        [_sub("source", 0, 2000)],
        {0: {"display_text": " ".join(["word"] * word_count)}},
        "English",
        rates,
    )

    assert result["required_speed_ratio"] == pytest.approx(word_count / 4)
    assert result["risk"] == expected_risk


def test_complete_non_source_cache_is_strict_sync_normalized_and_persisted(tmp_path, monkeypatch):
    subs = _SavableSubs([_sub("source", 0, 1000)])
    args = _translation_args()
    _write_translation_cache(
        tmp_path,
        subs,
        {0: {"display_text": "cached display", "tts_text": "divergent cached speech"}},
        args,
    )
    monkeypatch.setattr(translate, "apply_translations_to_subs", lambda *_args: None)
    monkeypatch.setattr(translate, "apply_ass_style", lambda *_args: None)

    _ass_path, _returned_subs, translations, _context_info = translate.translate_subtitles(
        subs, tmp_path, args
    )

    expected = {0: {"display_text": "cached display", "tts_text": "cached display"}}
    assert translations == expected
    assert json.loads((tmp_path / "translations_zh.json").read_text(encoding="utf-8")) == {
        "0": expected[0]
    }


@pytest.mark.parametrize("invalid_display", [None, "", "   ", 42])
def test_invalid_cached_speech_display_text_is_retranslated(
    tmp_path, monkeypatch, invalid_display
):
    subs = _SavableSubs([_sub("spoken source", 0, 1000)])
    args = _translation_args()
    _write_translation_cache(
        tmp_path,
        subs,
        {0: {"display_text": invalid_display, "tts_text": invalid_display}},
        args,
    )
    calls = []
    monkeypatch.setattr(
        translate,
        "load_model_config",
        lambda _args: {"model": "fake-model", "api_key": "unused", "api_base": "unused"},
    )
    monkeypatch.setattr(
        translate,
        "_translate_one_batch",
        lambda _config, _messages, _prompt, batch, _number: calls.append(
            [idx for idx, _sub_item in batch]
        )
        or {0: {"display_text": "fresh translation", "tts_text": "fresh translation"}},
    )
    monkeypatch.setattr(translate, "apply_translations_to_subs", lambda *_args: None)
    monkeypatch.setattr(translate, "apply_ass_style", lambda *_args: None)

    _ass_path, _returned_subs, translations, _context_info = translate.translate_subtitles(
        subs, tmp_path, args
    )

    assert calls == [[0]]
    assert translations == {
        0: {"display_text": "fresh translation", "tts_text": "fresh translation"}
    }


def test_invalid_complete_cache_never_uses_implicit_source_fallback_without_flag(
    tmp_path, monkeypatch
):
    subs = _SavableSubs([_sub("spoken source", 0, 1000)])
    args = _translation_args(allow_source_fallback=False)
    _write_translation_cache(
        tmp_path,
        subs,
        {0: {"display_text": "   ", "tts_text": "   "}},
        args,
    )
    monkeypatch.setattr(
        translate,
        "load_model_config",
        lambda _args: (_ for _ in ()).throw(RuntimeError("credentials unavailable")),
    )
    monkeypatch.setattr(
        translate,
        "apply_translations_to_subs",
        lambda *_args: pytest.fail("invalid cache must not be applied as source text"),
    )

    result = translate.translate_subtitles(subs, tmp_path, args)

    assert result[:3] == (None, None, None)


def test_partial_non_source_cache_is_strict_sync_normalized_before_resume_and_persisted(
    tmp_path, monkeypatch
):
    subs = _SavableSubs([_sub("source zero", 0, 1000), _sub("source one", 1000, 2000)])
    args = _translation_args()
    _write_translation_cache(
        tmp_path,
        subs,
        {0: {"display_text": "cached zero", "tts_text": "divergent cached speech"}},
        args,
    )
    monkeypatch.setattr(translate, "apply_translations_to_subs", lambda *_args: None)
    monkeypatch.setattr(translate, "apply_ass_style", lambda *_args: None)
    monkeypatch.setattr(
        translate,
        "load_model_config",
        lambda _args: {"model": "fake-model", "api_key": "unused", "api_base": "unused"},
    )
    monkeypatch.setattr(
        translate,
        "prepare_translation_context",
        lambda *_args: (
            {
                "schema_version": CONTEXT_SCHEMA_VERSION,
                "source_hash": source_hash(subs),
                "target_language": args.target_language,
                "generation_model": None,
                "summary": "",
                "terms": [],
                "provenance": {
                    "prompt_policy_version": PROMPT_POLICY_VERSION,
                    "context_mode": "off",
                    "user_terms_hash": context_hash({"terms": []}),
                    "char_budget": 10_000,
                    "generation_status": "off",
                },
            },
            tmp_path / "context.json",
            [],
        ),
    )
    monkeypatch.setattr(
        translate,
        "_translate_one_batch",
        lambda _config, _messages, _prompt, batch, _number: {
            idx: {"display_text": "new one", "tts_text": "new one"} for idx, _sub in batch
        },
    )

    _ass_path, _returned_subs, translations, _context_info = translate.translate_subtitles(
        subs, tmp_path, args
    )

    assert translations[0] == {"display_text": "cached zero", "tts_text": "cached zero"}
    persisted = json.loads((tmp_path / "translations_zh.json").read_text(encoding="utf-8"))
    assert persisted["0"] == {"display_text": "cached zero", "tts_text": "cached zero"}


def test_prepare_translation_context_backs_up_cached_artifact_with_malformed_term(
    tmp_path, monkeypatch
):
    subs = [_sub("source")]
    args = _context_args()
    artifact_path = tmp_path / "translation_context_zh.json"
    malformed = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "source_hash": source_hash(subs),
        "target_language": "Chinese",
        "generation_model": "fake-model",
        "summary": "cached",
        "terms": [{"source": "term", "target": 42, "note": "bad", "provenance": {}}],
        "provenance": {
            "prompt_policy_version": PROMPT_POLICY_VERSION,
            "context_mode": "auto",
            "user_terms_hash": context_hash({"terms": []}),
            "char_budget": 10_000,
            "generation_status": "ok",
        },
    }
    artifact_path.write_text(json.dumps(malformed), encoding="utf-8")
    calls = []

    def fake_request(_config, _prompt):
        calls.append(True)
        return {"summary": "regenerated", "terms": []}

    monkeypatch.setattr(translation_context, "_request_context", fake_request)

    context, _path, warnings = prepare_translation_context(
        subs,
        tmp_path,
        args,
        {"model": "fake-model", "api_key": "unused", "api_base": "unused"},
    )

    assert warnings == []
    assert context["summary"] == "regenerated"
    assert calls == [True]
    backups = list(tmp_path.glob("translation_context_zh.json.*.bak"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == malformed


def test_translate_one_batch_rejects_unexpected_response_ids(monkeypatch):
    monkeypatch.setattr(
        translate,
        "_request_batch",
        lambda *_args: {
            2: {"display_text": "expected", "tts_text": "expected"},
            1: {"display_text": "neighbor", "tts_text": "neighbor"},
        },
    )
    monkeypatch.setattr(translate.time, "sleep", lambda *_args: None)

    with pytest.raises(RuntimeError, match=r"unexpected ids \[1\]"):
        translate._translate_one_batch(
            {"model": "fake"},
            [],
            "prompt",
            [(2, _sub("active"))],
            1,
        )


def test_changed_context_invalidates_complete_translation_cache(tmp_path, monkeypatch):
    subs = _SavableSubs([_sub("source", 0, 2000)])
    args = _translation_args()
    cached_context = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "summary": "old summary",
        "terms": [],
        "provenance": {"prompt_policy_version": PROMPT_POLICY_VERSION},
    }
    changed_context = {**cached_context, "summary": "new summary"}
    (tmp_path / "translations_zh.json").write_text(
        json.dumps({"0": {"display_text": "stale", "tts_text": "stale"}}),
        encoding="utf-8",
    )
    (tmp_path / "translations_zh.meta.json").write_text(
        json.dumps(translate._translation_cache_identity(subs, args, cached_context)),
        encoding="utf-8",
    )
    monkeypatch.setattr(translate, "apply_translations_to_subs", lambda *_args: None)
    monkeypatch.setattr(translate, "apply_ass_style", lambda *_args: None)
    monkeypatch.setattr(
        translate,
        "load_model_config",
        lambda _args: {"model": "fake-model", "api_key": "unused", "api_base": "unused"},
    )
    monkeypatch.setattr(
        translate,
        "prepare_translation_context",
        lambda *_args: (changed_context, tmp_path / "context.json", []),
    )
    monkeypatch.setattr(
        translate,
        "_translate_one_batch",
        lambda _config, _messages, _prompt, batch, _number: {
            idx: {"display_text": "fresh", "tts_text": "fresh"} for idx, _sub in batch
        },
    )

    _ass_path, _returned_subs, translations, _context_info = translate.translate_subtitles(
        subs, tmp_path, args
    )

    assert translations[0]["display_text"] == "fresh"
    assert len(list(tmp_path.glob("translations_zh.json.*.bak"))) == 1
    assert len(list(tmp_path.glob("translations_zh.meta.json.*.bak"))) == 1


def test_translate_subtitles_persists_timing_report_and_exposes_status_summary(
    tmp_path, monkeypatch
):
    subs = _SavableSubs(
        [
            _sub("source zero", 250, 2250),
            _sub("source one", 2500, 4500),
        ]
    )
    before = [(sub.start, sub.end) for sub in subs]
    status_path = tmp_path / "status.json"
    args = _translation_args(status=str(status_path), target_language="English")
    context = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "summary": "summary",
        "terms": [],
        "provenance": {"prompt_policy_version": PROMPT_POLICY_VERSION},
    }
    context_path = tmp_path / "translation_context_english.json"
    monkeypatch.setattr(translate, "apply_translations_to_subs", lambda *_args: None)
    monkeypatch.setattr(translate, "apply_ass_style", lambda *_args: None)
    monkeypatch.setattr(
        translate,
        "load_model_config",
        lambda _args: {"model": "fake-model", "api_key": "unused", "api_base": "unused"},
    )
    monkeypatch.setattr(
        translate,
        "prepare_translation_context",
        lambda *_args: (context, context_path, ["context warning"]),
    )
    monkeypatch.setattr(
        translate,
        "_translate_one_batch",
        lambda _config, _messages, _prompt, batch, _number: {
            0: {"display_text": "one two three four", "tts_text": "one two three four"},
            1: {
                "display_text": "one two three four five six",
                "tts_text": "one two three four five six",
            },
        },
    )

    ass_path, returned_subs, translations, context_info = translate.translate_subtitles(
        subs, tmp_path, args
    )

    timing_path = tmp_path / "translation_timing_risks_english.json"
    assert ass_path == str(tmp_path / "subtitles_english_target.ass")
    assert returned_subs is subs
    assert translations[0]["display_text"] == "one two three four"
    assert [(sub.start, sub.end) for sub in subs] == before
    assert [item["risk"] for item in json.loads(timing_path.read_text(encoding="utf-8"))] == [
        "normal",
        "critical",
    ]
    assert context_info == {
        "translation_context_path": str(context_path),
        "timing_risks_path": str(timing_path),
        "warnings": ["context warning"],
        "timing_counts": {"normal": 1, "warning": 0, "critical": 1},
        "max_required_speed_ratio": 1.5,
    }
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["timing_counts"] == {"normal": 1, "warning": 0, "critical": 1}
    assert status["max_required_speed_ratio"] == 1.5
    assert status["timing_risks_path"] == str(timing_path)
    cache_meta = json.loads(
        (tmp_path / "translations_english.meta.json").read_text(encoding="utf-8")
    )
    assert set(cache_meta) == {
        "content_kind",
        "context_schema_version",
        "translation_context_hash",
        "terms_file_hash",
        "prompt_policy_version",
        "source_hash",
        "target_language",
        "translation_model",
    }


def test_translation_only_orphan_is_reset_before_metadata_and_never_accepted_next_run(
    tmp_path, monkeypatch
):
    subs = _SavableSubs([_sub("current source", 0, 1000)])
    args = _translation_args()
    stale = {"0": {"display_text": "stale translation", "tts_text": "stale translation"}}
    translations_path = tmp_path / "translations_zh.json"
    translations_path.write_text(json.dumps(stale), encoding="utf-8")

    def no_model(_args):
        raise RuntimeError("no translation model")

    monkeypatch.setattr(translate, "load_model_config", no_model)
    monkeypatch.setattr(
        translate,
        "apply_translations_to_subs",
        lambda *_args: pytest.fail("orphaned translations must not be applied"),
    )

    first = translate.translate_subtitles(subs, tmp_path, args)
    second = translate.translate_subtitles(subs, tmp_path, args)

    assert first[:3] == (None, None, None)
    assert second[:3] == (None, None, None)
    assert json.loads(translations_path.read_text(encoding="utf-8")) == {}
    backups = list(tmp_path.glob("translations_zh.json.*.bak"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == stale
    current_meta = json.loads(
        (tmp_path / "translations_zh.meta.json").read_text(encoding="utf-8")
    )
    assert current_meta["source_hash"] == source_hash(subs)


def test_metadata_only_orphan_is_backed_up_and_paired_with_empty_checkpoint(
    tmp_path, monkeypatch
):
    subs = _SavableSubs([_sub("current source", 0, 1000)])
    args = _translation_args()
    stale_meta = {
        "source_hash": "stale",
        "target_language": "Chinese",
        "translation_model": "fake-model",
    }
    metadata_path = tmp_path / "translations_zh.meta.json"
    metadata_path.write_text(json.dumps(stale_meta), encoding="utf-8")

    def no_model(_args):
        raise RuntimeError("no translation model")

    monkeypatch.setattr(translate, "load_model_config", no_model)

    result = translate.translate_subtitles(subs, tmp_path, args)

    assert result[:3] == (None, None, None)
    backups = list(tmp_path.glob("translations_zh.meta.json.*.bak"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == stale_meta
    assert json.loads((tmp_path / "translations_zh.json").read_text(encoding="utf-8")) == {}
    current_meta = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert current_meta["source_hash"] == source_hash(subs)
    assert current_meta != stale_meta
