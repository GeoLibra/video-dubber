import json
import os
import platform
import shutil
import sys
from pathlib import Path

import pysubs2

from .media import FFMPEG, probe_duration, run
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


DEFAULT_MLX_WHISPER_MODEL = "mlx-community/whisper-large-v3-mlx"
DEFAULT_QWEN3_ASR_MLX_MODEL = "mlx-community/Qwen3-ASR-1.7B-8bit"
DEFAULT_QWEN3_ALIGNER_MLX_MODEL = "mlx-community/Qwen3-ForcedAligner-0.6B-8bit"



def _normalize_whisper_language(source_lang):
    lang = (source_lang or "").strip()
    if not lang or lang.lower() in {"auto", "multi"}:
        return None
    return lang.split("-", 1)[0].lower()


def _append_segment(subs, start_s, end_s, text):
    st = int(float(start_s) * 1000)
    et = int(float(end_s) * 1000)
    text = (text or "").strip()
    if text and et > st:
        subs.append(pysubs2.SSAEvent(start=st, end=et, text=text))


def transcribe_audio_mlx_whisper(audio_path, out_dir, args):
    srt_path = Path(out_dir) / "raw_audio.srt"
    if srt_path.exists():
        return pysubs2.load(str(srt_path), encoding="utf-8")

    cache_home = Path(__file__).resolve().parents[1] / ".agent" / "hf-cache"
    cache_home.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = os.environ.get("MLX_WHISPER_HF_HOME", str(cache_home))
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

    try:
        import mlx_whisper
    except Exception as exc:
        raise RuntimeError("mlx-whisper is not installed in the active environment.") from exc

    model = (
        getattr(args, "mlx_whisper_model", None)
        or os.environ.get("MLX_WHISPER_MODEL")
        or DEFAULT_MLX_WHISPER_MODEL
    )
    print(f"[ASR] Using mlx-whisper model: {model}", flush=True)
    result = mlx_whisper.transcribe(
        str(audio_path),
        path_or_hf_repo=model,
        language=_normalize_whisper_language(getattr(args, "source_lang", None)),
        word_timestamps=True,
    )

    subs = pysubs2.SSAFile()
    for seg in result.get("segments", []) or []:
        _append_segment(subs, seg.get("start", 0), seg.get("end", 0), seg.get("text", ""))

    if len(subs) == 0:
        text = (result.get("text") or "").strip()
        if text:
            from .media import probe_duration

            subs.append(
                pysubs2.SSAEvent(
                    start=0,
                    end=max(1000, int(probe_duration(audio_path) * 1000)),
                    text=text,
                )
            )

    if len(subs) == 0:
        raise RuntimeError("mlx-whisper returned no transcription text.")
    subs.save(str(srt_path))
    return subs


def transcribe_audio_whisper_cpp(audio_path, out_dir, whisper_model=None):
    srt_path = Path(out_dir) / "raw_audio.srt"
    if srt_path.exists():
        return pysubs2.load(str(srt_path), encoding="utf-8")

    whisper_cli = shutil.which("whisper-cli") or "/opt/homebrew/bin/whisper-cli"
    if Path(whisper_cli).exists() and whisper_model and Path(whisper_model).exists():
        output_base = str(Path(out_dir) / "raw_audio")
        run([whisper_cli, "-ng", "-m", whisper_model, "-f", audio_path, "-osrt", "-of", output_base], "ASR")
        return pysubs2.load(str(srt_path), encoding="utf-8")
    raise RuntimeError("whisper.cpp needs whisper-cli and --whisper-model pointing to an existing ggml model.")


def transcribe_audio_faster_whisper(audio_path, out_dir):
    srt_path = Path(out_dir) / "raw_audio.srt"
    if srt_path.exists():
        return pysubs2.load(str(srt_path), encoding="utf-8")
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        raise RuntimeError("faster-whisper is not installed.") from exc

    print("[ASR] Using final fallback: faster-whisper base int8", flush=True)
    model = WhisperModel("base", device="auto", compute_type="int8")
    segments, _info = model.transcribe(audio_path, word_timestamps=True)
    subs = pysubs2.SSAFile()
    for seg in segments:
        subs.append(pysubs2.SSAEvent(start=int(seg.start * 1000), end=int(seg.end * 1000), text=seg.text.strip()))
    subs.save(str(srt_path))
    return subs


