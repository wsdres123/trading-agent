"""服务器时钟校准（clock sync）——数据层时钟功能。

背景：信号/回测/快照都依赖时间戳，本机时钟漂移会导致复盘数据错位。
无需 root、无需 NTP 守护进程：用行情源服务器的 HTTP Date 头作为基准
（"穷人版 NTP"），多样本取中位数 + RTT/2 修正网络延迟。
偏差超阈值只告警不改系统时间（改时间需 root，应交给 chrony/ntpdate）。

约定：信号时间戳统一使用 `clock.now()`，不要直接用 datetime.now()。
"""
from __future__ import annotations

import asyncio
import logging
import statistics
import time
from datetime import datetime
from email.utils import parsedate_to_datetime

logger = logging.getLogger("clock")

# 偏差告警阈值（秒）：超过则认为本机时钟不可信
DRIFT_WARN_SEC = 3.0

# 探测目标：复用现有免费行情源，Date 头即服务器当前时间（与开闭市无关）
_PROBES = [
    ("https://qt.gtimg.cn/q=sh000001",
     {"Referer": "https://finance.qq.com", "User-Agent": "Mozilla/5.0"}),
    ("https://hq.sinajs.cn/list=s_sh000001",
     {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}),
]

_offset: float = 0.0      # 服务器时间 - 本地时间（秒），正值=本机偏慢
_samples: int = 0
_last_sync: float = 0.0


async def _probe(client, url: str, headers: dict) -> float | None:
    """单次采样：offset = 服务器时间 + RTT/2 - 本地时间。"""
    t0 = time.time()
    try:
        r = await client.get(url, headers=headers, timeout=5)
        t1 = time.time()
        server = parsedate_to_datetime(r.headers.get("Date", "")).timestamp()
        return server + (t1 - t0) / 2 - t1
    except Exception as e:
        logger.debug("clock probe %s failed: %s", url, e)
        return None


async def sync(client, rounds: int = 3) -> float:
    """校准一轮：多源多采样取中位数，更新全局 offset。返回偏差秒数。"""
    global _offset, _samples, _last_sync
    samples: list[float] = []
    for url, headers in _PROBES:
        for _ in range(rounds):
            s = await _probe(client, url, headers)
            if s is not None:
                samples.append(s)
            await asyncio.sleep(0.2)
    if samples:
        _offset = statistics.median(samples)
        _samples = len(samples)
        _last_sync = time.time()
        logger.info("clock synced: offset=%.3fs samples=%d", _offset, _samples)
        if abs(_offset) > DRIFT_WARN_SEC:
            logger.warning(
                "本地时钟偏差 %.1fs 超过 %.0fs 阈值！信号时间戳可能失真，"
                "请用 chrony/ntpdate 手动校时（需 root）", _offset, DRIFT_WARN_SEC)
    else:
        logger.warning("clock sync failed: 所有探测源不可用，沿用上次 offset=%.3fs", _offset)
    return _offset


def now() -> datetime:
    """校准后的当前时间。信号时间戳请统一用本函数。"""
    return datetime.fromtimestamp(time.time() + _offset)


def offset_seconds() -> float:
    return _offset


def status() -> dict:
    """供 /api/health 与 UI 健康页展示。"""
    return {
        "offset_sec": round(_offset, 3),
        "drift_warning": abs(_offset) > DRIFT_WARN_SEC,
        "samples": _samples,
        "last_sync": (datetime.fromtimestamp(_last_sync).isoformat(timespec="seconds")
                      if _last_sync else None),
    }
