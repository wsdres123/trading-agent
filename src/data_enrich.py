"""筛选结果增强字段：涨速/竞价量/涨停封单额/自由流通比例/概念板块。"""
from __future__ import annotations

import time
import logging
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from config import settings as cfg
from src.data_base import _empty
from src.data_quotes import _sina_symbol

logger = logging.getLogger("data")

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

