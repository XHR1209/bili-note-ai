"""笔记渲染（唯一产物，README §6.3）。

结构：标题 → 每章 = 时间戳 + 讲义式导读(narrative) + 带解释要点
     + 关键帧截图（含 LLM 画面描述）。
纯函数：FinalOutput → Markdown 字符串。
"""

from __future__ import annotations

from src.models import Chapter, FinalOutput


def render_condensed_notes(output: FinalOutput) -> str:
    """渲染视频笔记。"""
    meta = output.meta
    lines: list[str] = []
    lines.append(f"# 《{meta.title}》 - 视频笔记")
    lines.append("")
    lines.append(f"> {meta.author} ｜ 时长 {_fmt_duration(meta.duration)} ｜ 视频链接：https://www.bilibili.com/video/{meta.bvid}")
    lines.append("")

    for ch in output.chapters:
        lines.extend(_render_chapter(ch))

    lines.append("---")
    lines.append("*本笔记由 Bili-Learn-AI 自动生成。*")
    lines.append("")
    return "\n".join(lines)


def _render_chapter(ch: Chapter) -> list[str]:
    lines: list[str] = []
    lines.append(f"## 第{ch.index}章 {ch.title}")
    lines.append("")
    lines.append(f"*{_fmt_ts(ch.start_time)} - {_fmt_ts(ch.end_time)}*")
    lines.append("")

    if ch.narrative:
        lines.append(ch.narrative)
        lines.append("")

    if ch.key_points:
        lines.append("**要点**：")
        for kp in ch.key_points:
            lines.append(f"- {kp}")
        lines.append("")

    for frame in ch.key_frames:
        desc = f" - {frame.description}" if frame.description else ""
        lines.append(f"![时刻 {_fmt_ts(frame.timestamp)}{desc}]({_image_rel_path(frame.image_path)})")
    lines.append("")
    return lines


def _fmt_ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}小时{m}分{s}秒"
    return f"{m}分{s}秒"


def _image_rel_path(image_path: str) -> str:
    """把完整路径规范化为相对笔记文件（outputs/notes/）的路径。

    存储层给的是完整路径（outputs/images/...）；笔记位于 outputs/notes/，
    图片位于 outputs/images/，因此需要 ../ 前缀。
    """
    p = image_path.replace("\\", "/")
    for prefix in ("outputs/", "./outputs/", "./"):
        if p.startswith(prefix):
            p = p[len(prefix):]
            break
    return f"../{p}"
