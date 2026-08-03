"""多源金融数据获取（akshare 为主，新浪为备，本地缓存兜底）。

所有函数独立于 Streamlit，便于单测；UI 层用 st.cache_data 进一步缓存。
网络失败时返回空 DataFrame，由 UI 友好提示，绝不抛异常打断界面。
"""
from __future__ import annotations

import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import wraps
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

from config import settings as cfg

logger = logging.getLogger("data")

# 全市场指标缓存（一次构建，筛选秒出）
METRICS_CACHE = cfg.DATA_DIR / "metrics_cache.parquet"
METRICS_CACHE_TTL = 12 * 3600  # 缓存有效期 12 小时

try:
    import akshare as ak
    _AKSHARE_OK = True
except Exception as e:  # 环境问题（如 libstdc++ 缺失）时给出清晰提示
    _AKSHARE_OK = False
    _AKSHARE_ERR = e
    logger.error("akshare 导入失败：%s。请用 run.sh 启动（设 LD_LIBRARY_PATH）。", e)


# ── 重试装饰器（应对东方财富偶发断连）────────────────────────────────────
def retry(retries: int = 4, base: float = 1.5):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            last = None
            for i in range(retries):
                try:
                    return fn(*args, **kwargs)
                except Exception as e:
                    last = e
                    time.sleep(base * (i + 1))
            logger.warning("%s 重试 %d 次仍失败：%s", fn.__name__, retries, last)
            raise last
        return wrapper
    return deco


# ── 简易 TTL 缓存（进程内）────────────────────────────────────────────────
_CACHE: dict[str, tuple[float, object]] = {}


def ttl_cache(ttl: float):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            key = f"{fn.__name__}:{args}:{sorted(kwargs.items())}"
            now = time.time()
            hit = _CACHE.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
            val = fn(*args, **kwargs)
            _CACHE[key] = (now, val)
            return val
        return wrapper
    return deco


def clear_cache(prefix: str = "") -> None:
    """清除内存 TTL 缓存（prefix 限定函数名前缀，空=全部）。"""
    for k in [k for k in _CACHE if k.startswith(prefix)]:
        _CACHE.pop(k, None)


def _need_akshare():
    if not _AKSHARE_OK:
        raise RuntimeError(
            "akshare 不可用，无法获取实时行情。"
            "请使用 run.sh 启动，或设置 LD_LIBRARY_PATH=/home/lixiang/anaconda3/lib。"
            f" 原始错误：{_AKSHARE_ERR}"
        )


def _empty(cols: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=cols)


# ── 个股实时快照 ──────────────────────────────────────────────────────────
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
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for part in ex.map(lambda b: _tencent_spot_batch(s, b), batches):
            rows.extend(part)
    df = pd.DataFrame(rows)
    if df.empty:
        return _empty(SPOT_COLS)
    df["流通市值_亿"] = (df["流通市值"] / 1e8).round(2)
    df["总市值_亿"] = (df["总市值"] / 1e8).round(2)
    return df.reset_index(drop=True)


@ttl_cache(cfg.SPOT_TTL)
def get_stock_spot() -> pd.DataFrame:
    """全 A 实时快照。腾讯批量首选（约2s），东财/新浪兜底。"""
    try:
        df = get_stock_spot_fast()
        if len(df) > 1000:
            return df
        logger.warning("腾讯快照仅 %d 行，回退 akshare", len(df))
    except Exception as e:
        logger.warning("腾讯快照失败：%s", e)
    _need_akshare()
    try:
        df = retry(retries=2, base=0.5)(ak.stock_zh_a_spot_em)()
    except Exception as e:
        logger.warning("spot_em 失败，尝试新浪源：%s", e)
        try:
            df = retry(retries=1)(ak.stock_zh_a_spot)()
        except Exception as e2:
            logger.error("新浪源也失败：%s", e2)
            return _empty(SPOT_COLS)
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


def get_realtime_avg_price() -> dict:
    """全市场实时平均股价（所有A股最新价算术均值）。"""
    import time as _time
    df = get_stock_spot()
    prices = pd.to_numeric(df["最新价"], errors="coerce") if not df.empty else pd.Series([], dtype=float)
    prices = prices[prices > 0].dropna()
    return {
        "avg_price": round(float(prices.mean()), 3) if len(prices) else None,
        "stock_count": int(len(prices)),
        "timestamp": _time.strftime("%H:%M:%S"),
    }


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


# ── 个股历史K线（含均线/涨幅/新高计算）────────────────────────────────────
HIST_COLS = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额",
             "涨跌幅", "涨跌额", "换手率", "股票代码"]


