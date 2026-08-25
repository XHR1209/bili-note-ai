"""内容理解层（README §6.2）：纯处理逻辑，不感知平台。"""

from src.processor.transcriber import Transcriber, create_transcriber

__all__ = ["Transcriber", "create_transcriber"]
