# 设计文档：Bili Note AI

> 使用者指南见 [README.md](README.md)。本文为架构与设计视角的文档：
> 数据模型、模块划分、决策记录（ADR）、里程碑规划。

## 0. 项目概述

构建一个自动化 CLI 工具，核心能力分两个里程碑：

- **里程碑 M1（已完成）**：用户输入 B 站视频链接（BV 号），工具自动产出**一份章节化图文学习笔记**：
  每章 = 讲义式导读（LLM 基于全文生成的讲解段落）+ 带解释的要点 + LLM 精选的关键帧截图。
- **里程碑 M2（远期预留，本期仅设计接口）**：用户输入**关键词 / 偏好 / 限制条件**，
  工具自动搜索并筛选候选视频清单，**由人工审核**后再走 M1 的同一套笔记生产流水线。

**两条核心设计原则**：

1. **对抗 B 站版本变化**：所有与 B 站交互的代码集中在 `fetcher` 层之后的一个适配器内，
   通过抽象接口隔离；抓取引擎使用维护活跃的 `yt-dlp`，私有 API 调用（字幕）收敛在单文件单函数。
2. **阶段式流水线**：整个流程被拆成"发现 → 审核 → 抓取 → 理解 → 渲染"的独立阶段，
   M2 只是往流水线头部插入新阶段，下游代码零改动。

## 1. 技术栈 (Tech Stack)

- **语言与版本**：`Python 3.10+`，强制类型注解。
- **核心依赖**：
  - 抓取/下载：`yt-dlp`，`ffmpeg`（系统依赖）。
  - 字幕/转写：**字幕获取链**（见 §6.1）——内置字幕优先；无字幕时本地 Whisper 转写，
    后端可插拔：默认 `faster-whisper`（快约 4 倍），备用 `openai-whisper`（官方实现）。
  - 帧提取：`opencv-python` + `scenedetect`（PySceneDetect 场景检测采样）。
  - AI 调用：统一 `OpenAI SDK`（兼容 Claude / DeepSeek / GPT），环境变量 `API_KEY`/`BASE_URL`
    或 `config.yaml` 的 `llm.api_key`/`llm.base_url`；模型名可配置。
  - 文档生成：仅 `Markdown` 纯文本 + 本地图片相对引用。
- **配置**：所有可调参数集中在 `config.yaml`（见 §8），代码中禁止硬编码。

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
    M1 入口：用户直接给 BV 号（可带 ?p=N 分 P），从 Fetch 阶段进入
```

- 阶段间只通过 §5 的 Pydantic 模型传输数据，不共享内部状态。
- 中间产物全部落盘缓存（`temp/{bvid}/`），支持**断点续跑**：重复执行跳过已完成阶段（省钱、抗中断）。

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

依赖规则：**任何模块不得直接 import yt-dlp / 请求 B 站 URL**，必须经由
`fetcher/base.py` 的抽象接口。这是"B 站变了只改一个文件"的机制保障。

## 3. 目录结构

```
bili-note-ai/
├── README.md              # 使用者指南
├── DESIGN.md              # 本文档
├── pyproject.toml
├── config.example.yaml    # 配置模板（脱敏，可提交）
├── config.yaml            # 本地配置（gitignore，含密钥）
├── src/
│   ├── main.py            # CLI 入口（python -m src.main <BV号> [--no-frames] [--force] ...）
│   ├── models.py          # Pydantic 数据模型（§5）
│   ├── config.py          # 加载 config.yaml + 环境变量
│   ├── llm.py             # OpenAI SDK 统一封装（重试/并发/token统计/预算控制）
│   ├── pipeline.py        # 阶段编排、缓存与断点恢复（§7）
│   ├── fetcher/
│   │   ├── base.py        # VideoSource 抽象接口（§6.1）
│   │   ├── bili_source.py # B站实现：yt-dlp 封装 + 私有 API 字幕路径（单点收敛）
│   │   └── subtitles.py   # 字幕降级链与解析（§6.1）
│   ├── processor/
│   │   ├── transcriber.py     # 转写后端抽象：faster-whisper / openai-whisper
│   │   ├── frame_extractor.py # 场景检测采样 + LLM 关键帧精选
│   │   └── analyzer.py        # 分块-摘要-合并章节化 + 讲义式导读生成
│   ├── discover/             # M2 预留（仅占位）
│   └── renderer/
│       └── condensed_notes.py # 唯一产物渲染（导读+要点+关键帧）
├── tests/                   # 单元测试 + fixtures 锁库
├── temp/{bvid}/             # 中间产物缓存（gitignore）
└── outputs/
    ├── images/{bvid}/       # 关键帧截图
    └── notes/{bvid}_notes.md
