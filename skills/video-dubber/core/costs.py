import time
from dataclasses import dataclass, field


@dataclass
class TaskMeter:
    """记录每个阶段的耗时和资源消耗，用于输出成本摘要。"""

    start_time: float = field(default_factory=time.time)
    phases: dict[str, float] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    synth_chars: int = 0
    audio_mins: float = 0.0
    _current_phase: str = ""

    def phase_start(self, name: str):
        self._current_phase = name
        self._phase_start = time.time()

    def phase_end(self):
        if self._current_phase and hasattr(self, "_phase_start"):
            elapsed = time.time() - self._phase_start
            self.phases[self._current_phase] = round(elapsed, 2)
            self._current_phase = ""

    def log_tokens(self, inp: int, out: int):
        self.input_tokens += inp
        self.output_tokens += out

    def log_tts(self, chars: int):
        self.synth_chars += chars

    def log_audio(self, minutes: float):
        self.audio_mins += minutes

    def report(self) -> dict:
        wall = round(time.time() - self.start_time, 2)
        return {
            "wall_clock_seconds": wall,
            "phase_timings_seconds": self.phases,
            "llm_input_tokens": self.input_tokens,
            "llm_output_tokens": self.output_tokens,
            "tts_char_count": self.synth_chars,
            "audio_minutes": round(self.audio_mins, 3),
        }
