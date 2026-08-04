"""L1内存 + L2 Redis 分层TTL缓存。

序列化升级：DataFrame 用 parquet，dict/list 用 msgpack，大值 gzip 压缩。
Redis 客户端单例复用，避免每次调用创建新实例。
L1 进程内存缓存（微秒级）用于热数据，跳过 Redis 网络 IO。
"""
from __future__ import annotations

import gzip
import hashlib
import io
import logging
import pickle
import time
from functools import wraps
from typing import Any

import pandas as pd

from config import settings as cfg

logger = logging.getLogger("redis_cache")

_GZIP_THRESHOLD = 10_000   # 超过 10KB 的值启用 gzip 压缩
_DF_MAGIC = b"__df__"      # DataFrame parquet 标记
_GZ_MAGIC = b"__gz__"      # gzip 压缩标记

_MEM_CACHE: dict[str, tuple[float, object]] = {}
_L1_CACHE: dict[str, tuple[float, object]] = {}

_redis_pool = None
_redis_client = None
_redis_last_check: float = 0.0
_REDIS_CHECK_INTERVAL: float = 30.0


def _redis_available() -> bool:
    """Check Redis connectivity, re-checking every 30s on failure."""
    global _redis_pool, _redis_client, _redis_last_check
    if not cfg.REDIS_ENABLED:
        return False
    if _redis_client is not None:
        try:
            _redis_client.ping()
            return True
        except Exception:
            _redis_pool = None
            _redis_client = None
    now = time.time()
    if now - _redis_last_check < _REDIS_CHECK_INTERVAL:
        return False
    _redis_last_check = now
    try:
        import redis
        _redis_pool = redis.ConnectionPool(
            host=cfg.REDIS_HOST, port=cfg.REDIS_PORT, db=cfg.REDIS_DB,
            password=cfg.REDIS_PASSWORD or None,
            socket_connect_timeout=1, socket_timeout=2,
            max_connections=50,
        )
        _redis_client = redis.Redis(connection_pool=_redis_pool)
        _redis_client.ping()
        logger.info("Redis connected at %s:%s", cfg.REDIS_HOST, cfg.REDIS_PORT)
        return True
    except Exception as e:
        _redis_pool = None
        _redis_client = None
        logger.debug("Redis not available, using in-memory cache: %s", e)
        return False


def _cache_key(fn_name: str, args: tuple, kwargs: dict) -> str:
    """Construct cache key matching data.py's ttl_cache wrapper."""
    raw = f"{fn_name}:{args}:{sorted(kwargs.items())}"
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{cfg.REDIS_KEY_PREFIX}:{fn_name}:{h}"


def _serialize(val: Any) -> bytes:
    """DataFrame → parquet，其他 → msgpack，大值 gzip 压缩。"""
    if isinstance(val, pd.DataFrame):
        buf = io.BytesIO()
        val.to_parquet(buf, compression=None)
        raw = _DF_MAGIC + buf.getvalue()
    else:
        import msgpack
        raw = msgpack.dumps(val, use_bin_type=True)
    if len(raw) > _GZIP_THRESHOLD:
        return _GZ_MAGIC + gzip.compress(raw)
    return raw


def _deserialize(data: bytes) -> Any:
    """反序列化：自动检测 gzip/parquet/msgpack/pickle 格式。"""
    if data[:6] == _GZ_MAGIC:
        data = gzip.decompress(data[6:])
    if data[:6] == _DF_MAGIC:
        return pd.read_parquet(io.BytesIO(data[6:]))
    # 旧 pickle 数据兼容（以 \x80 开头为 pickle protocol 2+）
    if data[:1] == b"\x80":
        return pickle.loads(data)
    import msgpack
    return msgpack.loads(data, raw=False)


def redis_get(key: str) -> Any:
    """Raw Redis get. Returns None on miss/unavailable."""
    if not _redis_available():
        return None
    try:
        raw = _redis_client.get(key)
        if raw is None:
            return None
        return _deserialize(raw)
    except Exception:
        return None


def redis_set(key: str, val: Any, ttl: float) -> None:
    """Raw Redis set with TTL."""
    if not _redis_available():
        return
    try:
        _redis_client.setex(key, int(ttl), _serialize(val))
    except Exception as e:
        logger.debug("Redis set failed for %s: %s", key, e)


def ttl_cache(ttl: float, l1_ttl: float | None = None):
    """TTL cache decorator with optional L1 in-memory fast path.

    Lookup order: L1 (process memory, l1_ttl) → L2 (Redis, ttl) → _MEM_CACHE (fallback).
    On miss, the result is written to all three tiers.
    """
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            key = _cache_key(fn.__name__, args, kwargs)
            now = time.time()
            if l1_ttl:
                hit = _L1_CACHE.get(key)
                if hit and now - hit[0] < l1_ttl:
                    return hit[1]
            val = redis_get(key)
            if val is not None:
                if l1_ttl:
                    _L1_CACHE[key] = (now, val)
                return val
            hit = _MEM_CACHE.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
            val = fn(*args, **kwargs)
            _MEM_CACHE[key] = (now, val)
            if l1_ttl:
                _L1_CACHE[key] = (now, val)
            redis_set(key, val, ttl)
            return val
        return wrapper
    return deco


def clear_cache(prefix: str = "") -> None:
    """Clear cache by function-name prefix. Works for L1 + Redis + in-memory."""
    mem_prefix = f"{cfg.REDIS_KEY_PREFIX}:{prefix}" if prefix else f"{cfg.REDIS_KEY_PREFIX}:"
    for k in [k for k in _MEM_CACHE if k.startswith(mem_prefix)]:
        _MEM_CACHE.pop(k, None)
    for k in [k for k in _L1_CACHE if k.startswith(mem_prefix)]:
        _L1_CACHE.pop(k, None)
    if _redis_available():
        try:
            pattern = f"{cfg.REDIS_KEY_PREFIX}:{prefix}*" if prefix else f"{cfg.REDIS_KEY_PREFIX}:*"
            pipe = _redis_client.pipeline()
            n = 0
            for key in _redis_client.scan_iter(pattern, count=500):
                pipe.delete(key)
                n += 1
                if n >= 500:
                    pipe.execute()
                    n = 0
            if n > 0:
                pipe.execute()
        except Exception as e:
            logger.debug("Redis clear failed: %s", e)
