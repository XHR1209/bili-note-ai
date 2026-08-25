# Bili Note AI：B站视频 → AI 生成结构化学习笔记

## 0. 项目概述

构建一个自动化 CLI 工具，核心能力分两个里程碑：

- **里程碑 M1（本期实现）**：用户输入 B 站视频链接（BV 号），工具产出两份笔记：
  1. **全视频图文笔记 (Full Notes)**：Markdown 格式，含带时间戳的章节标题、每章核心观点文字、以及对应时间点的关键视频帧截图。
  2. **极简速览摘要 (Executive Summary)**：500 字以内的结构化摘要，含"3 分钟看懂"的核心逻辑链和结论。
- **里程碑 M2（远期预留，本期仅设计接口）**：用户不提供 BV 号，而是输入**关键词 / 偏好 / 限制条件**，工具自动搜索并筛选出候选视频清单，**由人工审核视频质量**后，再走 M1 的同一套笔记生产流水线。

**两条核心设计原则贯穿全文：**

1. **对抗 B 站版本变化**：所有与 B 站交互的代码集中在 `fetcher` 层之后的一个适配器内，通过抽象接口隔离；抓取引擎使用维护活跃的 `yt-dlp`（社区会跟进 B 站接口变化），绝不直接散落调用 B 站私有 API。
2. **阶段式流水线**：整个流程被拆成"发现 → 审核 → 抓取 → 理解 → 渲染"的独立阶段，M2 只是往流水线头部插入新阶段，下游代码零改动。

---

## 1. 技术栈 (Tech Stack - Strict)

- **语言与版本**：`Python 3.10+`，全项目强制使用类型注解（Type Hints）。
- **核心依赖**：
  - 抓取/下载：`yt-dlp`（元数据 + 视频 + 字幕），`ffmpeg`（系统依赖，音频转换与关键帧探测）。
  - 字幕/转写：**字幕获取链**（见 §6.1）——内置字幕优先；无字幕时使用本地 Whisper 转写，**转写后端可插拔：默认 `faster-whisper`（快约 4 倍、内存低），备用 `openai-whisper`（官方实现，`pip install .[whisper-openai]` 可选安装）**，模型默认 `base`，均可在配置中调整（ADR #7）。
  - 帧提取：`opencv-python`（抽帧）+ `scenedetect`（PySceneDetect，**场景检测采样**，见 §6.2，ADR #8）。
  - AI 调用：统一使用 `OpenAI SDK`（兼容 Claude / DeepSeek / GPT 等一切兼容端点），从环境变量读取 `API_KEY`、`BASE_URL`，模型名在 `config.yaml` 中配置。
  - 文档生成：仅限 `Markdown` 纯文本 + 本地图片相对引用（不生成 PDF / Word / HTML）。
- **配置**：所有可调参数（采样间隔、LLM 分块大小、模型名、路径、Cookie）集中在 `config.yaml`（见 §8），代码中禁止硬编码。

---

## 2. 系统架构

### 2.1 阶段式流水线

```
M2 新增阶段 ─────────────┐
                         ▼
┌─────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐
│ Discover│──▶│  Review │──▶│  Fetch  │──▶│ Process │──▶│ Render  │
│ 发现搜索 │   │ 人工审核 │   │ 抓取元数 │   │ 内容理解 │   │ 渲染输出 │
└─────────┘   └─────────┘   │ 据/字幕  │   └─────────┘   └─────────┘
                            └─────────┘
    M1 入口：用户直接给 BV 号，从 Fetch 阶段进入
    M2 入口：用户给 关键词/偏好/限制，从 Discover 阶段进入，审核通过后与 M1 汇合
```

- 每个阶段输入、输出均为 §5 定义的 Pydantic 模型，阶段之间**只通过模型传输数据**，不共享内部状态。
- 中间产物全部落盘缓存（`temp/{bvid}/`），支持**断点续跑**：重复执行时跳过已完成阶段（省钱、抗中途失败）。

