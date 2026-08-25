"""转写后端（README §6.2 / ADR #7）。

Transcriber 抽象 + 两个实现：
- FasterWhisperTranscriber（默认）：快约 4 倍、内存低、支持词级时间戳
- OpenaiWhisperTranscriber（备用）：官方实现，需 `pip install .[whisper-openai]`

下游只看 Transcript 模型，切换后端零改动。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from src.config import TranscriptionConfig
from src.models import Transcript, TranscriptSegment

logger = logging.getLogger(__name__)


class Transcriber(Protocol):
    """转写后端抽象。"""

    def transcribe(self, audio_path: Path) -> Transcript:
        """把音频转写为带时间戳的文字稿。"""
        ...


class FasterWhisperTranscriber:
    """faster-whisper 后端（默认）。"""

    def __init__(self, model_size: str, language: str | None):
        self._model_size = model_size
        self._language = language
        self._model = None

    def transcribe(self, audio_path: Path) -> Transcript:
        from faster_whisper import WhisperModel  # 懒加载，避免无关后端拖慢启动

        if self._model is None:
            logger.info("加载 faster-whisper 模型：%s（int8）", self._model_size)
            self._model = WhisperModel(self._model_size, device="cpu", compute_type="int8")

        segments_iter, info = self._model.transcribe(
            str(audio_path), language=self._language
        )
        duration = info.duration or 0.0
        segments: list[TranscriptSegment] = []
        last_log = -1.0  # 每累计 60 秒音频打一条进度日志
        for seg in segments_iter:
            if seg.text.strip():
                segments.append(
                    TranscriptSegment(
                        start_time=float(seg.start), end_time=float(seg.end), text=seg.text.strip()
                    )
                )
            if duration and seg.end - last_log >= 60.0:
                last_log = seg.end
                logger.info(
                    "转写进度：%s / %s（%.0f%%）",
                    _fmt_duration(seg.end),
                    _fmt_duration(duration),
                    100.0 * seg.end / duration,
                )
        logger.info("转写完成：%d 段（%s）", len(segments), info.language)
        return Transcript(source="whisper", language=info.language or "", segments=segments)


def _fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class OpenaiWhisperTranscriber:
    """openai-whisper 官方后端（备用）。"""

    def __init__(self, model_size: str, language: str | None):
        self._model_size = model_size
        self._language = language
        self._model = None

    def transcribe(self, audio_path: Path) -> Transcript:
        import whisper  # 懒加载；未安装时给出明确提示

        try:
            if self._model is None:
                logger.info("加载 openai-whisper 模型：%s", self._model_size)
                self._model = whisper.load_model(self._model_size)
            result = self._model.transcribe(str(audio_path), language=self._language)
        except ModuleNotFoundError as e:
            raise RuntimeError(
                "openai-whisper 未安装：请执行 `pip install .[whisper-openai]`，"
                "或把 config.yaml 的 transcription.backend 改为 faster-whisper"
            ) from e

        segments = [
            TranscriptSegment(
                start_time=float(seg["start"]), end_time=float(seg["end"]), text=seg["text"].strip()
            )
            for seg in result.get("segments", [])
            if seg.get("text", "").strip()
        ]
        return Transcript(source="whisper", language=result.get("language", ""), segments=segments)


def create_transcriber(cfg: TranscriptionConfig) -> Transcriber:
    """按配置创建转写后端。"""
    backend = cfg.backend
    if backend == "faster-whisper":
        return FasterWhisperTranscriber(cfg.whisper_model, cfg.whisper_language)
    if backend == "openai-whisper":
        return OpenaiWhisperTranscriber(cfg.whisper_model, cfg.whisper_language)
    raise ValueError(
        f"未知转写后端：{backend}（可选：faster-whisper / openai-whisper）"
    )
