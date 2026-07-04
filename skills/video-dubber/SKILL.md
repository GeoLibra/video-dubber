---
name: video-dubber
description: >
  视频下载、字幕翻译、硬字幕烧录和可选声音克隆配音工具。用户要下载 YouTube/Bilibili/Twitter/X/TikTok
  或本地视频、生成中文/日语/韩语等字幕、保留原声只加字幕、或根据原视频/参考音频克隆声音生成配音视频时使用。
  默认目标语言是中文；克隆配音时要保持字幕、语音和原视频时间轴同步。
allowed-tools:
  - Read
  - Write
  - Bash
  - Glob
  - AskUserQuestion
model: gemini-3.5-flash
---

# 视频多语言配音与克隆工具

## 关键原则

执行优先级：先保证字幕、配音和画面同步，再保证过程可续跑、可验证、少返工。

1. **字体必须一致**：默认使用 `assets/fonts/HiraginoSansGB.ttc`。1080p 默认字幕样式为白字、黑描边、字号 40、描边 4、行距 8、底部偏移 74。
2. **视频时间轴不动**：字幕 start/end 和视频时长是基准，TTS 必须去适配它，不能反过来移动字幕或拉长视频。
3. **默认使用 strict sync**：用户要“中文字幕 + 中文克隆配音”时，屏幕字幕文本必须和 TTS 文本同源同字，不能再让 `tts_text` 比 `display_text` 多讲内容。
4. **平台自带字幕优先**：下载源平台可用字幕并抽样挑最干净轨道；只有没有可用字幕时才 ASR。
5. **禁止整段 TTS 后整体拉伸**：默认先合并过碎字幕成语义段，再按语义段生成 TTS；只有纯字幕或非严格口型同步场景才逐字幕条生成。
6. **字幕文本和 TTS 文本分离只用于 voiceover 模式**：`display_text` 短、`tts_text` 自然长会导致“语音比字幕说得多”。除非用户明确要解说型旁白，否则不要启用这套分离策略。
7. **翻译只传 id|text**：不要把完整 SRT、时间码、空行塞进对话。分批翻译，脚本按 id 拼回时间轴。
8. **支持外部参考音频**：用户给 `--ref-audio` 时优先使用它；必须配 `--ref-text`、`--ref-text-file` 或 `--auto-transcribe-ref`。
9. **长任务低噪声且可续跑**：核心脚本写 `status.json`、`run.log` 和阶段产物。意外中断后，用同一个 `job_dir/status/log` 和相同参数重跑，脚本会复用已完成的视频、ASR、翻译、参考音频和 TTS chunk；Agent 只在阶段变化、失败、完成或用户询问时汇报。
10. **完成后必须验证**：检查视频/音频流时长、字幕条数、TTS 生成/跳过/错误数，并输出 `verification_report_<lang>_<mode>.json`。
11. **音色质量优先于盲目重生**：如果已有 `output_cloned_<lang>_<mode>.mp4` 的音色更像原说话人，不要用新的参考片段或长分组全量覆盖。先保留旧版作为音色基线，再用 strict sync 重建字幕和 TTS 文本。
12. **非语音标记不进画面**：`[MUSIC]`、`[音乐]`、`[APPLAUSE]`、`[掌声]`、`music playing` 等只用于判断静音/背景音 gap，必须跳过 TTS，并把 ASS 屏幕字幕写空；不要把方括号提示烧进最终视频。
13. **原声硬字幕是独立路径**：用户说“不要克隆声音 / 只加中文字幕 / 原声保留 / 只下载并加字幕”时，不要进入参考音频、TTS、voice clone 或 `output_cloned_*` 流程；只生成原音轨硬字幕视频和字幕验证报告。

## 默认字体 Profile

默认使用以下样式：

```text
fontfile: assets/fonts/HiraginoSansGB.ttc
fontname: Hiragino Sans GB
fontsize: 40
fontcolor: white
outline/border: 4 black
line_spacing: 8
alignment: bottom center
MarginV / y offset: 74
```

如果使用 FFmpeg `drawtext`，对应参数为：