def transcribe_audio_local(audio_path, out_dir, args):
    engine = getattr(args, "asr_engine", "auto")
    if engine == "qwen3-asr-mlx":
        return transcribe_audio_qwen3_mlx(audio_path, out_dir, args)
    if engine == "mlx-whisper":
        return transcribe_audio_mlx_whisper(audio_path, out_dir, args)
    if engine == "whisper":
        return transcribe_audio_whisper_cpp(audio_path, out_dir, getattr(args, "whisper_model", None))
    if engine == "faster-whisper":
        return transcribe_audio_faster_whisper(audio_path, out_dir)

    errors = []
    if platform.system() == "Darwin":
        try:
            return transcribe_audio_qwen3_mlx(audio_path, out_dir, args)
        except Exception as exc:
            errors.append(f"qwen3-asr-mlx: {exc}")
        try:
            return transcribe_audio_mlx_whisper(audio_path, out_dir, args)
        except Exception as exc:
            errors.append(f"mlx-whisper: {exc}")

    if getattr(args, "whisper_model", None):
        try:
            return transcribe_audio_whisper_cpp(audio_path, out_dir, args.whisper_model)
        except Exception as exc:
            errors.append(f"whisper.cpp: {exc}")

    try:
        return transcribe_audio_faster_whisper(audio_path, out_dir)
    except Exception as exc:
        errors.append(f"faster-whisper: {exc}")
        raise RuntimeError("Local ASR fallback failed: " + "; ".join(errors)) from exc


def _qwen3_language(source_lang):
    aliases = {
        "zh": "Chinese",
        "cn": "Chinese",
        "en": "English",
        "ja": "Japanese",
        "jp": "Japanese",
        "ko": "Korean",
        "kr": "Korean",
        "yue": "Cantonese",
        "multi": None,
        "auto": None,
    }
    lang = (source_lang or "").strip()
    if not lang:
        return None
    return aliases.get(lang.lower(), lang)


def _append_qwen3_timestamp(subs, start_s, end_s, text):
    st = int(float(start_s) * 1000)
    et = int(float(end_s) * 1000)
    text = (text or "").strip()
    if text and et > st:
        subs.append(pysubs2.SSAEvent(start=st, end=et, text=text))



def _qwen3_mlx_cache_home():
    cache_home = Path(__file__).resolve().parents[1] / ".agent" / "hf-cache"
    cache_home.mkdir(parents=True, exist_ok=True)
    return cache_home


def _qwen3_mlx_env():
    env = os.environ.copy()
    cache_home = str(_qwen3_mlx_cache_home())
    env["HF_HOME"] = env.get("QWEN3_ASR_MLX_HF_HOME") or env.get("MLX_AUDIO_HF_HOME") or env.get("HF_HOME") or cache_home
    env.setdefault("HF_HUB_DISABLE_XET", "1")
    return env


def _extract_qwen3_words_from_json(data):
    words = []
    for sentence in data.get("sentences", []) or []:
        for token in sentence.get("tokens", []) or []:
            text = (token.get("text") or token.get("word") or "").strip()
            start = token.get("start")
            end = token.get("end")
            if text and start is not None and end is not None and float(end) > float(start):
                words.append({"text": text, "start": float(start), "end": float(end)})
    for segment in data.get("segments", []) or []:
        for token in segment.get("words", []) or segment.get("tokens", []) or []:
            text = (token.get("text") or token.get("word") or "").strip()
            start = token.get("start")
            end = token.get("end")
            if text and start is not None and end is not None and float(end) > float(start):
                words.append({"text": text, "start": float(start), "end": float(end)})
    return sorted(words, key=lambda w: (w["start"], w["end"]))