### 2.2 依赖方向

```
main.py (CLI)
   │
pipeline.py (阶段编排，只依赖 models)
   │
   ├── fetcher/   ──▶ 实现抽象接口 VideoSource（B站只是其中一个实现）
   ├── processor/ ──▶ 纯函数式处理，不感知 B 站
   └── discover/  ──▶ (M2) 同样只依赖 VideoSource 抽象
```

依赖规则：**任何模块不得直接 import yt-dlp / 请求 B 站 URL**，必须经由 `fetcher/base.py` 中定义的抽象接口。这是"B 站变了只改一个文件"的机制保障。

---

## 3. 目录结构

```
AI_Bilibili/
├── README.md              # 本文档
├── pyproject.toml
├── config.yaml            # 运行配置（§8）
├── src/
│   ├── __init__.py
│   ├── main.py            # CLI 入口（argparse / typer）
│   ├── models.py          # Pydantic 数据模型（§5）
│   ├── config.py          # 加载 config.yaml + 环境变量
│   ├── pipeline.py        # 阶段编排、缓存与断点恢复（§7）
│   ├── fetcher/
│   │   ├── __init__.py
│   │   ├── base.py        # VideoSource 抽象接口（§6.1）
│   │   ├── bili_source.py # B站实现：yt-dlp 封装 + 字幕链（§6.1）
│   │   └── subtitles.py   # 字幕获取链实现（§6.1）
│   ├── processor/
│   │   ├── __init__.py
│   │   ├── transcriber.py    # 转写后端抽象：faster-whisper / openai-whisper（§6.2）
│   │   ├── frame_extractor.py# 场景检测采样 + 关键帧定位（§6.2）
│   │   └── analyzer.py       # LLM 分块摘要 + 章节化（§6.2）
│   ├── discover/            # M2：本期只定义接口，实现留待 M2
│   │   ├── __init__.py
│   │   ├── searcher.py     # 搜索（关键词/偏好 → 候选列表）
│   │   ├── filter.py       # 按限制条件过滤 + 相关性排序
│   │   └── review.py       # 生成人工审核清单（候选报告）
│   ├── llm.py              # OpenAI SDK 统一封装（含重试/并发）
│   └── renderer/
│       ├── __init__.py
│       ├── full_notes.py   # 全视频图文笔记渲染（§6.3）
│       └── exec_summary.py # 极简速览摘要渲染（§6.3）
├── tests/
│   ├── fixtures/           # 录制好的 B 站响应/转写样本（§9）
│   └── ...
├── temp/                   # 中间产物缓存（gitignore）
│   └── {bvid}/audio.wav, transcript.json, chapters.json, ...
└── outputs/                # 最终产物
    ├── images/{bvid}/      # 关键帧截图
    └── notes/{bvid}_full_notes.md
        {bvid}_exec_summary.md
        candidates.md       # (M2) 人工审核清单
```

---

## 3.5 运行环境注意事项（实测踩坑）

**首次配置（克隆后）**：
```bash
cp config.example.yaml config.yaml   # 填入 API_KEY/BASE_URL（及可选 B站 Cookie）
```
`config.yaml` 已被 `.gitignore` 排除（含密钥），仓库只保留脱敏的 `config.example.yaml`。

- **国内网络必须配置 HuggingFace 镜像**：faster-whisper 首次运行会下载模型（~150MB），
  直连 huggingface.co 不可达。运行前：
  ```bash
  export HF_ENDPOINT=https://hf-mirror.com
  ```
- **镜像 + huggingface_hub 新版（1.28+）会走 xethub CAS 后端导致 401**：
  `hf-mirror.com` 不支持 xet 存储，需卸载 `hf-xet` 包使其回退传统下载：
  ```bash
  pip uninstall -y hf-xet
  ```
