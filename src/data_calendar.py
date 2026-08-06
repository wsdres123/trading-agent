"""股票列表、交易日历、证券主数据快照（时点化股票池）。"""
from __future__ import annotations

import time
import logging
from datetime import datetime

import pandas as pd

from config import settings as cfg
from src.redis_cache import ttl_cache
from src.data_base import retry, _empty, _need_akshare, _AKSHARE_OK, ak

logger = logging.getLogger("data")

@ttl_cache(86400)
def get_stock_list() -> pd.DataFrame:
    """全 A 代码/名称列表（磁盘缓存 7 天，冷启动零联网）。"""
    names_file = cfg.DATA_DIR / "stock_names.parquet"
    if names_file.exists() and time.time() - names_file.stat().st_mtime < 7 * 86400:
        try:
            return pd.read_parquet(names_file)
        except Exception:
            pass
    _need_akshare()
    try:
        df = retry(retries=2, base=0.5)(ak.stock_info_a_code_name)()
        df = df.rename(columns={"code": "代码", "name": "名称"})[["代码", "名称"]]
        try:
            df.to_parquet(names_file, index=False)
        except Exception:
            pass
        return df
    except Exception as e:
        logger.error("stock_info_a_code_name 失败：%s", e)
        if names_file.exists():
            try:
                return pd.read_parquet(names_file)
            except Exception:
                pass
        return _empty(["代码", "名称"])


# ── 交易日历 + 证券主数据时点化 ────────────────────────────────────────────

@ttl_cache(cfg.CALENDAR_TTL)
def get_trade_calendar() -> list[str]:
    """A股交易日历。Fuyao API 优先（近一年），akshare 兜底（全量历史），parquet 持久化。"""
    from src.ts_store import get_store
    ts = get_store()

    # L1: parquet
    dates = ts.load_trade_calendar()
    today = datetime.now().strftime("%Y-%m-%d")
    if dates and dates[-1] >= today:
        return dates

    # L2: Fuyao API
    fetched: list[str] = []
    try:
        from src import ths_data
        fetched = ths_data.trade_calendar()
    except Exception as e:
        logger.warning("Fuyao trade_calendar failed: %s", e)

    # L3: akshare 兜底（全量历史）
    if not fetched and _AKSHARE_OK:
        try:
            df = retry(retries=2, base=0.5)(ak.tool_trade_date_hist_sina)()
            fetched = sorted(df["trade_date"].astype(str).tolist())
        except Exception as e:
            logger.warning("akshare tool_trade_date_hist_sina failed: %s", e)

    if not fetched and dates:
        return dates  # parquet 旧数据兜底

    if fetched:
        # 合并 parquet 旧数据（补全 Fuyao 近一年之外的日期）
        if dates:
            combined = sorted(set(dates) | set(fetched))
        else:
            combined = fetched
        ts.save_trade_calendar(combined)
        return combined
    return dates


def is_trading_day(date_str: str | None = None) -> bool:
    """判断是否为交易日。date_str 格式 YYYY-MM-DD，默认今天。"""
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    dates = get_trade_calendar()
    return date_str in dates if dates else datetime.now().weekday() < 5


@ttl_cache(cfg.SPOT_TTL, l1_ttl=cfg.L1_TTL_SPOT)
def get_securities_snapshot() -> pd.DataFrame:
    """当前证券主数据快照：代码/名称/st_status。用于每日落盘消除幸存者偏差。"""
    df = get_stock_list()
    if df.empty:
        return df

    st_codes: set[str] = set()
    if _AKSHARE_OK:
        try:
            st_df = ak.stock_zh_a_st_em()
            if not st_df.empty and "代码" in st_df.columns:
                st_codes = set(st_df["代码"].astype(str).str.strip().tolist())
        except Exception as e:
            logger.warning("stock_zh_a_st_em failed: %s", e)

    df = df.copy()
    df["st_status"] = df["代码"].apply(
        lambda c: "ST" if str(c).strip() in st_codes else "")
    return df.reset_index(drop=True)


def get_point_in_time_pool(date_str: str) -> pd.DataFrame:
    """返回 ≤ date_str 的最近证券主数据快照（时点股票池，消除幸存者偏差）。"""
    from src.ts_store import get_store
    ts = get_store()
    df = ts.get_point_in_time_pool(date_str)
    return df if df is not None else _empty(["代码", "名称", "st_status"])

