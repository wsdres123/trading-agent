"""新浪数据源基础设施：行情拉取、符号转换、会话管理、指数/个股实时。"""
from __future__ import annotations

import logging

import pandas as pd

from config import settings as cfg
from src.redis_cache import ttl_cache

logger = logging.getLogger("data")

# ── 指数实时 ──────────────────────────────────────────────────────────────
INDEX_COLS = ["代码", "名称", "最新价", "涨跌幅", "涨跌额", "成交量", "成交额"]

# 新浪指数代码（快、稳，作为首选源）
_SINA_INDICES = [
    "sh000001", "sz399001", "sz399006", "sh000688",
    "sh000300", "sh000016", "sh000905", "sh000852", "bj899050",
]

def _sina_fetch(symbols: list[str]) -> list[tuple[str, list[str]]]:
    """新浪行情原始拉取：返回 [(symbol, fields), ...]。"""
    import requests
    url = "https://hq.sinajs.cn/list=" + ",".join(symbols)
    resp = requests.get(
        url, timeout=5,
        headers={"Referer": "https://finance.sina.com.cn",
                 "User-Agent": "Mozilla/5.0"},
    )
    resp.raise_for_status()
    out = []
    for line in resp.text.splitlines():
        if '="' not in line:
            continue
        sym = line.split("hq_str_")[1].split("=")[0]
        fields = line.split('="')[1].rstrip('";').split(",")
        if len(fields) >= 10 and fields[0]:
            out.append((sym, fields))
    return out


def _sina_index_spot() -> pd.DataFrame:
    """新浪指数实时行情（单次 HTTP，毫秒级）。"""
    rows = []
    for sym, fields in _sina_fetch(_SINA_INDICES):
        prev_close, last = float(fields[2]), float(fields[3])
        pct = (last / prev_close - 1) * 100 if prev_close else 0.0
        rows.append({
            "代码": sym[2:], "名称": fields[0], "最新价": round(last, 2),
            "涨跌幅": round(pct, 2), "涨跌额": round(last - prev_close, 2),
            "成交量": float(fields[8]), "成交额": float(fields[9]),
        })
    return pd.DataFrame(rows, columns=INDEX_COLS)


def _sina_symbol(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith("92") or code[0] in "48":
        return "bj" + code
    if code[0] in "569":
        return "sh" + code
    return "sz" + code


@ttl_cache(cfg.SPOT_TTL, l1_ttl=cfg.L1_TTL_SPOT)
def get_stock_quote_fast(code: str) -> dict:
    """单只个股实时行情（新浪，毫秒级）。失败返回 {}。"""
    code = str(code).strip().zfill(6)
    try:
        rows = _sina_fetch([_sina_symbol(code)])
        if not rows:
            return {}
        fields = rows[0][1]
        prev, last = float(fields[2]), float(fields[3])
        return {
            "代码": code, "名称": fields[0], "最新价": last,
            "涨跌幅": round((last / prev - 1) * 100, 2) if prev else 0.0,
            "涨跌额": round(last - prev, 2),
            "今开": float(fields[1]), "最高": float(fields[4]), "最低": float(fields[5]),
            "成交量": float(fields[8]), "成交额": float(fields[9]),
        }
    except Exception as e:
        logger.warning("新浪个股 %s 失败：%s", code, e)
        return {}


_SINA_KLINE_URL = ("https://money.finance.sina.com.cn/quotes_service/api/"
                   "json_v2.php/CN_MarketData.getKLineData")


def _sina_session():
    import requests
    s = requests.Session()
    s.headers.update({"Referer": "https://finance.sina.com.cn",
                      "User-Agent": "Mozilla/5.0"})
    a = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64)
    s.mount("https://", a)
    return s
