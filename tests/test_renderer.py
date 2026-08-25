"""renderer 纯函数测试：视频笔记（导读+要点+关键帧）的 Markdown 结构。"""

from datetime import datetime

from src.models import Chapter, ExecSummary, FinalOutput, KeyFrame, VideoMeta
from src.renderer import render_condensed_notes


def make_output() -> FinalOutput:
    meta = VideoMeta(
        bvid="BV1xx411c7mD",
        title="深度学习入门",
        duration=100.0,
        author="讲师",
        desc="",
    )
    chapters = [
        Chapter(
            index=1,
            title="什么是深度学习",
            start_time=0.0,
            end_time=50.0,
            summary="深度学习是机器学习的子领域。",
            key_points=["神经网络由多层组成，每层提取不同抽象级别的特征。"],
            key_frames=[
                KeyFrame(
                    timestamp=12.0,
                    image_path="images/BV1xx411c7mD/01_012.0.png",
                    description="讲解者在展示神经网络结构图",
                )
            ],
            narrative="本章用状态机视角解释深度学习：神经网络即多层状态变换，"
                      "通过反向传播逐步逼近目标函数。",
            raw_speaker_notes="[00:00] 大家好，欢迎\n[00:05] 今天讲深度学习",
        ),
        Chapter(
            index=2,
            title="激活函数",
            start_time=50.0,
            end_time=100.0,
            summary="ReLU 是常用激活函数。",
            key_points=["ReLU 计算简单且能缓解梯度消失问题。"],
            key_frames=[],
        ),
    ]
    return FinalOutput(
        meta=meta,
        chapters=chapters,
        summary=ExecSummary(
            conclusion="深度学习通过多层神经网络学习特征。",
            logic_chain=["输入数据", "多层特征提取", "输出结果"],
            key_conclusions=["神经网络三层结构", "反向传播是关键"],
        ),
        generated_at=datetime(2026, 8, 25, 12, 0, 0),
        stats={"transcript_source": "cc"},
    )


class TestCondensedNotes:
    def test_structure(self):
        md = render_condensed_notes(make_output())
        assert "# 《深度学习入门》 - 视频笔记" in md
        assert "讲师" in md
        assert "https://www.bilibili.com/video/BV1xx411c7mD" in md
        assert "## 第1章 什么是深度学习" in md
        assert "*00:00 - 00:50*" in md

    def test_narrative(self):
        md = render_condensed_notes(make_output())
        assert "状态机视角解释深度学习" in md

    def test_key_points(self):
        md = render_condensed_notes(make_output())
        assert "**要点**：" in md
        assert "- 神经网络由多层组成" in md

    def test_key_frames_with_image_path(self):
        md = render_condensed_notes(make_output())
        # 关键帧图片引用（相对 outputs/notes/ 的 ../images/... 路径）
        assert "![时刻 00:12 - 讲解者在展示神经网络结构图](../images/BV1xx411c7mD/01_012.0.png)" in md

    def test_no_frames_chapter(self):
        md = render_condensed_notes(make_output())
        assert "## 第2章 激活函数" in md
        # 无帧的章节不产生图片行

    def test_no_transcript_in_output(self):
        md = render_condensed_notes(make_output())
        # 产物不含全文转写
        assert "[00:00] 大家好" not in md
