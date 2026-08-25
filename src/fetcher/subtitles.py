"""字幕获取链（README §6.1 / ADR #3）。

降级链：UP主字幕（cc / zh-CN，无需登录）→ B站AI字幕（ai-zh / ai-cc，需登录
Cookie）→ 返回 None，由调用方走本地 Whisper 转写。

注意（实测踩坑）：
- yt-dlp 的字幕 entry 内容在 `data` 字段（已转好的 srt 文本），不是 `url`
- B 站 AI 字幕的语言代码是 `ai-zh`（不是 ai-cc）；UP 主 CC 字幕常见 `zh-CN`
"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

from src.models import Transcript, TranscriptSegment

logger = logging.getLogger(__name__)

# B 站字幕接口的防盗链要求
_BILI_HEADERS = {
    "Referer": "https://www.bilibili.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}

# 降级链顺序：UP主字幕 → B站AI字幕（AI字幕需登录 Cookie）
UP_SUB_LANGS = ("cc", "zh-CN", "zh-Hans", "zh")
AI_SUB_LANGS = ("ai-zh", "ai-cc")
SUBTITLE_CHAIN = UP_SUB_LANGS + AI_SUB_LANGS


def pick_subtitle_entry(info: dict[str, Any]) -> tuple[str, dict] | None:
    """从 yt-dlp 的 info dict 中按降级链挑选第一个可用的字幕入口。

    返回 (lang_key, entry)；无可用字幕返回 None。
    entry 的可用内容在 `data`（yt-dlp 内嵌 srt 文本）或 `url`（原始链接）。
    """
    subs: dict[str, Any] = info.get("subtitles") or {}
    auto_subs: dict[str, Any] = info.get("automatic_captions") or {}
    for lang in SUBTITLE_CHAIN:
        for pool in (subs, auto_subs):
            entries = pool.get(lang)
            if entries and entries[0]:
                entry = entries[0]
                if entry.get("url") or entry.get("data"):
                    return lang, entry
    return None


def parse_bili_json_subtitle(data: dict[str, Any]) -> list[TranscriptSegment]:
    """解析 B 站字幕 API 返回的 JSON（body 数组）。

    结构：{"body": [{"from": float, "to": float, "content": str, ...}]}
    """
    segments: list[TranscriptSegment] = []
    for item in data.get("body") or []:
        text = (item.get("content") or "").strip()
        if not text:
            continue
        segments.append(
            TranscriptSegment(
                start_time=float(item["from"]),
                end_time=float(item["to"]),
                text=text,
            )
        )
    return segments


def download_subtitle_transcript(lang_key: str, entry: dict, cookie: str = "") -> Transcript | None:
    """解析一个字幕入口为 Transcript（内容在 data 或 url）。

    yt-dlp 的 entry：`data` 为已转换的 srt 文本（优先），`url` 为原始接口链接。
    失败返回 None（不中断主流程，继续降级）。
    """
    # 路径 1：yt-dlp 内嵌的 srt 文本
    data = entry.get("data")
    if data:
        segments = _parse_srt(data)
        if segments:
            logger.debug("字幕 %s 使用 yt-dlp 内嵌 srt（%d 段）", lang_key, len(segments))
            return Transcript(source=lang_key, segments=segments)

    # 路径 2：从原始链接下载（B 站字幕 JSON 或 srt）
    url = entry.get("url")
    if not url:
        return None
    headers = dict(_BILI_HEADERS)
    if cookie:
        headers["Cookie"] = cookie
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        if entry.get("ext") == "json" or "json" in (resp.headers.get("content-type") or ""):
            data = resp.json()
            segments = parse_bili_json_subtitle(data)
        else:
            segments = _parse_srt(resp.text)
        if not segments:
            logger.warning("字幕 %s 解析后为空", lang_key)
            return None
        return Transcript(source=lang_key, segments=segments)
    except (requests.RequestException, ValueError, KeyError) as e:
        logger.warning("字幕 %s 下载/解析失败（继续降级）：%s", lang_key, e)
        return None


def _parse_srt(text: str) -> list[TranscriptSegment]:
    """极简 SRT 解析（兜底路径，B 站一般返回 JSON）。"""
    import re

    segments: list[TranscriptSegment] = []
    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        m = re.match(r"(\d{1,2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2}),(\d{3})", lines[1])
        if not m:
            continue
        start = _srt_ts_to_seconds(*m.groups()[:4])
        end = _srt_ts_to_seconds(*m.groups()[4:])
        content = " ".join(lines[2:])
        if content:
            segments.append(TranscriptSegment(start_time=start, end_time=end, text=content))
    return segments


def _srt_ts_to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


# ---------------------------------------------------------------------------
# 字幕 → 文字稿的合并工具（供 analyzer 使用）
# ---------------------------------------------------------------------------

def transcript_to_text(transcript: Transcript) -> str:
    """把 Transcript 展平为纯文本（带时间戳标记，供 LLM 分块摘要使用）。"""
    lines = []
    for seg in transcript.segments:
        ts = _fmt_ts(seg.start_time)
        lines.append(f"[{ts}] {seg.text}")
    return "\n".join(lines)


def _fmt_ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"
