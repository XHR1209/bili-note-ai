"""CLI 入口（README §4）。

用法：
    bili-learn <BV号或完整链接> [--outdir outputs] [--no-frames] [--force]
"""

from __future__ import annotations

import argparse
import logging
import sys

from src.config import load_config
from src.fetcher.bili_source import BiliSource, extract_bvid
from src.llm import LLMClient
from src.pipeline import Pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bili-learn",
        description="B站AI速学助手：输入BV号，自动产出全视频图文笔记与极简速览摘要",
    )
    parser.add_argument("video", help="BV号（如 BV1xx411c7mD）或完整链接")
    parser.add_argument("--outdir", default=None, help="输出目录（默认 outputs/）")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument(
        "--no-frames", action="store_true", help="跳过抽帧（仅生成纯文字笔记）"
    )
    parser.add_argument(
        "--force", action="store_true", help="忽略缓存强制重跑所有阶段"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)

    try:
        cfg = load_config(args.config)
        if args.outdir:
            cfg.paths.output_dir = args.outdir
        llm = LLMClient(cfg.llm)
        source = BiliSource(cfg.bilibili)
        pipeline = Pipeline(cfg, llm, source)
        bvid = extract_bvid(args.video)
        logging.info("开始处理视频：%s", bvid)
        output = pipeline.run(bvid, no_frames=args.no_frames, force=args.force)
    except (ValueError, RuntimeError) as e:
        logging.error("%s", e)
        return 1

    bvid = output.meta.bvid
    logging.info("完成！产物位置：%s", cfg.notes_dir() / f"{bvid}_notes.md")
    return 0


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


if __name__ == "__main__":
    sys.exit(main())
