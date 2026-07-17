"""Qwen3-TTS voice cloning backend for Apple Silicon via mlx-audio."""

from __future__ import annotations

import os
from pathlib import Path

from .tts_register import TTSBackend, register


LANG_CODES = {
    "Chinese": "chinese",
    "Japanese": "japanese",
    "Korean": "korean",
    "English": "english",
}


def resolve_model_path(explicit: str | None = None, hf_offline: bool = False) -> str:
    configured = explicit or os.environ.get("QWEN3_TTS_MODEL")
    if configured:
        path = Path(configured).expanduser()
        if path.exists():
            return str(path.resolve())
        if not hf_offline and "/" in configured:
            return configured
        raise FileNotFoundError(f"Qwen3-TTS model not found: {configured}")

    home = Path.home()
    candidates = [
        home / "myproject/blender-video-skills/.worktrees/video-highlight/assets/video-highlight-norway-england/commentary_qwen3/models/qwen3/1.7b_bf16",
        home / "myproject/video-dubber/.agent/models/Qwen3-TTS-12Hz-1.7B-Base-bf16",
        home / ".cache/qwen3-tts/1.7b_bf16",
    ]
    for path in candidates:
        if (path / "config.json").exists():
            return str(path.resolve())
    raise FileNotFoundError(
        "Qwen3-TTS model was not found. Pass --qwen3-model PATH or set "
        "QWEN3_TTS_MODEL. The skill does not silently redownload multi-GB weights."
    )


class Qwen3TTSBackend(TTSBackend):
    """Load Qwen3-TTS once and reuse it for every subtitle segment."""

    def __init__(self):
        self._model = None
        self._model_path = None

    @property
    def name(self):
        return "qwen3-tts"

    def _load(self, model_path: str):
        if self._model is None or self._model_path != model_path:
            from mlx_audio.tts.utils import load_model
            self._model = load_model(Path(model_path) if Path(model_path).exists() else model_path)
            self._model_path = model_path
        return self._model

    def synthesize(
        self, text, ref_audio, ref_text, output_path, hf_offline=False,
        target_language="Chinese", model_path=None, **kwargs,
    ):
        out = Path(output_path)
        if out.exists() and out.stat().st_size > 44:
            return str(out)
        if hf_offline:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
        resolved = resolve_model_path(model_path, hf_offline=hf_offline)
        model = self._load(resolved)

        import numpy as np
        import soundfile as sf

        results = list(model.generate(
            text=text,
            ref_audio=str(ref_audio),
            ref_text=ref_text,
            lang_code=LANG_CODES.get(target_language, str(target_language).lower()),
            temperature=0.8,
            top_k=40,
            top_p=0.95,
            repetition_penalty=1.08,
        ))
        if not results:
            raise RuntimeError("Qwen3-TTS returned no audio.")
        audio = np.concatenate([
            np.asarray(item.audio, dtype=np.float32).reshape(-1) for item in results
        ])
        sf.write(out, audio, model.sample_rate, subtype="PCM_16")
        return str(out)


register("qwen3-tts", Qwen3TTSBackend)
