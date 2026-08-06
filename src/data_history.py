"""个股历史K线（日K/分钟K）与关键指标，akshare 优先、同花顺兜底。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

from config import settings as cfg
from src.redis_cache import ttl_cache
from src import data_quality
from src.data_base import retry, _empty, _need_akshare, ak

logger = logging.getLogger("data")

# ── 个股历史K线（含均线/涨幅/新高计算）────────────────────────────────────
HIST_COLS = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额",
             "涨跌幅", "涨跌额", "换手率", "股票代码"]


@ttl_cache(cfg.HIST_TTL)
def get_stock_hist(code: str, days: int = 120, adjust: str = "qfq") -> pd.DataFrame:
    """个股日线，附加 MA5/10/20、N日涨幅、近 N 日最高。akshare 优先，失败回退同花顺。
    缓存链: L1→Redis→ts_store(parquet)→HTTP，与 _get_index_daily_full 一致。"""
    from src.ts_store import get_store
    ts = get_store()
    # parquet 命中且末日期>=今日，直接返回（跳过 HTTP）
    local_df = ts.load_stock_daily(code, days + 5)
    if local_df is not None:
        today = datetime.now().strftime("%Y-%m-%d")
        if str(local_df["日期"].iloc[-1]).strip() >= today:
            return local_df.tail(days + 5).reset_index(drop=True)
    _need_akshare()
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days + 60)).strftime("%Y%m%d")
    df = pd.DataFrame()
    try:
        df = retry()(ak.stock_zh_a_hist)(
            symbol=code, period="daily", start_date=start, end_date=end, adjust=adjust
        )
    except Exception as e:
        logger.debug("hist %s 失败：%s", code, e)
    if df.empty and cfg.THS_API_KEY:
        try:
            from src import ths_data
            # 同花顺 fallback 为不复权数据；显式标注 adjust，避免与 akshare 前复权混用
            df = ths_data.historical(ths_data._to_thscode(code), days=days + 10)
            if not df.empty:
                df = df.tail(days + 5).reset_index(drop=True)
                df["adjust"] = "none"
                df["source"] = "ths"
        except Exception as e:
            logger.debug("同花顺 hist %s 失败：%s", code, e)
    if df.empty:
        return _empty(HIST_COLS)
    df = df.tail(days + 5).reset_index(drop=True)
    if "adjust" not in df.columns:
        df["adjust"] = adjust
    if "source" not in df.columns:
        df["source"] = "akshare"
    for c in ("收盘", "最高", "最低", "开盘", "成交量", "成交额", "涨跌幅", "换手率"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = data_quality.validate_ohlc(df)
    if df.empty:
        return _empty(HIST_COLS)
    df["ma5"] = df["收盘"].rolling(5, min_periods=1).mean()
    df["ma10"] = df["收盘"].rolling(10, min_periods=1).mean()
    df["ma20"] = df["收盘"].rolling(20, min_periods=1).mean()
    if len(df) >= 2:
        df["ret_5d"] = (df["收盘"].iloc[-1] / df["收盘"].iloc[-6] - 1) * 100 if len(df) >= 6 else np.nan
        df["ret_30d"] = (df["收盘"].iloc[-1] / df["收盘"].iloc[-31] - 1) * 100 if len(df) >= 31 else np.nan
    else:
        df["ret_5d"] = df["ret_30d"] = np.nan
    df["high_100d"] = df["最高"].rolling(100, min_periods=1).max()
    # 回写 parquet，下次命中跳过 HTTP
    ts.save_stock_daily(code, df)
    return df


@ttl_cache(86400)
def get_stock_minute(code: str, date_str: str) -> pd.DataFrame:
    """个股1分钟K线（akshare stock_zh_a_hist_min_em），写回 ts_store parquet。
    历史分钟线当日不变，缓存1天。"""
    from src.ts_store import get_store
    ts = get_store()
    local_df = ts.load_stock_minute(code, date_str)
    if local_df is not None:
        return local_df
    _need_akshare()
    start_dt = f"{date_str} 09:00:00"
    end_dt = f"{date_str} 15:30:00"
    try:
        df = retry()(ak.stock_zh_a_hist_min_em)(
            symbol=code, period="1", start_date=start_dt, end_date=end_dt
        )
    except Exception as e:
        logger.warning("分钟线 %s %s 失败：%s", code, date_str, e)
        return pd.DataFrame()
    if df.empty:
        return pd.DataFrame()
    ts.save_stock_minute(code, df, date_str)
    return df


def get_stock_metrics(code: str) -> dict:
    """返回单只个股的关键指标（用于筛选最后一公里校验与展示）。"""
    df = get_stock_hist(code, days=120)
    if df.empty:
        return {}
    last = df.iloc[-1]
    return {
        "code": code,
        "close": float(last["收盘"]),
        "ma5": float(last["ma5"]),
        "ma10": float(last["ma10"]),
        "ma20": float(last["ma20"]),
        "ret_5d": float(last["ret_5d"]) if pd.notna(last["ret_5d"]) else None,
        "ret_30d": float(last["ret_30d"]) if pd.notna(last["ret_30d"]) else None,
        "high_100d": float(last["high_100d"]),
        "is_100d_new_high": bool(last["收盘"] >= last["high_100d"]),
        "closes": df["收盘"].tail(110).astype(float).tolist(),
        "highs": df["最高"].tail(110).astype(float).tolist(),
    }

