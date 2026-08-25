"""阶段编排与缓存（README §7）。

- 每个阶段产物落盘 temp/{bvid}/，存在即跳过（--force 强制重跑）
- 阶段失败保留已生成产物，可断点续跑
- M2 时只需在头部接入 discover/review 两个阶段，本文件 M1 代码零改动
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from src.config import Config
from src.fetcher.bili_source import BiliSource
from src.llm import LLMClient
from src.models import Chapter, ExecSummary, FinalOutput, KeyFrame, Transcript, VideoMeta
from src.processor.analyzer import analyze_video
from src.processor.frame_extractor import (
    detect_candidate_timestamps,
    extract_key_frames,
    load_candidate_pool,
    save_candidate_pool,
    select_keyframe_times,
)
from src.processor.transcriber import create_transcriber

logger = logging.getLogger(__name__)


class Pipeline:
    """M1 流水线：BV 号 → 两份笔记。"""

    def __init__(self, cfg: Config, llm: LLMClient, source: BiliSource):
        self.cfg = cfg
        self.llm = llm
        self.source = source

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def run(
        self,
        bvid: str,
        *,
        no_frames: bool = False,
        force: bool = False,
    ) -> FinalOutput:
        started = time.time()
        temp = self.cfg.video_temp_dir(bvid)
        temp.mkdir(parents=True, exist_ok=True)
        stats: dict[str, Any] = {}

        meta = self._load_or_run(
            temp / "meta.json", force, lambda: self.source.fetch_meta(bvid)
        )

        transcript = self._fetch_transcript(bvid, temp, force)
        stats["transcript_source"] = transcript.source
        stats["transcript_segments"] = len(transcript.segments)

        scene_pool: list[float] = []
        if not no_frames:
            scene_pool = self._prepare_frames(bvid, temp, force, stats)

        chapters, _ = self._load_or_run(
            temp / "chapters.json",
            force,
            lambda: _analyze(transcript, scene_pool, self.llm, self.cfg, meta),
        )

        if not no_frames:
            chapters = self._produce_key_frames(bvid, chapters, scene_pool, temp, force, stats)

        output = FinalOutput(
            meta=meta,
            chapters=chapters,
            summary=None,
            generated_at=datetime.now(),
            stats={
                "transcript_source": stats.get("transcript_source", ""),
                "transcript_segments": stats.get("transcript_segments", 0),
                "llm": self.llm.stats(),
                "elapsed_seconds": round(time.time() - started, 1),
                "stages": stats.get("stages", {}),
            },
        )

        self._render(output)
        logger.info("完成：%s（耗时 %.1fs）", meta.title, output.stats["elapsed_seconds"])
        return output

    # ------------------------------------------------------------------
    # 各阶段
    # ------------------------------------------------------------------
    def _fetch_transcript(self, bvid: str, temp: Path, force: bool) -> Transcript:
        """字幕链 → 无字幕走 Whisper（README §6.1 ADR #3）。"""
        cached = self._load_or_run(
            temp / "transcript.json", force, lambda: self.source.fetch_transcript(bvid)
        )
        if cached is not None:
            return cached

        # 降级：本地转写
        audio = self._load_or_run(
            temp / "audio.json",
            force,
            lambda: _as_json(self.source.download_audio(bvid, temp)),
        )
        audio_path = Path(audio["path"])
        transcriber = create_transcriber(self.cfg.transcription)
        logger.info("字幕不可用，使用 %s 转写", type(transcriber).__name__)
        transcript = transcriber.transcribe(audio_path)
        self._write_json(temp / "transcript.json", transcript.model_dump())
        return transcript

    def _prepare_frames(
        self, bvid: str, temp: Path, force: bool, stats: dict[str, Any]
    ) -> list[float]:
        """下载视频 + 生成候选帧池（场景检测 + 时间采样兜底）。"""
        video = self._load_or_run(
            temp / "video.json",
            force,
            lambda: _as_json(self.source.download_video(bvid, temp)),
        )
        video_path = Path(video["path"])
        pool = load_candidate_pool(temp / "scenes.json")
        if pool is None or force:
            pool = detect_candidate_timestamps(video_path, self.cfg.frames)
            save_candidate_pool(pool, temp / "scenes.json")
            logger.info("候选帧池：%d 个时刻", len(pool))
        stats.setdefault("stages", {})["candidate_frames"] = len(pool)
        return pool

    def _produce_key_frames(
        self,
        bvid: str,
        chapters: list[Chapter],
        scene_pool: list[float],
        temp: Path,
        force: bool,
        stats: dict[str, Any],
    ) -> list[Chapter]:
        """LLM 选帧 → 抽帧 → 帧描述（README §6.2 第 3、4 步）。"""
        video = json.loads((temp / "video.json").read_text(encoding="utf-8"))
        video_path = Path(video["path"])

        selection_path = temp / "keyframes.json"
        if selection_path.exists() and not force:
            key_frames = [
                KeyFrame(**kf)
                for kf in json.loads(selection_path.read_text(encoding="utf-8"))
            ]
        else:
            selection = select_keyframe_times(
                chapters, scene_pool, self.llm, self.cfg.frames
            )
            image_dir = self.cfg.video_image_dir(bvid)
            key_frames = extract_key_frames(
                video_path, selection, chapters, image_dir, self.llm
            )
            selection_path.write_text(
                json.dumps([kf.model_dump() for kf in key_frames], ensure_ascii=False),
                encoding="utf-8",
            )

        # 把关键帧挂回对应章节
        frame_by_ts: dict[float, KeyFrame] = {}
        for kf in key_frames:
            frame_by_ts[kf.timestamp] = kf
        for ch in chapters:
            ch.key_frames = [
                frame_by_ts[ts]
                for ts in sorted(frame_by_ts)
                if ch.start_time - 1 <= ts <= ch.end_time + 1
            ]
        stats.setdefault("stages", {})["key_frames"] = len(key_frames)
        return chapters

    def _render(self, output: FinalOutput) -> None:
        """渲染唯一产物：视频笔记（导读+要点+关键帧）。"""
        from src.renderer import render_condensed_notes

        notes_dir = self.cfg.notes_dir()
        notes_dir.mkdir(parents=True, exist_ok=True)
        bvid = output.meta.bvid
        note_path = notes_dir / f"{bvid}_notes.md"
        note_path.write_text(render_condensed_notes(output), encoding="utf-8")
        logger.info("产物已生成：%s", note_path)

    # ------------------------------------------------------------------
    # 缓存工具
    # ------------------------------------------------------------------
    def _load_or_run(self, path: Path, force: bool, fn) -> Any:
        """带 JSON 缓存的阶段执行器：产物存在且未 force 时直接读取。"""
        if path.exists() and not force:
            logger.info("命中缓存：%s", path.name)
            raw = json.loads(path.read_text(encoding="utf-8"))
            return _from_json(raw)
        result = fn()
        if result is not None:
            self._write_json(path, _to_json(result))
        return result

    def _write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=1, default=str),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# 序列化工具
