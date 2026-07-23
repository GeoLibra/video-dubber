# video-dubber skill

## Mac ASR

Mac Apple Silicon 本地 ASR 默认优先使用 `mlx-whisper` 的 `large-v3`：

```bash
python scripts/run_pipeline.py \
  --input-video "<video.mp4>" \
  --asr-engine mlx-whisper \
  --mlx-whisper-model mlx-community/whisper-large-v3-mlx \
  --status "$job_dir/status.json" \
  --log "$job_dir/run.log"
```

`--asr-engine auto` 在 Mac 上也会先尝试这条路线。只有 `mlx-whisper` 或模型不可用时，才会继续尝试显式 `whisper-cli --whisper-model` 或最终 `faster-whisper base int8` 兜底。

`large-v3` 精度通常高于 `faster-whisper base`，但模型更大，首次使用会下载 3GB 级 Hugging Face 权重；后续默认复用 skill 目录下的 `.agent/hf-cache`，也可用 `MLX_WHISPER_HF_HOME` 指到已有缓存。`mlx` 需要可访问 Metal 设备，headless/Codex 沙箱可能需要外部执行方式。

## Qwen3-ASR

Qwen3-ASR 是可选本地路线，适合你明确想测试 `Qwen/Qwen3-ASR-1.7B` 或 `Qwen/Qwen3-ASR-0.6B` 时使用。时间戳输出需要额外配合 `Qwen/Qwen3-ForcedAligner-0.6B`。

先安装可选依赖：

```bash
pip install -r /Users/hgis/myproject/video-editing/video-dubber/skills/video-dubber/requirements-qwen3-asr.txt
```

使用 1.7B：

```bash
python scripts/run_pipeline.py \
  --input-video "<video.mp4>" \
  --asr-engine qwen3-asr \
  --qwen3-asr-model Qwen/Qwen3-ASR-1.7B \
  --qwen3-asr-aligner Qwen/Qwen3-ForcedAligner-0.6B
```

Qwen3-ASR 权重大、环境要求也更高，不应在任务里静默下载。首次使用前要先确认磁盘、网络和 Python/torch 环境。
