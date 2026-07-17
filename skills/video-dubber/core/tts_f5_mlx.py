import os
import sys
from pathlib import Path

from .media import run
from .tts_register import TTSBackend, register


class F5MlxBackend(TTSBackend):
    """F5-TTS via MLX (Apple Silicon) 后端。"""

    @property
    def name(self):
        return "f5-mlx"

    def synthesize(self, text, ref_audio, ref_text, output_path, hf_offline=False, **kwargs):
        out = Path(output_path)
        if out.exists():
            return str(out)

        env = os.environ.copy()
        if hf_offline:
            env["HF_HUB_OFFLINE"] = "1"

        command = [
            sys.executable, "-m", "f5_tts_mlx.generate",
            "--text", text,
            "--ref-audio", ref_audio,
            "--ref-text", ref_text,
            "--output", output_path,
        ]
        run(command, "TTS", env=env)
        return str(out)


register("f5-mlx", F5MlxBackend)
