"""VideoSource 抽象接口（README §6.1 / ADR #2）。

所有与平台交互的代码都必须通过本接口 —— B 站只是其中一个实现。
未来接入其他平台（YouTube 等）只需新增一个实现类，pipeline 与 processor 无感。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from src.models import SearchCandidate, SearchQuery, Transcript, VideoMeta


class VideoSource(Protocol):
    """视频平台抽象接口。"""

    def fetch_meta(self, video_id: str) -> VideoMeta:
        """获取视频元数据。video_id 可以是 BV 号或完整链接。"""
        ...

    def fetch_transcript(self, video_id: str) -> Transcript | None:
        """获取内置字幕文字稿；无字幕时返回 None（调用方走 Whisper 兜底）。

        内部按降级链尝试：cc → ai-cc（ADR #3）。
        """
        ...

    def download_audio(self, video_id: str, out_dir: Path) -> Path:
        """下载音频（16k 单声道 wav），返回产物路径。"""
        ...

    def download_video(self, video_id: str, out_dir: Path) -> Path:
        """下载视频（优先 mp4/h264 纯视频流，供抽帧/场景检测使用），返回产物路径。"""
        ...

    # ------------------------------------------------------------------
    # M2 预留（README §6.4）：本期不实现，随 M2 填实现
    # ------------------------------------------------------------------
    def search(self, query: SearchQuery) -> list[SearchCandidate]:
        """按关键词/偏好/限制搜索候选视频（M2）。"""
        raise NotImplementedError("搜索能力随里程碑 M2 实现")
