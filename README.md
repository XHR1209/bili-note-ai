# Bili Note AI：B站视频 → AI 生成结构化学习笔记

输入一个 B 站视频链接（BV 号），自动产出**章节化的图文学习笔记**——每章包含可独立阅读的讲义式导读、带解释的要点、以及 LLM 精选的关键帧截图。

```bash
PYTHONPATH=. .venv/bin/python -m src.main BV1N8w9zJENu
# → outputs/notes/BV1N8w9zJENu_notes.md
```

## 效果示例

以下为真实运行结果（[BV1N8w9zJENu](https://www.bilibili.com/video/BV1N8w9zJENu)，南京大学操作系统原理课程，85 分钟，无内置字幕）：

- **耗时**：约 25 分钟（含 Whisper 转写 22 分钟；有 AI 字幕的视频仅需 **3 秒**）
- **产物**：6 章结构化笔记，每章含讲义式导读 + 带解释要点 + 关键帧截图（下图为一章节选）

```markdown
## 第3章 fork系统调用与进程树

*37:26 - 53:50*

fork 通过完整复制状态机（内存、寄存器、内核状态）来创建子进程：子进程返回
0，父进程返回子进程 PID。进程可以递归创建形成进程树，父进程退出后孤儿进程
会挂到 1 号进程……

**要点**：
- spawn 创建新进程，fork 复制状态机，两者机制完全不同
- fork 完整复制内存、寄存器和内核状态
- 子进程返回 0，父进程返回子 PID
- 父进程退出后孤儿挂到 1 号进程

![时刻 44:12 - 讲解者在展示fork形成的进程树示意图。](../images/BV1N8w9zJENu/03_02652.3.png)
```

## 功能特性

- **字幕降级链**：UP 主 CC 字幕 → B 站 AI 字幕（ai-zh，秒级）→ 本地 Whisper 转写（兜底）
- **场景检测抽帧**：PySceneDetect 按镜头切换采样，LLM 依据章节内容精选 0~3 张关键帧/章
- **讲义式导读**：每章由 LLM 基于全文生成讲解段落，没看过视频也能读懂
- **多 P 合集支持**：`BV号?p=6` 指定分 P
- **断点续跑**：已完成的阶段自动缓存（`temp/{bvid}/`），中断后重跑秒级跳过
- **优雅降级**：任何一步失败都不炸全局，自动降级并保留已生成产物

## 快速开始

### 1. 环境要求

- Python 3.10+
- **ffmpeg**（系统依赖）：`sudo apt install -y ffmpeg`（Ubuntu/Debian）
- 国内网络需配置 HuggingFace 镜像（见下方"国内网络"）

### 2. 安装

```bash
git clone <你的仓库地址>
cd bili-note-ai
python3 -m venv .venv
.venv/bin/pip install yt-dlp "faster-whisper>=1.0.0" opencv-python "scenedetect[opencv]>=0.6.4" "openai>=1.30.0" "pydantic>=2.6.0" PyYAML requests
```

> 本项目所有命令直接以 `PYTHONPATH=. .venv/bin/python -m src.main` 运行
> （无需 pip install 本包）。若你的环境 `pip install -e .` 成功，可用等价的
> `bili-learn` 命令（Windows 挂载盘/DrvFS 上 editable install 已知失败，故默认不依赖）。

### 3. 配置

```bash
cp config.example.yaml config.yaml
```

编辑 `config.yaml` 填入：

| 配置项 | 必填 | 说明 |
|---|---|---|
| `llm.api_key` | ✅ | 任意 OpenAI 兼容 API Key（DeepSeek/GLM/Claude 等） |
| `llm.base_url` | ✅ | OpenAI 兼容端点（DeepSeek：`https://api.deepseek.com/v1`） |
| `llm.model` | ✅ | 模型名（DeepSeek v4：`deepseek-v4-flash` 快 / `deepseek-v4-pro` 更优） |
| `bilibili.cookie` | 可选 | **强烈建议**：B 站登录 Cookie，启用 AI 字幕（秒级且更准，见下） |

**获取 B 站 Cookie**（可选但推荐，约 1 分钟）：
1. 浏览器登录 `bilibili.com` → 按 **F12** → **Network** 标签 → 刷新页面
2. 点击第一个请求 → **Request Headers** → 复制整行 **`Cookie:`** 的值
3. 粘贴到 `config.yaml` 的 `bilibili.cookie`（注意：不要用 `document.cookie`，拿不全 SESSDATA）

### 4. 运行

```bash
# 基本用法（BV 号或完整链接均可）
PYTHONPATH=. .venv/bin/python -m src.main BV1N8w9zJENu
PYTHONPATH=. .venv/bin/python -m src.main "https://www.bilibili.com/video/BV1kJ411E7AQ?p=6"

# 常用参数
--no-frames     # 跳过抽帧（更快，仅生成文字笔记）
--force         # 忽略缓存强制重跑所有阶段
--outdir DIR    # 自定义输出目录
-v              # 调试日志

# 后台运行（日志落盘，终端可关闭）
PYTHONPATH=. HF_ENDPOINT=https://hf-mirror.com nohup .venv/bin/python -m src.main BV1N8w9zJENu > run.log 2>&1 &
tail -5 run.log   # 随时查看进度
```

### 5. 产物

```
outputs/
├── images/{bvid}/          # 关键帧截图（每章 0~3 张）
└── notes/{bvid}_notes.md   # 结构化笔记（导读 + 要点 + 关键帧）
```

笔记中的图片以相对路径引用（`../images/...`），用 Typora / VS Code / 任意 Markdown 编辑器打开即可查看。

## 实测性能参考

| 视频类型 | 转写/字幕耗时 | 全流程耗时 |
|---|---|---|
| 有 B 站 AI 字幕（推荐） | **约 3 秒** | 约 3~6 分钟（下载+抽帧+LLM） |
| 无字幕（Whisper 兜底） | 约 0.2~0.3 倍视频时长 | 85 分钟视频实测约 25 分钟 |

- LLM 调用量：约 20~40 次/视频（分块摘要+合并+每章导读+选帧+帧描述）
- 费用参考：30 分钟视频约 2~4 万 tokens——DeepSeek 级约几毛钱，Claude/GPT 级约 $0.1~0.3
- 耗时大头：Whisper 转写 > 场景检测 > LLM；有 AI 字幕时大幅缩短

## 国内网络注意事项

faster-whisper 首次运行需从 HuggingFace 下载模型（~150MB），直连不可达：

```bash
export HF_ENDPOINT=https://hf-mirror.com
```

若仍报 401（huggingface_hub 1.28+ 的 xet 后端不兼容镜像），执行一次：

```bash
.venv/bin/pip uninstall -y hf-xet
```

## 常见问题（FAQ）

**Q：视频没有字幕怎么办？**
自动降级到本地 Whisper 转写（慢但保证可用）。注意 base 模型中文输出为繁体且有少量错别字——这就是推荐配置 Cookie 的原因：有 AI 字幕的视频（B 站多数课程都有）转写秒级且为简体。

**Q：笔记里没有图片？**
可能原因：`--no-frames` 模式、视频下载失败降级、或该视频抽帧失败（此时日志会有 warning）。图片路径相对 `outputs/notes/`，请从该目录打开笔记。

**Q：抽帧报错 AV1 / 视频解码失败？**
项目已默认强制下载 h264 视频流（多数环境无 AV1 解码器）。若你的视频连 h264 都没有，可检查 `src/fetcher/bili_source.py` 的格式偏好。

**Q：中断后重新运行会重新花钱吗？**
不会。`temp/{bvid}/` 缓存已完成阶段（下载/转写/章节/抽帧），重跑自动跳过；`--force` 强制重跑。

**Q：一个 BV 号有多个分 P（课程合集）？**
用 `BV号?p=N` 指定分 P。默认处理第一 P。

**Q：需要登录 B 站吗？**
默认不需要。Cookie 仅用于启用 AI 字幕（推荐）和访问登录可见视频。

## 安全提示

- `config.yaml` 含你的 API Key 和 B 站 Cookie，已加入 `.gitignore`；仓库只提交脱敏的 `config.example.yaml`
- Cookie 等同账号凭证，泄露后可被他人操作账号；建议定期轮换
- 生成的笔记含视频截图，注意版权，不宜公开分发

## 架构与设计

本项目为"对抗 B 站接口变化"做了专门设计（依赖倒置、字幕降级链、阶段式流水线、断点缓存、优雅降级），完整设计文档见 **[DESIGN.md](DESIGN.md)**（含数据模型、模块划分、决策记录 ADR、里程碑规划）。
