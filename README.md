# Video Dubber

Video Dubber 是一个强大的自动化视频多语言配音与字幕翻译工具，支持将 YouTube 等平台的视频，或者本地视频，自动翻译并生成带中文字幕和高质量中文克隆配音（或日语/韩语等目标语言）的视频。

## 核心能力 (Features)

- **广泛的平台支持**：支持下载 YouTube 等站点的视频，也支持处理本地 `.mp4` 视频。
- **高质量语音转写 (ASR)**：优先支持 NVIDIA Riva 高精度云端语音识别（带词级时间戳），支持回退到本地 Whisper（支持 Apple Silicon/Mac MLX 硬件加速或 NVIDIA GPU/faster-whisper）。
- **智能语义翻译与对齐**：使用大语言模型（如 Gemini 3.5 Flash）进行上下文感知的智能翻译。独创的自适应语义换行与对齐策略，确保生成的配音（TTS）和画面字幕精准同步。
- **零样本声音克隆 (Voice Cloning)**：支持通过 F5-TTS 进行高质量的声音克隆，保留原说话人的音色。自动处理背景噪音并恢复无语音间隙的背景音 (Gap Audio Preservation)。
- **高度可定制的硬字幕**：支持原音单语字幕、原音双语字幕、配音单语字幕等多种模式。自带固定规范的高清字体渲染配置。
- **断点续传与长任务稳定**：核心环节全量缓存（下载、ASR、翻译、分段 TTS Chunk 等），支持意外中断后安全续跑。

## 安装方法 (Installation)

```bash
npx skills add /path/to/agent-playbook/video-dubber -a opencode -a claude-code -a codex -g
```

安装后 Agent 会自动处理 Python 依赖。需要前置安装的依赖：

- **FFmpeg**（必需）：`brew install ffmpeg`
- **Node.js**（用于 `npx`）：`brew install node`

## 翻译模型配置 (Translation Model)

复制 `.env.example` 为 `.env`，填入任一 API key 即可自动生效：

```bash
cp .env.example .env
# 编辑 .env 填入 GEMINI_API_KEY 或 OPENAI_API_KEY 等
```

脚本会自动检测已配置的 key，按 `model-config.yaml` 中的顺序选择对应模型。同时设置多个 key 时排在前面的优先。

如需切换或强制指定模型，使用 `--translation-model`：

```bash
python scripts/run_pipeline.py --translation-model openai ...
```

> 当前翻译请求由脚本直连配置的 API 完成。若希望使用 Agent 自身的模型（如 Claude CLI 的 Claude），需在 `.env` 中配置对应 API key。

## 工作流程

```mermaid
flowchart TB
    subgraph Input[输入]
        A[环境预检<br/>ffmpeg / yt-dlp / 字体 / 磁盘] --> B{输入类型}
        B -->|URL| C[yt-dlp 下载视频<br/>并提取 16k mono WAV]
        B -->|--input-video| D[读取本地视频]
        C --> E{已有平台字幕？}
        D --> E
        E -->|是| F[选取最干净字幕轨道]
        E -->|否| G[ASR 语音转写]
        G --> H{NVIDIA_API_KEY？}
        H -->|是| I[NVIDIA Riva 云端转写]
        H -->|否| J[本地 Whisper 转写<br/>Apple Silicon / CPU]
        I --> K
        J --> K
        F --> K
    end

    subgraph Translate[翻译]
        K[源字幕 SRT] --> L[分批 LLM 翻译<br/>model-config.yaml]
        L --> M[缓存 translations_&lt;lang&gt;.json]
        M --> N{克隆配音？}
    end

    subgraph SubOnly[字幕-only 路径]
        N -->|否| O[生成 ASS 字幕<br/>自适应语义换行]
        O --> P[FFmpeg 硬字幕烧录<br/>保留原音轨]
    end

    subgraph FullClone[完整克隆路径]
        N -->|是| Q[参考音频准备<br/>用户提供 / 自动抽取]
        Q --> R[语义段合并<br/>strict sync]
        R --> S[F5-TTS 语音克隆<br/>逐段生成 + 缓存]
        S --> T[TTS 时间轴对齐<br/>atempo 适配]
        T --> U[风险段补丁<br/>缩短文本 / 局部重生]
        U --> V[Gap Audio 恢复<br/>保留背景音乐/环境声]
        V --> W[合并完整 TTS 音轨]
        W --> X[FFmpeg 烧录<br/>克隆配音 + 字幕]
    end

    subgraph Output[输出]
        P --> Y[output_original_*]
        X --> Z[output_cloned_*]
        Y --> AA[验证报告<br/>时长 / 字幕 / TTS 质量]
        Z --> AA
    end
```
