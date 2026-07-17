## 0. 强制云端优先策略 (Cloud-First ASR)
对于 ASR (语音识别与初级翻译) 环节，不再区分本地硬件，**绝对首选 NVIDIA Riva 云端 gRPC API** (`grpc.nvcf.nvidia.com:443`)。
它速度极快、并且不占任何本地显存，是防止后续 TTS 和 MuseTalk 发生 OOM 的最佳方案。
只有当 Riva 云端连接失败、或者缺乏 `$NVIDIA_API_KEY` 时，才允许“降级” (Fallback) 到下述的本地库中。

## 1. Mac (Apple Silicon / M系列芯片)

在 Mac 平台上，直接跑原生的 PyTorch (即使带 MPS) 往往会因为统一内存管理机制导致占用极高。必须优先寻找 `MLX` 框架重构的版本。

### 推荐降级仓库/依赖:
*   **ASR (Whisper) [Fallback]**: 
    *   **首选**: [ml-explore/mlx-examples (Whisper)](https://github.com/ml-explore/mlx-examples/tree/main/whisper)
    *   **优势**: 苹果官方维护，原生 MLX 极速推理。
*   **TTS (Qwen3-TTS，默认)**:
    *   **首选**: `mlx-audio` + 本地 Qwen3-TTS 1.7B BF16 模型。
    *   **优势**: 中文和日语声音克隆效果已在完整视频上验证；模型可单次加载、循环生成并断点续跑。
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

每次从 ASR 切换到 TTS，或从 TTS 切换到 MuseTalk 前，必须显式调用 `clear_vram()` 并确保上一个大模型已被 `del`。
