"""个股实时快照：腾讯批量首选 → Fuyao L2 降级 → akshare 兜底 → stale 缓存。"""
from __future__ import annotations

import time
import logging
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from config import settings as cfg
from src.redis_cache import ttl_cache, redis_get
from src.redis_cache import _cache_key as _redis_cache_key
from src import data_quality, source_health, monitor
from src.data_base import retry, _empty, _need_akshare, ak
from src.data_quotes import _sina_symbol
from src.data_calendar import get_stock_list

logger = logging.getLogger("data")

SPOT_COLS = ["代码", "名称", "最新价", "涨跌幅", "涨跌额", "最高", "成交量", "成交额",
             "换手率", "流通市值", "总市值"]

_TENCENT_BATCH = 80

def _tencent_spot_batch(session, syms: list[str]) -> list[dict]:
    """腾讯批量实时行情（含流通市值），单批 80 只约 0.3s。"""
    r = session.get("https://qt.gtimg.cn/q=" + ",".join(syms), timeout=8)
    r.encoding = "gbk"
    rows = []
    for line in r.text.splitlines():
        f = line.split("~")
        if len(f) < 47 or not f[1]:
            continue
        try:
            rows.append({
                "代码": f[2], "名称": f[1],
                "最新价": float(f[3]), "涨跌额": float(f[31]), "涨跌幅": float(f[32]),
                "最高": float(f[33]) if f[33] else None,
                "成交量": float(f[6]), "成交额": float(f[37]) * 1e4,
                "换手率": float(f[38]) if f[38] else None,
                "流通市值": float(f[44]) * 1e8 if f[44] else None,
                "总市值": float(f[45]) * 1e8 if f[45] else None,
            })
        except (ValueError, IndexError):
            continue
    return rows


def get_stock_spot_fast(max_workers: int = 16) -> pd.DataFrame:
    """全 A 实时快照（腾讯批量并发，全市场约 2s）。"""
    if not source_health.is_available("tencent"):
        return _empty(SPOT_COLS)
    import requests
    lst = get_stock_list()
    if lst.empty:
        return _empty(SPOT_COLS)
    codes = lst["代码"].astype(str).str.zfill(6).tolist()
    syms = [_sina_symbol(c) for c in codes]
    batches = [syms[i:i + _TENCENT_BATCH] for i in range(0, len(syms), _TENCENT_BATCH)]
    s = requests.Session()
    s.headers.update({"Referer": "https://finance.qq.com",
                      "User-Agent": "Mozilla/5.0"})
    s.mount("https://", requests.adapters.HTTPAdapter(
        pool_connections=max_workers, pool_maxsize=max_workers))
    rows: list[dict] = []
    with monitor.datasource_timer("tencent"):
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for part in ex.map(lambda b: _tencent_spot_batch(s, b), batches):
                rows.extend(part)
    df = pd.DataFrame(rows)
    if df.empty:
        source_health.record("tencent", False)
        return _empty(SPOT_COLS)
    df = data_quality.validate_spot(df)
    source_health.record("tencent", len(df) > 1000)
    if df.empty:
        return _empty(SPOT_COLS)
    df["流通市值_亿"] = (df["流通市值"] / 1e8).round(2)
    df["总市值_亿"] = (df["总市值"] / 1e8).round(2)
    return df.reset_index(drop=True)


@ttl_cache(cfg.SPOT_TTL, l1_ttl=cfg.L1_TTL_SPOT)
def get_stock_spot() -> pd.DataFrame:
    """全 A 实时快照。腾讯批量首选（约2s），Fuyao L2 降级，akshare 兜底。
    全源失败时返回 Redis 最近缓存（stale），不返回空。"""
    # L1: 腾讯批量
    if source_health.is_available("tencent"):
        try:
            df = get_stock_spot_fast()
            if len(df) > 1000:
                return df
            logger.warning("腾讯快照仅 %d 行，尝试 Fuyao 降级", len(df))
        except Exception as e:
            source_health.record("tencent", False)
            logger.warning("腾讯快照失败：%s，尝试 Fuyao 降级", e)
    # L2 降级：Fuyao snapshot（有 API Key 时）
    if cfg.THS_API_KEY and source_health.is_available("fuyao"):
        try:
            with monitor.datasource_timer("fuyao"):
                from src import ths_data
                codes = get_stock_list()["代码"].astype(str).str.zfill(6).tolist()
                df = ths_data.snapshot_batch(codes, batch_size=100)
            if not df.empty and len(df) > 1000:
                for c in SPOT_COLS:
                    if c not in df.columns:
                        df[c] = None
                df = data_quality.validate_spot(df)
                source_health.record("fuyao", len(df) > 1000)
                if len(df) > 1000:
                    logger.info("Fuyao 快照降级 %d 行", len(df))
                    return df[SPOT_COLS].reset_index(drop=True)
            else:
                source_health.record("fuyao", False)
            logger.warning("Fuyao 快照仅 %d 行，回退 akshare", len(df) if not df.empty else 0)
        except Exception as e:
            source_health.record("fuyao", False)
            logger.warning("Fuyao 快照降级失败：%s，回退 akshare", e)
    # L3: akshare
    if source_health.is_available("akshare"):
        _need_akshare()
        try:
            with monitor.datasource_timer("akshare"):
                df = retry(retries=2, base=0.5)(ak.stock_zh_a_spot_em)()
        except Exception as e:
            source_health.record("akshare", False)
            logger.warning("spot_em 失败，尝试新浪源：%s", e)
            try:
                df = retry(retries=1)(ak.stock_zh_a_spot)()
            except Exception as e2:
                source_health.record("akshare", False)
                logger.error("新浪源也失败：%s", e2)
                return _stale_spot()
        if not df.empty:
            df = data_quality.validate_spot(df)
            source_health.record("akshare", len(df) > 1000)
            if len(df) > 1000:
                keep = [c for c in SPOT_COLS if c in df.columns]
                df = df[keep].copy()
                for c in ("最新价", "涨跌幅", "涨跌额", "最高", "成交量", "成交额", "换手率", "流通市值", "总市值"):
                    if c in df.columns:
                        df[c] = pd.to_numeric(df[c], errors="coerce")
                if "流通市值" in df.columns:
                    df["流通市值_亿"] = (df["流通市值"] / 1e8).round(2)
                if "总市值" in df.columns:
                    df["总市值_亿"] = (df["总市值"] / 1e8).round(2)
                return df.reset_index(drop=True)
        else:
            source_health.record("akshare", False)
    # 全源失败：返回 Redis 最近缓存（stale）
    return _stale_spot()


def _stale_spot() -> pd.DataFrame:
    """全源失败时从 Redis 取最近缓存的 spot 数据，标记 stale=True。"""
    key = _redis_cache_key("get_stock_spot", (), {})
    cached = redis_get(key)
    if cached is not None and not cached.empty:
        logger.warning("全数据源失败，返回 Redis 缓存旧数据（stale）")
        cached = cached.copy()
        cached.attrs["stale"] = True
        cached.attrs["data_source"] = "cache"
        return cached
    return _empty(SPOT_COLS)


def get_realtime_avg_price() -> dict:
    """全市场实时平均股价。"""
    df = get_stock_spot()
    prices = pd.to_numeric(df["最新价"], errors="coerce") if not df.empty else pd.Series([], dtype=float)
    prices = prices[prices > 0].dropna()
    return {
        "avg_price": round(float(prices.mean()), 3) if len(prices) else None,
        "stock_count": int(len(prices)),
        "timestamp": time.strftime("%H:%M:%S"),
    }

