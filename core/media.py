import json
import shutil
import subprocess
from pathlib import Path


FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"


def run(cmd, step=None, capture=False, env=None):
    if step:
        parts = " ".join(str(x) for x in cmd[:6])
        print(f"[{step}] {parts}" + (" ..." if len(cmd) > 6 else ""), flush=True)
    result = subprocess.run(
        [str(x) for x in cmd],
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        env=env,
    )
    return result.stdout if capture else ""


def probe_duration(path):
    out = run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path],
        capture=True,
    ).strip()
    return float(out)


def probe_streams(path):
    out = run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=index,codec_type,duration",
            "-of",
            "json",
            path,
        ],
        capture=True,
    )
    return json.loads(out)


def escape_filter_path(path):
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
