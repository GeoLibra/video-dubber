from __future__ import annotations

import json
import sys
from pathlib import Path

import pysubs2
import pytest

import core.translation_context as translation_context
import core.translate as translate
import core.verifier as verifier
from core.config import build_config
from scripts import run_pipeline


def _parse(monkeypatch, tmp_path, *extra):
    input_video = tmp_path / "input.mp4"
    input_video.write_bytes(b"local video")
    argv = [
        "run_pipeline.py",
        "--input-video",
        str(input_video),
        "--status",
        str(tmp_path / "status.json"),
        "--log",
        str(tmp_path / "run.log"),
        *extra,
    ]
    monkeypatch.setattr(sys, "argv", argv)
    return run_pipeline.parse_args()


def test_translation_context_cli_defaults(monkeypatch, tmp_path):
    args = _parse(monkeypatch, tmp_path)

    assert args.terms_file is None
    assert args.translation_context == "auto"
    assert args.context_char_budget == 8000
    assert args.context_neighbor_lines == 2
    assert args.timing_risk_estimator is True


def test_translation_context_profile_overrides_defaults_but_not_explicit_cli(
    monkeypatch, tmp_path
):
    terms_path = tmp_path / "terms.yaml"
    terms_path.write_text("terms: []\n", encoding="utf-8")
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        "translation:\n"
        f"  terms_file: {terms_path}\n"
        "  context: 'off'\n"
        "  context_char_budget: 4321\n"
        "  context_neighbor_lines: 5\n"
        "  timing_risk_estimator: false\n",
        encoding="utf-8",
    )

    default_args = _parse(monkeypatch, tmp_path)
    profile_config = build_config(default_args, overlay_path=profile_path)
    assert {
        key: profile_config["translation"][key]
        for key in (
            "terms_file",
            "context",
            "context_char_budget",
            "context_neighbor_lines",
            "timing_risk_estimator",
        )
    } == {
        "terms_file": str(terms_path),
        "context": "off",
        "context_char_budget": 4321,
        "context_neighbor_lines": 5,
        "timing_risk_estimator": False,
    }

    explicit_args = _parse(
        monkeypatch,
        tmp_path,
        "--translation-context",
        "auto",
        "--context-char-budget",
        "9000",
        "--context-neighbor-lines",
        "1",
        "--timing-risk-estimator",
    )
    explicit_config = build_config(explicit_args, overlay_path=profile_path)
    assert explicit_config["translation"]["context"] == "auto"
    assert explicit_config["translation"]["context_char_budget"] == 9000
    assert explicit_config["translation"]["context_neighbor_lines"] == 1
    assert explicit_config["translation"]["timing_risk_estimator"] is True


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        (("--context-char-budget", "0"), "--context-char-budget must be > 0"),
        (("--context-neighbor-lines", "-1"), "--context-neighbor-lines must be >= 0"),
        (("--terms-file", "missing.yaml"), "--terms-file must exist"),
    ],
)
def test_translation_context_cli_validation(monkeypatch, tmp_path, capsys, flags, message):
    with pytest.raises(SystemExit):
        _parse(monkeypatch, tmp_path, *flags)

    assert message in capsys.readouterr().err


def _source_subtitles():
    subs = pysubs2.SSAFile()
    subs.events = [
        pysubs2.SSAEvent(start=250, end=2250, text="source zero"),
        pysubs2.SSAEvent(start=2500, end=4500, text="source one"),
    ]
    return subs


def _patch_media_pipeline(monkeypatch, tmp_path, subs):
    video_path = tmp_path / "downloaded.mp4"
    audio_path = tmp_path / "downloaded.wav"
    video_path.write_bytes(b"video")
    audio_path.write_bytes(b"audio")
    monkeypatch.setattr(run_pipeline, "load_dotenv", lambda *_args: None)
    monkeypatch.setattr(run_pipeline, "preflight", lambda *_args: None)
    monkeypatch.setattr(run_pipeline, "setup_runtime", lambda *_args, **_kwargs: tmp_path)
    monkeypatch.setattr(
        run_pipeline,
        "download_video",
        lambda *_args, **_kwargs: (str(video_path), str(audio_path)),
    )
    monkeypatch.setattr(run_pipeline, "probe_duration", lambda _path: 5.0)
    monkeypatch.setattr(
        run_pipeline,
        "separate_audio",
        lambda audio, _job, _skip: (audio, str(tmp_path / "background.wav")),
    )
    monkeypatch.setattr(run_pipeline, "transcribe_audio", lambda *_args: subs)
    return video_path, audio_path


def test_context_generation_waits_for_translation_confirmation(monkeypatch, tmp_path):
    args = _parse(monkeypatch, tmp_path)
    monkeypatch.setattr(run_pipeline, "parse_args", lambda: args)
    _patch_media_pipeline(monkeypatch, tmp_path, _source_subtitles())
    requests = []
    monkeypatch.setattr(
        translation_context,
        "_request_context",
        lambda *_args: requests.append(True),
    )

    with pytest.raises(SystemExit) as exited:
        run_pipeline.main()

    assert exited.value.code == 0
    assert requests == []
    assert json.loads(Path(args.status).read_text(encoding="utf-8"))["status"] == (
        "confirm_translation"
    )
    assert not list(tmp_path.glob("translation_context_*.json"))


