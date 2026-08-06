"""Async data fetchers using httpx.AsyncClient.

Parallel implementations of data.py's sync network functions.
Used by server.py (FastAPI) for background cache warming and REST/WS endpoints.
Does NOT use caching — that's handled by redis_cache.py via data.py's @ttl_cache.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime

import httpx
import pandas as pd

from config import settings as cfg
from src.data import (
    _sina_symbol, _em_secid, _EM_HEADERS,
    SPOT_COLS, INDEX_COLS, _TENCENT_BATCH,
    _SINA_INDICES, _SINA_KLINE_URL,
    CONCEPT_CACHE, FREE_FLOAT_CACHE, FREE_FLOAT_TTL, _FREE_KW,
    _load_concept_cache, _load_free_float_cache,
    get_stock_list,
)

logger = logging.getLogger("async_fetch")

_QQ_HEADERS = {"Referer": "https://finance.qq.com", "User-Agent": "Mozilla/5.0"}
_SINA_HEADERS = {"Referer": "https://finance.sina.com.cn",
                 "User-Agent": "Mozilla/5.0"}

# 异步限流：每源最大并发 + 最小请求间隔（秒），防止一次 gather 打满源站
_SOURCE_LIMITS = {
    "tencent": {"sem": asyncio.Semaphore(8), "min_interval": 0.05},
    "sina": {"sem": asyncio.Semaphore(8), "min_interval": 0.05},
    "ths": {"sem": asyncio.Semaphore(4), "min_interval": 0.1},
}


class _AsyncRateLimiter:
    """简单异步限流器：控制每源最小请求间隔。"""

    def __init__(self):
        self._last: dict[str, float] = {}

    async def acquire(self, source: str, min_interval: float):
        now = time.time()
        last = self._last.get(source, 0)
        wait = max(0, min_interval - (now - last))
        if wait > 0:
            await asyncio.sleep(wait)
        self._last[source] = time.time()


_RATE_LIMITER = _AsyncRateLimiter()


# ── Tencent batch spot (async) ────────────────────────────────────────────
async def _tencent_spot_batch_async(
    client: httpx.AsyncClient, syms: list[str],
) -> list[dict]:
    limit = _SOURCE_LIMITS["tencent"]
    await _RATE_LIMITER.acquire("tencent", limit["min_interval"])
    async with limit["sem"]:
        try:
            r = await client.get(
                "https://qt.gtimg.cn/q=" + ",".join(syms),
                timeout=8, headers=_QQ_HEADERS)
            text = r.content.decode("gbk", errors="replace")
        except Exception as e:
            logger.debug("Tencent batch failed: %s", e)
            return []
        rows = []
        for line in text.splitlines():
            f = line.split("~")
            if len(f) < 47 or not f[1]:
                continue
            try:
                rows.append({
                    "代码": f[2], "名称": f[1],
                    "最新价": float(f[3]), "涨跌额": float(f[31]),
                    "涨跌幅": float(f[32]),
                    "最高": float(f[33]) if f[33] else None,
                    "成交量": float(f[6]), "成交额": float(f[37]) * 1e4,
                    "换手率": float(f[38]) if f[38] else None,
                    "流通市值": float(f[44]) * 1e8 if f[44] else None,
                    "总市值": float(f[45]) * 1e8 if f[45] else None,
                })
            except (ValueError, IndexError):
                continue
        return rows


async def get_stock_spot_fast_async(
    client: httpx.AsyncClient,
) -> pd.DataFrame:
    """Async version of data.get_stock_spot_fast."""
    lst = get_stock_list()
    if lst.empty:
        return pd.DataFrame(columns=SPOT_COLS)
    codes = lst["代码"].astype(str).str.zfill(6).tolist()
    syms = [_sina_symbol(c) for c in codes]
    batches = [syms[i:i + _TENCENT_BATCH]
               for i in range(0, len(syms), _TENCENT_BATCH)]
    tasks = [_tencent_spot_batch_async(client, b) for b in batches]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    rows = []
    for r in results:
        if isinstance(r, Exception):
            continue
        rows.extend(r)
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=SPOT_COLS)
    df["流通市值_亿"] = (df["流通市值"] / 1e8).round(2)
    df["总市值_亿"] = (df["总市值"] / 1e8).round(2)
    return df.reset_index(drop=True)


# ── Sina fetch (async) ────────────────────────────────────────────────────
async def _sina_fetch_async(
    client: httpx.AsyncClient, symbols: list[str],
) -> list[tuple[str, list[str]]]:
    limit = _SOURCE_LIMITS["sina"]
    await _RATE_LIMITER.acquire("sina", limit["min_interval"])
    async with limit["sem"]:
        url = "https://hq.sinajs.cn/list=" + ",".join(symbols)
        try:
            resp = await client.get(url, timeout=5, headers=_SINA_HEADERS)
            resp.raise_for_status()
            text = resp.text
        except Exception as e:
            logger.debug("Sina fetch failed: %s", e)
            return []
        out = []
        for line in text.splitlines():
            if '="' not in line:
                continue
            try:
                sym = line.split("hq_str_")[1].split("=")[0]
                fields = line.split('="')[1].rstrip('";').split(",")
                if len(fields) >= 10 and fields[0]:
                    out.append((sym, fields))
            except Exception:
                continue
        return out


async def sina_index_spot_async(
    client: httpx.AsyncClient,
) -> pd.DataFrame:
    rows = []
    for sym, fields in await _sina_fetch_async(client, _SINA_INDICES):
        try:
            prev_close, last = float(fields[2]), float(fields[3])
            pct = (last / prev_close - 1) * 100 if prev_close else 0.0
            rows.append({
                "代码": sym[2:], "名称": fields[0], "最新价": round(last, 2),
                "涨跌幅": round(pct, 2), "涨跌额": round(last - prev_close, 2),
                "成交量": float(fields[8]), "成交额": float(fields[9]),
            })
        except (ValueError, IndexError):
            continue
    return pd.DataFrame(rows, columns=INDEX_COLS)


async def get_stock_quote_fast_async(
    client: httpx.AsyncClient, code: str,
) -> dict:
    code = str(code).strip().zfill(6)
    try:
        rows = await _sina_fetch_async(client, [_sina_symbol(code)])
        if not rows:
            return {}
        fields = rows[0][1]
        prev, last = float(fields[2]), float(fields[3])
        return {
            "代码": code, "名称": fields[0], "最新价": last,
            "涨跌幅": round((last / prev - 1) * 100, 2) if prev else 0.0,
            "涨跌额": round(last - prev, 2),
            "今开": float(fields[1]), "最高": float(fields[4]),
            "最低": float(fields[5]),
            "成交量": float(fields[8]), "成交额": float(fields[9]),
        }
    except Exception as e:
        logger.debug("Sina quote %s failed: %s", code, e)
        return {}


# ── Index daily K-line (async) ─────────────────────────────────────────────
async def get_index_daily_async(
    client: httpx.AsyncClient,
    symbol: str = "sh000001", days: int = 380,
) -> pd.DataFrame:
    cols = ["日期", "开盘", "最高", "最低", "收盘", "成交量"]
    limit = _SOURCE_LIMITS["sina"]
    await _RATE_LIMITER.acquire("sina", limit["min_interval"])
    async with limit["sem"]:
        try:
            r = await client.get(_SINA_KLINE_URL, timeout=8, params={
                "symbol": symbol, "scale": 240, "ma": "no",
                "datalen": days + 10})
            arr = r.json()
            df = pd.DataFrame([{
                "日期": x["day"], "开盘": float(x["open"]),
                "最高": float(x["high"]), "最低": float(x["low"]),
                "收盘": float(x["close"]), "成交量": float(x["volume"]),
            } for x in arr])
        except Exception as e:
            logger.warning("指数日K %s 失败：%s", symbol, e)
            return pd.DataFrame(columns=cols)
    if df.empty:
        return pd.DataFrame(columns=cols)
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        r = await client.get(
            "https://qt.gtimg.cn/q=" + symbol, timeout=6,
            headers=_QQ_HEADERS)
        text = r.content.decode("gbk", errors="replace")
        f = text.split("~")
        if len(f) > 36 and f[3]:
            last, op, hi, lo = float(f[3]), float(f[5]), float(f[33]), float(f[34])
            if last > 0 and op > 0:
                bar = {"日期": today, "开盘": op, "最高": hi,
                       "最低": lo, "收盘": last,
                       "成交量": float(f[6] or 0)}
                if df["日期"].iloc[-1] == today:
                    df.iloc[-1] = [bar[c] for c in cols]
                else:
                    df = pd.concat([df, pd.DataFrame([bar])], ignore_index=True)
    except Exception as e:
        logger.debug("补当日指数bar失败：%s", e)
    return df.tail(days).reset_index(drop=True)


# ── THS index daily (async) ───────────────────────────────────────────────
async def get_ths_index_daily_async(
    client: httpx.AsyncClient,
    code: str = "883902", days: int = 200,
) -> pd.DataFrame:
    cols = ["日期", "开盘", "最高", "最低", "收盘", "成交量"]
    limit = _SOURCE_LIMITS["ths"]
    await _RATE_LIMITER.acquire("ths", limit["min_interval"])
    async with limit["sem"]:
        try:
            r = await client.get(
                f"http://d.10jqka.com.cn/v6/line/bk_{code}/01/last.js",
                timeout=8,
                headers={"User-Agent": "Mozilla/5.0",
                         "Referer": "https://q.10jqka.com.cn/"})
            m = re.search(r"\(\(\{.*\}\)\)\s*$", r.text, re.S)
            if not m:
                return pd.DataFrame(columns=cols)
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
                rows.append({"日期": d, "开盘": float(p[1]),
                             "最高": float(p[2]), "最低": float(p[3]),
                             "收盘": float(p[4]), "成交量": vol})
            df = pd.DataFrame(rows)
        except Exception as e:
            logger.warning("同花顺指数日K %s 失败：%s", code, e)
            return pd.DataFrame(columns=cols)
    return df.tail(days).reset_index(drop=True) if not df.empty \
        else pd.DataFrame(columns=cols)


# ── Hot stocks (async) ────────────────────────────────────────────────────
async def _theme_boards_async(
    client: httpx.AsyncClient, code: str,
) -> str | None:
    try:
        from src.theme_mode import _is_theme_board
    except Exception:
        _is_theme_board = None
    try:
        r = await client.get(
            "https://push2.eastmoney.com/api/qt/slist/get",
            params={"spt": "3", "pi": "0", "pz": "80", "po": "1",
                    "fid": "f3", "fltt": "2", "invt": "2", "np": "1",
                    "secid": _em_secid(code), "fields": "f12,f14"},
            headers=_EM_HEADERS, timeout=6)
        diff = (r.json().get("data") or {}).get("diff") or []
        names = [d["f14"] for d in diff
                 if str(d.get("f12", "")).startswith("BK")
                 and not str(d.get("f14", "")).endswith("_")]
        if _is_theme_board:
            names = [n for n in names if _is_theme_board(n)]
        return "、".join(names[:3]) if names else None
    except Exception as e:
        logger.debug("热榜板块 %s 失败：%s", code, e)
        return None


async def get_hot_stocks_async(
    client: httpx.AsyncClient,
    spot_df: pd.DataFrame | None = None,
    top: int = 10,
) -> pd.DataFrame:
    df = pd.DataFrame()
    if cfg.THS_API_KEY:
        try:
            from src import ths_data
            df = ths_data.hot_stock_list("hour")
        except Exception as e:
            logger.warning("同花顺API热榜失败：%s", e)
    if df.empty:
        try:
            r = await client.get(
                "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/"
                "v1/stock?stock_type=a&type=hour&list_type=normal",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            items = (r.json().get("data") or {}).get("stock_list") or []
            df = pd.DataFrame([{
                "排名": it.get("order"),
                "代码": str(it.get("code", "")),
                "名称": it.get("name", ""),
                "热度": pd.to_numeric(it.get("rate"), errors="coerce"),
                "排名变化": it.get("hot_rank_chg"),
            } for it in items])
        except Exception as e:
            logger.warning("同花顺公开热榜失败：%s", e)
    if df.empty:
        return df
    df = df.head(top).reset_index(drop=True)
    if spot_df is not None and not spot_df.empty:
        try:
            spot = spot_df[["代码", "最新价", "涨跌幅", "成交额"]].copy()
            df = df.merge(spot, on="代码", how="left")
            df["成交额(亿)"] = (pd.to_numeric(df["成交额"], errors="coerce") / 1e8).round(1)
            df = df.drop(columns=["成交额"])
        except Exception as e:
            logger.debug("热榜合并快照失败：%s", e)
    try:
        cache_file = cfg.DATA_DIR / "hot_boards.json"
        try:
            boards = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            boards = {}
        missing = [c for c in df["代码"] if c not in boards]
        if missing:
            tasks = [_theme_boards_async(client, c) for c in missing]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for code, b in zip(missing, results):
                if isinstance(b, str):
                    boards[code] = b
            cache_file.write_text(json.dumps(boards, ensure_ascii=False), encoding="utf-8")
        df["板块"] = df["代码"].map(lambda c: boards.get(c, "-"))
    except Exception as e:
        logger.debug("热榜板块获取失败：%s", e)
    return df


# ── Enrichment (async) ────────────────────────────────────────────────────
async def _tencent_minute_async(client: httpx.AsyncClient, code: str) -> dict:
    out = {"竞价量": None, "涨速": None}
    try:
        sym = _sina_symbol(code)
        r = await client.get(
            "https://web.ifzq.gtimg.cn/appstock/app/minute/query",
            params={"code": sym}, timeout=6)
        d = r.json()["data"][sym]["data"]["data"]
        if not d:
            return out
        first = d[0].split()
        out["竞价量"] = float(first[2])
        if len(d) >= 2:
            p_now = float(d[-1].split()[1])
            p_prev = float(d[-2].split()[1])
            if p_prev:
                out["涨速"] = round((p_now / p_prev - 1) * 100, 2)
    except Exception as e:
        logger.debug("分时 %s 失败：%s", code, e)
    return out


async def _tencent_seal_async(client: httpx.AsyncClient, code: str) -> float | None:
    try:
        r = await client.get(
            "https://qt.gtimg.cn/q=" + _sina_symbol(code), timeout=6,
            headers=_QQ_HEADERS)
        text = r.content.decode("gbk", errors="replace")
        f = text.split("~")
        if len(f) < 48:
            return None
        last, bid1_vol, limit_up = float(f[3]), float(f[10]), float(f[47])
        if limit_up and last >= limit_up - 1e-4:
            return round(bid1_vol * 100 * limit_up / 1e8, 3)
        return 0.0
    except Exception as e:
        logger.debug("封单 %s 失败：%s", code, e)
        return None


async def _free_float_ratio_async(client: httpx.AsyncClient, code: str) -> float | None:
    code = str(code).zfill(6)
    prefix = _sina_symbol(code)[:2].upper()
    try:
        r = await client.get(
            "https://emweb.securities.eastmoney.com/PC_HSF10/"
            "ShareholderResearch/PageAjax",
            params={"code": prefix + code},
            headers={"User-Agent": "Mozilla/5.0",
                     "Referer": "https://emweb.securities.eastmoney.com/"},
            timeout=8)
        holders = r.json().get("sdltgd") or []
        if not holders:
            return None
        latest = max(h.get("END_DATE", "") for h in holders)
        locked = 0.0
        for h in holders:
            if h.get("END_DATE") != latest:
                continue
            ratio = float(h.get("FREE_HOLDNUM_RATIO") or 0)
            name = h.get("HOLDER_NAME") or ""
            if ratio >= 5 and not any(k in name for k in _FREE_KW):
                locked += ratio
        return max(0.0, min(1.0, 1 - locked / 100))
    except Exception as e:
        logger.debug("自由流通比例 %s 失败：%s", code, e)
        return None


async def _stock_concepts_async(client: httpx.AsyncClient, code: str) -> str | None:
    _SKIP = {"融资融券", "机构重仓", "深股通", "沪股通", "标准普尔",
             "富时罗素", "MSCI中国", "证金持股", "百元股", "创业板综",
             "转债标的", "同花顺漂亮100"}
    try:
        r = await client.get(
            "https://push2.eastmoney.com/api/qt/slist/get",
            params={"spt": "3", "pi": "0", "pz": "50", "po": "1", "fid": "f3",
                    "fltt": "2", "invt": "2", "np": "1",
                    "secid": _em_secid(code), "fields": "f12,f14"},
            headers=_EM_HEADERS, timeout=6)
        diff = (r.json().get("data") or {}).get("diff") or []
        names = [d["f14"] for d in diff
                 if str(d.get("f12", "")).startswith("BK")
                 and d.get("f14") not in _SKIP]
        return "、".join(names[:5]) if names else None
    except Exception as e:
        logger.debug("概念 %s 失败：%s", code, e)
        return None


async def enrich_stocks_async(
    client: httpx.AsyncClient, codes: list[str],
) -> pd.DataFrame:
    codes = [str(c).zfill(6) for c in codes if c]
    if not codes:
        return pd.DataFrame(
            columns=["代码", "涨速", "竞价量", "涨停封单额", "自由流通比例", "概念板块"])
    concepts = _load_concept_cache()
    ff_cache = _load_free_float_cache()
    now = time.time()

    async def one(code: str) -> dict:
        row = {"代码": code}
        row.update(await _tencent_minute_async(client, code))
        row["涨停封单额"] = await _tencent_seal_async(client, code)
        ent = ff_cache.get(code)
        if ent and now - ent.get("ts", 0) < FREE_FLOAT_TTL:
            row["自由流通比例"] = ent["ratio"]
        else:
            ratio = await _free_float_ratio_async(client, code)
            row["自由流通比例"] = ratio
            if ratio is not None:
                ff_cache[code] = {"ratio": ratio, "ts": now}
        if code in concepts:
            row["概念板块"] = concepts[code]
        else:
            c = await _stock_concepts_async(client, code)
            row["概念板块"] = c if c is not None else "-"
            if c is not None:
                concepts[code] = c
        return row

    rows = await asyncio.gather(*[one(c) for c in codes])
    try:
        CONCEPT_CACHE.write_text(json.dumps(concepts, ensure_ascii=False), encoding="utf-8")
        FREE_FLOAT_CACHE.write_text(json.dumps(ff_cache, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.debug("写增强缓存失败：%s", e)
    return pd.DataFrame(rows)


# ── Realtime avg price (async) ─────────────────────────────────────────────
async def get_realtime_avg_price_async(
    client: httpx.AsyncClient,
) -> dict:
    df = await get_stock_spot_fast_async(client)
    prices = pd.to_numeric(df["最新价"], errors="coerce") if not df.empty \
        else pd.Series([], dtype=float)
    prices = prices[prices > 0].dropna()
    return {
        "avg_price": round(float(prices.mean()), 3) if len(prices) else None,
        "stock_count": int(len(prices)),
        "timestamp": time.strftime("%H:%M:%S"),
    }
