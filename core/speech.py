import os
import shutil
from pathlib import Path

import pysubs2

from .media import FFMPEG, run
from .source_loader import find_platform_subtitle


def _normalize_riva_language_code(source_lang):
    lang = (source_lang or "en").strip()
    if lang.lower() == "multi":
        return "multi"
    return lang.split("-", 1)[0].lower()


def transcribe_audio_riva(audio_path, source_lang="en", config_type="word_time"):
    if not os.environ.get("NVIDIA_API_KEY"):
        raise RuntimeError("NVIDIA_API_KEY is missing")

    import riva.client

    auth = riva.client.Auth(
        use_ssl=True,
        uri="grpc.nvcf.nvidia.com:443",
        metadata_args=[
            ["function-id", "b702f636-f60c-4a3d-a6f4-f3568c13bd7d"],
            ["authorization", f"Bearer {os.environ['NVIDIA_API_KEY']}"],
        ],
        options=[
            ("grpc.max_receive_message_length", 256 * 1024 * 1024),
            ("grpc.max_send_message_length", 256 * 1024 * 1024),
        ],
    )
    service = riva.client.ASRService(auth)
    config = riva.client.RecognitionConfig(
        language_code=_normalize_riva_language_code(source_lang),
        max_alternatives=1,
        enable_automatic_punctuation=True,
        enable_word_time_offsets=config_type == "word_time",
    )
    with open(audio_path, "rb") as f:
        response = service.offline_recognize(f.read(), config)

    if config_type != "word_time":
        for result in response.results:
            if result.alternatives:
                return result.alternatives[0].transcript.strip()
        return ""

    subs = pysubs2.SSAFile()
    for result in response.results:
        if not result.alternatives:
            continue
        alt = result.alternatives[0]
        words = alt.words
        if not words:
            continue
        chunk = []
        for word in words:
            chunk.append(word)
            if len(chunk) >= 12 or word.word.endswith((".", "?", "!")):
                _append_word_chunk(subs, chunk)
                chunk = []
        if chunk:
            _append_word_chunk(subs, chunk)
    return subs


def _append_word_chunk(subs, words):
    st = int(words[0].start_time * 1000)
    et = int(words[-1].end_time * 1000)
    text = " ".join(w.word for w in words).strip()
    if text and et > st:
        subs.append(pysubs2.SSAEvent(start=st, end=et, text=text))


def transcribe_audio_local(audio_path, out_dir, whisper_model=None):
    srt_path = Path(out_dir) / "raw_audio.srt"
    if srt_path.exists():
        return pysubs2.load(str(srt_path), encoding="utf-8")

    whisper_cli = shutil.which("whisper-cli") or "/opt/homebrew/bin/whisper-cli"
    if Path(whisper_cli).exists() and whisper_model and Path(whisper_model).exists():
        output_base = str(Path(out_dir) / "raw_audio")
        run([whisper_cli, "-ng", "-m", whisper_model, "-f", audio_path, "-osrt", "-of", output_base], "ASR")
        return pysubs2.load(str(srt_path), encoding="utf-8")

    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        raise RuntimeError("Local ASR fallback needs either whisper-cli + --whisper-model or faster-whisper installed.") from exc

    model = WhisperModel("base", device="auto", compute_type="int8")
    segments, _info = model.transcribe(audio_path, word_timestamps=True)
    subs = pysubs2.SSAFile()
    for seg in segments:
        subs.append(pysubs2.SSAEvent(start=int(seg.start * 1000), end=int(seg.end * 1000), text=seg.text.strip()))
    subs.save(str(srt_path))
    return subs


def transcribe_audio(audio_path, out_dir, args):
    if args.source_srt:
        return pysubs2.load(args.source_srt, encoding="utf-8")

    srt_path = Path(out_dir) / "raw_audio.srt"
    if srt_path.exists():
        return pysubs2.load(str(srt_path), encoding="utf-8")

    platform_srt = find_platform_subtitle(out_dir)
    if platform_srt:
        print(f"[SUBTITLE] Using platform subtitle: {platform_srt.name}", flush=True)
        subs = pysubs2.load(str(platform_srt), encoding="utf-8")
        subs.save(str(srt_path))
        return subs

    try:
        print("[ASR] Trying NVIDIA Riva ASR", flush=True)
        subs = transcribe_audio_riva(audio_path, args.source_lang, config_type="word_time")
    except Exception as exc:
        print(f"[ASR] Riva unavailable; falling back locally: {exc}", flush=True)
        subs = transcribe_audio_local(audio_path, out_dir, args.whisper_model)

    subs.save(str(srt_path))
    return subs
