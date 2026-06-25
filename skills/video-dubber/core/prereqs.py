import os
import shutil
from pathlib import Path

from .media import FFMPEG, FFPROBE


SKILL_DIR = Path(__file__).resolve().parents[1]
ASSET_FONT = SKILL_DIR / "assets" / "fonts" / "HiraginoSansGB.ttc"


def resolve_font(font_file=None):
    candidates = []
    if font_file:
        candidates.append(Path(font_file))
    candidates.extend([
        ASSET_FONT,
        Path("/System/Library/Fonts/STHeiti Light.ttc"),
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    ])
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def check_disk(path, min_free_gb):
    free_gb = shutil.disk_usage(path).free / (1024 ** 3)
    if free_gb < min_free_gb:
        raise RuntimeError(
            f"Not enough free disk space: {free_gb:.2f}GB available, "
            f"{min_free_gb:.2f}GB required. Clean caches or use --no-segments."
        )
    return free_gb


def setup_runtime(job_dir, tmp_dir=None):
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)

    tmp = Path(tmp_dir) if tmp_dir else job_dir / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)

    cache_dir = job_dir / ".cache"
    mpl_dir = job_dir / ".mpl"
    cache_dir.mkdir(parents=True, exist_ok=True)
    mpl_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("TMPDIR", str(tmp))
    os.environ.setdefault("TEMP", str(tmp))
    os.environ.setdefault("TMP", str(tmp))
    os.environ.setdefault("MPLCONFIGDIR", str(mpl_dir))
    os.environ.setdefault("XDG_CACHE_HOME", str(cache_dir))
    os.environ.setdefault("HF_HOME", str(cache_dir / "huggingface"))
    os.environ["FFMPEG_BINARY"] = FFMPEG
    os.environ["FFPROBE_BINARY"] = FFPROBE
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ["PATH"] = f"{Path(FFMPEG).parent}:{os.environ.get('PATH', '')}"
    return tmp


def verify_tools(url):
    missing = []
    for tool in (FFMPEG, FFPROBE):
        if not Path(tool).exists() and not shutil.which(tool):
            missing.append(tool)
    if url and not shutil.which("yt-dlp"):
        missing.append("yt-dlp")
    if missing:
        raise RuntimeError(f"Missing required tools: {', '.join(missing)}")
