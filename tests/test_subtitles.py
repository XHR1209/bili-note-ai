"""字幕获取链测试：解析、入口挑选、SRT 兜底（不依赖网络）。"""

from src.fetcher.subtitles import (
    _parse_srt,
    parse_bili_json_subtitle,
    pick_subtitle_entry,
)


class TestPickSubtitleEntry:
    def test_cc_priority(self):
        info = {
            "subtitles": {
                "cc": [{"url": "https://x/cc.json", "ext": "json"}],
                "ai-cc": [{"url": "https://x/ai.json", "ext": "json"}],
            }
        }
        lang, entry = pick_subtitle_entry(info)
        assert lang == "cc"
        assert entry["url"].endswith("cc.json")

    def test_ai_cc_when_no_cc(self):
        info = {"subtitles": {"ai-cc": [{"url": "https://x/ai.json", "ext": "json"}]}}
        lang, entry = pick_subtitle_entry(info)
        assert lang == "ai-cc"

    def test_empty_entry_skipped(self):
        info = {"subtitles": {"cc": [{"url": ""}]}, "automatic_captions": {}}
        assert pick_subtitle_entry(info) is None

    def test_none_when_no_subs(self):
        assert pick_subtitle_entry({"subtitles": {}}) is None
        assert pick_subtitle_entry({}) is None


class TestParseBiliJson:
    def test_basic(self):
        data = {
            "body": [
                {"from": 0.0, "to": 3.5, "content": " 你好 ", "location": 2},
                {"from": 3.5, "to": 8.0, "content": "世界"},
                {"from": 8.0, "to": 10.0, "content": "   "},
            ]
        }
        segs = parse_bili_json_subtitle(data)
        assert len(segs) == 2
        assert segs[0].start_time == 0.0
        assert segs[0].end_time == 3.5
        assert segs[0].text == "你好"  # 去空白
        assert segs[1].text == "世界"

    def test_empty_body(self):
        assert parse_bili_json_subtitle({"body": []}) == []


class TestParseSrt:
    def test_basic(self):
        srt = """1
00:00:01,000 --> 00:00:03,500
第一行内容

2
00:00:04,000 --> 00:00:06,500
第二行内容
"""
        segs = _parse_srt(srt)
        assert len(segs) == 2
        assert segs[0].start_time == 1.0
        assert segs[0].end_time == 3.5
        assert segs[0].text == "第一行内容"

    def test_garbage(self):
        assert _parse_srt("随便什么文本\n没有时间轴") == []