```text
fontfile='<skill>/assets/fonts/HiraginoSansGB.ttc':
fontsize=40:fontcolor=white:borderw=4:bordercolor=black:
line_spacing=8:x=(w-text_w)/2:y=h-text_h-74
```

如果使用 ASS，必须设置 `fontsdir=<skill>/assets/fonts`，并用 `Hiragino Sans GB` 作为样式 fontname。

ASS 字幕换行规则：
- 默认使用自适应语义换行：先按输出分辨率、字号、左右边距和字幕模式计算每行 display-width，再优先在 `，。！？；：` 等语义边界断行。
- 不要把 `52` 或某个固定宽度当成通用规则。1080p、字号 40、左右边距 120、中文字幕单语时通常会自动得到约 52 display-width；换分辨率、字号、边距或双语字幕时必须重新计算。
- 长句最多 3 行；双语字幕目标语言通常最多 2 行，因为原文还会占一行。只有单个语义短句仍超宽时才 token 级拆分。
- 中英混排必须 token-aware wrapping，英文技术词不拆开；中文/日文/韩文可以按字兜底断行。
- 生成 ASS 时换行符必须保留为真正的 `\N`。不要在 wrap 之后再全局 escape 反斜杠，否则会把 `\N` 变成可见的 `\`。
- 正确顺序是先 escape 文本里的 `{}`，再插入 `\N` 换行。

## 推荐命令

后台启动，避免长任务占用对话：

```bash
job_id=$(date +%s)
job_dir=".agent/jobs/$job_id"
mkdir -p "$job_dir"
python .agents/skills/video-dubber/scripts/run_pipeline.py \
  --url "<video_url>" \
  --cookies-from-browser chrome \
  --status "$job_dir/status.json" \
  --log "$job_dir/run.log" \
  --target-language Chinese \
  --subtitle-mode target \
  --preserve-gap-audio \
  --no-segments \
  > "$job_dir/stdout.log" 2>&1 &
```

通用下载参数：

```bash
--cookies-from-browser chrome  # Bilibili / X protected posts / Instagram / age-gated content
--write-subs --write-auto-subs --sub-langs "en.*,en" --convert-subs srt
--list-formats                 # 只列出可用格式并退出
-f "bv*[height<=1080]+ba/best[height<=1080]/bv*+ba/best"
--allow-playlist               # 用户明确要合集/分 P/播放列表时才开启
--playlist-items 1-10
--proxy socks5://127.0.0.1:1080
--concurrent-fragments 3
--external-downloader aria2c
--use-yt-dlp-config            # 默认忽略全局 yt-dlp 配置，必要时才允许
```

目标语言可直接写英文名或常用简写/中文名，例如：

```bash
--target-language Chinese   # 或 中文 / zh
--target-language Japanese  # 或 日语 / ja
--target-language Korean    # 或 韩语 / ko
```

字幕模式：

```bash
--subtitle-mode bilingual   # 目标语言 + 原文，例如中文 + 英文
--subtitle-mode target      # 只有目标语言，例如只有中文
--subtitle-mode source      # 只有原文，主要用于调试
```

如果用户提供参考音频：

```bash
python .agents/skills/video-dubber/scripts/run_pipeline.py \
  --input-video "<local_video.mp4>" \
  --source-srt "<source.srt>" \
  --ref-audio "<reference.mp3>" \
  --ref-text-file "<reference.txt>" \
  --status "$job_dir/status.json" \
  --log "$job_dir/run.log" \
  --no-segments
