"""analyzer 纯逻辑测试：分块、章节校验、章节组装。"""

import json
from pathlib import Path

import pytest

from src.models import Chapter, Transcript
from src.processor.analyzer import (
    _build_chapters,
    _validate_merge,
    chunk_transcript,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_transcript.json"


@pytest.fixture
def transcript() -> Transcript:
    return Transcript.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 分块
# ---------------------------------------------------------------------------

class TestChunkTranscript:
    def test_small_transcript_single_chunk(self, transcript):
        chunks = chunk_transcript(transcript, chunk_chars=10_000, overlap_chars=300)
        assert len(chunks) == 1
        start, end, text = chunks[0]
        assert start == pytest.approx(0.0)
        assert end == pytest.approx(100.0)
        assert "大家好" in text
        assert "下节课再见" in text

    def test_large_chunk_splits_by_segments(self, transcript):
        # 每块 50 字左右 → 应切成多块，且切分在 segment 边界
        chunks = chunk_transcript(transcript, chunk_chars=50, overlap_chars=10)
        assert len(chunks) > 1
        # 相邻块时间上有重叠（overlap 参数生效）
        times = [(c[0], c[1]) for c in chunks]
        for i in range(1, len(times)):
            assert times[i][0] <= times[i - 1][1] + 1e-6

    def test_overlap_keeps_text(self, transcript):
        chunks = chunk_transcript(transcript, chunk_chars=60, overlap_chars=20)
        assert len(chunks) >= 2
        # 相邻块有文本重叠：重叠边界句同时出现在前块结尾与后块开头
        for i in range(1, len(chunks)):
            prev_text = chunks[i - 1][2]
            cur_text = chunks[i][2]
            boundary = prev_text.splitlines()[-1]
            assert boundary in cur_text

    def test_no_overlap_adjacent(self, transcript):
        # overlap_chars=0 时相邻块相接不重叠
        chunks = chunk_transcript(transcript, chunk_chars=60, overlap_chars=0)
        for i in range(1, len(chunks)):
            assert chunks[i][0] == chunks[i - 1][1] or chunks[i][0] > chunks[i - 1][1]

    def test_empty_transcript(self):
        t = Transcript(source="whisper", segments=[])
        assert chunk_transcript(t, 100, 10) == []


# ---------------------------------------------------------------------------
# 合并校验
# ---------------------------------------------------------------------------

class TestValidateMerge:
    def test_valid_coverage(self):
        raw = [
            {"title": "A", "block_start": 0, "block_end": 1},
            {"title": "B", "block_start": 2, "block_end": 3},
        ]
        out = _validate_merge(raw, n_blocks=4)
        assert out[0]["block_start"] == 0 and out[0]["block_end"] == 1
        assert out[1]["block_start"] == 2 and out[1]["block_end"] == 3

    def test_overlap_rejected(self):
        raw = [
            {"title": "A", "block_start": 0, "block_end": 2},
            {"title": "B", "block_start": 2, "block_end": 3},
        ]
        with pytest.raises(ValueError):
            _validate_merge(raw, n_blocks=4)

    def test_missing_block_rejected(self):
        raw = [
            {"title": "A", "block_start": 0, "block_end": 1},
            {"title": "B", "block_start": 3, "block_end": 3},
        ]
        with pytest.raises(ValueError):
            _validate_merge(raw, n_blocks=4)

    def test_out_of_range_rejected(self):
        raw = [{"title": "A", "block_start": 0, "block_end": 5}]
        with pytest.raises(ValueError):
            _validate_merge(raw, n_blocks=4)

    def test_missing_fields_rejected(self):
        raw = [{"title": "A"}]
        with pytest.raises(ValueError):
            _validate_merge(raw, n_blocks=4)


# ---------------------------------------------------------------------------
# 章节组装
# ---------------------------------------------------------------------------

class TestBuildChapters:
    def test_build(self, transcript):
        raw_chunks = chunk_transcript(transcript, chunk_chars=40, overlap_chars=5)
        chunks_meta = [
            {"title": f"块{i}", "summary": "摘要", "key_points": ["p1"]}
            for i in range(len(raw_chunks))
        ]
        raw = [
            {"title": "章节一", "block_start": 0, "block_end": len(raw_chunks) - 1,
             "summary": "总摘要", "key_points": ["kp1"]}
        ]
        chapters = _build_chapters(raw, raw_chunks, chunks_meta)
        assert len(chapters) == 1
        ch = chapters[0]
        assert isinstance(ch, Chapter)
        assert ch.index == 1
        assert ch.start_time == pytest.approx(raw_chunks[0][0])
        assert ch.end_time == pytest.approx(raw_chunks[-1][1])
        assert "大家好" in ch.raw_speaker_notes
        assert ch.title == "章节一"
