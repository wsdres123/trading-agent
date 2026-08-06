"""板块/概念：板块列表、成分股、板块名→代码集合。"""
from __future__ import annotations

import logging

import pandas as pd

from config import settings as cfg
from src.redis_cache import ttl_cache
from src.data_base import retry, _empty, _need_akshare, ak

logger = logging.getLogger("data")

BOARD_COLS = ["板块名称", "板块代码", "最新价", "涨跌幅", "涨跌额", "总市值"]


@ttl_cache(cfg.BOARD_TTL)
def _board_list(kind: str) -> pd.DataFrame:
    """kind: 'industry' | 'concept'。"""
    _need_akshare()
    fn = ak.stock_board_industry_name_em if kind == "industry" else ak.stock_board_concept_name_em
    try:
        df = retry()(fn)()
    except Exception as e:
        logger.warning("board %s 失败：%s", kind, e)
        return _empty(BOARD_COLS)
    keep = [c for c in BOARD_COLS if c in df.columns]
    return df[keep].reset_index(drop=True)


def get_industry_boards() -> pd.DataFrame:
    return _board_list("industry")

@ttl_cache(cfg.BOARD_TTL)
def get_board_constituents(board_name: str, kind: str = "industry") -> pd.DataFrame:
    """板块/概念成分股。"""
    _need_akshare()
    fn = ak.stock_board_industry_cons_em if kind == "industry" else ak.stock_board_concept_cons_em
    try:
        df = retry()(fn)(symbol=board_name)
    except Exception as e:
        logger.debug("cons %s/%s 失败：%s", kind, board_name, e)
        return _empty(["代码", "名称"])
    keep = [c for c in ("代码", "名称", "最新价", "涨跌幅") if c in df.columns]
    return df[keep].reset_index(drop=True)


def sector_to_codes(name: str) -> set[str]:
    """根据板块/概念名（支持模糊匹配）返回成分股代码集合。

    同时检索行业板块与概念板块，取并集，满足"半导体板块"这类宽口径需求。
    """
    codes: set[str] = set()
    for kind in ("industry", "concept"):
        boards = _board_list(kind)
        if boards.empty or "板块名称" not in boards.columns:
            continue
        hit = boards[boards["板块名称"].str.contains(name, na=False, regex=False)]
        for bn in hit["板块名称"].tolist():
            cons = get_board_constituents(bn, kind=kind)
            if not cons.empty and "代码" in cons.columns:
                codes.update(cons["代码"].astype(str).str.zfill(6).tolist())
    return codes

