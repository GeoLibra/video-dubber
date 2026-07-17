from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pysubs2
import soundfile as sf

from core import audio_builder
from core.tts_qwen3_mlx import Qwen3TTSBackend, resolve_model_path
from core.tts_register import TTSBackend, get_engine, register
from scripts.run_pipeline import parse_args


class FakeQwenModel:
    sample_rate = 24000

    def __init__(self):
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        yield SimpleNamespace(audio=np.zeros(2400, dtype=np.float32))


class FakeBackend(TTSBackend):
    @property
    def name(self):
        return "qwen3-tts"

    def synthesize(self, text, ref_audio, ref_text, output_path, **kwargs):
        sf.write(output_path, np.zeros(2400, dtype=np.float32), 24000)
        return output_path


class Qwen3BackendTest(unittest.TestCase):
    def test_cli_defaults_to_qwen3_and_deepseek_flash_alias(self):
        argv = ["run_pipeline.py", "--input-video", "input.mp4", "--status", "status.json", "--log", "run.log"]
        with patch("sys.argv", argv):
            args = parse_args()
        self.assertEqual(args.tts_engine, "qwen3-tts")
        self.assertEqual(args.translation_model, "deepseek")

    def test_registry_and_known_local_model(self):
        self.assertEqual(get_engine("qwen3-tts").name, "qwen3-tts")
        self.assertTrue(Path(resolve_model_path()).is_dir())

    def test_language_code_is_forwarded(self):
        with tempfile.TemporaryDirectory() as td:
            backend = Qwen3TTSBackend()
            model = FakeQwenModel()
            backend._load = lambda _path: model
            out = Path(td) / "ja.wav"
            backend.synthesize(
                "こんにちは", "ref.wav", "hello", str(out),
                target_language="Japanese", model_path=td,
            )
            self.assertEqual(model.calls[0]["lang_code"], "japanese")
            self.assertGreater(out.stat().st_size, 44)

    def test_pipeline_cache_and_outputs_are_engine_scoped(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ref = root / "ref.wav"
            sf.write(ref, np.zeros(2400, dtype=np.float32), 24000)
            subs = pysubs2.SSAFile()
            subs.events = [pysubs2.SSAEvent(start=0, end=500, text="テスト")]
            register("qwen3-tts", FakeBackend)
            args = SimpleNamespace(
                target_language="Japanese", tts_engine="qwen3-tts",
                qwen3_model=td, hf_offline=False, max_atempo=1.6,
                max_clip_ms=80, max_overhang_ms=450, no_segments=False,
            )
            merged, report = audio_builder.generate_and_merge(
                subs, root, str(ref), "hello", 1.0, args
            )
            self.assertTrue(merged.endswith("merged_tts_ja_qwen3tts.wav"))
            self.assertEqual(len(report), 1)
            meta = json.loads((root / "tts_alignment_report_ja_qwen3tts.meta.json").read_text())
            self.assertEqual(meta["tts_engine"], "qwen3-tts")
            self.assertEqual(Path(meta["tts_model"]).resolve(), root.resolve())


if __name__ == "__main__":
    unittest.main()