# ---------------------------------------------------------------------------

def _to_json(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, dict):
        return {k: _to_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_json(v) for v in obj]
    return obj


def _from_json(raw: Any) -> Any:
    """把缓存 JSON 反序列化回 Pydantic 模型（按结构启发式匹配）。"""
    if isinstance(raw, dict):
        if "segments" in raw and "source" in raw:
            return Transcript(**raw)
        if "bvid" in raw and "title" in raw:
            return VideoMeta(**raw)
        if "conclusion" in raw and "logic_chain" in raw:
            return ExecSummary(**raw)
        return {k: _from_json(v) for k, v in raw.items()}
    if isinstance(raw, list):
        if raw and isinstance(raw[0], dict) and "index" in raw[0] and "title" in raw[0]:
            return [Chapter(**item) for item in raw]
        return [_from_json(v) for v in raw]
    return raw


def _as_json(path: Path) -> dict:
    return {"path": str(path)}


def _analyze(
    transcript: Transcript,
    scene_pool: list[float],
    llm: LLMClient,
    cfg: Config,
    meta: VideoMeta,
) -> tuple[list[Chapter], ExecSummary]:
    """章节化 + 速览摘要（pipeline 阶段函数）。"""
    return analyze_video(
        transcript,
        llm,
        cfg.llm,
        meta.title,
        scene_boundaries=scene_pool or None,
    )