```

## 4. 运行方式 (CLI)

```
# M1：生产笔记（安装成功时可用等价的 bili-learn 命令）
PYTHONPATH=. .venv/bin/python -m src.main <BV号或完整链接> [--outdir outputs] [--no-frames] [--force] [-v]
PYTHONPATH=. .venv/bin/python -m src.main "https://www.bilibili.com/video/BV1kJ411E7AQ?p=6"   # 多 P

# M2（未来）：关键词发现 → 候选清单 → 人工挑选后批量生产
python -m src.main discover "强化学习 入门" --prefer "up主:李宏毅" --min-duration 20 --top 10
python -m src.main process candidates.md
```

## 5. 核心数据模型 (Data Models - Pydantic)

所有模块间数据传输基于以下 Schema（`src/models.py`）。**M2 的搜索/审核模型本期就定义好**。

```python
class VideoMeta(BaseModel):
    bvid: str
    title: str
    duration: float            # 秒
    author: str
    cover_url: str | None = None
    desc: str = ""
    # —— M2 筛选用字段，fetcher 尽力填充 ——
    play_count: int | None = None
    danmaku_count: int | None = None
    pubdate: datetime | None = None
    tags: list[str] = []
    like_count: int | None = None
    coin_count: int | None = None

class TranscriptSegment(BaseModel):
    start_time: float
    end_time: float
    text: str

class Transcript(BaseModel):
    source: str                # "cc" | "zh-CN" | "ai-zh" | "whisper"
    language: str = ""
    segments: list[TranscriptSegment]

class KeyFrame(BaseModel):
    timestamp: float
    image_path: str            # 完整路径（渲染时规范为相对笔记的 ../images/...）
    description: str = ""

class Chapter(BaseModel):
    index: int
    title: str
    start_time: float
    end_time: float
    summary: str               # 核心观点（1-2 句）
    narrative: str = ""        # 讲义式导读段落（LLM 生成，产物核心）
    key_points: list[str]      # 带解释的要点（每条 1-2 句）
    key_frames: list[KeyFrame]
    raw_speaker_notes: str     # 章节全文（带 [HH:MM:SS] 时间戳行）

class FinalOutput(BaseModel):
    meta: VideoMeta
    chapters: list[Chapter]
    summary: ExecSummary | None = None   # 预留（当前产物不使用）
    generated_at: datetime
    stats: dict

# —— M2 模型（本期仅定义）——
class SearchQuery(BaseModel): keywords, preferences, filters, top_k
class SearchCandidate(BaseModel): meta, score, match_reason
class ReviewSelection(BaseModel): query, candidates, selected_bvids
```

## 6. 模块设计

### 6.1 `fetcher/` —— B 站交互层（对抗变化的堡垒）

**抽象接口 `VideoSource`**（`base.py`）：`fetch_meta` / `fetch_transcript` /
`download_audio` / `download_video` / `search`（M2）。未来接入其他平台只需新增实现类。

**字幕获取链**（`subtitles.py` + `bili_source._fetch_bili_api_transcript`）：

```
1. UP主字幕（cc / zh-CN）：yt-dlp 直接提取，无需登录
2. B站AI字幕（ai-zh）：需登录 Cookie；yt-dlp 提取器不传 cookie，
   故走私有 API 路径（view → cid → player/wbi/v2 → subtitle_url），
   全部收敛在 bili_source.py 单函数内（ADR #11）