- 依赖 yt-dlp 为 2025+ 新版时，`postprocessor_args` 是 `YoutubeDL` **顶层参数**，
  不在 postprocessor 定义内（`FFmpegExtractAudioPP` 不再接受该 kwarg）。
- 无 `API_KEY`/`BASE_URL` 环境变量时从 `config.yaml` 的 `llm.api_key`/`llm.base_url` 读取。

## 4. 运行方式 (CLI)

```
# M1：直接生产笔记
bili-learn <BV号或完整链接> [--outdir outputs] [--no-frames] [--force]

# M2（未来）：关键词发现 → 生成候选清单 → 人工挑选后批量生产
bili-learn discover "强化学习 入门" --prefer "up主:李宏毅" --min-duration 20 --max-duration 60 --min-views 50000 --top 10
bili-learn process candidates.md        # 读取人工勾选后的清单，走 M1 流水线
```

- `--no-frames`：跳过抽帧（无字幕视频 + 纯文字笔记场景）。
- `--force`：忽略缓存强制重跑某个阶段。
- 无参数时打印帮助与当前配置摘要。

---

## 5. 核心数据模型 (Data Models - Pydantic)

所有模块间数据传输必须基于以下 Schema（定义在 `src/models.py`）。**M2 的搜索/审核模型本期就定义好**，保证 M2 实现时无需改动下游模型。

### 5.1 M1 模型（笔记生产）

```python
class VideoMeta(BaseModel):
    bvid: str
    title: str
    duration: float            # 秒
    author: str
    cover_url: str | None
    desc: str = ""             # 视频简介
    # —— 以下为 M2 筛选用字段，fetcher 尽力填充，缺失可为 None ——
    play_count: int | None
    danmaku_count: int | None
    pubdate: datetime | None
    tags: list[str] = []
    like_count: int | None
    coin_count: int | None

class TranscriptSegment(BaseModel):
    start_time: float          # 秒
    end_time: float
    text: str

class Transcript(BaseModel):
    source: str                # "cc" | "ai-cc" | "whisper"
    language: str
    segments: list[TranscriptSegment]

class KeyFrame(BaseModel):
    timestamp: float           # 视频内秒数
    image_path: str            # 相对 outputs/ 的本地路径
    description: str           # LLM 生成的画面描述（可为空串）

class Chapter(BaseModel):
    index: int                 # 1 起
    title: str                 # 章节名
    start_time: float
    end_time: float
    summary: str               # LLM 生成的核心观点（若干句）
    key_points: list[str]      # 本章要点（分点列出）
    key_frames: list[KeyFrame] # 0~3 张关键帧
    raw_speaker_notes: str = "" # 章节内文字稿原文（渲染时可选隐藏）

class FinalOutput(BaseModel):
    meta: VideoMeta
    chapters: list[Chapter]
    quick_summary: str         # 极简摘要（渲染时再结构化）
    generated_at: datetime
    stats: dict = {}           # 耗时、token 用量、字幕来源等
```

### 5.2 M2 模型（发现与审核，本期仅定义）

```python
class SearchQuery(BaseModel):
    keywords: list[str]             # 核心关键词（必填 ≥1）
    preferences: list[str] = []     # 偏好："up主:xxx"、"类型:课程" 等自由文本
    filters: dict = {}              # 结构化限制：
                                    #   duration_min/duration_max(分钟)
                                    #   min_views / min_likes
                                    #   pubdate_after(YYYY-MM-DD)
                                    #   authors / tags 白名单
    top_k: int = 10                 # 候选数量上限

class SearchCandidate(BaseModel):
    meta: VideoMeta
    score: float                    # 相关性得分
    match_reason: str               # LLM 依据简介/标签/字幕片段说明为何符合
                                    #   （用于人工审核时快速判断）

class ReviewSelection(BaseModel):
    query: SearchQuery
    candidates: list[SearchCandidate]
    selected_bvids: list[str]       # 人工勾选结果（candidates.md 中被标记 ✅）
```

