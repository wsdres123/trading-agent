"""个股筛选 + 分组管理。

- run_filter(conditions): 按结构化条件筛选全 A，返回结果 DataFrame。
  策略：先用实时快照做粗筛（流通市值、板块成分），再对候选逐个拉历史K线
  精算 MA/涨幅/新高，保证准确度同时控制请求数。
- 分组管理：保存/删除/更新，持久化到 .data/groups.json。
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd
import numpy as np

from config import settings as cfg
from src import data

logger = logging.getLogger("stock_filter")

# 并行拉取历史K线的线程数（akshare 为 HTTP IO，并发可大幅提速）
MAX_WORKERS = 12

RESULT_COLS = ["代码", "名称", "最新价", "涨跌幅", "成交额", "流通市值_亿",
               "close", "ma5", "ret_5d", "ret_30d", "is_100d_new_high"]


def _has_hist_only_conds(conds: list) -> bool:
    """是否含需历史K线才能验证的条件。"""
    hist_fields = ("close_gt_ma", "close_lt_ma", "return_ndays", "new_high", "new_low",
                   "consecutive_up", "consecutive_down", "ma_bullish", "ma_bearish",
                   "volume_surge", "drawdown_from_high")
    return any(c["field"] in hist_fields for c in conds)


def _board_of(code: str) -> str:
    """按代码前缀判断市场板块。"""
    code = str(code).zfill(6)
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("300", "301", "302")):
        return "创业板"
    if code.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
        return "主板"
    if code[0] in "48" or code.startswith("92"):
        return "北交所"
    return "其他"


def _board_hit(code: str, cond: dict) -> bool:
    hit = _board_of(code) == str(cond.get("name", ""))
    return (not hit) if cond.get("exclude") else hit


def _price_matrix(df: pd.DataFrame, col: str) -> "np.ndarray | None":
    """list 列 → (n, L) 矩阵，前侧补 NaN 对齐（数据不足自动不命中）。"""
    if col not in df.columns:
        return None
    arrs = [a if a is not None and len(a) else [] for a in df[col].tolist()]
    L = max((len(a) for a in arrs), default=0)
    if L == 0:
        return None
    M = np.full((len(arrs), L), np.nan)
    for i, a in enumerate(arrs):
        M[i, L - len(a):] = a
    return M


def _num_col(df: pd.DataFrame, col: str) -> pd.Series:
    """安全取数值列，缺失时返回全 NaN（比较结果自然不命中）。"""
    if col in df.columns:
        return pd.to_numeric(df[col], errors="coerce")
    return pd.Series(np.nan, index=df.index)


def _filter_from_cache(cache: pd.DataFrame, conditions: list) -> pd.DataFrame:
    """基于本地指标缓存做向量化筛选，零联网、毫秒级。

    缓存内含 110 日 OHLCV 序列，任意 N 日涨幅/均线/新高/新低/量比均可现算。
    """
    df = cache.reset_index(drop=True)
    df["代码"] = df["代码"].astype(str).str.zfill(6)
    # 缓存最长可能是数小时前构建的，最新价/涨跌幅等实时字段先用快照覆盖
    try:
        spot = data.get_stock_spot()
        if not spot.empty:
            s = spot.copy()
            s["代码"] = s["代码"].astype(str).str.zfill(6)
            s = s.drop_duplicates("代码").set_index("代码")
            for col in ("最新价", "涨跌幅", "成交额", "流通市值_亿",
                        "换手率", "总市值_亿", "成交量"):
                if col in s.columns:
                    live = df["代码"].map(pd.to_numeric(s[col], errors="coerce"))
                    df[col] = live.fillna(df[col]) if col in df.columns else live
    except Exception as e:
        logger.warning("实时快照合并失败，沿用缓存值：%s", e)
    C = H = L = V = None  # 收盘/最高/最低/成交量矩阵，懒加载
    mask = pd.Series([True] * len(df), index=df.index)
    for c in conditions:
        f = c["field"]
        if f == "free_float_cap":
            v = _num_col(df, "流通市值_亿")
            if c.get("min_yi") is not None:
                mask &= v >= float(c["min_yi"])
            if c.get("max_yi") is not None:
                mask &= v <= float(c["max_yi"])
        elif f == "total_cap":
            v = _num_col(df, "总市值_亿")
            if c.get("min_yi") is not None:
                mask &= v >= float(c["min_yi"])
            if c.get("max_yi") is not None:
                mask &= v <= float(c["max_yi"])
        elif f == "amount":
            v = _num_col(df, "成交额") / 1e8
            if c.get("min_yi") is not None:
                mask &= v >= float(c["min_yi"])
            if c.get("max_yi") is not None:
                mask &= v <= float(c["max_yi"])
        elif f == "turnover_rate":
            v = _num_col(df, "换手率")
            if c.get("min_pct") is not None:
                mask &= v >= float(c["min_pct"])
            if c.get("max_pct") is not None:
                mask &= v <= float(c["max_pct"])
        elif f == "price":
            v = _num_col(df, "最新价")
            if c.get("min") is not None:
                mask &= v >= float(c["min"])
            if c.get("max") is not None:
                mask &= v <= float(c["max"])
        elif f == "board":
            mask &= df["代码"].map(lambda x: _board_hit(x, c))
        elif f == "sector":
            codes = data.sector_to_codes(c["name"])
            if not codes:
                return data._empty(RESULT_COLS)
            mask &= df["代码"].isin(codes)
        elif f in ("close_gt_ma", "close_lt_ma"):
            gt = f == "close_gt_ma"
            n = int(c["ma"])
            m_days = int(c.get("days", 1) or 1)
            if m_days <= 1:
                ma_col = {5: "ma5", 10: "ma10", 20: "ma20"}.get(n)
                if ma_col and ma_col in df.columns:
                    close_v = df["close"].astype(float)
                    ma_v = df[ma_col].astype(float)
                    mask &= (close_v > ma_v) if gt else (close_v < ma_v)
                    continue
            if C is None:
                C = _price_matrix(df, "closes")
            if C is None or C.shape[1] < n + m_days - 1:
                return data._empty(RESULT_COLS)
            ok = np.ones(len(df), dtype=bool)
            for j in range(m_days):  # j=0 为最新一日，逐日回看
                end = C.shape[1] - j
                ma = C[:, end - n:end].mean(axis=1)  # 含 NaN 则为 NaN → 不命中
                day_ok = (C[:, end - 1] > ma) if gt else (C[:, end - 1] < ma)
                ok &= np.where(np.isnan(ma) | np.isnan(C[:, end - 1]), False, day_ok)
            mask &= pd.Series(ok, index=df.index)
        elif f == "return_ndays":
            days = int(c["days"])
            mn, mx = c.get("min_pct"), c.get("max_pct")
            if days == 1:  # 今日涨幅：用实时涨跌幅，而非缓存K线
                r = _num_col(df, "涨跌幅")
            else:
                col = {5: "ret_5d", 30: "ret_30d"}.get(days)
                if col and col in df.columns:
                    r = df[col].astype(float)
                else:
                    if C is None:
                        C = _price_matrix(df, "closes")
                    if C is None or C.shape[1] < days + 1:
                        return data._empty(RESULT_COLS)
                    r = pd.Series((C[:, -1] / C[:, -days - 1] - 1) * 100, index=df.index)
            if mn is not None:
                mask &= r >= float(mn)
            if mx is not None:
                mask &= r <= float(mx)
        elif f in ("new_high", "new_low"):
            days = int(c["days"])
            if f == "new_high" and days == 100 and "is_100d_new_high" in df.columns:
                mask &= df["is_100d_new_high"].astype(bool)
                continue
            if C is None:
                C = _price_matrix(df, "closes")
            M = None
            if f == "new_high":
                if H is None:
                    H = _price_matrix(df, "highs")
                M = H
            else:
                if L is None:
                    L = _price_matrix(df, "lows")
                M = L
            if M is None or C is None or M.shape[1] < days:
                return data._empty(RESULT_COLS)
            ext = np.nanmax(M[:, -days:], axis=1) if f == "new_high" \
                else np.nanmin(M[:, -days:], axis=1)
            hit = C[:, -1] >= ext if f == "new_high" else C[:, -1] <= ext
            mask &= pd.Series(hit, index=df.index).fillna(False)
        elif f in ("consecutive_up", "consecutive_down"):
            d = int(c["days"])
            if C is None:
                C = _price_matrix(df, "closes")
            if C is None or C.shape[1] < d + 1:
                return data._empty(RESULT_COLS)
            seg = C[:, -d - 1:]
            diffs = np.diff(seg, axis=1)
            ok = np.all(diffs > 0, axis=1) if f == "consecutive_up" \
                else np.all(diffs < 0, axis=1)
            ok &= ~np.isnan(seg).any(axis=1)
            mask &= pd.Series(ok, index=df.index)
        elif f in ("ma_bullish", "ma_bearish"):
            ma5, ma10, ma20 = (_num_col(df, "ma5"), _num_col(df, "ma10"),
                               _num_col(df, "ma20"))
            mask &= ((ma5 > ma10) & (ma10 > ma20)) if f == "ma_bullish" \
                else ((ma5 < ma10) & (ma10 < ma20))
        elif f == "volume_surge":
            ratio = float(c.get("ratio", 2))
            if V is None:
                V = _price_matrix(df, "volumes")
            if V is None or V.shape[1] < 6:
                return data._empty(RESULT_COLS)
            avg5 = np.nanmean(V[:, -6:-1], axis=1)  # 前5日均量（不含最新一根）
            live_v = _num_col(df, "成交量").to_numpy(dtype=float)
            vol = np.where(np.isnan(live_v), V[:, -1], live_v)
            ok = (avg5 > 0) & (vol >= avg5 * ratio)
            mask &= pd.Series(np.where(np.isnan(vol) | np.isnan(avg5), False, ok),
                              index=df.index)
        elif f == "drawdown_from_high":
            days = int(c.get("days", 100) or 100)
            if days == 100 and "high_100d" in df.columns:
                hi = pd.to_numeric(df["high_100d"], errors="coerce").to_numpy(dtype=float)
            else:
                if H is None:
                    H = _price_matrix(df, "highs")
                if H is None or H.shape[1] < days:
                    return data._empty(RESULT_COLS)
                hi = np.nanmax(H[:, -days:], axis=1)
            close_v = pd.to_numeric(df["close"], errors="coerce").to_numpy(dtype=float)
            dd = (1 - close_v / hi) * 100
            ok = ~np.isnan(dd)
            if c.get("max_pct") is not None:
                ok &= dd <= float(c["max_pct"])
            if c.get("min_pct") is not None:
                ok &= dd >= float(c["min_pct"])
            mask &= pd.Series(ok, index=df.index)
    res = df[mask]
    keep = [c for c in RESULT_COLS if c in res.columns]
    res = res[keep].sort_values("涨跌幅", ascending=False, na_position="last")
    return res.reset_index(drop=True)


# 展示列顺序（用户指定）
DISPLAY_COLS = ["代码", "名称", "最新价", "涨跌幅", "涨速", "竞价量", "涨停封单额",
                "自由流通市值(亿)", "成交额(亿)", "概念板块"]
ENRICH_MAX = 300  # 命中太多时不逐股增强，避免拖慢


def finalize_results(res: pd.DataFrame) -> pd.DataFrame:
    """把筛选命中结果整形为展示列：补涨速/竞价量/封单额/概念，成交额转亿。"""
    if res is None or res.empty:
        return data._empty(DISPLAY_COLS)
    df = res.copy()
    df["代码"] = df["代码"].astype(str).str.zfill(6)
    if "成交额" in df.columns:
        df["成交额(亿)"] = (pd.to_numeric(df["成交额"], errors="coerce") / 1e8).round(2)
    if "流通市值_亿" in df.columns:
        df["自由流通市值(亿)"] = pd.to_numeric(df["流通市值_亿"], errors="coerce").round(2)
    if len(df) <= ENRICH_MAX:
        try:
            extra = data.enrich_stocks(df["代码"].tolist())
            if not extra.empty:
                df = df.merge(extra, on="代码", how="left")
                if "自由流通比例" in df.columns and "流通市值_亿" in df.columns:
                    ratio = pd.to_numeric(df["自由流通比例"], errors="coerce")
                    free = (pd.to_numeric(df["流通市值_亿"], errors="coerce") * ratio).round(2)
                    df["自由流通市值(亿)"] = free.fillna(df["自由流通市值(亿)"])
        except Exception as e:
            logger.warning("结果增强失败：%s", e)
    for c in DISPLAY_COLS:
        if c not in df.columns:
            df[c] = None
    return df[DISPLAY_COLS].reset_index(drop=True)


def run_filter(conditions: list, progress_cb=None, use_cache: bool = True) -> pd.DataFrame:
    """执行筛选，返回结果 DataFrame（按涨跌幅降序）。

    优先用本地指标缓存（亚秒级）；缓存缺失时回退到实时并行拉取（较慢）。
    """
    if not conditions:
        return data._empty(RESULT_COLS)

    if use_cache:
        cache = data.load_metrics_cache()
        if cache is None:
            cache = data.load_metrics_cache(allow_stale=True)  # 旧缓存也远快于实时逐股
        if cache is not None:
            return _filter_from_cache(cache, conditions)

    spot = data.get_stock_spot()
    if spot.empty:
        return data._empty(RESULT_COLS)

    df = spot.copy()
    # ── 粗筛：市值、成交额、换手率、股价、市场板块、板块 ──
    for c in conditions:
        if c["field"] == "free_float_cap":
            if "流通市值_亿" not in df.columns and "流通市值" in df.columns:
                df["流通市值_亿"] = (df["流通市值"].astype(float) / 1e8).round(2)
            v = _num_col(df, "流通市值_亿")
            if c.get("min_yi") is not None:
                df = df[v >= float(c["min_yi"])]
            if c.get("max_yi") is not None:
                df = df[_num_col(df, "流通市值_亿") <= float(c["max_yi"])]
        elif c["field"] == "total_cap":
            if "总市值_亿" not in df.columns and "总市值" in df.columns:
                df["总市值_亿"] = (df["总市值"].astype(float) / 1e8).round(2)
            if c.get("min_yi") is not None:
                df = df[_num_col(df, "总市值_亿") >= float(c["min_yi"])]
            if c.get("max_yi") is not None:
                df = df[_num_col(df, "总市值_亿") <= float(c["max_yi"])]
        elif c["field"] == "amount":
            if c.get("min_yi") is not None:
                df = df[_num_col(df, "成交额") / 1e8 >= float(c["min_yi"])]
            if c.get("max_yi") is not None:
                df = df[_num_col(df, "成交额") / 1e8 <= float(c["max_yi"])]
        elif c["field"] == "turnover_rate":
            if c.get("min_pct") is not None:
                df = df[_num_col(df, "换手率") >= float(c["min_pct"])]
            if c.get("max_pct") is not None:
                df = df[_num_col(df, "换手率") <= float(c["max_pct"])]
        elif c["field"] == "price":
            if c.get("min") is not None:
                df = df[_num_col(df, "最新价") >= float(c["min"])]
            if c.get("max") is not None:
                df = df[_num_col(df, "最新价") <= float(c["max"])]
        elif c["field"] == "board":
            df = df[df["代码"].astype(str).str.zfill(6).map(lambda x: _board_hit(x, c))]
        elif c["field"] == "return_ndays" and int(c["days"]) == 1:
            if c.get("min_pct") is not None:
                df = df[pd.to_numeric(df["涨跌幅"], errors="coerce") >= float(c["min_pct"])]
            if c.get("max_pct") is not None:
                df = df[pd.to_numeric(df["涨跌幅"], errors="coerce") <= float(c["max_pct"])]
        elif c["field"] == "sector":
            codes = data.sector_to_codes(c["name"])
            if not codes:  # 板块未命中：尝试模糊
                codes = set()
                for kind in ("industry", "concept"):
                    b = data._board_list(kind)
                    if not b.empty and "板块名称" in b.columns:
                        hit = b[b["板块名称"].str.contains(c["name"], na=False, regex=False)]
                        for bn in hit["板块名称"].tolist():
                            cons = data.get_board_constituents(bn, kind=kind)
                            if not cons.empty and "代码" in cons.columns:
                                codes.update(cons["代码"].astype(str).str.zfill(6).tolist())
            if codes:
                df = df[df["代码"].astype(str).str.zfill(6).isin(codes)]
            else:
                # 板块无法定位，直接返回空，避免误筛全市场
                return data._empty(RESULT_COLS)

    if df.empty:
        return data._empty(RESULT_COLS)

    need_hist = _has_hist_only_conds(conditions)
    if not need_hist:
        return _format(df)

    # ── 精筛：并行拉历史K线校验 ──
    df = df.copy()
    df["_code6"] = df["代码"].astype(str).str.zfill(6)
    spot_by_code = {r["_code6"]: r for _, r in df.iterrows()}
    candidates = list(spot_by_code.keys())
    total = len(candidates)

    rows = []
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(_eval_one, code, conditions, spot_by_code[code]): code
                for code in candidates}
        for fut in as_completed(futs):
            done += 1
            if progress_cb:
                progress_cb(done, total)
            try:
                row = fut.result()
            except Exception as e:
                logger.debug("eval %s 失败：%s", futs[fut], e)
                row = None
            if row:
                rows.append(row)
    if not rows:
        return data._empty(RESULT_COLS)
    res = pd.DataFrame(rows).sort_values("涨跌幅", ascending=False, na_position="last")
    return res.reset_index(drop=True)


def _eval_one(code: str, conditions: list, spot_row) -> dict | None:
    """单只个股：拉历史指标 → 校验条件 → 命中则拼装结果行。"""
    m = data.get_stock_metrics(code)
    if not m or not _match_hist(m, conditions):
        return None
    return {
        "代码": code,
        "名称": spot_row.get("名称", ""),
        "最新价": spot_row.get("最新价", m["close"]),
        "涨跌幅": spot_row.get("涨跌幅", None),
        "成交额": spot_row.get("成交额", None),
        "流通市值_亿": spot_row.get("流通市值_亿", None),
        "close": m["close"],
        "ma5": round(m["ma5"], 2),
        "ret_5d": round(m["ret_5d"], 2) if m["ret_5d"] is not None else None,
        "ret_30d": round(m["ret_30d"], 2) if m["ret_30d"] is not None else None,
        "is_100d_new_high": m["is_100d_new_high"],
    }


def _match_hist(m: dict, conds: list) -> bool:
    """校验个股指标是否满足所有历史类条件。"""
    for c in conds:
        f = c["field"]
        if f in ("close_gt_ma", "close_lt_ma"):
            gt = f == "close_gt_ma"
            n = int(c["ma"])
            m_days = int(c.get("days", 1) or 1)
            if m_days <= 1:
                ma = {5: m["ma5"], 10: m["ma10"], 20: m["ma20"]}.get(n)
                if ma is None:
                    closes = m.get("closes") or []
                    if len(closes) < n:
                        return False
                    ma = float(np.mean(closes[-n:]))
                if (m["close"] <= ma) if gt else (m["close"] >= ma):
                    return False
            else:
                closes = m.get("closes") or []
                if len(closes) < n + m_days - 1:
                    return False
                arr = np.asarray(closes, dtype=float)
                for j in range(m_days):
                    end = len(arr) - j
                    ma = arr[end - n:end].mean()
                    if (arr[end - 1] <= ma) if gt else (arr[end - 1] >= ma):
                        return False
        elif f == "return_ndays":
            days = int(c["days"])
            if days == 1:
                continue  # 今日涨幅已在粗筛用实时涨跌幅过滤
            if days == 5:
                val = m["ret_5d"]
            elif days == 30:
                val = m["ret_30d"]
            else:
                val = _calc_nday_return(m["code"], days)
            if val is None:
                return False
            if c.get("min_pct") is not None and val < float(c["min_pct"]):
                return False
            if c.get("max_pct") is not None and val > float(c["max_pct"]):
                return False
        elif f == "new_high":
            days = int(c["days"])
            if days == 100:
                if not m["is_100d_new_high"]:
                    return False
            else:
                highs = m.get("highs") or []
                if len(highs) < days or m["close"] < max(highs[-days:]):
                    return False
        elif f == "new_low":
            # 指标缺最低价序列，用收盘序列近似
            days = int(c["days"])
            closes = m.get("closes") or []
            if len(closes) < days or m["close"] > min(closes[-days:]):
                return False
        elif f in ("consecutive_up", "consecutive_down"):
            d = int(c["days"])
            closes = m.get("closes") or []
            if len(closes) < d + 1:
                return False
            seg = np.asarray(closes[-d - 1:], dtype=float)
            diffs = np.diff(seg)
            if f == "consecutive_up" and not np.all(diffs > 0):
                return False
            if f == "consecutive_down" and not np.all(diffs < 0):
                return False
        elif f in ("ma_bullish", "ma_bearish"):
            ma5, ma10, ma20 = m.get("ma5"), m.get("ma10"), m.get("ma20")
            if None in (ma5, ma10, ma20):
                return False
            if f == "ma_bullish" and not (ma5 > ma10 > ma20):
                return False
            if f == "ma_bearish" and not (ma5 < ma10 < ma20):
                return False
        elif f == "volume_surge":
            return False  # 实时回退路径无成交量序列，无法验证
        elif f == "drawdown_from_high":
            days = int(c.get("days", 100) or 100)
            if days == 100:
                hi = m.get("high_100d")
            else:
                highs = m.get("highs") or []
                hi = max(highs[-days:]) if len(highs) >= days else None
            if not hi:
                return False
            dd = (1 - m["close"] / hi) * 100
            if c.get("max_pct") is not None and dd > float(c["max_pct"]):
                return False
            if c.get("min_pct") is not None and dd < float(c["min_pct"]):
                return False
    return True


def _calc_nday_return(code: str, days: int) -> float | None:
    """通用 N 日涨幅（非 5/30 时回退逐日计算）。"""
    df = data.get_stock_hist(code, days=days + 10)
    if len(df) < days + 1:
        return None
    return round((df["收盘"].iloc[-1] / df["收盘"].iloc[-days - 1] - 1) * 100, 2)


def _format(df: pd.DataFrame) -> pd.DataFrame:
    keep = [c for c in RESULT_COLS if c in df.columns]
    out = df[keep].copy()
    if "涨跌幅" in out.columns:
        out = out.sort_values("涨跌幅", ascending=False, na_position="last")
    return out.reset_index(drop=True)


# ── 分组管理 ──────────────────────────────────────────────────────────────
def _load_groups() -> dict:
    if not cfg.GROUPS_FILE.exists():
        return {"groups": []}
    try:
        return json.loads(cfg.GROUPS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"groups": []}


def _save_groups(data_obj: dict) -> None:
    cfg.GROUPS_FILE.write_text(json.dumps(data_obj, ensure_ascii=False, indent=2), encoding="utf-8")


def list_groups() -> list:
    return _load_groups().get("groups", [])


def save_group(name: str, conditions: list, stocks: list) -> dict:
    """新建或覆盖同名分组。stocks 为结果行 dict 列表。"""
    obj = _load_groups()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    g = {"name": name, "conditions": conditions, "stocks": stocks,
         "created_at": now, "updated_at": now}
    obj["groups"] = [g if x["name"] == name else x for x in obj["groups"]]
    if not any(x["name"] == name for x in obj["groups"]):
        obj["groups"].append(g)
    _save_groups(obj)
    return g


def delete_group(name: str) -> None:
    obj = _load_groups()
    obj["groups"] = [x for x in obj["groups"] if x["name"] != name]
    _save_groups(obj)


def update_group(name: str, progress_cb=None) -> dict | None:
    """按分组的原条件重新筛选并刷新个股。"""
    obj = _load_groups()
    g = next((x for x in obj["groups"] if x["name"] == name), None)
    if not g:
        return None
    res = run_filter(g["conditions"], progress_cb=progress_cb)
    res = finalize_results(res)
    g["stocks"] = res.to_dict("records")
    g["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    _save_groups(obj)
    return g
