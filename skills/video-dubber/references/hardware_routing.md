## ASR 总路由

1. 若设置 `NVIDIA_API_KEY`，优先 NVIDIA Riva 云端 ASR；key 申请地址：https://build.nvidia.com/models 。
2. 云端不可用/无 key 时，Mac 本地优先 `qwen3-asr-mlx + mlx-community/Qwen3-ASR-1.7B-8bit`。
3. Qwen3-ASR MLX 不可用时回退 `mlx-whisper + mlx-community/whisper-large-v3-mlx`。
4. 最后兜底 `whisper.cpp` / `faster-whisper base int8`。

## 0. 强制云端优先策略 (Cloud-First ASR)
对于 ASR (语音识别与初级翻译) 环节，不再区分本地硬件，**绝对首选 NVIDIA Riva 云端 gRPC API** (`grpc.nvcf.nvidia.com:443`)。`NVIDIA_API_KEY` 可在 https://build.nvidia.com/models 申请。
它速度极快、并且不占任何本地显存，是防止后续 TTS 和 MuseTalk 发生 OOM 的最佳方案。
只有当 Riva 云端连接失败、或者缺乏 `$NVIDIA_API_KEY` 时，才允许“降级” (Fallback) 到下述的本地库中。

## 1. Mac (Apple Silicon / M系列芯片)

在 Mac 平台上，默认优先使用 MLX/Metal 路线，避免 PyTorch/MPS 在长音频上占用过高统一内存。

### 推荐降级仓库/依赖:
*   **ASR (Qwen3-ASR MLX，默认高质量路线)**:
    *   **首选**: `mlx_audio.stt` + `mlx-community/Qwen3-ASR-1.7B-8bit`。
    *   **命令**: `--asr-engine qwen3-asr-mlx --qwen3-asr-mlx-model mlx-community/Qwen3-ASR-1.7B-8bit`。
    *   **省内存/快速**: `mlx-community/Qwen3-ASR-0.6B-8bit`。
    *   **输出**: ASR 直接给句段级时间戳，足够生成普通 SRT。
*   **ASR 时间戳增强 (Qwen3-ForcedAligner MLX，可选)**:
    *   **模型**: `mlx-community/Qwen3-ForcedAligner-0.6B-8bit`。
    *   **用途**: 把“已有文本”重新对齐到音频，得到更细的词级时间戳；默认 `--qwen3-aligner-mode sentence` 会用词级边界重建句级 SRT，不逐词显示。
    *   **参数**: `--qwen3-aligner-mode off|word|sentence`、`--qwen3-aligner-mlx-model mlx-community/Qwen3-ForcedAligner-0.6B-8bit`。
*   **ASR (Whisper MLX，稳定 fallback)**:
    *   **模型**: `mlx-whisper` + `mlx-community/whisper-large-v3-mlx`。
    *   **命令**: `--asr-engine mlx-whisper --mlx-whisper-model mlx-community/whisper-large-v3-mlx`。
    *   **用途**: Qwen3-ASR MLX 依赖、缓存或 Metal 环境不可用时回退；仍优先于 `faster-whisper base int8`。
*   **ASR (Qwen3 official/PyTorch，兼容测试)**:
    *   **命令**: `--asr-engine qwen3-asr --qwen3-asr-model Qwen/Qwen3-ASR-1.7B --qwen3-asr-aligner Qwen/Qwen3-ForcedAligner-0.6B`。
    *   **用途**: 只用于兼容或对照测试；在 Mac/Codex 环境中通常会跑 CPU，不作为默认路线。
*   **运行约束**:
    *   `mlx` 需要可访问 Metal 设备；Codex/CI/headless 沙箱若报 `No Metal device available`，不要误判为模型损坏，改用允许访问 Metal 的外部执行方式跑 ASR。
    *   长视频必须分块或阶段落盘，避免 20 分钟音频长时间无产物、失败后全丢。
*   **TTS (Qwen3-TTS，默认)**:
    *   **首选**: `mlx-audio` + 本地 Qwen3-TTS 1.7B BF16 模型。
    *   **优势**: 中文、日语、韩语声音克隆效果已在完整视频上验证；模型可单次加载、循环生成并断点续跑。
*   **TTS (F5-TTS，兼容)**:
    *   **首选**: [lucasnewman/f5-tts-mlx](https://github.com/lucasnewman/f5-tts-mlx)
    *   **用途**: 用户显式要求 F5 或需要与旧版本做音色对比时使用。
*   **TTS (CosyVoice)**:
    *   **首选**: 官方仓库结合 MPS (目前 MLX 生态对 CosyVoice 的支持尚在早期，需使用 PyTorch MPS 后端)。
*   **Lip-Sync (MuseTalk)**:
    *   **首选**: 官方仓库 [TMElyralab/MuseTalk](https://github.com/TMElyralab/MuseTalk) (强制指定 device=mps)。

## 2. Windows / Linux (NVIDIA GPU / N卡)

在 N 卡机器上，核心诉求是榨干 CUDA 算力。

### 推荐降级仓库/依赖:
*   **ASR (Whisper) [Fallback]**:
    *   **本地极速推荐**: [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper) (基于 CTranslate2，显存占用小，速度极快)。
*   **TTS (F5-TTS & CosyVoice)**:
    *   **首选**: 官方仓库，开启 FlashAttention2。
*   **Lip-Sync (MuseTalk)**:
    *   **首选**: 官方仓库，开启 `xformers` 或 `torch.compile` 加速。

## 3. OOM 防御代码模板

在串行执行大模型时，必须严格执行清理释放：

```python
import torch
import gc

def clear_vram():
    # 清理 Python 垃圾回收
    gc.collect()
    
    # 清理 CUDA 缓存 (NVIDIA)
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.ipc_collect()
    
    # 清理 MPS 缓存 (Mac Apple Silicon)
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        torch.mps.empty_cache()
```

每次从 ASR 切换到 TTS，或从 TTS 切换到 MuseTalk 前，必须显式调用 `clear_vram()` 并确保上一个大模型已被 `del`。MLX 后端虽然不走 PyTorch CUDA/MPS cache，也要释放模型对象，避免统一内存长期占用。