---

## 6. 模块设计

### 6.1 `fetcher/` —— B 站交互层（对抗变化的堡垒）

#### 抽象接口 `VideoSource`（`base.py`）

```python
class VideoSource(Protocol):
    def fetch_meta(self, video_id: str) -> VideoMeta: ...
    def fetch_transcript(self, video_id: str, cookie: str | None) -> Transcript: ...
    def download_audio(self, video_id: str, out_path: Path) -> Path: ...
    def download_video(self, video_id: str, out_path: Path) -> Path: ...
    def search(self, query: SearchQuery, cookie: str | None) -> list[SearchCandidate]: ...  # M2
```

`bili_source.py` 用 `yt-dlp` 实现该接口。**未来无论 B 站接口怎么变、或要接入 YouTube，都只需新增一个实现类**，pipeline 与 processor 完全无感。

#### 字幕获取链（`subtitles.py`）—— 降级策略

```
1. cc 字幕（up主上传）：yt-dlp 直接提取，无需登录
   │ 失败/不存在
2. ai-cc 字幕（B站AI字幕）：需登录 Cookie（config.yaml 提供）
   │ 失败/不存在
3. 本地 Whisper 转写：下载音频（ffmpeg 转 wav 16k 单声道）→ 转写（后端可插拔，见 §6.2）
```

- 每一级失败都记录原因，供 `stats` 和日志展示（"为何降级到 Whisper"）。
- 字幕获取与音频下载**并行**：即使字幕成功，音频也在后台下载备查（`--no-frames` 时可不下载）。

#### 对抗 B 站版本变化的具体措施

| 变化场景 | 应对 |
|---|---|
| B 站接口字段调整 / 风控升级 | 全部经由 yt-dlp（社区活跃维护）；私有 API 调用（如有）必须收敛在 `bili_source.py` 内单函数，并以 fixtures 测试锁定 |
| 登录态要求（大会员/高清/部分视频） | 支持从 `config.yaml` 注入 Cookie；无 Cookie 时自动降级到可用的最低清晰度 |
| 视频格式导致无法快速 seek（flv/h265） | 下载时优先请求 `mp4/h264` 格式（`format` 偏好写进配置），保证 opencv 随机抽帧可用 |
| 字幕接口路径变更 | 字幕链各级独立 try/except，失败即降级，不中断主流程 |

### 6.2 `processor/` —— 内容理解层（纯处理，不感知平台）

#### `transcriber.py` —— 转写（后端可插拔）

- 定义 `Transcriber` 抽象接口，两个实现：`FasterWhisperTranscriber`（默认）、`OpenaiWhisperTranscriber`（备用），由 `config.transcription.backend` 选择，工厂函数统一创建（ADR #7）。
- 输入：`temp/{bvid}/audio.wav`；输出：`Transcript`（JSON 落盘缓存）。
- 模型大小、语言、是否分块转写（长音频内存优化）全部读配置。
- 转写结果统一为 `TranscriptSegment`，与字幕来源的 Transcript 结构完全一致 → **下游章节化逻辑不区分字幕来自 cc、faster-whisper 还是 openai-whisper**。

#### `frame_extractor.py` —— 场景检测采样与关键帧定位

**采样策略（高质量优先）**：以**场景检测为主、时间采样兜底**（ADR #8）。

