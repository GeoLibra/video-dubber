# video-dubber skill

这是仓库内的 skill 子目录说明。完整用户文档统一维护在仓库根目录 `README.md`；
Agent 执行规则统一维护在本目录的 `SKILL.md`。这里保留一份极简摘要，避免双份 README 内容长期漂移。

## 运行特点摘要

- 默认 faithful 翻译：不为了时间线删减内容；只有显式 `--translation-style concise|summary` 才允许压缩。
- Mac ASR 路由：有 `NVIDIA_API_KEY` 优先 NVIDIA Riva；本地优先 `qwen3-asr-mlx`，可选 `qwen3-aligner-mode sentence` 精修句级边界；再回退 `mlx-whisper`。
- NVIDIA key 申请地址：https://build.nvidia.com/models
- 默认 Qwen3-TTS MLX；`--qwen3-tts-max-tokens 260` 用于限制单条生成拖长/卡住，不是删减文本。
- 原声字幕版和克隆配音版是独立产物；默认先快速产出原声字幕版。
- 长任务用 detached runner 启动，状态检查看 PID、`pipeline_status.json` 心跳和产物增长；stale 后用同一 job 目录 `resume_job.py --detached` 续跑。

## 关键文件

- `SKILL.md`：Agent 必读执行规则
- `scripts/run_pipeline.py`：主流程
- `scripts/start_detached_job.py`：后台启动
- `scripts/status_job.py`：PID + 心跳 + 产物增长检查
- `scripts/resume_job.py`：同 job 续跑
- `references/hardware_routing.md`：ASR/TTS 硬件路由
