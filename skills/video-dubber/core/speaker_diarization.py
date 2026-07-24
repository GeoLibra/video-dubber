"""WeSpeaker diarization and per-speaker voice-cloning reference selection."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections import defaultdict
from pathlib import Path

import numpy as np

from .job_runtime import atomic_write_json
from .media import FFMPEG, run
from .subtitle import get_sub_source_text, normalize_spaces


TRANSITION_RE = re.compile(
    r"\b(hand(?:ing)? (?:it )?off|first speaker|next speaker|moderator|"
    r"welcome (?:to|our)|please welcome|applause|over to you|thank you[, ]+\w+)\b",
    re.IGNORECASE,
)


def _file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _overlap(start, end, turn):
    return max(0.0, min(end, turn["end"]) - max(start, turn["start"]))


def assign_speakers(subs, turns):
    """Assign each subtitle to its dominant WeSpeaker turn."""
    if not turns:
        raise RuntimeError("WeSpeaker found no speech.")
    raw_labels = sorted({turn["label"] for turn in turns})
    first_seen = {
        label: min(turn["start"] for turn in turns if turn["label"] == label)
        for label in raw_labels
    }
    names = {
        label: f"speaker_{idx:02d}"
        for idx, label in enumerate(sorted(raw_labels, key=first_seen.get))
    }
    assignments, purity = {}, {}
    for idx, sub in enumerate(subs):
        start, end = sub.start / 1000.0, sub.end / 1000.0
        by_label = defaultdict(float)
        for turn in turns:
            by_label[turn["label"]] += _overlap(start, end, turn)
        speech = sum(by_label.values())
        if speech:
            label, best = max(by_label.items(), key=lambda item: item[1])
            item_purity = best / speech
        else:
            middle = (start + end) / 2
            nearest = min(
                turns,
                key=lambda turn: min(abs(middle - turn["start"]), abs(middle - turn["end"])),
            )
            label, item_purity = nearest["label"], 0.0
        assignments[str(idx)] = names[label]
        purity[str(idx)] = round(item_purity, 4)
    return assignments, purity


def _audio_metrics(y, sr):
    import librosa

    intervals = librosa.effects.split(y, top_db=30)
    voiced_samples = sum(end - start for start, end in intervals)
    gaps = [
        max(0.0, (intervals[idx][0] - intervals[idx - 1][1]) / sr)
        for idx in range(1, len(intervals))
    ]
    rms = float(np.sqrt(np.mean(np.square(y)))) if len(y) else 0.0
    return {
        "duration": len(y) / sr,
        "voiced_ratio": float(voiced_samples / max(1, len(y))),
        "max_internal_silence": float(max(gaps, default=0.0)),
        "rms": rms,
    }


def _run_wespeaker(audio_path, out_dir, model, speaker_count):
    skill_dir = Path(__file__).resolve().parent.parent
    python = skill_dir / ".venv-speaker" / "bin" / "python"
    helper = skill_dir / "scripts" / "run_wespeaker_diarization.py"
    if not python.exists():
        raise RuntimeError(
            f"WeSpeaker environment is missing: {python}. Run scripts/setup_env.sh."
        )
    raw_path = Path(out_dir) / "speaker_diarization_wespeaker_raw.json"
    if raw_path.exists():
        cached = json.loads(raw_path.read_text(encoding="utf-8"))
        if (
            cached.get("model") == model
            and int(cached.get("speaker_count", 0)) == int(speaker_count or 0)
            and cached.get("turns")
        ):
            return cached["turns"], raw_path
    env = os.environ.copy()
    env.setdefault(
        "WESPEAKER_HOME",
        str(skill_dir / ".agent" / "models" / "wespeaker"),
    )
    command = [
        str(python),
        str(helper),
        "--audio", str(audio_path),
        "--output", str(raw_path),
        "--model", model,
    ]
    if speaker_count:
        command.extend(["--speaker-count", str(speaker_count)])
    subprocess.run(command, check=True, env=env)
    return json.loads(raw_path.read_text(encoding="utf-8"))["turns"], raw_path


def prepare_speaker_references(
    subs,
    audio_path,
    out_dir,
    *,
    speaker_count=0,
    max_speakers=4,
    model="english",
):
    """Diarize speakers and extract one clean, transcript-matched reference each."""
    import librosa

    del max_speakers  # WeSpeaker estimates automatically unless count is supplied.
    out_dir = Path(out_dir)
    report_path = out_dir / "speaker_diarization.json"
    identity = {
        "audio_hash": _file_hash(audio_path),
        "subtitle_hash": hashlib.sha256(
            "\n".join(
                f"{sub.start}|{sub.end}|{get_sub_source_text(sub)}" for sub in subs
            ).encode("utf-8")
        ).hexdigest(),
        "backend": "wespeaker",
        "model": model,
        "speaker_count": int(speaker_count or 0),
        "schema_version": 3,
    }
    if report_path.exists():
        cached = json.loads(report_path.read_text(encoding="utf-8"))
        if cached.get("identity") == identity:
            refs = cached.get("references", {})
            if refs and all(Path(item["audio"]).exists() for item in refs.values()):
                for idx, sub in enumerate(subs):
                    sub.speaker_id = cached["assignments"][str(idx)]
                return refs, str(report_path)

    turns, raw_path = _run_wespeaker(audio_path, out_dir, model, speaker_count)
    assignments, purity = assign_speakers(subs, turns)
    for idx, sub in enumerate(subs):
        sub.speaker_id = assignments[str(idx)]

    audio, sr = librosa.load(audio_path, sr=16000, mono=True)
    metrics = {}
    for idx, sub in enumerate(subs):
        start = max(0, round(sub.start * sr / 1000))
        end = min(len(audio), round(sub.end * sr / 1000))
        metrics[idx] = _audio_metrics(audio[start:end], sr)

    references, selected, candidate_report = {}, [], []
    speakers = list(dict.fromkeys(assignments.values()))
    for speaker_id in speakers:
        candidates = []
        for idx, sub in enumerate(subs):
            if assignments[str(idx)] != speaker_id:
                continue
            item = metrics[idx]
            text = normalize_spaces(get_sub_source_text(sub))
            transition = bool(TRANSITION_RE.search(text))
            item_purity = purity[str(idx)]
            score = float(
                (1.0 - item_purity) * 15.0
                + abs(item["duration"] - 7.0) * 0.18
                + max(0.0, 0.72 - item["voiced_ratio"]) * 6.0
                + item["max_internal_silence"] * 4.0
                + (30.0 if transition else 0.0)
            )
            eligible = bool(
                3.0 <= item["duration"] <= 12.0
                and item_purity >= 0.90
                and item["voiced_ratio"] >= 0.55
                and item["max_internal_silence"] <= 0.55
                and not transition
                and len(text.split()) >= 5
            )
            row = {
                "speaker_id": speaker_id,
                "source_index": idx,
                "eligible": eligible,
                "score": round(score, 4),
                "purity": item_purity,
                **item,
                "text": text,
            }
            candidate_report.append(row)
            candidates.append((not eligible, score, idx, text, item, item_purity))
        if not candidates:
            raise RuntimeError(f"WeSpeaker found no subtitle candidates for {speaker_id}.")
        not_eligible, score, idx, text, item, item_purity = min(candidates)
        if not_eligible:
            raise RuntimeError(
                f"No clean single-speaker reference found for {speaker_id}; "
                "refusing to clone from mixed/noisy audio."
            )
        ref_path = out_dir / f"{speaker_id}_ref.wav"
        start_sec = subs[idx].start / 1000.0
        duration_sec = (subs[idx].end - subs[idx].start) / 1000.0
        run(
            [
                FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(audio_path), "-ss", f"{start_sec:.3f}",
                "-t", f"{duration_sec:.3f}", "-acodec", "pcm_s16le",
                "-ar", "24000", "-ac", "1", str(ref_path),
            ],
            "SPEAKER_REF",
        )
        references[speaker_id] = {
            "audio": str(ref_path),
            "text": text,
            "source_index": idx,
            "start_ms": subs[idx].start,
            "end_ms": subs[idx].end,
            "purity": item_purity,
        }
        selected.append(
            {
                "speaker_id": speaker_id,
                "source_index": idx,
                "score": round(score, 4),
                "purity": item_purity,
                **item,
                "text": text,
            }
        )

    report = {
        "identity": identity,
        "method": "wespeaker_spectral_diarization",
        "speaker_count": len(references),
        "raw_turns_path": str(raw_path),
        "turns": turns,
        "assignments": assignments,
        "assignment_purity": purity,
        "references": references,
        "selected_candidates": selected,
        "candidates": candidate_report,
    }
    atomic_write_json(report_path, report)
    return references, str(report_path)