1. **场景检测（主）**：用 PySceneDetect（OpenCV 后端）全视频扫描镜头切换边界 → `temp/{bvid}/scenes.json` 缓存（每视频仅检测一次）；过短场景（`min_scene_len`，默认 2 秒）并入相邻场景；**每个场景取 1 帧代表帧**形成候选池 —— 板书 / PPT 换页 / 代码切换等画面语义边界天然成为候选帧，图文笔记的"图"质量上限高。
2. **时间采样（兜底）**：均匀采样（`≤10分钟每30秒1张，>10分钟每60秒1张`，可配置）作为候选池补充 —— 场景检测失败、场景数爆炸（快节奏视频）或 `--fast-sampling` 模式时启用。
3. **关键帧精选（LLM 参与）**：`analyzer` 章节化完成后，由 LLM 依据章节文字稿**指定 0~3 个代表时刻**（例如"讲解示意图 12:30 处"），extractor 在候选池中就近取帧（或直接 seek 精确帧）作为该章 `key_frames`；LLM 未指定时回退到章节中点帧。
4. **关键帧描述**：对精选后的少数帧调用 LLM 生成 `description`（每帧一句，说明画面内容）。

#### `analyzer.py` —— LLM 分块摘要与章节化（核心算法）

长视频文字稿无法一次送入 LLM，采用**分块-摘要-合并**两阶段策略：

```
阶段A 分块摘要（并行）：
  文字稿按时间滑窗分块（默认每块 ~2500 字，相邻块重叠 ~300 字，可配置）
  每块调 LLM 产出：块主题 / 一句话摘要 / 3~5 条要点 / 建议章节标题
   —— 各块互不依赖，可并发调用（llm.py 提供并发与重试）

阶段B 合并章节（串行）：
  将阶段A结果整体交给 LLM，合并相邻同主题块 → Chapter 列表
  强制约束（prompt 中声明 + 代码侧校验）：
    - 章节按时间有序、不重叠、覆盖全程
    - 每章 1 个标题 + 核心观点 summary + 要点列表
    - 输出严格 JSON（解析失败自动重试 1 次）
  - 可选辅助信号：把场景检测的边界时间列表一并提供给 LLM —— 讲解者切换
    主题常伴随画面切换，场景边界可作为章节候选边界的参考（仅建议、不强约束）
```

- 模型输出一律要求 **JSON**，用 `model_dump_json` 校验后回填模型，解析失败重试。
- `llm.py` 统一封装：环境变量 `API_KEY`/`BASE_URL` + 配置模型名；内置指数退避重试、并发控制、token 用量统计（写入 `stats`）与**每视频预算控制**（ADR #9）：
  - `config.llm.budget.max_calls_per_video`（默认 200，宽松、不误伤 M1 单视频）。
  - `llm.py` 维护按视频的调用计数器（pipeline 初始化），超限后按降级优先级依次砍步骤：① 关键帧描述生成 → ② 块级摘要要点精简 → ③ 章节合并粗粒度（摘要更短、允许章节变少）——笔记"能用但略糙"，**绝不整体失败**。

### 6.3 `renderer/` —— 输出层

| 产物 | 结构 |
|---|---|
| `full_notes.md` | 标题(含链接/作者/时长) → 目录（章节 + 时间戳）→ 每章：`## 第N章 标题`、时间范围、要点列表、summary、`![帧描述](images/{bvid}/xx.png)` → 结尾附字幕来源与统计 |
| `exec_summary.md` | **≤500 字**：一句话结论 → "3分钟看懂"逻辑链（3~6 步箭头链）→ 关键结论 bullet → 信息来源（视频链接） |

- 渲染为纯函数：`FinalOutput → markdown 字符串`，不做任何 IO（便于测试与复用）。
- 图片路径全部相对 `outputs/` 写死规范，Markdown 在任何编辑器 / Typora / VS Code 中可直接查看。

### 6.4 `discover/` —— M2 预留（本期仅定义接口与流程）

本期**不实现**，但在文档与目录中锁定以下契约，M2 直接填实现：

