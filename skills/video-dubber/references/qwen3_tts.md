# Qwen3-TTS 默认后端

## 已验证配置

- Apple Silicon + Metal
- `mlx-audio==0.4.5`
- Qwen3-TTS 12Hz 1.7B Base BF16
- 24kHz 单声道参考音频和输出
- 中文、日语声音克隆

模型路径优先级：

1. CLI：`--qwen3-model /absolute/model/path`
2. 环境变量：`QWEN3_TTS_MODEL=/absolute/model/path`
3. skill 内置的本机缓存候选

默认不静默下载模型，因为主权重和 speech tokenizer 合计数 GB。模型缺失时明确报错，由用户决定下载位置。

## 生成参数

默认参数来自中文和日语实测：

```text
temperature=0.8
top_k=40
top_p=0.95
repetition_penalty=1.08
lang_code=chinese | japanese | korean | english
```

这些参数是稳定基线，不是质量结论。比较不同模型时，保持参考音频、参考文本、翻译文本、字幕时间轴和背景音策略一致，只改变 TTS 后端。

## 缓存与续跑

模型应在一个 Python 进程中只加载一次。每段生成后立即写 WAV，并更新 `status.json`。chunk 文件名带目标语言、引擎和缓存 hash；hash 至少覆盖：

- 目标朗读文本
- 目标语言
- 引擎与模型路径/ID
- 参考音频内容 hash
- 参考文本 hash
- `max_atempo`、最大裁切和 overhang 参数

任务中断后，用同一个 job 目录和相同参数重跑。完整输出成功后，`--no-segments` 才清理本轮 chunk。

## 对齐策略

原视频和字幕窗口固定不动。先合并碎字幕为语义段，再生成 TTS。对每段记录：

- `raw_ms` / `target_ms`
- `needed_atempo_ratio` / `atempo_ratio`
- `cropped_ms` / `overhang_ms`
- `quality_warning`

如果出现句尾裁切或 `quality_warning`：

1. 压短该段译文，同时更新屏幕字幕和 TTS 文本。
2. 必要时合并相邻语义窗口。
3. 只重生风险 chunk。
4. 最后才温和提高局部 `atempo`。

不要把 2x 以上变速作为默认修复。它可以用于模型 A/B 试版避免漏字，但不能据此宣称自然的 strict sync 成片。

## 多语言注意事项

- 中文：技术缩写可保留英文，换行时不能拆 token。
- 日语：翻译通常比英文窗口更长，优先使用简洁自然的口语表达；`lang_code` 必须是 `japanese`。
- 韩语：传 `korean`，先做短句和长句 smoke test。
- 参考文本始终使用参考音频的原语言原文，不翻译参考文本。
