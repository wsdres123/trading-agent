"""数据源健康追踪 + 熔断器。

内存状态，进程重启重置。连续失败3次 → 黑名单300秒，期间 is_available() 返回 False。
黑名单到期后自动恢复（试探性重试）。
"""
from __future__ import annotations

import logging
import time
import threading

logger = logging.getLogger("source_health")

_BLACKLIST_THRESHOLD = 3      # 连续失败3次触发熔断
_BLACKLIST_DURATION = 300    # 熔断 5 分钟


class SourceHealth:
    """数据源健康追踪：连续失败计数、失败率、黑名单。"""

    def __init__(self):
        self._lock = threading.Lock()
        # source -> {consecutive_fail, total, success, blacklist_until}
        self._sources: dict[str, dict] = {}

    def _ensure(self, source: str) -> dict:
        if source not in self._sources:
            self._sources[source] = {
                "consecutive_fail": 0,
                "total": 0,
                "success": 0,
                "blacklist_until": 0.0,
            }
        return self._sources[source]

    def record(self, source: str, success: bool) -> None:
        """记录一次请求结果。连续失败3次→黑名单5分钟。"""
        with self._lock:
            s = self._ensure(source)
            s["total"] += 1
            if success:
                s["success"] += 1
                s["consecutive_fail"] = 0
            else:
                s["consecutive_fail"] += 1
                if s["consecutive_fail"] >= _BLACKLIST_THRESHOLD:
                    s["blacklist_until"] = time.time() + _BLACKLIST_DURATION
                    logger.warning(
                        "数据源 %s 连续失败 %d 次，熔断 %d 秒",
                        source, s["consecutive_fail"], _BLACKLIST_DURATION,
                    )

    def is_available(self, source: str) -> bool:
        """源是否可用（未被黑名单且健康）。"""
        with self._lock:
            s = self._ensure(source)
            if s["blacklist_until"] > time.time():
                return False
            # 黑名单过期自动恢复
            if s["blacklist_until"] > 0 and s["blacklist_until"] <= time.time():
                s["blacklist_until"] = 0.0
                s["consecutive_fail"] = 0
                logger.info("数据源 %s 熔断恢复，试探性重试", source)
            return True

    def status(self) -> dict:
        """返回所有源的健康状态。"""
        with self._lock:
            out = {}
            now = time.time()
            for source, s in self._sources.items():
                fail_rate = 1.0 - (s["success"] / s["total"]) if s["total"] > 0 else 0.0
                out[source] = {
                    "available": s["blacklist_until"] <= now,
                    "fail_rate": round(fail_rate, 3),
                    "consecutive_fail": s["consecutive_fail"],
                    "total": s["total"],
                    "success": s["success"],
                }
            return out


_health = SourceHealth()


def record(source: str, success: bool) -> None:
    """模块级便捷接口。"""
    _health.record(source, success)


def is_available(source: str) -> bool:
    """模块级便捷接口。"""
    return _health.is_available(source)


def status() -> dict:
    """模块级便捷接口。"""
    return _health.status()
