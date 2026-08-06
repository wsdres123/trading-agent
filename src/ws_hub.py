"""WebSocket 广播中枢：Redis Pub/Sub fan-out。

痛点3 改造核心：上游采集（L2 推送 / 后台轮询）写入统一频道 jc:quotes 后，
所有 WS 客户端由本模块统一消费广播——客户端数量不再放大上游请求量。

频道协议（JSON）：
    {"type": "market", "avg_price": ..., "indices": [...], "timestamp": ...}
    {"type": "quote", "code": "600519", "data": {...}, "timestamp": ...}
"""
from __future__ import annotations

import json
import logging

from config import settings as cfg

logger = logging.getLogger("ws_hub")

CHANNEL_QUOTES = f"{cfg.REDIS_KEY_PREFIX}:quotes"
_pub = None
_sub = None


def _redis_async():
    """懒加载 redis.asyncio 连接（Redis 不可用时返回 None，广播降级为无）。"""
    try:
        import redis.asyncio as aioredis
        return aioredis.Redis(
            host=cfg.REDIS_HOST, port=cfg.REDIS_PORT, db=cfg.REDIS_DB,
            password=cfg.REDIS_PASSWORD or None,
            socket_connect_timeout=1, socket_timeout=2,
            decode_responses=True,
        )
    except Exception:
        return None


def _redis_sub():
    """订阅专用连接：不设 socket_timeout，否则空闲超过 2 秒 listen() 会断。"""
    try:
        import redis.asyncio as aioredis
        return aioredis.Redis(
            host=cfg.REDIS_HOST, port=cfg.REDIS_PORT, db=cfg.REDIS_DB,
            password=cfg.REDIS_PASSWORD or None,
            socket_connect_timeout=2, socket_timeout=None,
            decode_responses=True,
        )
    except Exception:
        return None


async def publish_quote(payload: dict) -> None:
    """发布行情消息到广播频道（上游采集方调用，失败静默）。"""
    global _pub
    if _pub is None:
        _pub = _redis_async()
        if _pub is None:
            return
    try:
        await _pub.publish(CHANNEL_QUOTES, json.dumps(payload, ensure_ascii=False))
    except Exception:
        logger.debug("ws_hub publish failed", exc_info=True)


async def iter_quotes():
    """订阅广播频道，yield 消息 dict。连接失败立即结束（调用方自行重试）。"""
    global _sub
    if _sub is None:
        _sub = _redis_sub()
    if _sub is None:
        return
    pubsub = _sub.pubsub()
    await pubsub.subscribe(CHANNEL_QUOTES)
    try:
        async for msg in pubsub.listen():
            if msg.get("type") == "message":
                try:
                    yield json.loads(msg["data"])
                except Exception:
                    continue
    finally:
        try:
            await pubsub.unsubscribe(CHANNEL_QUOTES)
            await pubsub.close()
        except Exception:
            pass