```

## 工作流程

### 1. 环境和空间预检

先检查：
- `ffmpeg` / `ffprobe`（需要在 `PATH` 中可用）。
- `yt-dlp`，仅 URL 输入时必需。
- 字体文件 `assets/fonts/HiraginoSansGB.ttc`。
- 可用磁盘空间，默认至少 2GB。F5/PyTorch 模型缓存可能占 1.5GB 以上，空间不足时使用 `--no-segments` 并清缓存。
- 设置 `TMPDIR`、`MPLCONFIGDIR`、`XDG_CACHE_HOME`、`HF_HOME` 到 job 目录，避免系统 `/tmp` 或用户 cache 不可写。
- ASR/TTS 硬件路由要先做 10-15 秒 smoke test：
  - macOS 上 `whisper-cli` 先试 `--no-gpu`。如果默认 Metal 加载后崩溃，不要反复重跑默认命令，直接固定 CPU ASR。
  - MLX/F5 需要 Metal；沙箱或 headless 环境可能报 `No Metal device available`，这时必须用允许访问 Metal 的外部执行方式跑 TTS。
  - 有 `NVIDIA_API_KEY` 时优先走 NVIDIA Riva gRPC ASR；如果 API、依赖或服务不可用，再回退本地 ASR。

### 2. 输入准备

支持两种输入：
- `--url`：用 `yt-dlp` 下载视频并提取 16k mono WAV，支持 YouTube、Bilibili、Twitter/X、TikTok、Vimeo、Instagram、Twitch 和 yt-dlp 支持的 1000+ 站点。
- `--input-video`：读取本地视频并提取音频。

如果用户已有字幕，传 `--source-srt`，不要重新 ASR。

URL 下载默认使用：
- `--ignore-config`：避免用户本地 yt-dlp 配置污染任务。
- `--no-playlist`：避免误下载整个合集/分 P/播放列表。
- `-f "bv*[height<=1080]+ba/best[height<=1080]/bv*+ba/best"`：优先真实 1080p 及以下最佳视频 + 音频，失败时回落到平台可用的 best；不要把 360p 升采样冒充 1080p。
- `--merge-output-format mp4`：统一得到 `raw_video.mp4`。
- `--write-subs --write-auto-subs --sub-langs "en.*,en" --convert-subs srt`：优先拿平台字幕，减少 ASR 时间和错误；源语言不是英语时调整 `--sub-langs`。

平台要点见 [platform_download_tips.md](references/platform_download_tips.md)。常用处理：
- Bilibili 高画质、分 P、登录字幕：加 `--cookies-from-browser chrome`；需要分 P 时加 `--allow-playlist --playlist-items 1-10`。
- Twitter/X 受保护或登录可见内容：加 `--cookies-from-browser chrome`。
- TikTok 格式或水印问题：先 `--list-formats`，再指定 `-f`。
- Instagram/Reels：通常需要 `--cookies-from-browser chrome`。
- 网络问题：使用 `--proxy`；慢下载可用 `--concurrent-fragments` 或 `--external-downloader aria2c`。

若下载失败，按顺序重试：
1. 加 `--cookies-from-browser chrome`。
2. 跑 `--list-formats` 查看可用格式。
3. 改用 `-f "best"`。
4. 合集/分 P 明确选择 `--allow-playlist` 或 `--playlist-items`。
5. 检查 yt-dlp 版本是否过旧。

YouTube 字幕优先级：
- 优先下载具体语言轨，例如 `en-en-*.srt`。
- 避免直接使用泛化 `en.srt`，它可能包含重复、重叠和错乱字幕。
- 若多个字幕文件存在，先抽样检查重叠率、重复文本和时间连续性，再选最干净的作为源字幕。

YouTube 高清下载策略：
- 先用 `bv*+ba/best` 获取 1080p 或更高；若只有 storyboard/360p，分别测试 `--cookies-from-browser chrome` 和 `--extractor-args "youtube:player_client=android,web,mweb,ios"`。
- Chrome cookies 只提供登录态，不等于一定能拿到 1080p。YouTube 可能还要求 PO Token、n-signature challenge、SABR 或 impersonation 支持；没有这些时，yt-dlp 可能只能列出 360p format 18。
- 如果 `--list-formats` 最终只显示 360p/format 18，必须在结果里说明“当前环境只能下载到 360p”，不要把 360p 升采样后称作真实 1080p。

### 3. ASR

默认云端优先：
1. 有 `NVIDIA_API_KEY` 时优先尝试 NVIDIA Riva gRPC（`grpc.nvcf.nvidia.com:443`，function-id `b702f636-f60c-4a3d-a6f4-f3568c13bd7d`）。
2. 失败后降级本地：`whisper-cli + --whisper-model` 或 `faster-whisper`。

本地 ASR 路由：
- 先跑 10-15 秒音频 smoke test，确认模型、语言识别和字幕输出格式。
- `whisper-cli` 在 Apple Silicon 上若默认 Metal 崩溃，立即改用 `--no-gpu`；不要重复下载模型或反复试默认 GPU 路径。
- 如果没有 whisper.cpp ggml 模型，优先下载/复用小模型做初稿；字幕质量不足再升级模型，不要一开始就加载大模型。
- 若存在 `faster-whisper` 模型缓存但当前 Python 环境没有包，先检查已有 venv；不要临时在全局环境里安装大量依赖。

不要把完整 ASR 文本贴回对话；只报告路径、条数、抽样问题。

### 4. 翻译

首次运行时脚本会计算翻译所需 token 量并暂停确认。看到状态为 `confirm_translation` 时，向用户展示预估 token 数和字幕条数，由用户决定：
- **继续**：加 `--confirm-translation` 重跑
- **换模型**：改 `--translation-model` 换个便宜的模型
- **手动翻译**：不配 API key，走 Agent 自身模型翻译（见 [4a](#4a-无-api-key-时的翻译兜底)）

翻译阶段只传：

```text
id|text
```

模型返回：

```json
{
  "translations": [
    {"id": 0, "display_text": "屏幕字幕", "tts_text": "自然口播文本"}
  ]
}
```

脚本保存 `translations_<lang>.json`，并生成 `subtitles_<lang>_<mode>.ass`。

长字幕翻译必须 checkpoint：
- 每批翻译成功后立刻写入 `translations_<lang>.json` 和 `status.json`，不要等全部批次完成才落盘；长视频中断或 API 卡住时，内存累计会丢掉已完成批次。
- 重跑时读取 hash 匹配的已有翻译缓存，只请求缺失 id。缓存 key 读入后要转回整数；hash 或模型不匹配时先备份旧缓存，再开始新缓存。
- 每批打印低噪声进度，例如 `batch 23/75 saved; total=920/2995`，并把 `translated/total/batch/batches/last_seen` 写入 `status.json`；Agent 不要为了确认进度反复读日志。
- 长视频可以使用 `--translation-workers 4` 并发翻译；优先靠 checkpoint 和缺失 id 补翻保持可续跑，不要在 job 目录临时写另一个翻译脚本。
- 模型可能少返回某些 id，完成后必须检查 `0..len(subs)-1` 是否全部覆盖；缺失时只把缺失 id 组成小批次补翻译。默认缺失即失败，只有显式 `--allow-source-fallback` 才允许用原文回退。
- 如果模型返回不合法 JSON、控制字符或连接错误，重试当前批次，不要回退成原文字幕，也不要直接写 ASS。

默认 strict sync 输出时：
- 只使用 `display_text` 生成屏幕字幕和 TTS。
- `tts_text` 必须等于 `display_text`，或直接忽略 `tts_text`。
- 如果翻译缓存里 `display_text != tts_text`，不要直接用于配音；先生成 strict-sync 文本。
只有用户明确要求“自然解说/旁白优先，不要求字幕逐字一致”时，才允许使用更长的 `tts_text`。

读取 JSON 翻译缓存时必须把 key 转回整数。JSON 会把 `{0: ...}` 保存成 `{"0": ...}`，如果不恢复为 int，缓存命中后会取不到翻译，导致字幕/TTS 回退到原文。

目标语言由用户参数决定，默认 `Chinese`。支持 `Chinese/中文/zh`、`Japanese/日语/ja`、`Korean/韩语/ko`，也可传其他语言英文名。翻译缓存按语言保存为 `translations_<lang>.json`，并用源字幕 hash 校验；同一个 job 目录切换语言时不能复用旧语言翻译。

字幕模式由 `--subtitle-mode` 决定：
- `bilingual`：目标语言在上，原文在下，适合“中英双语/日英双语”等需求。
- `target`：只显示目标语言，适合“只要中文字幕/只要日文字幕”。
- `source`：只显示原文，主要用于排查 ASR 和样式。

### 4a. 无 API Key 时的翻译兜底

如果任务状态为 `awaiting_translation`，说明脚本没有检测到可用的翻译 API key。此时需要 Agent 用自己的模型来翻译。**必须先向用户展示预估 token 数和字幕条数，获得用户确认后再开始翻译，不得擅自直接翻译。**

1. 从 `status.json` 读取 `estimated_tokens` 和 `subtitle_count`，向用户展示并询问是否继续
2. 用户确认后，读取 `source_raw.srt`，按 `id|text` 格式解析每条字幕
3. 逐条翻译为目标语言，生成 `display_text`（屏幕字幕文本）和 `tts_text`（口播文本）
4. 写入 `translations_<lang>.json`，格式为 `{"0": {"display_text": "...", "tts_text": "..."}, ...}`
5. 确认每条 id 都覆盖到（从 0 到 N-1）
6. 用相同参数重新运行脚本，脚本会读取缓存跳过翻译阶段

非 `awaiting_translation` 状态时不走此流程。脚本自动的 `confirm_translation` 阶段已提供 token 估算，Agent 采用 `--confirm-translation` 继续时必须已获得用户确认。

### 5. 参考音频

优先级：
1. 用户显式提供 `--ref-audio`。
2. 用户同时提供 `--ref-text` 或 `--ref-text-file`。
3. 如果用户允许，使用 `--auto-transcribe-ref` 转写参考音频。
4. 没有外部参考音频时，从原视频语音中自动抽取 3-12 秒干净片段。

参考文本必须尽量匹配参考音频，否则克隆音色会漂。

### 6. TTS 和时间轴对齐

默认 strict sync 执行策略：
- 不逐原始字幕小片段生成 TTS。YouTube 自动字幕经常有 0.5s、0.8s、1.2s 的碎片，中文 TTS 无法自然说完。
- 先把相邻字幕合并成语义段：合并到窗口至少约 3.2s，且估算中文语速能在窗口内说完。短句如“好的/拜拜/或者是下周”必须并入前后句。
- 每个语义段只生成一条字幕和一条 TTS，字幕文本与 TTS 文本完全一致。
- 如果 TTS 仍略长，优先缩短该段译文、局部提高 TTS speed 或合并相邻窗口；不要拉长视频，也不要让语音读 A 而字幕显示 B。
- 验证必须报告：分组数、`max_needed_atempo_ratio`、`max_atempo_ratio`、裁切片段数和 `quality_warning`。

F5/MLX 克隆策略：
- 不要用 CLI 每段单独启动模型生成几十段音频；应写脚本加载一次 `F5TTS.from_pretrained(...)`，循环生成所有 chunk，速度更快也更少出错。
- 先用 1 条代表性中文长句做 `--speed` smoke test。F5 的 `speed` 对时长很敏感：过慢会让短字幕窗口裁尾，过快会听起来突兀。
- 长窗口可用较慢速度保留自然度；短窗口风险段可局部提高速度或缩短文本。不要全片固定一个速度后盲目接受大量裁切。
- TTS chunk 必须落盘并可续跑；重跑时跳过已存在 chunk，局部补丁只重生风险段。

兼容的逐字幕执行策略：
- 仅用于纯字幕、调试、或用户明确接受字幕和配音不逐字一致的 voiceover 模式。
- 非语音字幕如“音乐 / music / applause / 欢快”跳过，时间轴保留静音，屏幕字幕写空，不烧录方括号提示。
- 对每条 TTS 做局部适配：短了补静音，稍长用 `atempo` 温和加速，避免突然高速说话。
- 默认 `--max-atempo 1.6`、`--max-clip-ms 80`、`--max-overhang-ms 450`。不要再用 2x 以上大幅变速或粗暴裁掉句尾。
- 当 TTS 明显长于字幕窗口时，优先保留完整句子并在报告中标记 overhang/quality_warning；之后只重译或重生这些风险段。
- 预分配完整视频时长的静音 timeline，把每条音频 overlay 到原字幕 start。
- 不允许修改 `sub.start` 或 `sub.end`。
- 最终音频必须补齐到视频总时长。

如果用户反馈“声音和字幕不同步、语速突然很快、句子不完整”，按这个顺序修：
1. 先检查 `display_text` 和 `tts_text` 是否不一致。如果不一致，这是第一根因。
2. 检查原字幕窗口是否太碎。若大量 TTS 原始时长超过窗口，例如 0.5s 字幕生成 2s 音频，逐字幕修补不是根治。
3. 生成 strict sync 版：合并字幕段、只用屏幕字幕文本生成 TTS、字幕随语音语义段显示。
4. 如果 strict sync 后音色差，再回到参考音频选择问题；不要先怀疑字幕样式或 atempo。
5. 如果用户接受旁白型视频，再考虑 voiceover 模式。

风险段补丁流程：
1. 读取 `tts_alignment_report`，找出 `quality_warning`、`cropped_ms`、`needed_atempo_ratio > max_atempo` 的段。
2. 只压短这些段的字幕文本，并同步更新字幕 JSON/ASS；屏幕字幕和 TTS 文本仍必须同源同字。
3. 只重生风险段 chunk，复用其他 chunk 重建 timeline。
4. 重新烧录两个输出视频，再验证 `warnings == 0`、视频/音频 duration 与源视频一致。

背景音处理：
- 克隆配音默认会替换原音轨；如果原视频有无语音区间的音乐/环境声，必须把这些 gap 的原音频铺回克隆音轨。
- 没有做 vocal/background separation 时，不要把整段原音低音量混进中文 TTS，否则英文原声会压在中文下面。
- 主流程可加 `--preserve-gap-audio`，只在真实语音字幕段外恢复原音频，保留开头/结尾音乐和无语音环境声；默认 `--gap-audio-gain-db -6`、`--gap-pad-ms 60`。`[音乐]`、`[掌声]` 等非语音字幕窗口也属于 gap，不能因为它们有时间码就屏蔽背景音。
- 重建已有输出时可用 `scripts/rebuild_outputs.py --preserve-gap-audio` 做同样处理。

默认用 `--no-segments`，只保留最终 `merged_tts_<lang>.wav` 和 `tts_alignment_report_<lang>.json`。只有调试时保存每条 chunk。

为了支持中断续跑，`--no-segments` 不会在每条 TTS 生成后立刻删除 chunk；脚本会先保留 `chunk_<lang>_0000.wav` 这类片段，直到完整 `merged_tts_<lang>.wav` 和报告写完后再清理。若任务中断，使用同一个 job 目录和相同参数重跑即可从已有 chunk 继续。

### 7. 字幕烧录和视频导出

默认生成：
- `output_original_<lang>_<mode>.mp4`：原音轨 + 指定字幕。
- `output_cloned_<lang>_<mode>.mp4`：克隆配音 + 可选 BGM + 指定字幕。

例如中文双语输出为 `output_cloned_zh_bilingual.mp4`，日语单语输出为 `output_cloned_ja_target.mp4`。

原声硬字幕 / subtitle-only 模式：
- 当用户明确不要克隆声音时，流程是 `下载/读取视频 -> 获取或 ASR 源字幕 -> checkpoint 翻译 -> 生成 ASS -> ffmpeg 保留原音轨烧录字幕 -> 验证`。
- 不要调用 `prepare_reference_audio`、`generate_tts_and_merge`、F5/MLX、音频分离或 gap-audio 混音；不要生成 `output_cloned_*`。
- 输出文件建议命名为 `output_original_<lang>_<mode>.mp4`，并在 `job_config.json` / `status.json` / 验证报告中写明 `clone_voice: false` 和 `tts_engine: none`。
- 如果现有主流程脚本默认会走 TTS，就用专门的字幕-only 脚本或轻量 Python glue 复用其下载、翻译、ASS 样式函数，避免为了“不要克隆声音”仍然跑参考音频和 TTS。

FFmpeg 必须使用 `-loglevel error`。ASS 烧录必须传 `fontsdir=assets/fonts`。

重烧字幕或音频时不要写 job 临时脚本，优先使用：

```bash
python .agents/skills/video-dubber/scripts/rebuild_outputs.py \
  --job-dir "$job_dir" \
  --max-width 0 \
  --max-lines 3 \
  --preserve-gap-audio
