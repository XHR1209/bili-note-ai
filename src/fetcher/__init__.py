"""B站交互层（README §6.1）：唯一的平台接触点。"""

from src.fetcher.bili_source import BiliSource, extract_bvid

__all__ = ["BiliSource", "extract_bvid"]
