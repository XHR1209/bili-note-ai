"""核心数据模型（README §5）。

所有模块间数据传输必须基于本文件定义的 Schema。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# M1 模型：笔记生产
# ---------------------------------------------------------------------------

class VideoMeta(BaseModel):
    """视频元数据。"""

    bvid: str
    title: str
    duration: float                     # 秒
    author: str
    cover_url: str | None = None
    desc: str = ""                      # 视频简介
    # —— M2 筛选用字段，fetcher 尽力填充，缺失可为 None ——
    play_count: int | None = None
    danmaku_count: int | None = None
    pubdate: datetime | None = None
    tags: list[str] = Field(default_factory=list)
    like_count: int | None = None
    coin_count: int | None = None


class TranscriptSegment(BaseModel):
    """一段带时间戳的文字稿（字幕或转写）。"""

    start_time: float                   # 秒
    end_time: float
    text: str


class Transcript(BaseModel):
    """完整文字稿。"""

    source: str                         # "cc" | "ai-cc" | "whisper"
    language: str = ""
    segments: list[TranscriptSegment]


class KeyFrame(BaseModel):
    """一张关键帧截图。"""

    timestamp: float                    # 视频内秒数
    image_path: str                     # 相对 outputs/ 的本地路径
    description: str = ""               # LLM 生成的画面描述（可为空串）


class Chapter(BaseModel):
    """一个章节。"""

    index: int                          # 1 起
    title: str
    start_time: float
    end_time: float
    summary: str                        # 核心观点（1-2 句，速览用）
    narrative: str = ""                 # 讲义式导读段落：解释核心概念与推理，
                                        #   让没看过视频的人也能理解（完整/精简笔记用）
    key_points: list[str] = Field(default_factory=list)  # 带解释的要点（每条 1-2 句）
    key_frames: list[KeyFrame] = Field(default_factory=list)
    raw_speaker_notes: str = ""         # 章节内文字稿原文（完整笔记全文展示用）


class ExecSummary(BaseModel):
    """极简速览摘要（≤500 字，结构化渲染）。"""

    conclusion: str                     # 一句话结论
    logic_chain: list[str]              # "3 分钟看懂"逻辑链（3~6 步）
    key_conclusions: list[str] = Field(default_factory=list)  # 关键结论 bullet


class FinalOutput(BaseModel):
    """流水线最终产物。"""

    meta: VideoMeta
    chapters: list[Chapter]
    summary: ExecSummary | None = None   # 预留（当前产物不使用）
    generated_at: datetime
    stats: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# M2 模型：发现与审核（本期仅定义，随 M2 实现）
# ---------------------------------------------------------------------------

class SearchQuery(BaseModel):
    """搜索条件：关键词 / 偏好 / 限制。"""

    keywords: list[str]                 # 核心关键词（必填 ≥1）
    preferences: list[str] = Field(default_factory=list)  # 自由文本偏好
    filters: dict[str, Any] = Field(default_factory=dict)
    top_k: int = 10


class SearchCandidate(BaseModel):
    """候选视频 + 相关性依据。"""

    meta: VideoMeta
    score: float = 0.0
    match_reason: str = ""


class ReviewSelection(BaseModel):
    """人工审核结果。"""

    query: SearchQuery
    candidates: list[SearchCandidate]
    selected_bvids: list[str] = Field(default_factory=list)