@ttl_cache(cfg.HIST_TTL)
def get_stock_hist(code: str, days: int = 120, adjust: str = "qfq") -> pd.DataFrame:
    """个股日线，附加 MA5/10/20、N日涨幅、近 N 日最高。akshare 优先，失败回退同花顺。"""
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
            df = ths_data.historical(ths_data._to_thscode(code), days=days + 10)
            if not df.empty:
                df = df.tail(days + 5).reset_index(drop=True)
        except Exception as e:
            logger.debug("同花顺 hist %s 失败：%s", code, e)
    if df.empty:
        return _empty(HIST_COLS)
    df = df.tail(days + 5).reset_index(drop=True)
    for c in ("收盘", "最高", "最低", "开盘", "成交量", "成交额", "涨跌幅", "换手率"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ma5"] = df["收盘"].rolling(5, min_periods=1).mean()
    df["ma10"] = df["收盘"].rolling(10, min_periods=1).mean()
    df["ma20"] = df["收盘"].rolling(20, min_periods=1).mean()
    if len(df) >= 2:
        df["ret_5d"] = (df["收盘"].iloc[-1] / df["收盘"].iloc[-6] - 1) * 100 if len(df) >= 6 else np.nan
        df["ret_30d"] = (df["收盘"].iloc[-1] / df["收盘"].iloc[-31] - 1) * 100 if len(df) >= 31 else np.nan
    else:
        df["ret_5d"] = df["ret_30d"] = np.nan
    df["high_100d"] = df["最高"].rolling(100, min_periods=1).max()
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


@ttl_cache(cfg.SPOT_TTL)
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


# ── 筛选结果增强字段（涨速/竞价量/涨停封单额/概念板块）────────────────────
CONCEPT_CACHE = cfg.DATA_DIR / "concepts.json"
_EM_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}


def _em_secid(code: str) -> str:
    code = str(code).zfill(6)
    return ("1." if code[0] in "569" else "0.") + code


def _tencent_minute(session, code: str) -> dict:
    """腾讯分时 → 竞价量（09:30首笔，手）与涨速（最近1分钟涨跌%）。"""
    out = {"竞价量": None, "涨速": None}
    try:
        r = session.get("https://web.ifzq.gtimg.cn/appstock/app/minute/query",
                        params={"code": _sina_symbol(code)}, timeout=6)
        d = r.json()["data"][_sina_symbol(code)]["data"]["data"]
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


def _tencent_seal(session, code: str) -> float | None:
    """涨停封单额（亿）：最新价达到涨停价时 = 买一量(手)×100×涨停价；未涨停为 0。"""
    try:
        r = session.get("https://qt.gtimg.cn/q=" + _sina_symbol(code), timeout=6)
        r.encoding = "gbk"
        f = r.text.split("~")
        if len(f) < 48:
            return None
        last, bid1_vol, limit_up = float(f[3]), float(f[10]), float(f[47])
        if limit_up and last >= limit_up - 1e-4:
            return round(bid1_vol * 100 * limit_up / 1e8, 3)
        return 0.0
    except Exception as e:
        logger.debug("封单 %s 失败：%s", code, e)
        return None


