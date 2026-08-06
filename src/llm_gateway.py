"""统一 LLM 网关：超时、重试、速率限制、token 统计、模型降级。

所有策略模块不再直接实例化 OpenAI client，统一通过 `call_llm()` 调用，
保证线上/回测的 LLM 行为一致、可观测、可限流。
"""
from __future__ import annotations

import logging
import time
from collections import deque

from config import settings as cfg

logger = logging.getLogger("llm_gateway")


class _RateLimiter:
    """滑动窗口速率限制器（调用次数/窗口秒数）。"""

    def __init__(self, max_calls: int, window_seconds: float):
        self.max_calls = max_calls
        self.window = window_seconds
        self._calls: deque[float] = deque()

    def acquire(self) -> bool:
        now = time.time()
        cutoff = now - self.window
        while self._calls and self._calls[0] < cutoff:
            self._calls.popleft()
        if len(self._calls) >= self.max_calls:
            return False
        self._calls.append(now)
        return True


# 按模型 tier 设置默认限流：turbo 最快，max 最慢
_LIMITERS: dict[str, _RateLimiter] = {
    "qwen-turbo": _RateLimiter(60, 60),
    "qwen-plus": _RateLimiter(30, 60),
    "qwen-max": _RateLimiter(10, 60),
}

_token_stats = {"total_input": 0, "total_output": 0, "calls": 0}


def _normalize_model(model: str) -> str:
    """将具体模型名归一化为 tier，用于限流键。"""
    m = model.lower()
    if "turbo" in m:
        return "qwen-turbo"
    if "plus" in m:
        return "qwen-plus"
    if "max" in m:
        return "qwen-max"
    return model


def token_stats() -> dict:
    """返回累计 token 消耗与调用次数。"""
    return _token_stats.copy()


def reset_token_stats() -> None:
    """重置 token 统计（主要用于评测）。"""
    _token_stats.update({"total_input": 0, "total_output": 0, "calls": 0})


def call_llm(
    prompt: str,
    model: str | None = None,
    fallback_models: list[str] | None = None,
    system: str | None = None,
    temperature: float = 0.1,
    max_tokens: int = 500,
    timeout: float = 30.0,
    retries: int = 1,
    extra_body: dict | None = None,
) -> str | None:
    """统一调用 LLM，返回原始文本；失败返回 None。

    Args:
        prompt: 用户 prompt
        model: 首选模型，默认 cfg.QWEN_CHAT_MODEL
        fallback_models: 首选失败后依次尝试的模型列表
        system: 系统提示（可选）
        temperature: 采样温度
        max_tokens: 最大输出 token 数
        timeout: 单次调用超时（秒）
        retries: 同一模型重试次数（不含首次）

    Returns:
        LLM 原始输出字符串，全部失败则 None
    """
    if not cfg.QWEN_API_KEY:
        logger.warning("未配置 QWEN_API_KEY")
        return None

    models = []
    if model:
        models.append(model)
    if fallback_models:
        models.extend(fallback_models)
    if not models:
        models.append(cfg.QWEN_CHAT_MODEL)

    from openai import OpenAI
    client = OpenAI(api_key=cfg.QWEN_API_KEY, base_url=cfg.QWEN_BASE_URL, timeout=timeout)

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    for m in models:
        limiter = _LIMITERS.get(_normalize_model(m))
        if limiter and not limiter.acquire():
            logger.warning("模型 %s 触发速率限制，跳过", m)
            continue

        for attempt in range(retries + 1):
            try:
                start = time.time()
                kwargs = {
                    "model": m,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if extra_body:
                    kwargs["extra_body"] = extra_body
                resp = client.chat.completions.create(**kwargs)
                elapsed = time.time() - start
                content = resp.choices[0].message.content.strip()

                usage = resp.usage
                if usage:
                    _token_stats["total_input"] += usage.prompt_tokens or 0
                    _token_stats["total_output"] += usage.completion_tokens or 0
                _token_stats["calls"] += 1

                logger.debug("LLM %s 成功 (%.2fs, %d chars)", m, elapsed, len(content))
                return content
            except Exception as e:
                logger.warning("LLM %s 调用失败 (attempt %d/%d): %s", m, attempt + 1, retries + 1, e)
                if attempt == retries:
                    break

    logger.error("LLM 全部模型调用失败")
    return None
