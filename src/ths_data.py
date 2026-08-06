"""同花顺金融数据 API（fuyao.aicubes.cn）封装。

文档: https://fuyao.aicubes.cn/docs/quickstart/
      https://fuyao.aicubes.cn/docs/api-reference/prices/
鉴权: Header "X-api-key: <key>"
Base URL: https://fuyao.aicubes.cn/api
"""
from __future__ import annotations

import logging
import time

import pandas as pd
import requests

from config import settings as cfg

logger = logging.getLogger("ths_data")


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "X-api-key": cfg.THS_API_KEY,
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (jiecai-ai-trading)",
    })
    return s


def _request(path: str, params: dict | None = None, timeout: int = 15) -> dict:
    """通用 GET，返回 data 字段；失败返回空 dict。"""
    if not cfg.THS_API_KEY:
        logger.warning("THS_API_KEY 未配置")
        return {}
    url = cfg.THS_BASE_URL.rstrip("/") + "/" + path.lstrip("/")
    try:
        r = _session().get(url, params=params or {}, timeout=timeout)
        r.raise_for_status()
        j = r.json()
        if j.get("code") != 0:
            logger.warning("THS API %s 返回错误: %s", path, j.get("message"))
            return {}
        return j.get("data", {})
    except Exception as e:
        logger.warning("THS API %s 请求失败: %s", path, e)
        return {}


def health() -> bool:
    """简单探测：尝试取茅台快照（短超时，避免阻塞页面）。"""
    try:
        item = (_request("a-share/prices/snapshot",
                         {"thscodes": "600519.SH"}, timeout=3) or {}).get("item")
        return bool(item)
    except Exception:
        return False


def trade_calendar() -> list[str]:
    """A股交易日历（近一年）。返回标准化 ["YYYY-MM-DD", ...] 列表，失败返回空 list。"""
    data = _request("/a-share/calendar/trading-days", {})
    items = data.get("item") if isinstance(data, dict) else None
    if not items:
        return []
    dates = []
    for it in items:
        d = str(it.get("date", ""))
        if len(d) == 8:
            dates.append(f"{d[:4]}-{d[4:6]}-{d[6:8]}")
    return sorted(dates)


def _to_thscode(code6: str) -> str:
    """6位代码 → THS 带后缀代码。"""
    c = str(code6).strip()
    if c.startswith(("6", "68", "88")):
        return c + ".SH"
    if c.startswith(("0", "3", "002", "300", "301")):
        return c + ".SZ"
    if c.startswith(("4", "8", "92")):
        return c + ".BJ"
    return c


def _from_thscode(thscode: str) -> str:
    """THS 带后缀代码 → 6位代码。"""
    return str(thscode).replace(".SH", "").replace(".SZ", "").replace(".BJ", "")


def snapshot(thscodes: str) -> pd.DataFrame:
    """A股实时快照。thscodes 用逗号分隔，如 "600519.SH,000001.SH"。

    返回 DataFrame，列与现有 spot 表对齐：代码/名称/最新价/涨跌幅/成交额/流通市值_亿。
    """
    data = _request("/a-share/prices/snapshot", {"thscodes": thscodes})
    items = data.get("item") if isinstance(data, dict) else None
    if not items:
        return pd.DataFrame()
    rows = []
    for it in items:
        rows.append({
            "代码": _from_thscode(it.get("thscode", "")),
            "名称": it.get("ticker", ""),
            "最新价": _to_float(it.get("last_price")),
            "涨跌幅": _to_float(it.get("price_change_ratio_pct")),
            "涨跌额": _to_float(it.get("price_change")),
            "今开": _to_float(it.get("open_price")),
            "最高": _to_float(it.get("high_price")),
            "最低": _to_float(it.get("low_price")),
            "昨收": _to_float(it.get("prev_price")),
            "成交量": _to_float(it.get("volume")),
            "成交额": _to_float(it.get("turnover")),
        })
    df = pd.DataFrame(rows)
    # 名称列目前接口返回 ticker（代码），留空更稳妥
    df["名称"] = ""
    return df


def snapshot_batch(codes: list[str], batch_size: int = 100) -> pd.DataFrame:
    """批量快照，codes 为6位代码列表。"""
    parts = []
    for i in range(0, len(codes), batch_size):
        batch = ",".join(_to_thscode(c) for c in codes[i:i + batch_size])
        df = snapshot(batch)
        if not df.empty:
            parts.append(df)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


def historical(thscode: str, days: int = 1000, interval: str = "1d",
               adjust: str = "none") -> pd.DataFrame:
    """日线数据。返回 日期/开盘/最高/最低/收盘/成交量/成交额。"""
    end = int(time.time()) * 1000
    start = end - days * 24 * 3600 * 1000
    data = _request("/a-share/prices/historical", {
        "thscode": thscode,
        "interval": interval,
        "start": start,
        "end": end,
        "adjust": adjust,
    })
    items = data.get("item") if isinstance(data, dict) else None
    if not items:
        return pd.DataFrame()
    df = pd.DataFrame(items)
    df["日期"] = pd.to_datetime(df["date_ms"], unit="ms").dt.strftime("%Y-%m-%d")
    df = df.rename(columns={
        "open_price": "开盘", "high_price": "最高", "low_price": "最低",
        "close_price": "收盘", "volume": "成交量", "turnover": "成交额",
    })
    return df[["日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额"]].copy()


def _to_float(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def index_snapshot(thscodes: str = "000001.SH") -> pd.DataFrame:
    """指数快照。"""
    return snapshot(thscodes)


def hot_stock_list(period: str = "day") -> pd.DataFrame:
    """同花顺热榜（A股热股榜单）。period: day=24小时榜 / hour=小时榜。

    返回 排名/代码/名称/热度/排名变化。
    """
    data = _request("a-share/special-data/hot-stock-list", {"period": period})
    items = data.get("item") if isinstance(data, dict) else None
    if not items:
        return pd.DataFrame()
    rows = [{
        "排名": it.get("rank"),
        "代码": str(it.get("ticker", "")),
        "名称": it.get("name", ""),
        "热度": _to_float(it.get("heat")),
        "排名变化": it.get("rank_change"),
    } for it in items]
    return pd.DataFrame(rows).sort_values("排名").reset_index(drop=True)