```
1. searcher.search(SearchQuery) → list[SearchCandidate]
   底层用 B 站搜索接口（收敛在 bili_source.py 的 search() 内），
   B 站返回的"视频卡片"字段只映射到 VideoMeta，不泄漏到上层
2. filter 层：按 SearchQuery.filters 硬过滤 → 按 score 排序取 top_k
   score = 文本相关性(LLM 或关键词匹配) + 质量信号(播放/点赞/时长) 加权
3. review.py：把候选渲染成 outputs/candidates.md
   （表格：✅勾选列 | BV | 标题 | up主 | 播放 | 时长 | 发布 | match_reason）
   用户手动标注后，`bili-learn process candidates.md` 读取勾选项批量进 M1 流水线
```

关键设计点：**"人工筛选"是流水线的一个正式阶段**（Review），而不是流程外的操作 —— 它的产物（ReviewSelection）就是下一阶段的输入，可缓存、可审计。

---

## 7. 流水线编排 (`pipeline.py`)

- 每个阶段实现为一个函数：`(输入模型, 缓存目录, config) → 输出模型`，产物序列化到 `temp/{bvid}/`。
- 阶段间通过**产物文件是否存在**判断是否可跳过（`--force` 强制重跑）：
  ```
  fetch(meta.json, transcript.json, audio.wav)
    → process(scenes.json, transcript → chapters.json, keyframes.json)
      → render(full_notes.md, exec_summary.md)
  ```
- 阶段失败不炸掉全局：明确报错 + 给出"可用 --force 重跑某阶段"提示，且已生成的部分产物保留。
- M2 时 `discover + review` 作为前置阶段接入，**pipeline.py 只新增两个 stage 函数，M1 代码零改动**。

---

## 8. 配置 (`config.yaml` + 环境变量)

```yaml
llm:
  model: "deepseek-chat"        # 任意 OpenAI 兼容模型名
  max_tokens: 4096
  temperature: 0.3
  chunk_chars: 2500             # analyzer 分块大小
  chunk_overlap_chars: 300
  max_concurrency: 4            # LLM 并发数
  timeout_seconds: 120
  budget:
    max_calls_per_video: 200    # 每视频 LLM 调用上限（超限优雅降级，ADR #9）

transcription:
  backend: "faster-whisper"     # "faster-whisper"(默认) | "openai-whisper"(备用)
  whisper_model: "base"         # tiny/base/small/medium/large
  whisper_language: null        # null = 自动检测

frames:
  strategy: "scene+time"        # 主：场景检测；时间采样兜底（ADR #8）
  scene_detection:
    detector: "content"         # PySceneDetect ContentDetector
    threshold: 27.0             # 灵敏度（需真实样本标定，见 §12）
    min_scene_len: 2.0          # 最短场景时长(秒)，过短并入相邻场景
  time_sampling:                # 兜底采样参数
    sample_every_short: 30      # ≤10分钟视频采样间隔(秒)
    sample_every_long: 60       # >10分钟视频采样间隔(秒)
    duration_threshold: 600     # 长短视频阈值(秒)
  max_keyframes_per_chapter: 3

paths:
  temp_dir: "temp"
  output_dir: "outputs"

bilibili:
  cookie: ""                    # 可选，登录 Cookie（M2 搜索 / ai-cc 字幕需要）
  format_pref: "bv*+ba/b"       # yt-dlp 格式偏好，优先 mp4
```

环境变量（优先级高于 config.yaml）：`API_KEY`、`BASE_URL`、`MODEL`。

---

## 9. 测试策略

- **单元测试（必须）**：所有纯逻辑（分块算法、章节校验、渲染函数）直接测。
- **fixtures 锁库（关键）**：把录制的 B 站响应 JSON、示例 Transcript、Whisper 输出存 `tests/fixtures/`，测试走本地 fixture，**不依赖网络、不依赖 B 站现状** —— B 站改版导致行为变化时，先在 fixture 层暴露出 diff。
- **集成测试（`--network` 标记，默认跳过）**：真实抓取 + 真实 LLM 调用，需要环境变量，CI 中不跑。
- **对抗变化的回归用例**：为字幕链的每一级失败路径写降级测试（模拟 cc 缺失 → 走 whisper）。

