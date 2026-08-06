"""全市场指标缓存（筛选秒出的关键）：新浪日K并发拉取 → parquet。"""
from __future__ import annotations

import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd
import numpy as np

from config import settings as cfg
from src.data_quotes import _sina_symbol, _sina_session, _SINA_KLINE_URL
from src.data_spot import get_stock_spot
from src.data_history import get_stock_metrics

logger = logging.getLogger("data")

# 全市场指标缓存（一次构建，筛选秒出）
METRICS_CACHE = cfg.DATA_DIR / "metrics_cache.parquet"
METRICS_CACHE_TTL = 12 * 3600  # 缓存有效期 12 小时

_METRICS_DATALEN = 390  # 存390日序列：筛选用110日足够，平均股价K线需≥360日


def _sina_metrics(session, code: str) -> dict | None:
    """新浪日K → 单股指标（收盘/均线/涨幅/百日新高 + 390日OHLC序列）。"""
    r = session.get(_SINA_KLINE_URL, timeout=8, params={
        "symbol": _sina_symbol(code), "scale": 240, "ma": "no", "datalen": _METRICS_DATALEN,
    })
    r.raise_for_status()
    arr = r.json()
    if not arr or len(arr) < 2:
        return None
    close = np.array([float(x["close"]) for x in arr])
    high = np.array([float(x["high"]) for x in arr])
    low = np.array([float(x["low"]) for x in arr])
    open_ = np.array([float(x["open"]) for x in arr])
    volume = np.array([float(x.get("volume", 0) or 0) for x in arr])
    dates = [x["day"] for x in arr]
    last = float(close[-1])
    n = len(close)
    high_100d = float(high[-100:].max())
    return {
        "code": code, "close": last,
        "ma5": float(close[-5:].mean()),
        "ma10": float(close[-10:].mean()),
        "ma20": float(close[-20:].mean()),
        "ret_5d": round((last / close[-6] - 1) * 100, 2) if n >= 6 else None,
        "ret_30d": round((last / close[-31] - 1) * 100, 2) if n >= 31 else None,
        "high_100d": high_100d,
        "is_100d_new_high": bool(last >= high_100d),
        "closes": close.tolist(),
        "highs": high.tolist(),
        "lows": low.tolist(),
        "opens": open_.tolist(),
        "volumes": volume.tolist(),
        "last_date": dates[-1],
    }


def build_metrics_cache(progress_cb=None, max_workers: int = 32) -> pd.DataFrame | None:
    """构建全市场指标缓存：实时快照 + 新浪日K并发拉取 → parquet。

    新浪单股约 0.1s，32 并发全市场约 1-2 分钟；构建后筛选零联网、毫秒级。
    """
    spot = get_stock_spot()
    if spot.empty:
        logger.error("构建缓存失败：实时快照为空")
        return None
    spot["_code6"] = spot["代码"].astype(str).str.extract(r"(\d{6})")[0]
    spot = spot.dropna(subset=["_code6"])
    spot_by = {r["_code6"]: r for _, r in spot.iterrows()}
    codes = list(spot_by.keys())
    total = len(codes)

    session = _sina_session()

    def one(code: str) -> dict | None:
        try:
            return _sina_metrics(session, code)
        except Exception:
            try:  # 兜底：东财历史
                return get_stock_metrics(code) or None
            except Exception:
                return None

    metrics: dict[str, dict] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(one, c): c for c in codes}
        for fut in as_completed(futs):
            done += 1
            if progress_cb:
                progress_cb(done, total)
            m = fut.result()
            if m:
                metrics[futs[fut]] = m

    missing_codes = set(codes) - set(metrics.keys())
    if missing_codes:
        # 数据正确性：拉取失败的股票不再用“昨价拉平”伪造 390 日 OHLCV，
        # 避免污染平均股价、MA、百日新高、情绪统计等下游计算。
        logger.info("剔除 %d 只拉取失败的股票，不参与指标缓存", len(missing_codes))

    rows = []
    for code, m in metrics.items():
        s = spot_by.get(code, {})
        rows.append({
            "代码": code, "名称": s.get("名称", ""), "最新价": s.get("最新价", m["close"]),
            "涨跌幅": s.get("涨跌幅", None), "成交额": s.get("成交额", None),
            "流通市值_亿": s.get("流通市值_亿", None),
            "close": m["close"], "ma5": m["ma5"], "ma10": m["ma10"], "ma20": m["ma20"],
            "ret_5d": m["ret_5d"], "ret_30d": m["ret_30d"],
            "high_100d": m["high_100d"], "is_100d_new_high": m["is_100d_new_high"],
            "closes": m.get("closes"), "highs": m.get("highs"),
            "lows": m.get("lows"), "opens": m.get("opens"),
            "volumes": m.get("volumes"),
            "last_date": m.get("last_date"),
        })
    if not rows:
        return None
    df = pd.DataFrame(rows)
    try:
        df.to_parquet(METRICS_CACHE, index=False)
        logger.info("指标缓存已构建：%d 只", len(df))
    except Exception as e:
        logger.error("写指标缓存失败：%s", e)
    return df


def load_metrics_cache(allow_stale: bool = False) -> pd.DataFrame | None:
    """加载指标缓存；默认过期返回 None，allow_stale=True 时过期也返回。"""
    if not METRICS_CACHE.exists():
        return None
    if not allow_stale and time.time() - METRICS_CACHE.stat().st_mtime > METRICS_CACHE_TTL:
        return None
    try:
        df = pd.read_parquet(METRICS_CACHE)
        if "closes" not in df.columns:  # 旧版缓存缺价格序列，触发重建
            return None
        return df
    except Exception as e:
        logger.warning("读指标缓存失败：%s", e)
        return None


def metrics_cache_status() -> dict:
    """缓存状态（不加载全表）。"""
    if not METRICS_CACHE.exists():
        return {"exists": False}
    st = METRICS_CACHE.stat()
    age_min = int((time.time() - st.st_mtime) / 60)
    try:
        n = len(pd.read_parquet(METRICS_CACHE, columns=["代码"]))
    except Exception:
        n = 0
    return {
        "exists": True, "rows": n,
        "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
        "age_min": age_min, "fresh": age_min * 60 < METRICS_CACHE_TTL,
    }