3. 本地 Whisper 转写：下载音频（ffmpeg 转 wav 16k 单声道）→ 转写
```

**对抗 B 站版本变化的实测措施**：

| 变化场景 | 应对 |
|---|---|
| B 站接口字段调整 / 风控升级 | 全部经由 yt-dlp；私有 API 调用收敛单函数，fixtures 测试锁定 |
| 登录态要求（ai-zh 字幕/部分视频） | config.yaml 注入 Cookie；无 Cookie 自动降级 whisper |
| AV1 编码（多数 opencv 无解码器） | 下载格式白名单 `vcodec^=avc1`（h264），保证抽帧可用 |
| 多 P 合集 | 支持 `BV号?p=N` 语法，传递分 P 参数 |
| 字幕接口路径变更 | 降级链各级独立 try/except，失败即降级 |

### 6.2 `processor/` —— 内容理解层（纯处理，不感知平台）

**`transcriber.py`**：`Transcriber` 抽象 + `FasterWhisperTranscriber`（默认）/
`OpenaiWhisperTranscriber`（备用），由 config 选择。转写输出统一 `Transcript`，
下游不区分字幕来源。实测：85 分钟音频约 22 分钟（4 核 CPU，约 4 倍实时速）。

**`frame_extractor.py`**：场景检测为主、时间采样兜底。
1. PySceneDetect 扫镜头切换 → 每场景 1 帧代表帧（候选池）
2. 时间采样兜底（检测失败/场景数爆炸/`--fast-sampling`）
3. LLM 依据章节内容为每章选 0~3 个代表时刻（就近取池中帧或精确 seek）
4. 帧描述：对精选帧调用 LLM 生成一句话画面描述（预算降级优先级 ①）

**`analyzer.py`**（核心算法，两阶段 + 导读生成）：

```
阶段A 分块摘要（并行）：文字稿按时间滑窗分块（默认 2500 字，重叠 300 字）
  → 每块输出：块主题 / 叙述体总结 / 带解释的要点
  → 提示词策略：不限制句子数与字数（ADR #13），由 LLM 自定篇幅，以讲清楚为准

阶段B 合并章节（串行）：相邻同主题块合并 → Chapter 列表
  强制约束：章节按时间有序、不重叠、覆盖全程；输出严格 JSON（校验失败重试/兜底）

阶段C 导读生成：每章基于全文调 LLM 生成讲义式讲解段落（narrative）
  → 这是笔记质量的核心：让没看过视频的读者也能理解本章
```

预算降级（ADR #9）：块数超预算时两两合并粗粒度重试；帧描述 → 导读 → 合并依次降级，
笔记"能用但略糙"，绝不整体失败。

### 6.3 `renderer/` —— 输出层

唯一产物 `{bvid}_notes.md`：标题/元信息 → 每章 = 时间戳 + 讲义式导读 +
带解释要点 + 关键帧截图（`../images/...` 相对路径）。纯函数渲染，不做 IO。

## 7. 流水线编排 (`pipeline.py`)

- 每阶段产物落盘 `temp/{bvid}/`（meta.json / transcript.json / chapters.json /
  scenes.json / keyframes.json / audio.wav / video.mp4），存在即跳过（`--force` 重跑）
- 阶段失败保留已生成产物，可断点续跑
- 实测：二次运行全缓存命中 0.1 秒完成；崩溃后重跑只补缺失阶段

## 8. 配置 (`config.yaml` + 环境变量)

见 `config.example.yaml`（脱敏模板）。关键项：

```yaml
llm:            # model / api_key / base_url / chunk_chars / max_concurrency
                # budget.max_calls_per_video（每视频 LLM 调用上限，默认 200）
transcription:  # backend: faster-whisper|openai-whisper, whisper_model
frames:         # scene_detection.threshold / min_scene_len, time_sampling
bilibili:       # cookie（ai-zh 字幕 / 登录可见视频）
```

环境变量优先级高于 config.yaml：`API_KEY` / `BASE_URL` / `MODEL`。
国内网络需 `export HF_ENDPOINT=https://hf-mirror.com`（模型下载）。