def _subs_from_qwen3_words(words, mode="sentence"):
    subs = pysubs2.SSAFile()
    if not words:
        return subs
    chunk = []
    max_words = 14 if mode == "sentence" else 1
    max_seconds = 6.0 if mode == "sentence" else 0.0
    sentence_endings = tuple(".?!。！？…")
    for word in words:
        chunk.append(word)
        elapsed = chunk[-1]["end"] - chunk[0]["start"]
        should_flush = (
            len(chunk) >= max_words
            or (max_seconds and elapsed >= max_seconds)
            or word["text"].endswith(sentence_endings)
            or mode == "word"
        )
        if should_flush:
            _append_qwen3_timestamp(
                subs,
                chunk[0]["start"],
                chunk[-1]["end"],
                " ".join(item["text"] for item in chunk).strip(),
            )
            chunk = []
    if chunk:
        _append_qwen3_timestamp(
            subs,
            chunk[0]["start"],
            chunk[-1]["end"],
            " ".join(item["text"] for item in chunk).strip(),
        )
    return subs


def _subs_from_qwen3_aligner_json(json_path, mode="sentence"):
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    if mode == "sentence":
        subs = pysubs2.SSAFile()
        for sentence in data.get("sentences", []) or []:
            text = (sentence.get("text") or "").strip()
            start = sentence.get("start")
            end = sentence.get("end")
            if text and start is not None and end is not None:
                _append_qwen3_timestamp(subs, start, end, text)
        if len(subs) > 0:
            return subs
    return _subs_from_qwen3_words(_extract_qwen3_words_from_json(data), mode=mode)


def _plain_text_from_subs(subs):
    return " ".join(
        event.text.replace("\\N", " ").replace("\n", " ").strip()
        for event in subs
        if event.text and event.text.strip()
    ).strip()


def transcribe_audio_qwen3_mlx(audio_path, out_dir, args):
    srt_path = Path(out_dir) / "raw_audio.srt"
    if srt_path.exists():
        return pysubs2.load(str(srt_path), encoding="utf-8")

    prefix = Path(out_dir) / "raw_audio_qwen3_mlx"
    asr_srt = prefix.with_suffix(".srt")
    env = _qwen3_mlx_env()
    model = (
        getattr(args, "qwen3_asr_mlx_model", None)
        or os.environ.get("QWEN3_ASR_MLX_MODEL")
        or DEFAULT_QWEN3_ASR_MLX_MODEL
    )
    language = _qwen3_language(getattr(args, "source_lang", None)) or "English"
    print(f"[ASR] Using Qwen3-ASR MLX: {model}", flush=True)
    run(
        [
            sys.executable,
            "-m",
            "mlx_audio.stt.generate",
            "--model",
            model,
            "--audio",
            str(audio_path),
            "--output-path",
            str(prefix),
            "--format",
            "srt",
            "--language",
            language,
            "--chunk-duration",
            "30",
        ],
        "ASR",
        env=env,
    )
    if not asr_srt.exists():
        raise RuntimeError(f"Qwen3-ASR MLX did not create expected SRT: {asr_srt}")

    subs = pysubs2.load(str(asr_srt), encoding="utf-8")
    mode = (getattr(args, "qwen3_aligner_mode", "sentence") or "sentence").strip().lower()
    if mode not in {"off", "word", "sentence"}:
        raise RuntimeError("--qwen3-aligner-mode must be off, word, or sentence")

    if mode != "off":
        aligner_model = (
            getattr(args, "qwen3_aligner_mlx_model", None)
            or os.environ.get("QWEN3_ALIGNER_MLX_MODEL")
            or DEFAULT_QWEN3_ALIGNER_MLX_MODEL
        )
        aligner_prefix = Path(out_dir) / "raw_audio_qwen3_mlx_aligned"
        aligner_json = aligner_prefix.with_suffix(".json")
        transcript_text = _plain_text_from_subs(subs)
        if transcript_text:
            print(f"[ASR] Refining timestamps with Qwen3 ForcedAligner MLX: {aligner_model} ({mode})", flush=True)
            try:
                run(
                    [
                        sys.executable,
                        "-m",
                        "mlx_audio.stt.generate",
                        "--model",
                        aligner_model,
                        "--audio",
                        str(audio_path),
                        "--output-path",
                        str(aligner_prefix),
                        "--format",
                        "json",
                        "--language",
                        language,
                        "--text",
                        transcript_text,
                    ],
                    "ASR",
                    env=env,
                )
                if aligner_json.exists():
                    aligned_subs = _subs_from_qwen3_aligner_json(aligner_json, mode=mode)
                    if len(aligned_subs) > 0:
                        subs = aligned_subs
                    else:
                        print("[ASR] ForcedAligner returned no usable timestamps; keeping ASR segments.", flush=True)
                else:
                    print(f"[ASR] ForcedAligner JSON missing ({aligner_json}); keeping ASR segments.", flush=True)
            except Exception as exc:
                print(f"[ASR] ForcedAligner failed; keeping ASR segments: {exc}", flush=True)

    if len(subs) == 0:
        raise RuntimeError("Qwen3-ASR MLX returned no transcription text.")
    subs.save(str(srt_path))
    return subs



