"""配置加载：config.yaml + 环境变量（README §8）。

环境变量优先级高于 config.yaml：API_KEY / BASE_URL / MODEL。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class LLMConfig:
    model: str = "deepseek-chat"
    max_tokens: int = 4096
    temperature: float = 0.3
    chunk_chars: int = 2500
    chunk_overlap_chars: int = 300
    max_concurrency: int = 4
    timeout_seconds: float = 120.0
    max_calls_per_video: int = 200
    api_key: str = ""
    base_url: str = ""
    disable_thinking: bool = False     # DeepSeek v4 系列：关闭思考（快 10 倍、输出稳定）


@dataclass
class TranscriptionConfig:
    backend: str = "faster-whisper"     # "faster-whisper" | "openai-whisper"
    whisper_model: str = "base"
    whisper_language: str | None = None


@dataclass
class SceneDetectionConfig:
    detector: str = "content"
    threshold: float = 27.0
    min_scene_len: float = 2.0


@dataclass
class TimeSamplingConfig:
    sample_every_short: int = 30        # ≤10分钟视频采样间隔(秒)
    sample_every_long: int = 60         # >10分钟视频采样间隔(秒)
    duration_threshold: int = 600       # 长短视频阈值(秒)


@dataclass
class FramesConfig:
    strategy: str = "scene+time"
    scene_detection: SceneDetectionConfig = field(default_factory=SceneDetectionConfig)
    time_sampling: TimeSamplingConfig = field(default_factory=TimeSamplingConfig)
    max_keyframes_per_chapter: int = 3


@dataclass
class PathsConfig:
    temp_dir: str = "temp"
    output_dir: str = "outputs"


@dataclass
class BilibiliConfig:
    cookie: str = ""
    format_pref: str = "bv*+ba/b"


@dataclass
class Config:
    """全量配置。"""

    llm: LLMConfig = field(default_factory=LLMConfig)
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    frames: FramesConfig = field(default_factory=FramesConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    bilibili: BilibiliConfig = field(default_factory=BilibiliConfig)

    # ------------------------------------------------------------------
    # 便捷路径解析
    # ------------------------------------------------------------------
    def temp_dir(self) -> Path:
        return Path(self.paths.temp_dir)

    def output_dir(self) -> Path:
        return Path(self.paths.output_dir)

    def video_temp_dir(self, bvid: str) -> Path:
        """单个视频的中间产物目录。"""
        return self.temp_dir() / bvid

    def video_image_dir(self, bvid: str) -> Path:
        """单个视频的关键帧输出目录（相对 outputs/）。"""
        return Path(self.paths.output_dir) / "images" / bvid

    def notes_dir(self) -> Path:
        return Path(self.paths.output_dir) / "notes"


def load_config(path: str | Path = "config.yaml") -> Config:
    """加载配置：先读 config.yaml（若存在），再用环境变量覆盖。"""
    raw: dict = {}
    p = Path(path)
    if p.exists():
        with open(p, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    cfg = Config()
    _apply_dict(cfg, raw)

    # 环境变量覆盖（优先级最高）
    if os.environ.get("API_KEY"):
        cfg.llm.api_key = os.environ["API_KEY"]
    if os.environ.get("BASE_URL"):
        cfg.llm.base_url = os.environ["BASE_URL"]
    if os.environ.get("MODEL"):
        cfg.llm.model = os.environ["MODEL"]
    return cfg


def _apply_dict(cfg: Config, raw: dict) -> None:
    """把 config.yaml 的 dict 映射到 Config dataclass（按字段名，忽略未知键）。"""
    mapping = {
        "llm": cfg.llm,
        "transcription": cfg.transcription,
        "paths": cfg.paths,
        "bilibili": cfg.bilibili,
    }
    for section_name, obj in mapping.items():
        section = raw.get(section_name) or {}
        for field_name, val in section.items():
            if hasattr(obj, field_name):
                setattr(obj, field_name, val)

    # frames 是嵌套结构，单独处理
    frames = raw.get("frames") or {}
    for field_name, val in frames.items():
        if field_name == "scene_detection" and isinstance(val, dict):
            for k, v in val.items():
                if hasattr(cfg.frames.scene_detection, k):
                    setattr(cfg.frames.scene_detection, k, v)
        elif field_name == "time_sampling" and isinstance(val, dict):
            for k, v in val.items():
                if hasattr(cfg.frames.time_sampling, k):
                    setattr(cfg.frames.time_sampling, k, v)
        elif hasattr(cfg.frames, field_name):
            setattr(cfg.frames, field_name, val)

    # llm.budget 嵌套
    llm_section = raw.get("llm") or {}
    budget = llm_section.get("budget") or {}
    if budget.get("max_calls_per_video") is not None:
        cfg.llm.max_calls_per_video = int(budget["max_calls_per_video"])
