"""数据质量校验器：丢弃价格为0/NaN、涨跌幅异常、OHLC违规的行。

纯函数，无副作用，校验失败只 log warning + drop 行，不抛异常。
"""
from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger("data_quality")


def _limit_pct(code: str, name: str = "") -> float:
    """涨跌停幅度：创业板/科创板20%，北交所30%，ST 5%，其余主板10%。"""
    code = str(code).zfill(6)
    if code.startswith(("30", "68")):
        return 20.0
    if code.startswith(("8", "4", "92")):
        return 30.0
    if "ST" in str(name).upper():
        return 5.0
    return 10.0


def validate_spot(df: pd.DataFrame) -> pd.DataFrame:
    """校验实时快照：丢弃价格为0/NaN的行，标记异常涨跌幅。

    规则：
      1. 最新价 <= 0 或 NaN → 丢弃
      2. 涨跌幅 绝对值 > 涨跌停幅度 + 2%（超出法定限制，大概率数据错误）→ 丢弃
    """
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()

    n_before = len(df)
    code_col = "代码" if "代码" in df.columns else None
    name_col = "名称" if "名称" in df.columns else None

    # 1. 丢弃价格为0/NaN的行
    if "最新价" in df.columns:
        mask_valid_price = pd.to_numeric(df["最新价"], errors="coerce") > 0
        df = df[mask_valid_price].copy()

    # 2. 丢弃涨跌幅异常的行（超出法定涨跌停幅度+2%）
    if "涨跌幅" in df.columns and code_col:
        pct = pd.to_numeric(df["涨跌幅"], errors="coerce")
        bad_mask = pd.Series(False, index=df.index)
        for idx in df.index:
            c = str(df.at[idx, code_col])
            nm = str(df.at[idx, name_col]) if name_col else ""
            lim = _limit_pct(c, nm)
            p = pct.loc[idx]
            if pd.notna(p) and abs(p) > lim + 2.0:
                bad_mask.loc[idx] = True
        if bad_mask.any():
            n_bad = int(bad_mask.sum())
            logger.warning("validate_spot: 丢弃 %d 行异常涨跌幅数据", n_bad)
            df = df[~bad_mask].copy()

    n_after = len(df)
    if n_after < n_before:
        logger.info("validate_spot: %d → %d 行 (丢弃 %d)", n_before, n_after, n_before - n_after)
    return df.reset_index(drop=True)


def validate_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    """校验K线OHLC约束：high >= max(open,close), low <= min(open,close)。

    违反的行 drop 并 log warning。
    """
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()

    cols = {"开盘", "收盘", "最高", "最低"}
    if not cols.issubset(df.columns):
        return df

    n_before = len(df)
    o = pd.to_numeric(df["开盘"], errors="coerce")
    c = pd.to_numeric(df["收盘"], errors="coerce")
    h = pd.to_numeric(df["最高"], errors="coerce")
    l = pd.to_numeric(df["最低"], errors="coerce")

    upper = np.maximum(o, c)
    lower = np.minimum(o, c)
    mask_bad = (h < upper - 1e-6) | (l > lower + 1e-6)
    mask_bad = mask_bad.fillna(False)

    if mask_bad.any():
        n_bad = int(mask_bad.sum())
        logger.warning("validate_ohlc: 丢弃 %d 行 OHLC 违规数据", n_bad)
        df = df[~mask_bad].copy()

    n_after = len(df)
    if n_after < n_before:
        logger.info("validate_ohlc: %d → %d 行 (丢弃 %d)", n_before, n_after, n_before - n_after)
    return df.reset_index(drop=True)
