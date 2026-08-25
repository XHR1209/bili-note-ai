"""frame_extractor 测试：候选池序列化、最近值匹配（不依赖视频/LLM）。"""

from src.processor.frame_extractor import _nearest, load_candidate_pool, save_candidate_pool


class TestNearest:
    def test_exact(self):
        assert _nearest(12.0, [10.0, 12.0, 30.0]) == 12.0

    def test_between(self):
        assert _nearest(13.5, [10.0, 12.0, 30.0]) == 12.0
        assert _nearest(20.0, [10.0, 12.0, 30.0]) == 12.0   # |20-12|=8 < |20-30|=10
        assert _nearest(25.0, [10.0, 12.0, 30.0]) == 30.0

    def test_empty_pool(self):
        assert _nearest(5.0, []) == 5.0  # min() 空集返回自身（无候选时兜底）


class TestPoolPersistence:
    def test_round_trip(self, tmp_path):
        pool = [0.0, 30.5, 60.0, 100.25]
        path = tmp_path / "scenes.json"
        save_candidate_pool(pool, path)
        assert load_candidate_pool(path) == pool

    def test_missing_file(self, tmp_path):
        assert load_candidate_pool(tmp_path / "nope.json") is None

    def test_corrupt_file(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json {{{", encoding="utf-8")
        assert load_candidate_pool(path) is None
