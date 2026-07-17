"""TTS 引擎注册与抽象层。

使用方只需:
  engine = get_engine("qwen3-tts")
  engine.synthesize(text, ref_audio, ref_text, output_path)

新增后端只需实现 TTSBackend 协议并在 _REGISTRY 中注册。
"""

from abc import ABC, abstractmethod
from pathlib import Path


class TTSBackend(ABC):
    """TTS 引擎抽象接口。"""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def synthesize(
        self, text: str, ref_audio: str, ref_text: str, output_path: str,
        hf_offline: bool = False, **kwargs,
    ):
        """生成 TTS 音频并写入 output_path。"""
        ...


_REGISTRY: dict[str, type[TTSBackend]] = {}


def register(name: str, cls: type[TTSBackend]):
    _REGISTRY[name] = cls


def get_engine(name: str) -> TTSBackend:
    if name == "none":
        return _NoopBackend()
    if name not in _REGISTRY:
        modules = {
            "qwen3-tts": ".tts_qwen3_mlx",
            "f5-mlx": ".tts_f5_mlx",
        }
        module = modules.get(name)
        if module:
            import importlib
            importlib.import_module(module, package=__package__)
    cls = _REGISTRY.get(name)
    if cls is None:
        raise RuntimeError(f"Unknown TTS engine: {name}. Available: {list(_REGISTRY)}")
    return cls()


class _NoopBackend(TTSBackend):
    @property
    def name(self):
        return "none"

    def synthesize(self, text, ref_audio, ref_text, output_path, hf_offline=False, **kwargs):
        raise RuntimeError("TTS engine is disabled.")
