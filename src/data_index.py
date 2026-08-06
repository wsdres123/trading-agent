"""指数日K（指数择时/主线模式用）与同花顺指数日K。"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime

import pandas as pd

from config import settings as cfg
from src.redis_cache import ttl_cache
from src.data_base import retry, _empty, _AKSHARE_OK, ak
from src.data_quotes import INDEX_COLS, _sina_index_spot, _sina_session, _SINA_KLINE_URL

logger = logging.getLogger("data")

# ── 指数日K（指数择时用）──────────────────────────────────────────────────
INDEX_KLINE_SYMBOLS = {"上证指数": "sh000001", "深证成指": "sz399001",
                       "创业板指": "sz399006", "科创50": "sh000688"}


@ttl_cache(cfg.SPOT_TTL, l1_ttl=cfg.L1_TTL_KLINE)
def _get_index_daily_full(symbol: str = "sh000001", days: int = 1060) -> pd.DataFrame:
    """拉取并缓存全量指数日K（canonical days=1060）。盘中用腾讯实时补当日未收盘bar。
    缓存链: L1(15s) → Redis(60s) → ts_store(本地parquet) → HTTP。
    与 get_index_daily(days=380) 共享同一份全量缓存，避免预热 key 与读取 key 不一致。"""
    import requests
    from src.ts_store import get_store
    cols = ["日期", "开盘", "最高", "最低", "收盘", "成交量"]
    ts = get_store()
    local_df = ts.load_index_daily(symbol, days)
    if local_df is not None:
        today = datetime.now().strftime("%Y-%m-%d")
        last_date = str(local_df["日期"].iloc[-1]).strip()
        if last_date >= today:
            return local_df
    try:
        s = _sina_session()
        r = s.get(_SINA_KLINE_URL, timeout=8, params={
            "symbol": symbol, "scale": 240, "ma": "no", "datalen": days + 10})
        arr = r.json()
        df = pd.DataFrame([{
            "日期": x["day"], "开盘": float(x["open"]), "最高": float(x["high"]),
            "最低": float(x["low"]), "收盘": float(x["close"]), "成交量": float(x["volume"]),
        } for x in arr])
    except Exception as e:
        logger.warning("指数日K %s 失败：%s", symbol, e)
        if local_df is not None:
            return local_df
        return _empty(cols)
    if df.empty:
        return local_df if local_df is not None else _empty(cols)
    # 盘中：新浪日K可能缺当日bar或数值滞后，用腾讯实时行情补/刷新
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        r = requests.get("https://qt.gtimg.cn/q=" + symbol, timeout=6,
                         headers={"Referer": "https://finance.qq.com",
                                  "User-Agent": "Mozilla/5.0"})
        r.encoding = "gbk"
        f = r.text.split("~")
        if len(f) > 36 and f[3]:
            last, op, hi, lo = float(f[3]), float(f[5]), float(f[33]), float(f[34])
            if last > 0 and op > 0:
                bar = {"日期": today, "开盘": op, "最高": hi, "最低": lo,
                       "收盘": last, "成交量": float(f[6] or 0)}
                if df["日期"].iloc[-1] == today:
                    df.iloc[-1] = [bar[c] for c in cols]
                else:
                    df = pd.concat([df, pd.DataFrame([bar])], ignore_index=True)
    except Exception as e:
        logger.debug("补当日指数bar失败：%s", e)
    result = df.tail(days).reset_index(drop=True)
    ts.save_index_daily(symbol, result)
    return result


def get_index_daily(symbol: str = "sh000001", days: int = 380) -> pd.DataFrame:
    """指数日K（新浪，OHLCV）。复用 _get_index_daily_full 的全量缓存，本地切片。
    预热与读取共享同一全量缓存 key，避免 days 参数不一致导致预热失效。"""
    full = _get_index_daily_full(symbol, days=1060)
    return full.tail(days).reset_index(drop=True)


def _append_ths_realtime(code: str, df: pd.DataFrame) -> pd.DataFrame:
    """若 df 中无当日数据，从同花顺 v4 实时接口补充当日 OHLCV。"""
    today = datetime.now().strftime("%Y-%m-%d")
    if not df.empty:
        last_date = str(df.iloc[-1]["日期"])[:10]
        if last_date >= today:
            return df
    try:
        r = _sina_session().get(
            f"https://d.10jqka.com.cn/v4/line/bk_{code}/01/today.js",
            timeout=5,
            headers={"User-Agent": "Mozilla/5.0",
                     "Referer": "https://q.10jqka.com.cn/"})
        m = re.search(r"\((\{.*\})\)\s*$", r.text, re.S)
        if not m:
            return df
        obj = json.loads(m.group(1))
        bk = obj.get(f"bk_{code}", {})
        d = bk.get("1", "")
        if len(d) == 8:
            d = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        if not d or d[:10] != today:
            return df
        row = {
            "日期": d,
            "开盘": float(bk["7"]), "最高": float(bk["8"]),
            "最低": float(bk["9"]), "收盘": float(bk["11"]),
            "成交量": float(bk.get("13", 0)),
        }
        if df.empty:
            return pd.DataFrame([row])
        return pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    except Exception as e:
        logger.debug("同花顺实时补充 %s 失败: %s", code, e)
        return df


@ttl_cache(cfg.SPOT_TTL, l1_ttl=cfg.L1_TTL_KLINE)
def get_ths_index_daily(code: str = "883902", days: int = 200) -> pd.DataFrame:
    """同花顺指数日K（如 883902 昨日成交前十）。
    缓存链: L1(15s) → Redis(60s) → ts_store(本地parquet) → HTTP。"""
    from src.ts_store import get_store
    cols = ["日期", "开盘", "最高", "最低", "收盘", "成交量"]
    ts = get_store()
    local_df = ts.load_ths_index(code, days)
    if local_df is not None:
        today = datetime.now().date()
        now_time = datetime.now().strftime("%H:%M")
        last_date = str(local_df["日期"].iloc[-1]).strip()[:10]
        try:
            last_dt = datetime.strptime(last_date, "%Y-%m-%d").date()
        except ValueError:
            last_dt = None
        if last_dt is not None:
            is_weekday = today.weekday() < 5
            after_close = now_time >= "15:30"
            have_today = last_dt >= today
            if have_today:
                return local_df
            if not is_weekday or not after_close:
                if (today - last_dt).days <= 3:
                    return local_df
    try:
        r = _sina_session().get(
            f"http://d.10jqka.com.cn/v6/line/bk_{code}/01/last.js", timeout=8,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://q.10jqka.com.cn/"})
        m = re.search(r"\((\{.*\})\)\s*$", r.text, re.S)
        if not m:
            fallback = local_df if local_df is not None else _empty(cols)
            return _append_ths_realtime(code, fallback)
        obj = json.loads(m.group(1))
        rows = []
        for seg in (obj.get("data") or "").split(";"):
            seg = seg.strip()
            if not seg:
                continue
            p = seg.split(",")
            if len(p) < 5:
                continue
            d = p[0]
            if len(d) == 8:
                d = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            vol = float(p[5]) if len(p) > 5 and p[5] else 0.0
            try:
                rows.append({"日期": d, "开盘": float(p[1]), "最高": float(p[2]),
                             "最低": float(p[3]), "收盘": float(p[4]), "成交量": vol})
            except (ValueError, IndexError):
                continue
        df = pd.DataFrame(rows)
    except Exception as e:
        logger.warning("同花顺指数日K %s 失败：%s", code, e)
        fallback = local_df if local_df is not None else _empty(cols)
        return _append_ths_realtime(code, fallback)
    result = df.tail(days).reset_index(drop=True) if not df.empty else _empty(cols)
    result = _append_ths_realtime(code, result)
    if not result.empty:
        ts.save_ths_index(code, result)
    return result


@ttl_cache(cfg.SPOT_TTL, l1_ttl=cfg.L1_TTL_SPOT)
def get_index_spot() -> pd.DataFrame:
    """主要指数实时。新浪首选（快），东财兜底。"""
    try:
        df = _sina_index_spot()
        if not df.empty:
            return df
    except Exception as e:
        logger.warning("新浪指数源失败：%s", e)
    if not _AKSHARE_OK:
        return _empty(INDEX_COLS)
    frames = []
    for sym in ("上证系列指数", "深证系列指数"):
        try:
            frames.append(retry(retries=1)(ak.stock_zh_index_spot_em)(symbol=sym))
        except Exception as e:
            logger.warning("index %s 失败：%s", sym, e)
    if not frames:
        return _empty(INDEX_COLS)
    df = pd.concat(frames, ignore_index=True)
    keep = [c for c in INDEX_COLS if c in df.columns]
    return df[keep].reset_index(drop=True)

