"""帧采样与关键帧定位（README §6.2 / ADR #8）。

采样策略（高质量优先）：场景检测为主、时间采样兜底。
- 场景检测：PySceneDetect 扫镜头切换边界 → 每场景 1 帧代表帧（候选池）
- 时间采样：均匀采样作为兜底补充（检测失败 / 场景数爆炸 / --fast-sampling）
- LLM 精选：依据章节内容为每章选 0~3 个代表时刻（就近取池中帧或精确 seek）
- 帧描述：对精选帧调用 LLM 生成画面描述（预算降级优先级 ①，超限则留空）
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import cv2

from src.config import FramesConfig
from src.llm import LLMBudgetExceeded, LLMClient
from src.models import Chapter, KeyFrame

logger = logging.getLogger(__name__)

SYSTEM_FRAME_SELECT = (
    "你是视频学习笔记助手。根据章节内容为每个章节挑选最具代表性的视频画面时刻。"
)

SYSTEM_FRAME_DESC = (
    "你是视频学习笔记助手。根据章节内容推测该时刻画面的内容（视频画面不可见，"
    "请基于上下文合理推断，如'讲解者在展示流程图/代码/板书'）。一句话描述即可。"
)


# ---------------------------------------------------------------------------
# 候选帧池：场景检测（主）+ 时间采样（兜底）
# ---------------------------------------------------------------------------

def detect_candidate_timestamps(
    video_path: Path, cfg: FramesConfig
) -> list[float]:
    """生成候选帧时间戳列表（升序、去重）。

    主策略：场景检测，每场景取 1 帧代表帧；
    兜底：均匀时间采样（检测失败 / 场景数爆炸时）。
    """
    if cfg.strategy == "time" or cfg.strategy == "time-only":
        logger.info("使用时间采样（strategy=time）")
        return _time_sampling(video_path, cfg)

    try:
        scene_times = _scene_sampling(video_path, cfg)
    except Exception as e:  # 场景检测任何失败都降级，不中断流程
        logger.warning("场景检测失败，降级到时间采样：%s", e)
        return _time_sampling(video_path, cfg)

    if not scene_times:
        return _time_sampling(video_path, cfg)

    # 场景数爆炸（快节奏视频）时用时间采样控制候选池规模
    duration = _video_duration(video_path)
    max_scenes = max(20, int(duration / 10))
    if len(scene_times) > max_scenes:
        logger.warning("场景数 %d 超出阈值 %d，回退时间采样", len(scene_times), max_scenes)
        return _time_sampling(video_path, cfg)

    logger.info("场景检测命中 %d 个场景边界，生成 %d 个候选时刻", len(scene_times), len(scene_times))
    return scene_times


def _scene_sampling(video_path: Path, cfg: FramesConfig) -> list[float]:
    """场景检测：每场景取中点作为代表帧时刻。"""
    from scenedetect import ContentDetector, detect

    detector = ContentDetector(threshold=cfg.scene_detection.threshold)
    scenes = detect(str(video_path), detector, show_progress=False)

    min_len = cfg.scene_detection.min_scene_len
    times: list[float] = []
    for scene in scenes:
        # scenedetect 0.7：Scene 是 (begin, end) 元组（0.6 是带 .length 的对象）
        begin, end = scene
        length = end.get_seconds() - begin.get_seconds()
        if length < min_len:
            continue  # 过短场景：丢弃（候选池只是选项，不影响正确性）
        mid = (begin.get_seconds() + end.get_seconds()) / 2
        times.append(mid)
    return times


def _time_sampling(video_path: Path, cfg: FramesConfig) -> list[float]:
    """均匀时间采样（README §6.2 兜底策略）。"""
    duration = _video_duration(video_path)
    ts = cfg.time_sampling
    if duration <= 0:
        return []
    if duration <= ts.duration_threshold:
        step = ts.sample_every_short
    else:
        step = ts.sample_every_long
    times = [i * step for i in range(int(duration // step) + 1)]
    return [t for t in times if t < duration]


def _video_duration(video_path: Path) -> float:
    cap = cv2.VideoCapture(str(video_path))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        if fps and frames:
            return frames / fps
        return 0.0
    finally:
        cap.release()


# ---------------------------------------------------------------------------
# 关键帧精选（LLM）与抽取
# ---------------------------------------------------------------------------

def select_keyframe_times(
    chapters: list[Chapter],
    pool: list[float],
    llm: LLMClient,
    cfg: FramesConfig,
) -> dict[int, list[float]]:
    """LLM 为每章选 0~max 个代表时刻；失败或超预算时回退章节中点。

    返回 {chapter_index: [时刻...]}，时刻取最近池中值。
    """
    max_per_chapter = cfg.max_keyframes_per_chapter
    # 每章只给"该章时间范围内"的候选时刻，控制 prompt 规模
    per_chapter_pool: dict[int, list[float]] = {}
    for ch in chapters:
        in_range = [t for t in pool if ch.start_time - 1 <= t <= ch.end_time + 1]
        per_chapter_pool[ch.index] = in_range or [ch.start_time + 0.01]

    prompt_lines = ["【章节列表】"]
    for ch in chapters:
        prompt_lines.append(
            f"- 第{ch.index}章《{ch.title}》[{ch.start_time:.0f}s-{ch.end_time:.0f}s] "
            f"要点：{'；'.join(ch.key_points[:3])}"
        )
    prompt_lines.append("\n【候选时刻池】每个章节只能从自己的候选时刻中选：")
    for idx, times in per_chapter_pool.items():
        prompt_lines.append(f"- 第{idx}章候选（秒）：{', '.join(f'{t:.1f}' for t in times[:60])}")

    prompt_lines.append(
        f"\n请为每个章节挑选 0~{max_per_chapter} 个最能代表该章内容的关键画面时刻"
        "（例如板书、示意图、代码出现的位置）。"
        "输出 JSON：{\"selections\": {\"1\": [12.0, 34.5], \"2\": [60.0]}}，"
        "时刻必须来自该章候选池，按时间升序。"
    )
    prompt = "\n".join(prompt_lines)

    try:
        data = llm.chat_json(SYSTEM_FRAME_SELECT, prompt)
        raw = data.get("selections") or {}
        selection: dict[int, list[float]] = {}
        for key, times in raw.items():
            try:
                idx = int(key)
            except (TypeError, ValueError):
                continue
            chapter = next((c for c in chapters if c.index == idx), None)
            if chapter is None or not isinstance(times, list):
                continue
            pool_ts = per_chapter_pool[idx]
            picked = sorted({_nearest(t, pool_ts) for t in times})[:max_per_chapter]
            if picked:
                selection[idx] = picked
        if selection:
            return selection
        logger.warning("LLM 未返回任何选帧结果，回退章节中点帧")
    except Exception as e:
        logger.warning("LLM 选帧失败/预算超限（%s），回退章节中点帧", e)

    # 回退：每章取中点
    return {ch.index: [ (ch.start_time + ch.end_time) / 2 ] for ch in chapters}


def _nearest(target: float, pool: list[float]) -> float:
    if not pool:
        return target
    return min(pool, key=lambda t: abs(t - target))


def extract_key_frames(
    video_path: Path,
    selection: dict[int, list[float]],
    chapters: list[Chapter],
    image_dir: Path,
    llm: LLMClient,
) -> list[KeyFrame]:
    """按章节时刻抽取关键帧并生成描述。

    返回平铺的 KeyFrame 列表（含所属章节 index 由调用方自行关联——本函数
    将 KeyFrame 与章节用 timestamp 对齐，调用方按章节整理）。
    """
    image_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(video_path))
    key_frames: list[KeyFrame] = []
    try:
        for chapter in chapters:
            for ts in selection.get(chapter.index, []):
                img_path = image_dir / f"{chapter.index:02d}_{ts:07.1f}.png"
                ok = _grab_frame(cap, ts, img_path)
                if not ok:
                    logger.warning("时刻 %.1fs 抽帧失败，跳过", ts)
                    continue
                key_frames.append(
                    KeyFrame(
                        timestamp=ts,
                        image_path=str(img_path),
                        description=_describe_frame(chapter, ts, llm),
                    )
                )
    finally:
        cap.release()
    return key_frames


def _grab_frame(cap: cv2.VideoCapture, timestamp: float, out_path: Path) -> bool:
    """seek 到指定时刻并保存帧；失败时向前回退 0.5s 重试一次。"""
    for offset in (0.0, -0.5):
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, timestamp + offset) * 1000)
        ret, frame = cap.read()
        if ret and frame is not None and not frame.size == 0:
            cv2.imwrite(str(out_path), frame)
            return out_path.exists()
    return False


def _describe_frame(chapter: Chapter, timestamp: float, llm: LLMClient) -> str:
    """帧描述（预算降级优先级 ①：超限/失败时留空，不影响笔记主体）。"""
    try:
        prompt = (
            f"视频章节：第{chapter.index}章《{chapter.title}》"
            f"（{chapter.start_time:.0f}s-{chapter.end_time:.0f}s）\n"
            f"章节要点：{'；'.join(chapter.key_points)}\n"
            f"时刻 {timestamp:.1f}s 的画面内容可能是？输出一句话描述。"
        )
        return llm.chat(SYSTEM_FRAME_DESC, prompt).strip()
    except Exception as e:
        logger.warning("帧描述生成失败（%s），留空", e)
        return ""


# ---------------------------------------------------------------------------
# 候选池缓存（scenes.json）
# ---------------------------------------------------------------------------

def save_candidate_pool(pool: list[float], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pool, ensure_ascii=False, indent=1), encoding="utf-8")


def load_candidate_pool(path: Path) -> list[float] | None:
    if not path.exists():
        return None
    try:
        data: list[Any] = json.loads(path.read_text(encoding="utf-8"))
        return [float(t) for t in data]
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