def transcribe_audio_qwen3(audio_path, out_dir, args):
    srt_path = Path(out_dir) / "raw_audio.srt"
    if srt_path.exists():
        return pysubs2.load(str(srt_path), encoding="utf-8")

    try:
        import torch
        from qwen_asr import Qwen3ASRModel
    except Exception as exc:
        raise RuntimeError(
            "Qwen3-ASR needs qwen-asr and torch installed in the active environment."
        ) from exc

    model_name = args.qwen3_asr_model or "Qwen/Qwen3-ASR-1.7B"
    aligner = args.qwen3_asr_aligner
    kwargs = {
        "dtype": torch.bfloat16,
        "device_map": "auto",
        "max_inference_batch_size": 8,
        "max_new_tokens": 4096,
    }
    if aligner:
        kwargs["forced_aligner"] = aligner
        kwargs["forced_aligner_kwargs"] = {"dtype": torch.bfloat16, "device_map": "auto"}

    print(f"[ASR] Using Qwen3-ASR: {model_name}", flush=True)
    model = Qwen3ASRModel.from_pretrained(model_name, **kwargs)
    results = model.transcribe(
        audio=str(audio_path),
        language=_qwen3_language(args.source_lang),
        return_time_stamps=bool(aligner),
    )
    result = results[0] if isinstance(results, list) else results

    subs = pysubs2.SSAFile()
    time_stamps = getattr(result, "time_stamps", None) or getattr(result, "timestamps", None)
    if time_stamps:
        for item in time_stamps:
            if isinstance(item, dict):
                _append_qwen3_timestamp(
                    subs,
                    item.get("start", item.get("begin", 0)),
                    item.get("end", item.get("finish", 0)),
                    item.get("text", item.get("word", "")),
                )
            elif isinstance(item, (list, tuple)) and len(item) >= 3:
                _append_qwen3_timestamp(subs, item[0], item[1], item[2])

    if len(subs) == 0:
        text = (getattr(result, "text", "") or "").strip()
        if text:
            duration_ms = max(1000, int(probe_duration(audio_path) * 1000))
            subs.append(pysubs2.SSAEvent(start=0, end=duration_ms, text=text))

    if len(subs) == 0:
        raise RuntimeError("Qwen3-ASR returned no transcription text.")
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

    if getattr(args, "asr_engine", "auto") == "qwen3-asr-mlx":
        return transcribe_audio_qwen3_mlx(audio_path, out_dir, args)

    if getattr(args, "asr_engine", "auto") == "qwen3-asr":
        return transcribe_audio_qwen3(audio_path, out_dir, args)

    if getattr(args, "asr_engine", "auto") == "whisper":
        subs = transcribe_audio_local(audio_path, out_dir, args)
        subs.save(str(srt_path))
        return subs

    if getattr(args, "asr_engine", "auto") in {"qwen3-asr-mlx", "mlx-whisper", "faster-whisper"}:
        subs = transcribe_audio_local(audio_path, out_dir, args)
        subs.save(str(srt_path))
        return subs

    try:
        print("[ASR] Trying NVIDIA Riva ASR", flush=True)
        subs = transcribe_audio_riva(audio_path, args.source_lang, config_type="word_time")
    except Exception as exc:
        print(f"[ASR] Riva unavailable; falling back locally: {exc}", flush=True)
        subs = transcribe_audio_local(audio_path, out_dir, args)

    subs.save(str(srt_path))
    return subs