---

## 10. 里程碑计划

| 里程碑 | 内容 | 验收标准 |
|---|---|---|
| **M1.1** | fetcher（yt-dlp 封装 + 字幕链）+ transcriber | 任意可访问的 BV 号能产出 Transcript（cc 或 whisper） |
| **M1.2** | frame_extractor（场景检测采样）+ analyzer（章节化，含场景边界辅助信号） | 章节时间覆盖全程、JSON 校验通过；场景检测结果缓存可用 |
| **M1.3** | renderer + pipeline 断点缓存 + CLI | 一条命令产出两份笔记，图片可正常显示 |
| **M2.1** | searcher + filter + candidates.md | 关键词搜索产出可审核清单 |
| **M2.2** | ReviewSelection 接入 + 批量流水线（含 dry-run 成本预估提示） | 勾选清单后一键批量产笔记；批量前展示预估调用数与费用 |

---

## 11. 已决策问题与理由 (ADR)

1. **用 yt-dlp 而非直接调 B 站 API**：社区维护活跃，自动跟进 B 站变化；私有 API 只作为补充且收敛在单点。
2. **依赖倒置（VideoSource 抽象）**：平台差异被隔离，M2 的搜索与 M1 共用同一套接口，未来接 YouTube 只加一个类。
3. **字幕降级链 cc → ai-cc → whisper**：知识区视频常无 cc 字幕，Whisper 兜底保证可用性；降级原因全程可追踪。
4. **LLM 输出强制 JSON + 代码侧校验**：LLM 幻觉章节结构时直接重试，不把脏数据带进渲染层。
5. **中间产物全落盘**：断点续跑 + 审计 + 省钱（同一视频二次生成不重复付转写费）。
6. **"人工筛选"作为流水线正式阶段**：M2 的审核是数据流的一环（ReviewSelection），可回放、可缓存，而非临时脚本。
7. **转写后端可插拔（faster-whisper 默认）**：同硬件快约 4 倍、内存更低、支持词级时间戳；openai-whisper 保留为官方实现兜底（极端环境一键切回）；下游只看 Transcript 模型，切换零改动；两后端的性能/准确率对比可作为论文实验点。
8. **场景检测为核心采样策略**：用户要求高质量图文笔记，场景边界（板书/PPT/代码切换）天然贴合关键帧语义；时间采样保留为兜底（检测失败、场景数爆炸、`--fast-sampling` 模式）；检测结果落盘缓存，"慢"的成本只付一次。
9. **LLM 预算上限 + 优雅降级**：M2 批量成本可预期；超限按降级优先级砍步骤（帧描述 → 块摘要 → 合并粗粒度），笔记"能用但略糙"而非失败；默认值宽松不误伤 M1。
10. **M2 搜索匿名优先 + Cookie 可选（实现随 M2）**：人工低频搜索风控风险低；遇风控特征（连续空结果/验证码响应）时提示用户填 Cookie 重试；Cookie 只存配置、不做自动刷新（模拟登录维护成本高且踩风控）。

## 12. 待决策问题 (Open Questions)

> 注：原 Q1（faster-whisper 后端）、Q2（场景检测采样）、Q3（搜索 Cookie）、Q4（预算控制）均已定案，见 ADR #7~#10。以下为剩余开放问题：

- **场景检测阈值标定**：`threshold` / `min_scene_len` 的默认值需用真实知识区视频样本（课堂录制、PPT 讲解、代码演示各若干）标定，M1.3 后数据驱动调参。
- **超长视频（>2h）策略**：单段转写的内存/耗时与 LLM 分块数激增的处理（是否分段转写、分幕章节化），待遇到真实长视频样本后定。
- **M2 批量 dry-run 预估的误差容忍度**：按"时长→字数→块数"换算的预估有误差，批量确认时展示粒度（次数 vs 金额）待 M2 实现时定。
