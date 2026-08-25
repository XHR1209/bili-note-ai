"""LLM 分块摘要与章节化（README §6.2 核心算法）。

两阶段策略：
  阶段A 分块摘要（并行）：文字稿按时间滑窗分块 → 每块产出主题/摘要/要点
  阶段B 合并章节（串行）：相邻同主题块合并 → Chapter 列表

预算降级（ADR #9）：块数超预算时把块两两合并（粗粒度）重试，绝不整体失败。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.config import LLMConfig
from src.llm import LLMBudgetExceeded, LLMClient
from src.models import Chapter, Transcript

logger = logging.getLogger(__name__)

SYSTEM_CHUNK = (
    "你是专业的视频学习笔记助手。把给定的一段视频文字稿提炼成内容充实的块摘要："
    "summary 要写成连贯的叙述段落（不是短句列表），充分解释清楚概念是什么、为什么、"
    "怎么用，长度由你自己决定，以讲清楚为准；key_points 每条都要带充分的解释，"
    "让没看过视频的人也能读懂。不要刻意压缩篇幅。"
)

SYSTEM_MERGE = (
    "你是视频学习笔记助手。把视频文字稿的分块摘要合并成有逻辑结构的章节。"
)

SYSTEM_NARRATIVE = (
    "你是专业的视频学习笔记整理者。根据给定章节的完整文字稿，写一段讲义式的"
    "讲解：解释这一章的核心概念、关键推理过程与内在逻辑，让没看过视频的"
    "读者仅凭这段文字就能理解本章内容。用连贯的叙述体，篇幅由你自己决定，"
    "以讲清楚为准，不要刻意压缩。"
)

def _fmt_ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# 阶段A：分块
# ---------------------------------------------------------------------------

def chunk_transcript(
    transcript: Transcript, chunk_chars: int, overlap_chars: int
) -> list[tuple[float, float, str]]:
    """把 Transcript 切成 [(start, end, text)] 块，块间按字符数重叠。

    切割永远发生在 segment 边界上（不截断句子）。
    """
    segments = transcript.segments
    chunks: list[tuple[float, float, str]] = []
    start_idx = 0
    while start_idx < len(segments):
        text_parts: list[str] = []
        char_count = 0
        end_idx = start_idx
        while end_idx < len(segments) and char_count < chunk_chars:
            seg = segments[end_idx]
            # 每行带时间戳，供完整笔记展示与 LLM 定位上下文
            text_parts.append(f"[{_fmt_ts(seg.start_time)}] {seg.text}")
            char_count += len(seg.text)
            end_idx += 1
        chunk_text = "\n".join(text_parts)
        chunks.append(
            (
                segments[start_idx].start_time,
                segments[end_idx - 1].end_time,
                chunk_text,
            )
        )
        # 回退 overlap 个字符（向前找 segment 边界）
        overlap = 0
        while end_idx > start_idx and overlap < overlap_chars:
            end_idx -= 1
            overlap += len(segments[end_idx].text)
        if end_idx <= start_idx:
            break  # 防止死循环
        start_idx = end_idx
    return chunks


def _summarize_chunk(
    llm: LLMClient,
    title: str,
    chunk_no: int,
    total: int,
    start: float,
    end: float,
    text: str,
) -> dict:
    prompt = (
        f"视频《{title}》文字稿第 {chunk_no}/{total} 块（{start:.0f}s-{end:.0f}s）：\n"
        f"{text}\n\n"
        '输出 JSON：{"title": "块主题(≤15字)", "summary": "叙述体总结（连贯段落，'
        '充分解释概念与逻辑，长度自定）", "key_points": ["要点：充分解释…", ...]'
        '（3-5条，每条都要解释清楚，不限制字数）}'
    )
    return llm.chat_json(SYSTEM_CHUNK, prompt)


# ---------------------------------------------------------------------------
# 阶段B：合并章节
# ---------------------------------------------------------------------------

def _merge_chapters(
    llm: LLMClient,
    title: str,
    chunks: list[dict],
    scene_boundaries: list[float],
    n_blocks: int,
) -> list[dict]:
    """合并块摘要 → 章节草案（JSON dict 列表，未校验）。"""
    lines = [f"视频《{title}》分块摘要（共 {n_blocks} 块）："]
    for i, c in enumerate(chunks):
        kp = "；".join((c.get("key_points") or [])[:3])
        lines.append(
            f"{i}. [{c.get('_start', 0):.0f}s-{c.get('_end', 0):.0f}s] "
            f"主题：{c.get('title', '')}；要点：{kp}"
        )
    if scene_boundaries:
        lines.append(
            "\n场景切换时刻（秒）："
            + ", ".join(f"{t:.0f}" for t in scene_boundaries[:80])
            + "\n（讲解者切换主题常伴随画面切换，可作为章节边界参考，仅建议不强约束）"
        )
    lines.append(
        '\n请把相邻同主题块合并为章节，输出 JSON：{"chapters": [{"title": "章节名", '
        '"block_start": 0, "block_end": 1, "summary": "本章核心观点（叙述体，'
        '长度自定）", "key_points": ["要点：解释…", ...]}]}\n'
        "约束：block_start/block_end 为块索引（含端点）；必须有序、不重叠、"
        "覆盖全部 0..N-1 块；章节数 ≥1。"
    )
    data = llm.chat_json(SYSTEM_MERGE, "\n".join(lines))
    chapters = data.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise ValueError("合并结果缺少 chapters 数组")
    return chapters


def _validate_merge(chapters_raw: list[dict], n_blocks: int) -> list[dict]:
    """校验章节覆盖性/有序性；失败抛 ValueError。"""
    covered: set[int] = set()
    prev_end = -1
    for ch in chapters_raw:
        try:
            start = int(ch["block_start"])
            end = int(ch["block_end"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("章节块索引缺失或非法")
        if start < 0 or end >= n_blocks or start > end:
            raise ValueError(f"块索引越界：[{start}, {end}] (n={n_blocks})")
        if start <= prev_end:
            raise ValueError("章节块区间重叠或无序")
        for i in range(start, end + 1):
            covered.add(i)
        prev_end = end
        ch["block_start"], ch["block_end"] = start, end
    if covered != set(range(n_blocks)):
        missing = sorted(set(range(n_blocks)) - covered)
        raise ValueError(f"块覆盖不完整，缺失块：{missing}")
    return chapters_raw


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def analyze_video(
    transcript: Transcript,
    llm: LLMClient,
    cfg: LLMConfig,
    title: str,
    scene_boundaries: list[float] | None = None,
) -> tuple[list[Chapter], None]:
    """分块摘要 + 合并章节 + 生成速览摘要。

    返回 (chapters, summary)。
    """
    raw_chunks = chunk_transcript(
        transcript, cfg.chunk_chars, cfg.chunk_overlap_chars
    )
    if not raw_chunks:
        raise ValueError("文字稿为空，无法章节化")

    logger.info("文字稿共 %d 块，开始分块摘要（并发 %d）", len(raw_chunks), cfg.max_concurrency)
    chunks = _summarize_all_chunks(llm, title, raw_chunks)

    logger.info("合并 %d 块为章节", len(chunks))
    chapter_raw = _merge_with_retry(llm, title, chunks, scene_boundaries or [], len(chunks))

    chapters = _build_chapters(chapter_raw, raw_chunks, chunks)

    chapters = _generate_narratives(llm, chapters, title)
    return chapters, None


def _generate_narratives(
    llm: LLMClient, chapters: list[Chapter], title: str
) -> list[Chapter]:
    """阶段C：为每章生成讲义式导读段落（150-300 字）。

    预算降级：超限时 narrative 留空（完整笔记仍可展示全文转写原文）。
    """
    for ch in chapters:
        prompt = (
            f"视频《{title}》第{ch.index}章《{ch.title}》"
            f"（{ch.start_time:.0f}s-{ch.end_time:.0f}s）完整文字稿：\n"
            f"{ch.raw_speaker_notes[:8000]}\n\n"
            "请输出该章的讲义式讲解段落（叙述体，篇幅自定，直接输出文本，不要JSON）。"
        )
        try:
            narrative = llm.chat(SYSTEM_NARRATIVE, prompt).strip()
            if narrative:
                ch.narrative = narrative
        except Exception as e:
            logger.warning("章节 %d 导读生成失败（%s），narrative 留空", ch.index, e)
    return chapters


def _summarize_all_chunks(
    llm: LLMClient, title: str, raw_chunks: list[tuple[float, float, str]]
) -> list[dict]:
    """阶段A：并行分块摘要；预算超限时块两两合并降级重试（ADR #9）。"""
    chunks: list[dict] = []
    pool = raw_chunks[:]
    while pool:
        try:
            with ThreadPoolExecutor(max_workers=llm.max_concurrency) as ex:
                futures = {
                    ex.submit(
                        _summarize_chunk,
                        llm,
                        title,
                        i + 1,
                        len(pool),
                        c[0],
                        c[1],
                        c[2],
                    ): (i, c)
                    for i, c in enumerate(pool)
                }
                results: list[dict] = [None] * len(pool)  # type: ignore[list-item]
                for fut in as_completed(futures):
                    idx, chunk = futures[fut]
                    results[idx] = {**fut.result(), "_start": chunk[0], "_end": chunk[1]}
            chunks = results
            break
        except LLMBudgetExceeded:
            if len(pool) <= 1:
                logger.error("预算不足以完成任何分块摘要")
                raise
            merged = [
                (
                    pool[2 * i][0],
                    pool[2 * i + 1][1],
                    pool[2 * i][2] + "\n" + pool[2 * i + 1][2],
                )
                for i in range(len(pool) // 2)
            ]
            if len(pool) % 2 == 1:
                merged.append(pool[-1])
            logger.warning("LLM 预算超限：块数 %d → %d，粗粒度重试", len(pool), len(merged))
            pool = merged
    return chunks


def _merge_with_retry(
    llm: LLMClient,
    title: str,
    chunks: list[dict],
    scene_boundaries: list[float],
    n_blocks: int,
) -> list[dict]:
    """阶段B：合并章节，逻辑校验失败重试 1 次，再失败按每块一章兜底。"""
    for attempt in range(2):
        try:
            raw = _merge_chapters(llm, title, chunks, scene_boundaries, n_blocks)
            return _validate_merge(raw, n_blocks)
        except (ValueError, LLMBudgetExceeded) as e:
            if isinstance(e, LLMBudgetExceeded) or attempt == 1:
                logger.warning("合并失败（%s），按每块一章兜底", e)
                return [
                    {
                        "title": chunks[i].get("title") or f"第{i+1}部分",
                        "block_start": i,
                        "block_end": i,
                        "summary": chunks[i].get("summary", ""),
                        "key_points": chunks[i].get("key_points", []),
                    }
                    for i in range(n_blocks)
                ]
            logger.warning("章节校验失败（第 %d 次），重试：%s", attempt + 1, e)
    return []  # 不可达


def _build_chapters(
    chapter_raw: list[dict],
    raw_chunks: list[tuple[float, float, str]],
    chunks: list[dict],
) -> list[Chapter]:
    """把校验过的章节草案 + 原始块信息组装成 Chapter。"""
    chapters: list[Chapter] = []
    for i, ch in enumerate(chapter_raw, start=1):
        b_start, b_end = ch["block_start"], ch["block_end"]
        start_time = raw_chunks[b_start][0]
        end_time = raw_chunks[b_end][1]
        speaker_notes = "\n".join(raw_chunks[j][2] for j in range(b_start, b_end + 1))
        chapters.append(
            Chapter(
                index=i,
                title=str(ch.get("title", "")).strip() or f"第{i}部分",
                start_time=start_time,
                end_time=end_time,
                summary=str(ch.get("summary", "")).strip(),
                key_points=[str(kp).strip() for kp in ch.get("key_points", []) if kp],
                raw_speaker_notes=speaker_notes,
            )
        )
    return chapters