def _load_concept_cache() -> dict:
    try:
        import json
        return json.loads(CONCEPT_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _stock_concepts(session, code: str) -> str | None:
    """东财：个股所属概念板块（前若干个，顿号分隔）。"""
    _SKIP = {"融资融券", "机构重仓", "深股通", "沪股通", "标准普尔", "富时罗素",
             "MSCI中国", "证金持股", "百元股", "创业板综", "转债标的", "同花顺漂亮100"}
    try:
        r = session.get("https://push2.eastmoney.com/api/qt/slist/get",
                        params={"spt": "3", "pi": "0", "pz": "50", "po": "1", "fid": "f3",
                                "fltt": "2", "invt": "2", "np": "1",
                                "secid": _em_secid(code), "fields": "f12,f14"},
                        headers=_EM_HEADERS, timeout=6)
        diff = (r.json().get("data") or {}).get("diff") or []
        names = [d["f14"] for d in diff
                 if str(d.get("f12", "")).startswith("BK") and d.get("f14") not in _SKIP]
        return "、".join(names[:5]) if names else None
    except Exception as e:
        logger.debug("概念 %s 失败：%s", code, e)
        return None


FREE_FLOAT_CACHE = cfg.DATA_DIR / "free_float.json"
FREE_FLOAT_TTL = 7 * 24 * 3600  # 股东数据按季度披露，缓存 7 天
# 名称含这些关键词的≥5%流通股东仍视为自由流通（基金/社保等可随时买卖）
_FREE_KW = ("基金", "资管", "资产管理", "理财", "信托计划", "社保", "养老",
            "年金", "证券投资", "香港中央结算")


def _load_free_float_cache() -> dict:
    try:
        import json
        return json.loads(FREE_FLOAT_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _free_float_ratio(session, code: str) -> float | None:
    """自由流通比例 = 1 − 十大流通股东中≥5%的非基金类持股比例之和。

    数据源：东财 F10 股东研究（sdltgd=十大流通股东，取最新报告期）。
    """
    code = str(code).zfill(6)
    prefix = _sina_symbol(code)[:2].upper()
    try:
        r = session.get("https://emweb.securities.eastmoney.com/PC_HSF10/"
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


def enrich_stocks(codes: list[str], max_workers: int = 16) -> pd.DataFrame:
    """为筛选命中的个股补充：涨速/竞价量/涨停封单额/自由流通比例/概念板块（并发，仅对命中集）。"""
    import json
    import requests
    codes = [str(c).zfill(6) for c in codes]
    if not codes:
        return _empty(["代码", "涨速", "竞价量", "涨停封单额", "自由流通比例", "概念板块"])
    s = requests.Session()
    s.headers.update({"Referer": "https://finance.qq.com", "User-Agent": "Mozilla/5.0"})
    s.mount("https://", requests.adapters.HTTPAdapter(
        pool_connections=max_workers, pool_maxsize=max_workers))
    concepts = _load_concept_cache()
    ff_cache = _load_free_float_cache()
    now = time.time()

    def one(code: str) -> dict:
        row = {"代码": code}
        row.update(_tencent_minute(s, code))
        row["涨停封单额"] = _tencent_seal(s, code)
        ent = ff_cache.get(code)
        if ent and now - ent.get("ts", 0) < FREE_FLOAT_TTL:
            row["自由流通比例"] = ent["ratio"]
        else:
            ratio = _free_float_ratio(s, code)
            row["自由流通比例"] = ratio
            if ratio is not None:
                ff_cache[code] = {"ratio": ratio, "ts": now}
        if code in concepts:
            row["概念板块"] = concepts[code]
        else:
            c = _stock_concepts(s, code)
            row["概念板块"] = c if c is not None else "-"
            if c is not None:
                concepts[code] = c
        return row

    rows = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for r in ex.map(one, codes):
            rows.append(r)
    try:
        CONCEPT_CACHE.write_text(json.dumps(concepts, ensure_ascii=False), encoding="utf-8")
        FREE_FLOAT_CACHE.write_text(json.dumps(ff_cache, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.debug("写增强缓存失败：%s", e)
    return pd.DataFrame(rows)


# ── 指数日K（指数择时用）──────────────────────────────────────────────────
INDEX_KLINE_SYMBOLS = {"上证指数": "sh000001", "深证成指": "sz399001",
                       "创业板指": "sz399006", "科创50": "sh000688"}


@ttl_cache(cfg.SPOT_TTL)
def get_index_daily(symbol: str = "sh000001", days: int = 380) -> pd.DataFrame:
    """指数日K（新浪，OHLCV），盘中用腾讯实时补当日未收盘bar；60秒缓存实时刷新。"""
    import requests
    cols = ["日期", "开盘", "最高", "最低", "收盘", "成交量"]
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
        return _empty(cols)
    if df.empty:
        return _empty(cols)
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
    return df.tail(days).reset_index(drop=True)


@ttl_cache(cfg.SPOT_TTL)
def get_ths_index_daily(code: str = "883902", days: int = 200) -> pd.DataFrame:
    """同花顺指数日K（如 883902 昨日成交前十）。接口 d.10jqka.com.cn/v6/line/bk_<code>/01/last.js。"""
    import json as _json
    import re as _re
    cols = ["日期", "开盘", "最高", "最低", "收盘", "成交量"]
    try:
        r = _sina_session().get(
            f"http://d.10jqka.com.cn/v6/line/bk_{code}/01/last.js", timeout=8,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://q.10jqka.com.cn/"})
        m = _re.search(r"\((\{.*\})\)\s*$", r.text, _re.S)
        if not m:
            return _empty(cols)
        obj = _json.loads(m.group(1))
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
            rows.append({"日期": d, "开盘": float(p[1]), "最高": float(p[2]),
                         "最低": float(p[3]), "收盘": float(p[4]), "成交量": vol})
        df = pd.DataFrame(rows)
    except Exception as e:
        logger.warning("同花顺指数日K %s 失败：%s", code, e)
        return _empty(cols)
    return df.tail(days).reset_index(drop=True) if not df.empty else _empty(cols)


@ttl_cache(cfg.SPOT_TTL)
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


# ── 同花顺热榜（热门个股）──────────────────────────────────────────────────
@ttl_cache(cfg.SPOT_TTL)
def get_hot_stocks(top: int = 10) -> pd.DataFrame:
    """同花顺热榜前N热门股：排名/代码/名称/热度/最新价/涨跌幅。

    数据源：同花顺金融数据API（fuyao MCP 热榜）优先，同花顺公开热榜兜底。
    """
    df = pd.DataFrame()
    if cfg.THS_API_KEY:
        try:
            from src import ths_data
            df = ths_data.hot_stock_list("hour")
        except Exception as e:
            logger.warning("同花顺API热榜失败：%s", e)
    if df.empty:
        try:
            import requests
            r = requests.get(
                "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/"
                "v1/stock?stock_type=a&type=hour&list_type=normal",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            items = (r.json().get("data") or {}).get("stock_list") or []
            df = pd.DataFrame([{
                "排名": it.get("order"), "代码": str(it.get("code", "")),
                "名称": it.get("name", ""),
                "热度": pd.to_numeric(it.get("rate"), errors="coerce"),
                "排名变化": it.get("hot_rank_chg"),
            } for it in items])
        except Exception as e:
            logger.warning("同花顺公开热榜失败：%s", e)
    if df.empty:
        return df
    df = df.head(top).reset_index(drop=True)
    try:
        spot = get_stock_spot()[["代码", "最新价", "涨跌幅", "成交额"]]
        df = df.merge(spot, on="代码", how="left")
        df["成交额(亿)"] = (pd.to_numeric(df["成交额"], errors="coerce") / 1e8).round(1)
        df = df.drop(columns=["成交额"])
    except Exception as e:
        logger.debug("热榜合并快照失败：%s", e)
    try:
        import json
        import requests

        def _theme_boards(session, code: str) -> str | None:
            """个股所属板块（拉全量概念，过滤指数/风格类，取前3个题材板块）。"""
            try:
                r = session.get("https://push2.eastmoney.com/api/qt/slist/get",
                                params={"spt": "3", "pi": "0", "pz": "80", "po": "1",
                                        "fid": "f3", "fltt": "2", "invt": "2", "np": "1",
                                        "secid": _em_secid(code), "fields": "f12,f14"},
                                headers=_EM_HEADERS, timeout=6)
                diff = (r.json().get("data") or {}).get("diff") or []
                names = [d["f14"] for d in diff
                         if str(d.get("f12", "")).startswith("BK")
                         and not str(d["f14"]).endswith("_")]
                try:
                    from src.theme_mode import _is_theme_board
                    names = [n for n in names if _is_theme_board(n)]
                except Exception:
                    pass
                return "、".join(names[:3]) if names else None
            except Exception as e:
                logger.debug("热榜板块 %s 失败：%s", code, e)
                return None

        cache_file = cfg.DATA_DIR / "hot_boards.json"
        try:
            boards = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            boards = {}
        s = requests.Session()
        missing = [c for c in df["代码"] if c not in boards]
        if missing:
            with ThreadPoolExecutor(max_workers=8) as ex:
                for code, b in zip(missing, ex.map(lambda c: _theme_boards(s, c), missing)):
                    if b is not None:
                        boards[code] = b
            cache_file.write_text(json.dumps(boards, ensure_ascii=False), encoding="utf-8")
        df["板块"] = df["代码"].map(lambda c: boards.get(c, "-"))
    except Exception as e:
        logger.debug("热榜板块获取失败：%s", e)
    return df


# ── 板块/概念 ─────────────────────────────────────────────────────────────
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


def get_concept_boards() -> pd.DataFrame:
    return _board_list("concept")


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


def list_sector_names() -> list[str]:
    """所有行业+概念板块名（用于下拉提示/校验）。"""
    names: list[str] = []
    for kind in ("industry", "concept"):
        b = _board_list(kind)
        if not b.empty and "板块名称" in b.columns:
            names.extend(b["板块名称"].dropna().tolist())
    return sorted(set(names))


# ── 全市场指标缓存（筛选秒出的关键）───────────────────────────────────────
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


# ── 自检 ──────────────────────────────────────────────────────────────────
@ttl_cache(300)  # 每次页面交互都会重跑，健康检查含网络请求，必须缓存
def health() -> dict:
    _ths_ok = False
    try:
        from src import ths_data
        _ths_ok = ths_data.health()
    except Exception:
        pass
    return {"akshare": _AKSHARE_OK, "qwen_key": bool(cfg.QWEN_API_KEY),
            "ths_key": bool(cfg.THS_API_KEY), "ths_api": _ths_ok}