## 9. 测试策略

- 单元测试（34 个通过）：分块算法、章节校验、渲染、字幕解析、配置加载等纯逻辑
- fixtures 锁库：`tests/fixtures/sample_transcript.json`，不依赖网络/B 站现状
- 对抗变化回归：字幕链每级失败路径的降级测试
- 真实端到端：实测 BV1N8w9zJENu（85 分钟无字幕视频）与 BV1kJ411E7AQ（ai-zh 字幕视频）

## 10. 里程碑计划

| 里程碑 | 状态 | 内容 |
|---|---|---|
| **M1** | ✅ 完成 | fetcher（yt-dlp + 字幕链 + 私有 API 字幕）→ transcriber（双后端）→ frame_extractor（场景检测）→ analyzer（章节化 + 导读）→ renderer + pipeline 缓存 + CLI |
| **M2.1** | ⏳ | searcher + filter + candidates.md（关键词搜索产出可审核清单） |
| **M2.2** | ⏳ | ReviewSelection 接入 + 批量流水线（含 dry-run 成本预估） |

## 11. 已决策问题与理由 (ADR)

1. **用 yt-dlp 而非直接调 B 站 API**：社区维护活跃，自动跟进 B 站变化。
2. **依赖倒置（VideoSource 抽象）**：平台差异隔离，M2 共用接口，未来接 YouTube 只加一个类。
3. **字幕降级链**：内置字幕优先，Whisper 兜底保证可用性；降级原因全程可追踪。
4. **LLM 输出强制 JSON + 代码侧校验**：脏数据不进渲染层，解析/校验失败自动重试。
5. **中间产物全落盘**：断点续跑 + 审计 + 省钱。
6. **"人工筛选"作为流水线正式阶段**：M2 的审核是数据流一环（ReviewSelection），可回放可缓存。
7. **转写后端可插拔（faster-whisper 默认）**：速度/内存优势；openai-whisper 兜底；两后端对比可作论文实验点。
8. **场景检测为核心采样策略**：场景边界天然贴合关键帧语义；时间采样兜底；检测结果落盘缓存。
9. **LLM 预算上限 + 优雅降级**：M2 批量成本可预期；降级优先级：帧描述 → 块摘要 → 合并粗粒度。
10. **M2 搜索匿名优先 + Cookie 可选**：人工低频搜索风控风险低；Cookie 只存配置、不做自动刷新。
11. **B 站 AI 字幕走私有 API 路径**：yt-dlp 提取器不传 cookie（类属性 headers），
    ai-zh 必须直连 `x/player/wbi/v2`；该私有调用收敛在 `bili_source` 单函数，符合原则 2 的例外条款。
12. **多 P 合集支持（`?p=N`）**：B 站课程合集最常见形态；cid 按 pages 索引选择。
13. **提示词不限制句子数/字数**：实测"每条≤30字"等硬约束会把笔记压缩成无解释的短句；
    改为质量导向描述（"长度自定，以讲清楚为准"），配合每章导读生成（阶段C），笔记质量显著提升。
14. **单产物收敛**：早期三产物（全文+图/精简/速览）中，速览价值低被移除；全文并入导读后
    产物收敛为一份（导读+要点+关键帧），避免信息冗余。

## 12. 待决策问题 (Open Questions)

- **场景检测阈值标定**：`threshold` / `min_scene_len` 默认值需用真实知识区样本标定（数据驱动调参）。
- **超长视频（>2h）策略**：单段转写内存/耗时与 LLM 分块数激增的处理（分段转写、分幕章节化）。
- **whisper 兜底的繁→简转换**：base 模型中文输出繁体，是否引入 opencc 做兜底路径转换。
- **M2 批量 dry-run 预估误差容忍度**：按"时长→字数→块数"换算的预估有误差，展示粒度待 M2 定。