```

该脚本会从 `source_groups_zh.json` 用自适应语义换行重建 ASS，输出原音字幕版，并在存在 `merged_tts_*.wav` 时输出克隆版。`--max-width 0` 表示按分辨率、字号、边距和字幕模式自动估算；只在人工确认某个视频需要特殊字幕宽度时才手动覆盖。

### 8. 验证

完成后生成 `verification_report_<lang>_<mode>.json`，至少包含：
- 源视频和输出视频的 format duration。
- 输出视频 video/audio stream duration。
- 字幕条数。
- 配音任务包含 TTS 总条数、生成条数、跳过条数、错误数。
- 配音任务包含 TTS 最大变速比、裁尾片段数量、质量风险片段 index。
- strict sync 输出还必须包含分组数、`max_needed_atempo_ratio`、`max_atempo_ratio`、裁切片段数、`quality_warning` 计数、`display_text != tts_text` 计数。
- 原声硬字幕任务不需要 TTS 统计，但必须包含 `width`、`height`、`subtitle_count`、`dialogue_count`、`visible_backslash_N`、`clone_voice: false`、`tts_engine: none`，并确认输出音视频时长与源视频一致。

若 `tts_errors > 0`，任务不能标记成功。
若 strict sync 输出中 `quality_warning > 0` 或存在明显裁尾，不能宣称“已严格同步”，只能称为试版或继续合并/缩短文本。

## Token 节省协议

- 用户只反馈字幕问题时，只重烧 ASS；只反馈配音问题时，只重跑 TTS/audio mux；不要重新下载视频或重新 ASR。
- QA 用脚本输出短摘要，例如坏换行数量、最大变速比、裁尾片段、时长差；不要读入大文件全文。
- 磁盘低时先清理旧的失败输出和临时 chunk，再继续，避免最后 FFmpeg 失败造成重复上下文和重复计算。

## 依赖安装

环境缓存在 skill 目录的 `.venv/`，复用哈希检测，跨任务不重复安装。首次或 requirements 变更时自动重建。

```bash
# 一键安装，后续任务直接 source .venv/bin/activate
./scripts/setup_env.sh
```

TTS 后端切换：

```bash
VIDEO_DUBBER_TTS_BACKEND=mlx ./scripts/setup_env.sh
VIDEO_DUBBER_TTS_BACKEND=pytorch ./scripts/setup_env.sh
VIDEO_DUBBER_TTS_BACKEND=none ./scripts/setup_env.sh
```

手动按需安装（不通过 setup_env.sh 时）：

```bash
uv pip install -r requirements.txt
uv pip install -r requirements-f5-mlx.txt     # MLX 后端
uv pip install -r requirements-f5-pytorch.txt  # PyTorch 后端
```

## 长任务运行协议

超过 3 分钟的任务不要频繁自然语言汇报。启动后台任务后，把对话让给脚本；除非用户追问，只在阶段变化、失败、完成或 `last_seen` 超时风险时更新。读取进度时只看 `status.json`：

```json
{
  "status": "running",
  "message": "generating aligned tts",
  "stage": "tts"
}
```

详细日志保存在 `run.log`，除非失败排查，不要整段读入上下文。

进度更新要低噪声，但长字幕翻译要有可诊断的批次进度。不要输出“已生成 4 段，继续跑全量”这类只证明任务仍在运行的信息；翻译进度看 `translated/total/batch/batches`，FFmpeg 长视频合成只确认开始和结束，除非文件不增长或状态超时。

续跑前先确认没有同一 job 的旧进程还在运行。若无法查看进程，就至少检查 chunk 数量和最终输出文件时间戳；避免两个 TTS 进程同时写同一目录。

### 心跳监护（Heartbeat Watchdog）

长任务（翻译 >500 条、TTS >50 段、FFmpeg 长视频合成）启动前应注册监护，参照三层模式：

| 层 | 形式 | 依赖会话 | 职责 |
|----|------|----------|------|
| **L2** | 脚本内 `last_seen` 更新 | 否 | 每个关键阶段更新 `status.json` 的时间戳 |
| **L1** | 同一会话的后台监视器 | 是 | 每分钟轮询 `last_seen`，超时则重启卡死阶段 |
| **L0** | cron / `launchctl` 兜底 | 否 | 监视器不可靠时仍能发现过期 |

`status.json` 扩展字段：

```json
{
  "status": "running",
  "stage": "tts",
  "last_seen": "2026-01-15T10:23:00Z",
  "stage_timeout_min": 30
}
```

阈值：翻译阶段 15 分钟无更新 → 卡死；TTS 阶段 30 分钟无更新 → 卡死；最终合成 10 分钟无更新 → 卡死。正常运行时不要按分钟向用户自然语言汇报。

`run_pipeline.py` 在翻译每批、TTS 每生成一个 chunk、合成前后各写一次 `last_seen`。L1 监视器每分钟读 `status.json`，超时 `stage_timeout_min × 3` 则执行阶段重启命令并写入 `heartbeat.log`，不做其他操作。L0 若发现 `last_seen` 超过 2 小时，无论监视器是否存在，都重新拉起脚本。

意外中断后可以继续：不要换 job 目录，不要删除阶段产物，使用同一条命令重跑。脚本会复用：
- `raw_video.mp4` / `raw_audio.wav`
- `raw_audio.srt` 或用户传入的 `--source-srt`
- `translations_<lang>.json`
- `ref_audio.wav` / `ref_text.txt`
- 已生成的 `chunk_<lang>_*.wav`
- 完整的 `merged_tts_<lang>.wav` 和最终 mp4

如果用户改变 `--target-language`、`--subtitle-mode`、源字幕或参考文本，相关缓存会按语言/hash 重新生成，避免串用旧结果。

## 常见坑

- 不要在 TTS 超长时推迟后续字幕时间。
- 不要把 `display_text` 做短字幕、`tts_text` 做长口播后还声称字幕和配音一致；这是“语音比字幕说得多”的直接原因。
- 不要逐条处理 YouTube 的碎字幕来生成中文克隆配音；必须先做语义合并，否则只能在“加速、裁尾、越界”三种坏结果里选一个。
- 不要把英文技术词按字符硬拆；中英混排字幕必须 token-aware wrapping，保护 `session`、`profile`、`Postgres`、`Google Cloud`、`Memory Bank Service`、`PreloadMemoryTool` 等词。
- 不要把 ASS 换行 `\N` 转义成 `\\N`；画面上出现可见反斜杠时，优先检查 ASS escaping 顺序。
- 不要把 strict sync 的合并字幕包裹得太窄；使用自适应语义换行，避免固定 24/30 这类窄列宽度。1080p 字号 40 的中文字幕单语常见有效宽度约 52 display-width，但这只是当前样式的计算结果，不是通用常量。
- 不要默认保存所有 chunk；磁盘紧张时会导致 `LibsndfileError`、Python 临时目录不可用或 FFmpeg `+faststart` 失败。
- 不要强制 `HF_HUB_OFFLINE=1`，除非确认模型已经缓存。
- 不要默认安装 PyTorch F5；它体积大，应与 MLX 路径拆开。
- 不要把音色更差的新版本当成升级版覆盖旧版本；旧版音色好时应保留，并把新修复做成 v2/v3 文件便于 A/B 对比。
- 不要看到 `whisper-cli` Metal 崩溃就误判模型坏了；先加 `--no-gpu` 做 CPU ASR smoke test。
- 不要让短窗口中文段使用全片慢速 TTS 后再裁尾；这会重新制造“句子不完整”。短窗口要先缩短文本或局部提高 TTS speed。
- 不要同时启动多个同一 job 的 TTS 续跑脚本；并发写 chunk 会让进度和报告变得混乱。
- 不要用升采样冒充真实高清；下载阶段要记录实际源格式和分辨率。
- 不要在克隆版里丢掉无语音区间的背景音乐；没有分离模型时至少恢复字幕 gap 的原音频。
- 不要为了字幕换行问题写一次性脚本；优先用 `scripts/rebuild_outputs.py`。
