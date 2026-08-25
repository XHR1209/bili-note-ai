"""B 站实现（README §6.1 / ADR #1、#10）。

整个项目中**唯一**接触 B 站/yt-dlp 的模块：
- B 站接口变更、风控升级全部收敛在本文件内
- 登录 Cookie（可选）：仅影响 ai-cc 字幕与登录可见视频
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

import yt_dlp
import yt_dlp.utils

from src.config import BilibiliConfig
from src.fetcher.subtitles import download_subtitle_transcript, pick_subtitle_entry
from src.models import SearchCandidate, SearchQuery, Transcript, VideoMeta

logger = logging.getLogger(__name__)

_BVID_PATTERN = re.compile(r"BV[0-9A-Za-z]{10}")
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def extract_bvid(video_id: str) -> str:
    """从 BV 号或任意形式的链接中提取 bvid；无法识别时原样返回。"""
    m = _BVID_PATTERN.search(video_id)
    return m.group(0) if m else video_id


def extract_page(video_id: str) -> int | None:
    """从链接中提取分 P 号（?p=2 或 /P2 形式），无则返回 None。"""
    m = re.search(r"(?:[?&]p=)(\d+)", video_id, re.IGNORECASE)
    if not m:
        m = re.search(r"/P(\d+)", video_id, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _video_url(video_id: str, page: int | None = None) -> str:
    """yt-dlp 不认裸 BV 号，统一包装为完整链接（保留分 P 参数）。"""
    if video_id.startswith("http://") or video_id.startswith("https://"):
        return video_id
    url = f"https://www.bilibili.com/video/{video_id}"
    if page:
        url += f"?p={page}"
    return url


class BiliSource:
    """B 站视频源（实现 VideoSource 协议）。"""

    def __init__(self, cfg: BilibiliConfig):
        self._cfg = cfg
        self._cookie = cfg.cookie or ""

    # ------------------------------------------------------------------
    # 元数据
    # ------------------------------------------------------------------
    def fetch_meta(self, video_id: str) -> VideoMeta:
        bvid = extract_bvid(video_id)
        page = extract_page(video_id)
        info = self._extract(bvid, download=False, page=page)
        meta = VideoMeta(
            bvid=info.get("id") or bvid,
            title=info.get("title") or "",
            duration=float(info.get("duration") or 0),
            author=info.get("uploader") or info.get("channel") or "",
            cover_url=info.get("thumbnail"),
            desc=info.get("description") or "",
            play_count=_safe_int(info.get("view_count")),
            danmaku_count=_safe_int(info.get("comment_count")),
            pubdate=_timestamp_to_datetime(info.get("timestamp")),
            tags=[t.get("name", "") for t in (info.get("tags") or []) if isinstance(t, dict)],
            like_count=_safe_int(info.get("like_count")),
            coin_count=_safe_int(info.get("coin_count")),
        )
        if not meta.title:
            raise RuntimeError(f"未获取到视频信息（bvid={bvid}）")
        return meta

    # ------------------------------------------------------------------
    # 字幕链：UP主字幕(cc/zh-CN) → B站AI字幕(ai-zh) → Whisper（ADR #3）
    # 注意：yt-dlp 提取器的字幕请求不带传入的 cookie（用类属性 headers），
    # 因此 ai-zh 需要走私有 API 路径（_fetch_bili_api_transcript）。
    # ------------------------------------------------------------------
    def fetch_transcript(self, video_id: str) -> Transcript | None:
        bvid = extract_bvid(video_id)
        page = extract_page(video_id)
        info = self._extract(bvid, download=False, page=page)
        picked = pick_subtitle_entry(info)
        if picked is not None:
            lang_key, entry = picked
            logger.info("命中字幕（yt-dlp）：%s", lang_key)
            transcript = download_subtitle_transcript(lang_key, entry, cookie=self._cookie)
            if transcript is not None:
                return transcript
            logger.warning("字幕 %s 解析失败，继续尝试 API 路径", lang_key)

        # API 路径（需 Cookie）：UP主字幕 → B站AI字幕
        if self._cookie:
            transcript = self._fetch_bili_api_transcript(bvid, page)
            if transcript is not None:
                return transcript

        logger.info("无内置字幕，降级到 Whisper 转写")
        return None

    def _fetch_bili_api_transcript(self, bvid: str, page: int | None = None) -> Transcript | None:
        """私有 API 字幕路径：view → cid → player → subtitle_url（需登录 Cookie）。

        返回 None 表示无可用字幕（继续降级）。本函数是 B 站私有 API 的唯一收敛点。
        """
        import requests

        from src.fetcher.subtitles import parse_bili_json_subtitle

        headers = {
            "User-Agent": _UA,
            "Referer": "https://www.bilibili.com/",
            "Cookie": self._cookie,
        }
        try:
            view = requests.get(
                "https://api.bilibili.com/x/web-interface/view",
                params={"bvid": bvid},
                headers=headers,
                timeout=15,
            ).json()
            data = view.get("data") or {}
            pages = data.get("pages") or []
            if page and 1 <= page <= len(pages):
                cid = pages[page - 1].get("cid")
            else:
                cid = data.get("cid")  # 默认第一 P
            if not cid:
                return None
            player = requests.get(
                f"https://api.bilibili.com/x/player/wbi/v2",
                params={"bvid": bvid, "cid": cid},
                headers=headers,
                timeout=15,
            ).json()
            sub_list = (player.get("data") or {}).get("subtitle", {}).get("subtitles") or []
        except Exception as e:
            logger.warning("B站字幕 API 请求失败（继续降级）：%s", e)
            return None

        from src.fetcher.subtitles import AI_SUB_LANGS, UP_SUB_LANGS

        sub_list = [s for s in sub_list if s.get("subtitle_url") and s.get("lan")]
        ordered = []
        for lang in UP_SUB_LANGS + AI_SUB_LANGS:
            ordered.extend(s for s in sub_list if s.get("lan") == lang)
        for sub in ordered:
            try:
                url = sub["subtitle_url"]
                if url.startswith("//"):
                    url = f"https:{url}"  # 协议相对地址补全
                resp = requests.get(url, headers=headers, timeout=30)
                resp.raise_for_status()
                segments = parse_bili_json_subtitle(resp.json())
                if segments:
                    logger.info("命中字幕（B站API）：%s（%d 段）", sub["lan"], len(segments))
                    return Transcript(source=sub["lan"], segments=segments)
            except Exception as e:
                logger.warning("字幕 %s 下载失败（尝试下一个）：%s", sub.get("lan"), e)
        return None

    # ------------------------------------------------------------------
    # 下载（音频 / 视频）
    # ------------------------------------------------------------------
    def download_audio(self, video_id: str, out_dir: Path) -> Path:
        bvid = extract_bvid(video_id)
        page = extract_page(video_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        outtmpl = str(out_dir / "audio")
        opts = self._base_opts() | {
            "format": "ba/b",
            "outtmpl": outtmpl,
            "postprocessor_args": ["-ar", "16000", "-ac", "1"],
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "wav",
                    "preferredquality": "0",
                }
            ],
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([_video_url(bvid, page)])
        wav_path = out_dir / "audio.wav"
        if not wav_path.exists():
            raise RuntimeError(f"音频下载后未找到产物：{wav_path}")
        return wav_path

    def download_video(self, video_id: str, out_dir: Path) -> Path:
        bvid = extract_bvid(video_id)
        page = extract_page(video_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        outtmpl = str(out_dir / "video.%(ext)s")
        # 纯视频流优先（mp4/h264，保证 opencv 可 seek 抽帧）。
        # 白名单 vcodec^=avc1：只选 h264（avc1.xxx）。多数 opencv 构建无 AV1/HEVC
        # 解码器，抽帧/场景检测会失败；B 站默认会优先给 AV1（av01.xxx）。
        opts = self._base_opts() | {
            "format": (
                "bv*[ext=mp4][vcodec^=avc1]"
                "/bv*[vcodec^=avc1]"
                "/bv*[ext=mp4]"
                "/b"
            ),
            "outtmpl": outtmpl,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([_video_url(bvid, page)])
        matches = sorted(out_dir.glob("video.*"))
        if not matches:
            raise RuntimeError(f"视频下载后未找到产物（bvid={bvid}）")
        return matches[0]

    # ------------------------------------------------------------------
    # M2 预留
    # ------------------------------------------------------------------
    def search(self, query: SearchQuery) -> list[SearchCandidate]:
        raise NotImplementedError("搜索能力随里程碑 M2 实现")

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _base_opts(self) -> dict:
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": False,
            "http_headers": {"User-Agent": _UA},
            "noplaylist": True,
        }
        if self._cookie:
            opts["http_headers"]["Cookie"] = self._cookie
        return opts

    def _extract(self, bvid: str, *, download: bool, page: int | None = None) -> dict:
        with yt_dlp.YoutubeDL(self._base_opts()) as ydl:
            try:
                return ydl.extract_info(_video_url(bvid, page), download=download)
            except yt_dlp.utils.DownloadError as e:
                # 把 yt-dlp 的错误信息翻译成对用户友好的报错
                msg = str(e)
                if "This video is only available to" in msg or "login" in msg.lower():
                    raise RuntimeError(
                        "该视频需要登录才能访问：请在 config.yaml 的 bilibili.cookie 填入登录 Cookie 后重试"
                    ) from e
                if "not found" in msg.lower() or "不存在" in msg or "404" in msg:
                    raise RuntimeError(f"视频不存在或已被删除（{bvid}）") from e
                raise RuntimeError(f"获取视频信息失败：{msg}") from e


def _safe_int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _timestamp_to_datetime(ts) -> datetime | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts))
    except (TypeError, ValueError, OSError):
        return None
