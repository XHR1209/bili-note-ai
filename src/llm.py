"""LLM 统一封装（README §6.2 / ADR #9）。

- 统一 OpenAI SDK 接口，环境变量 API_KEY / BASE_URL + 配置模型名
- 指数退避重试（网络错误 / 5xx / 429）
- 并发控制（信号量）
- token 用量统计（写入 stats）
- 每视频预算控制：超过 max_calls_per_video 抛 BudgetExceeded，由调用方优雅降级
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    OpenAI,
    RateLimitError,
)

from src.config import LLMConfig

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BASE_SECONDS = 2.0


class LLMBudgetExceeded(Exception):
    """每视频 LLM 调用预算超限（调用方应降级而非失败）。"""

    def __init__(self, limit: int):
        super().__init__(f"LLM 调用次数超过预算上限 {limit}，进入优雅降级")
        self.limit = limit


class LLMClient:
    """线程安全的 LLM 客户端。"""

    def __init__(self, cfg: LLMConfig):
        self._cfg = cfg
        if not cfg.api_key:
            raise ValueError(
                "缺少 API_KEY：请设置环境变量 API_KEY（以及可选 BASE_URL），"
                "或在 config.yaml 中配置。"
            )
        self._client = OpenAI(api_key=cfg.api_key, base_url=cfg.base_url or None)
        self.max_concurrency = cfg.max_concurrency
        self._semaphore = threading.Semaphore(cfg.max_concurrency)
        self._lock = threading.Lock()
        self._calls = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def chat(self, system: str, prompt: str, *, max_tokens: int | None = None) -> str:
        """单次对话调用，返回纯文本。"""
        self._check_budget()
        messages = [{"role": "system", "content": system}]
        if prompt:
            messages.append({"role": "user", "content": prompt})
        resp = self._call_with_retry(messages, max_tokens=max_tokens)
        return resp

    def chat_json(self, system: str, prompt: str, *, max_tokens: int | None = None) -> dict[str, Any]:
        """要求模型输出 JSON，解析失败自动重试 1 次（README §6.2）。"""
        self._check_budget()
        json_prompt = (
            f"{prompt}\n\n"
            "【输出要求】只输出一个合法 JSON 对象（不要包含 markdown 代码块标记），"
            "键名严格使用我要求的结构。"
        )
        last_err: Exception | None = None
        for attempt in range(2):
            try:
                text = self.chat(system, json_prompt, max_tokens=max_tokens)
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
                raise ValueError("JSON 根节点不是对象")
            except (json.JSONDecodeError, ValueError) as e:
                last_err = e
                logger.warning("chat_json 解析失败（第 %d 次），重试：%s", attempt + 1, e)
        raise ValueError(f"LLM JSON 输出连续解析失败：{last_err}")

    def stats(self) -> dict[str, int]:
        """累计统计（线程安全）。"""
        with self._lock:
            return {
                "llm_calls": self._calls,
                "prompt_tokens": self._prompt_tokens,
                "completion_tokens": self._completion_tokens,
            }

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _check_budget(self) -> None:
        with self._lock:
            if self._calls >= self._cfg.max_calls_per_video:
                raise LLMBudgetExceeded(self._cfg.max_calls_per_video)

    def _call_with_retry(self, messages: list[dict], *, max_tokens: int | None) -> str:
        with self._semaphore:
            last_err: Exception | None = None
            for attempt in range(MAX_RETRIES):
                try:
                    return self._call_once(messages, max_tokens=max_tokens)
                except RateLimitError as e:
                    last_err = e
                    logger.warning("LLM 限流（第 %d 次重试）：%s", attempt + 1, e)
                except APIConnectionError as e:
                    last_err = e
                    logger.warning("LLM 连接失败（第 %d 次重试）：%s", attempt + 1, e)
                except APIStatusError as e:
                    if e.status_code >= 500:
                        last_err = e
                        logger.warning("LLM 服务端错误 %d（第 %d 次重试）", e.status_code, attempt + 1)
                    else:
                        raise
                except APIError as e:
                    last_err = e
                    logger.warning("LLM 调用异常（第 %d 次重试）：%s", attempt + 1, e)
                time.sleep(RETRY_BASE_SECONDS * (2**attempt))
            raise RuntimeError(f"LLM 调用连续失败（已重试 {MAX_RETRIES} 次）：{last_err}")

    def _call_once(self, messages: list[dict], *, max_tokens: int | None) -> str:
        resp = self._client.chat.completions.create(
            model=self._cfg.model,
            messages=messages,
            temperature=self._cfg.temperature,
            max_tokens=max_tokens or self._cfg.max_tokens,
            timeout=self._cfg.timeout_seconds,
        )
        with self._lock:
            self._calls += 1
            usage = resp.usage
            if usage:
                self._prompt_tokens += usage.prompt_tokens or 0
                self._completion_tokens += usage.completion_tokens or 0
        return resp.choices[0].message.content or ""