def test_awaiting_manual_translation_status_keeps_context_metadata(monkeypatch, tmp_path):
    args = _parse(monkeypatch, tmp_path, "--confirm-translation")
    monkeypatch.setattr(run_pipeline, "parse_args", lambda: args)
    _patch_media_pipeline(monkeypatch, tmp_path, _source_subtitles())
    monkeypatch.setattr(run_pipeline, "load_dotenv", lambda *_args: None)
    monkeypatch.setattr(
        translate,
        "load_model_config",
        lambda _args: (_ for _ in ()).throw(RuntimeError("no model configured")),
    )

    with pytest.raises(SystemExit) as exited:
        run_pipeline.main()

    assert exited.value.code == 0
    status = json.loads(Path(args.status).read_text(encoding="utf-8"))
    assert status["status"] == "awaiting_translation"
    assert status["translation_context_path"] == str(
        tmp_path / "translation_context_zh.json"
    )
    assert status["timing_risks_path"] is None
    assert status["warnings"] == []
    assert status["timing_counts"] == {
        "normal": 0,
        "warning": 0,
        "critical": 0,
    }


@pytest.mark.parametrize(
    ("context_mode", "timing_enabled"),
    [("auto", True), ("off", False)],
)
def test_mock_pipeline_propagates_context_and_preserves_strict_sync_and_timestamps(
    monkeypatch, tmp_path, context_mode, timing_enabled
):
    flags = ["--confirm-translation", "--translation-context", context_mode]
    if not timing_enabled:
        flags.append("--no-timing-risk-estimator")
    args = _parse(monkeypatch, tmp_path, *flags)
    monkeypatch.setattr(run_pipeline, "parse_args", lambda: args)
    subs = _source_subtitles()
    _patch_media_pipeline(monkeypatch, tmp_path, subs)
    canonical_before_translation = []
    real_translate_subtitles = run_pipeline.translate_subtitles

    def tracked_translate_subtitles(*call_args, **call_kwargs):
        canonical_before_translation.append(
            (tmp_path / "canonical_source.srt").read_bytes()
        )
        return real_translate_subtitles(*call_args, **call_kwargs)

    monkeypatch.setattr(
        run_pipeline, "translate_subtitles", tracked_translate_subtitles
    )

    secret = "integration-secret-api-key"
    monkeypatch.setattr(
        translate,
        "load_model_config",
        lambda _args: {
            "model": "fake-model",
            "api_key": secret,
            "api_base": "https://unused.invalid/v1",
        },
    )
    context_requests = []
    canonical_at_context = []

    def fake_context_request(_config, _prompt):
        context_requests.append(True)
        canonical_at_context.append((tmp_path / "canonical_source.srt").read_bytes())
        return {
            "summary": "A local integration fixture.",
            "terms": [{"source": "source", "target": "译文", "note": "fixture"}],
        }

    monkeypatch.setattr(translation_context, "_request_context", fake_context_request)

    class FakeCompletions:
        @staticmethod
        def create(**_kwargs):
            content = json.dumps(
                {
                    "translations": [
                        {
                            "id": 0,
                            "display_text": "译文零",
                            "tts_text": "must not survive",
                        },
                        {
                            "id": 1,
                            "display_text": "译文一",
                            "tts_text": "must not survive",
                        },
                    ]
                }
            )
            message = type("Message", (), {"content": content})()
            choice = type("Choice", (), {"message": message})()
            return type("Response", (), {"choices": [choice]})()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            assert kwargs["api_key"] == secret
            self.chat = type("Chat", (), {"completions": FakeCompletions()})()

        def close(self):
            return None

    monkeypatch.setattr(translate, "OpenAI", FakeOpenAI)

    tts_checks = []

    def fake_tts(translated_subs, *_args):
        tts_checks.extend(
            (sub.display_text.replace("\\N", " "), sub.tts_text) for sub in translated_subs
        )
        tts_path = tmp_path / "tts.wav"
        tts_path.write_bytes(b"tts")
        return str(tts_path), [{"index": 0, "text": "译文零", "raw_ms": 1000}]

    monkeypatch.setattr(
        run_pipeline,
        "prepare_reference_audio",
        lambda *_args: (str(tmp_path / "reference.wav"), "reference"),
    )
    monkeypatch.setattr(run_pipeline, "generate_and_merge", fake_tts)

    def fake_synthesis(*_args):
        original = tmp_path / "original.mp4"
        cloned = tmp_path / "cloned.mp4"
        original.write_bytes(b"original")
        cloned.write_bytes(b"cloned")
        return str(original), str(cloned)

    monkeypatch.setattr(run_pipeline, "synthesize_videos", fake_synthesis)
    monkeypatch.setattr(verifier, "probe_streams", lambda _path: {"duration": 5.0})

    run_pipeline.main()

    status = json.loads(Path(args.status).read_text(encoding="utf-8"))
    verification = json.loads(
        Path(status["verification_report"]).read_text(encoding="utf-8")
    )
    context_path = tmp_path / "translation_context_zh.json"
    timing_path = tmp_path / "translation_timing_risks_zh.json"

    assert context_requests == ([True] if context_mode == "auto" else [])
    assert tts_checks == [("译文零", "译文零"), ("译文一", "译文一")]
    assert canonical_at_context == (
        canonical_before_translation
        if context_mode == "auto"
        else []
    )
    assert canonical_before_translation == [
        (tmp_path / "canonical_source.srt").read_bytes()
    ]
    assert status["translation_context_path"] == str(context_path)
    assert verification["translation_context_path"] == str(context_path)
    assert status["timing_risks_path"] == (str(timing_path) if timing_enabled else None)
    assert verification["timing_risks_path"] == (
        str(timing_path) if timing_enabled else None
    )
    assert status["warnings"] == verification["warnings"]
    assert status["timing_counts"] == verification["timing_counts"]
    assert timing_path.exists() is timing_enabled
    assert all(left == right for left, right in tts_checks)
    assert [(event.start, event.end) for event in subs] == [(250, 2250), (2500, 4500)]
    for artifact in tmp_path.glob("*.json"):
        assert secret not in artifact.read_text(encoding="utf-8")
