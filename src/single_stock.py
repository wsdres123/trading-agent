"""个股模式：基于 single stock.md 的个股筛选与AI判断。

三个子板块：
1. 主线模式个股 — 主线核心容量 + 补涨前5
2. 短线模式个股 — 起变信号触发后的模式候选
3. 庄股 — 连续5日收盘>MA5, 自由流通市值>30亿, 5日涨幅>15%, 主板, 30日涨幅>40%

仅在A/B/C周期弹个股，D周期不弹。
核心：AI学习 single stock.md 后对候选个股给出买卖点判断。
"""
from __future__ import annotations

import json
import logging
import re

import numpy as np
import pandas as pd

from config import settings as cfg
from src import data, stock_filter as sf

logger = logging.getLogger("single_stock")

SPEC = cfg.DOCS_DIR / "single stock.md"

DISPLAY_COLS = ["代码", "名称", "最新价", "涨跌幅", "涨速", "自由流通市值(亿)",
                "成交额(亿)", "概念板块"]


def current_cycle() -> str:
    """获取当前中级周期（A/B/C/D），D周期不弹个股。"""
    try:
        from src import index_timing
        sig = index_timing.load_history_signals()
        if sig is not None and not sig.empty:
            return str(sig.iloc[-1].get("中级周期", "")).strip()
    except Exception:
        pass
    return ""


def _pre_close(code: str) -> float:
    """从腾讯行情取昨收价（qt.gtimg.cn f[4]）。"""
    try:
        session = data._sina_session()
        sym = data._sina_symbol(code)
        r = session.get(f"https://qt.gtimg.cn/q={sym}", timeout=6)
        r.encoding = "gbk"
        f = r.text.split("~")
        return float(f[4]) if len(f) > 4 and f[4] else 0.0
    except Exception:
        return 0.0


def _fmt_time(t: str) -> str:
    """将 'HHMM' 转为 'HH:MM'，其他格式原样返回。"""
    t = str(t).strip()
    if len(t) == 4 and t.isdigit():
        return f"{t[:2]}:{t[2:]}"
    return t


def get_intraday(code: str) -> pd.DataFrame:
    """获取个股当日分时数据（分钟价格 + 均价线 + 涨幅 + 分时量）。

    - 时间格式统一为 HH:MM
    - 仅保留交易时段 09:30–15:00，裁掉盘后数据
    - 午休时段 11:30–13:00 插入 NaN 行，保持 x 轴连续
    """
    session = data._sina_session()
    sym = data._sina_symbol(code)
    try:
        r = session.get("https://web.ifzq.gtimg.cn/appstock/app/minute/query",
                        params={"code": sym}, timeout=8)
        d = r.json().get("data", {}).get(sym, {}).get("data", {})
        raw = d.get("data", [])
        if not raw:
            return pd.DataFrame(columns=["时间", "价格", "均价", "涨幅", "成交量"])
        pre_close = _pre_close(code)
        rows = []
        for line in raw:
            parts = line.split()
            if len(parts) >= 2:
                row = {"时间": _fmt_time(parts[0]), "价格": float(parts[1])}
                if len(parts) >= 4:
                    row["累计量"] = float(parts[2])
                    row["累计额"] = float(parts[3])
                rows.append(row)
        df = pd.DataFrame(rows)
        if df.empty:
            return pd.DataFrame(columns=["时间", "价格", "均价", "涨幅", "成交量"])

        # 裁掉 15:00 之后的盘后数据
        df = df[df["时间"] <= "15:00"].copy()

        if "累计量" in df.columns:
            df["成交量"] = df["累计量"].diff().fillna(df["累计量"])
            # 午后第一分钟 diff 为负或零（上午末累计量延续），修正为 0
            df.loc[df["成交量"] < 0, "成交量"] = 0
            df["成交量"] = df["成交量"].astype(int)
        if "累计额" in df.columns and "累计量" in df.columns:
            mask = df["累计量"] > 0
            df.loc[mask, "均价"] = (df.loc[mask, "累计额"] /
                                    df.loc[mask, "累计量"] / 100).round(2)
        else:
            df["均价"] = df["价格"].expanding().mean().round(2)
        if pre_close:
            df["涨幅"] = ((df["价格"] / pre_close - 1) * 100).round(2)
            df["昨收"] = pre_close

        # 插入午休空行（11:31–12:59），让 x 轴视觉上有午休断开
        lunch_times = [f"{h:02d}:{m:02d}"
                       for h in range(11, 13)
                       for m in range(60)
                       if f"{h:02d}:{m:02d}" > "11:30" and f"{h:02d}:{m:02d}" < "13:00"]
        if lunch_times and "11:30" in df["时间"].values and "13:00" in df["时间"].values:
            gap = pd.DataFrame({"时间": lunch_times})
            df = pd.concat([df, gap], ignore_index=True).sort_values("时间").reset_index(drop=True)

        cols = ["时间", "价格", "均价", "涨幅"]
        if "成交量" in df.columns:
            cols.append("成交量")
        if "昨收" in df.columns:
            cols.append("昨收")
        return df[cols]
    except Exception as e:
        logger.warning("分时数据获取失败 %s: %s", code, e)
        return pd.DataFrame(columns=["时间", "价格", "均价", "涨幅", "成交量"])


def _is_main_board(code: str) -> bool:
    c = str(code).zfill(6)
    return c.startswith(("600", "601", "603", "605", "000", "001", "002", "003"))


def screen_zhuanggu(date_str: str | None = None) -> pd.DataFrame:
    """庄股筛选：连续5日收盘>MA5, 自由流通市值>30亿, 5日涨幅>15%, 主板, 30日涨幅>40%。

    date_str: 指定日期(YYYY-MM-DD)，None=今日。历史日期用缓存矩阵算。
    """
    from datetime import date as _date
    if not date_str or date_str == _date.today().strftime("%Y-%m-%d"):
        conditions = [
            {"field": "close_gt_ma", "ma": 5, "days": 5},
            {"field": "free_float_cap", "min_yi": 30},
            {"field": "return_ndays", "days": 5, "min_pct": 15},
            {"field": "board", "name": "主板"},
            {"field": "return_ndays", "days": 30, "min_pct": 40},
        ]
        res = sf.run_filter(conditions)
        return sf.finalize_results(res)
    return _zhuanggu_from_cache(date_str)


def _find_date_idx(dates: list[str], date_str: str) -> int | None:
    """在日期轴中找到 <= date_str 的最近日期索引。"""
    date_str = str(date_str)[:10]
    if date_str in dates:
        return dates.index(date_str)
    matching = [i for i, d in enumerate(dates) if d <= date_str]
    return matching[-1] if matching else None


def _zhuanggu_from_cache(date_str: str) -> pd.DataFrame:
    """历史日期庄股筛选：直接从缓存矩阵算，不依赖实时快照。"""
    from src import theme_mode as tm
    m = tm._matrices()
    if m is None:
        return pd.DataFrame(columns=DISPLAY_COLS)
    dates, C, A = m["dates"], m["C"], m["A"]
    codes, names = m["codes"], m["names"]
    idx = _find_date_idx(dates, date_str)
    if idx is None or idx < 30:
        return pd.DataFrame(columns=DISPLAY_COLS)

    with np.errstate(invalid="ignore"):
        ret_5d = (C[:, idx] / C[:, idx - 5] - 1) * 100
        ret_30d = (C[:, idx] / C[:, idx - 30] - 1) * 100
    # 连续5日收盘>MA5
    ok_ma5 = np.ones(len(codes), dtype=bool)
    for j in range(5):
        i = idx - j
        if i < 4:
            ok_ma5 = np.zeros(len(codes), dtype=bool)
            break
        ma5 = np.nanmean(C[:, i - 4:i + 1], axis=1)
        ok_ma5 &= np.where(np.isnan(ma5) | np.isnan(C[:, i]), False, C[:, i] > ma5)
    is_main = np.array([_is_main_board(c) for c in codes])
    cache = data.load_metrics_cache()
    mkt = (pd.to_numeric(cache["流通市值_亿"], errors="coerce").to_numpy()
           if cache is not None and "流通市值_亿" in cache.columns
           else np.full(len(codes), np.nan))
    mask = (ret_5d > 15) & (ret_30d > 40) & ok_ma5 & is_main & (mkt > 30)
    mask = np.where(np.isnan(ret_5d) | np.isnan(ret_30d), False, mask)
    rows = []
    for r in np.flatnonzero(mask):
        rows.append({
            "代码": codes[r], "名称": names[r],
            "最新价": round(float(C[r, idx]), 2),
            "涨跌幅": round((C[r, idx] / C[r, idx - 1] - 1) * 100, 2)
                     if idx > 0 and not np.isnan(C[r, idx - 1]) else None,
            "涨速": None,
            "自由流通市值(亿)": round(float(mkt[r]), 2) if not np.isnan(mkt[r]) else None,
            "成交额(亿)": round(float(A[r, idx]) / 1e8, 2) if not np.isnan(A[r, idx]) else None,
            "概念板块": "",
        })
    df = pd.DataFrame(rows, columns=DISPLAY_COLS)
    if not df.empty:
        try:
            extra = data.enrich_stocks(df["代码"].tolist())
            if not extra.empty and "概念板块" in extra.columns:
                df = df.merge(extra[["代码", "概念板块"]], on="代码", how="left",
                              suffixes=("", "_e"))
                df["概念板块"] = df["概念板块_e"].fillna(df["概念板块"])
                df = df.drop(columns=["概念板块_e"])
        except Exception:
            pass
        df = df.sort_values("涨跌幅", ascending=False, na_position="last")
    return df.reset_index(drop=True)


def get_mainline_stocks(start: str, end: str) -> dict:
    """主线模式个股：从主线识别结果提取核心容量 + 补涨前5。"""
    from src import theme_mode as tm
    result = tm.detect(start, end)
    if not result.get("has_mainline"):
        return {"has_mainline": False, "board": "", "core": [], "follow": [],
                "start": start, "end": end}
    ml = result["mainlines"][0]
    core_codes = [s["代码"] for s in ml.get("core", [])]
    follow = sorted(ml.get("follow", []),
                    key=lambda x: float(x.get("区间涨幅") or 0), reverse=True)[:5]
    follow_codes = [s["代码"] for s in follow]
    return {
        "has_mainline": True,
        "board": ml["board"],
        "start": ml["start"], "end": ml["end"],
        "core_codes": core_codes, "core_raw": ml.get("core", []),
        "follow_codes": follow_codes, "follow_raw": follow,
    }


def get_shortterm_stocks(date_str: str) -> dict:
    """短线模式个股：从起变信号检测结果提取候选。"""
    from src import short_term as st_mod
    result = st_mod.detect(date_str)
    ai_result = result.get("ai_result", {})
    modes = ai_result.get("modes", [])
    all_codes = []
    mode_summary = []
    for m in modes:
        cands = m.get("candidates", [])
        codes = [c.get("code", "") for c in cands if c.get("code")]
        all_codes.extend(codes)
        mode_summary.append({
            "mode": m.get("mode", ""), "buy_point": m.get("buy_point", ""),
            "sell_point": m.get("sell_point", ""), "position": m.get("position", ""),
            "candidates": cands,
        })
    return {
        "is_signal": ai_result.get("is_signal", False),
        "signal_reason": ai_result.get("signal_reason", ""),
        "modes": mode_summary,
        "all_codes": list(set(all_codes)),
        "ladder": result.get("ladder_df"),
    }


def enrich_to_display(codes: list[str], date_str: str | None = None) -> pd.DataFrame:
    """代码列表 → 展示列（涨幅/涨速/自由流通市值/成交额/板块）。

    date_str: 历史日期时从缓存矩阵取该日数据，None=今日实时。
    """
    from datetime import date as _date
    codes = [str(c).zfill(6) for c in codes if c]
    if not codes:
        return pd.DataFrame(columns=DISPLAY_COLS)
    if date_str and date_str != _date.today().strftime("%Y-%m-%d"):
        return _enrich_from_cache(codes, date_str)
    spot = data.get_stock_spot()
    if spot.empty:
        return pd.DataFrame(columns=DISPLAY_COLS)
    spot = spot.copy()
    spot["代码"] = spot["代码"].astype(str).str.zfill(6)
    spot = spot[spot["代码"].isin(codes)].drop_duplicates("代码")

    try:
        extra = data.enrich_stocks(codes)
        if not extra.empty:
            spot = spot.merge(extra, on="代码", how="left")
    except Exception as e:
        logger.warning("增强失败: %s", e)

    if "成交额" in spot.columns:
        spot["成交额(亿)"] = (pd.to_numeric(spot["成交额"], errors="coerce") / 1e8).round(2)
    if "流通市值" in spot.columns:
        spot["自由流通市值(亿)"] = (pd.to_numeric(spot["流通市值"], errors="coerce") / 1e8).round(2)
    if "自由流通比例" in spot.columns and "流通市值" in spot.columns:
        ratio = pd.to_numeric(spot["自由流通比例"], errors="coerce")
        mkt = pd.to_numeric(spot["流通市值"], errors="coerce")
        free = (mkt * ratio / 1e8).round(2)
        spot["自由流通市值(亿)"] = free.fillna(spot.get("自由流通市值(亿)"))

    for c in DISPLAY_COLS:
        if c not in spot.columns:
            spot[c] = None
    spot = spot.sort_values("涨跌幅", ascending=False, na_position="last")
    return spot[DISPLAY_COLS].reset_index(drop=True)


def _enrich_from_cache(codes: list[str], date_str: str) -> pd.DataFrame:
    """从缓存矩阵取指定日期的展示数据（涨幅/成交额/市值/板块）。"""
    from src import theme_mode as tm
    m = tm._matrices()
    if m is None:
        return pd.DataFrame(columns=DISPLAY_COLS)
    dates, C, A = m["dates"], m["C"], m["A"]
    all_codes, all_names = m["codes"], m["names"]
    idx = _find_date_idx(dates, date_str)
    if idx is None:
        return pd.DataFrame(columns=DISPLAY_COLS)
    cache = data.load_metrics_cache()
    mkt_map = {}
    if cache is not None and "流通市值_亿" in cache.columns:
        mkt_map = dict(zip(cache["代码"].astype(str).str.zfill(6),
                           pd.to_numeric(cache["流通市值_亿"], errors="coerce")))
    code_set = set(codes)
    rows = []
    for r, c in enumerate(all_codes):
        if c not in code_set:
            continue
        close = float(C[r, idx]) if not np.isnan(C[r, idx]) else None
        pct = round((C[r, idx] / C[r, idx - 1] - 1) * 100, 2) \
            if idx > 0 and not np.isnan(C[r, idx - 1]) and close else None
        amt = round(float(A[r, idx]) / 1e8, 2) if not np.isnan(A[r, idx]) else None
        mkt = mkt_map.get(c)
        rows.append({
            "代码": c, "名称": all_names[r], "最新价": close,
            "涨跌幅": pct, "涨速": None,
            "自由流通市值(亿)": round(float(mkt), 2) if mkt and not np.isnan(mkt) else None,
            "成交额(亿)": amt, "概念板块": "",
        })
    df = pd.DataFrame(rows, columns=DISPLAY_COLS)
    if not df.empty:
        try:
            extra = data.enrich_stocks(df["代码"].tolist())
            if not extra.empty and "概念板块" in extra.columns:
                df = df.merge(extra[["代码", "概念板块"]], on="代码", how="left",
                              suffixes=("", "_e"))
                df["概念板块"] = df["概念板块_e"].fillna(df["概念板块"])
                df = df.drop(columns=["概念板块_e"])
        except Exception:
            pass
        df = df.sort_values("涨跌幅", ascending=False, na_position="last")
    return df.reset_index(drop=True)


# ── AI 个股判断（精简prompt + qwen-turbo，目标3s内）───────────────────────
AI_PROMPT = """A股个股判断。周期{cycle}。对以下候选给出买卖判断，只输出JSON：
{{"zhuanggu":[{{"code":"代码","name":"名称","verdict":"可做/观察/回避","buy":"买点","sell":"卖点","risk":"风险","reason":"理由20字"}}],"mainline":[{{"code":"","name":"","type":"核心/补涨","verdict":"","buy":"","sell":"","reason":""}}],"shortterm":[{{"code":"","name":"","mode":"","verdict":"","buy":"","sell":"","reason":""}}],"summary":"总结100字"}}
规则：A/B/C弹个股D不弹。主线核心是阵眼补涨跟随，买卖点后续补充先给趋势判断。短线起变信号后才介入。庄股趋势不破盘中低吸，收盘破5日均线卖，优先独立趋势票。无候选返回空数组。
庄股({n_zg}只):{zhuanggu}
主线(核心{n_core}+补涨{n_follow},信号{signal}):{mainline}
短线({n_st}只):{shortterm}
"""


def _stocks_to_text(df: pd.DataFrame, max_n: int = 20) -> str:
    if df is None or df.empty:
        return "无"
    return "、".join(
        f"{r.get('名称','')}({r.get('代码','')}){r.get('涨跌幅','')}%"
        for _, r in df.head(max_n).iterrows())


def _mainline_to_text(ml_data: dict) -> str:
    if not ml_data.get("has_mainline"):
        return "无主线"
    parts = [f"主线[{ml_data['board']}]{ml_data['start']}~{ml_data['end']}"]
    for s in ml_data.get("core_raw", [])[:5]:
        parts.append(f"核心:{s['名称']}({s['代码']})成交{s.get('最大成交额_亿','')}亿区间{s.get('区间涨幅','')}%")
    for s in ml_data.get("follow_raw", [])[:5]:
        parts.append(f"补涨:{s['名称']}({s['代码']})区间{s.get('区间涨幅','')}%")
    return "、".join(parts)


def _shortterm_to_text(st_data: dict) -> str:
    if not st_data.get("is_signal"):
        return f"无信号({st_data.get('signal_reason','')})"
    parts = [f"信号:{st_data.get('signal_reason','')}"]
    for m in st_data.get("modes", []):
        cands = "、".join(f"{c.get('name','')}" for c in m.get("candidates", [])[:3])
        parts.append(f"[{m.get('mode','')}]{cands}")
    return "、".join(parts)


def ai_judge(cycle: str, zhuanggu_df: pd.DataFrame,
             ml_data: dict, st_data: dict) -> dict:
    """AI个股判断（qwen-turbo + 精简prompt，目标3s内完成）。"""
    zg_text = _stocks_to_text(zhuanggu_df)
    ml_text = _mainline_to_text(ml_data)
    st_text = _shortterm_to_text(st_data)
    signal = "已现" if st_data.get("is_signal") else "未现"

    def _fallback() -> dict:
        return {
            "zhuanggu": [], "mainline": [], "shortterm": [],
            "summary": f"AI判读失败。周期{cycle}，庄股{len(zhuanggu_df) if zhuanggu_df is not None else 0}只，"
                       f"主线{'有' if ml_data.get('has_mainline') else '无'}，信号{signal}。",
        }

    if not cfg.QWEN_API_KEY:
        return _fallback()

    from openai import OpenAI
    client = OpenAI(api_key=cfg.QWEN_API_KEY, base_url=cfg.QWEN_BASE_URL)
    prompt = AI_PROMPT.format(
        cycle=cycle or "未知",
        n_zg=len(zhuanggu_df) if zhuanggu_df is not None else 0,
        n_core=len(ml_data.get("core_raw", [])),
        n_follow=len(ml_data.get("follow_raw", [])),
        n_st=len(st_data.get("all_codes", [])),
        signal=signal,
        zhuanggu=zg_text, mainline=ml_text, shortterm=st_text)

    for _model in ("qwen-turbo", "qwen-plus", cfg.QWEN_CHAT_MODEL):
        try:
            resp = client.chat.completions.create(
                model=_model, messages=[{"role": "user", "content": prompt}],
                temperature=0.1, max_tokens=500)
            raw = resp.choices[0].message.content.strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            return json.loads(raw)
        except Exception as e:
            logger.warning("AI (%s) 个股判读失败: %s", _model, e)
            continue
    return _fallback()


# ── 入口：一键运行三个子板块 + AI判断 ────────────────────────────────────
def run(date_str: str | None = None) -> dict:
    """个股模式主入口。指定日期筛选三板块候选 + AI判断。

    date_str: YYYY-MM-DD，None=今日。连板天梯仅支持30天内。
    """
    from datetime import date, timedelta
    if not date_str:
        date_str = date.today().strftime("%Y-%m-%d")

    cyc = current_cycle()
    zhuanggu = screen_zhuanggu(date_str)

    # 主线：30天窗口 ending at date_str
    _end = date_str
    from datetime import datetime as _dt
    _start = (_dt.strptime(date_str, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
    ml_data = get_mainline_stocks(_start, _end)

    st_data = get_shortterm_stocks(date_str)
    ai_result = ai_judge(cyc, zhuanggu, ml_data, st_data)

    # 主线/短线个股增强为展示列（历史日期从缓存取）
    ml_display = pd.DataFrame()
    if ml_data.get("has_mainline"):
        codes = ml_data.get("core_codes", []) + ml_data.get("follow_codes", [])
        ml_display = enrich_to_display(codes, date_str)
    st_display = pd.DataFrame()
    if st_data.get("all_codes"):
        st_display = enrich_to_display(st_data["all_codes"], date_str)

    return {
        "date": date_str,
        "cycle": cyc,
        "zhuanggu": zhuanggu,
        "mainline_display": ml_display,
        "mainline_data": ml_data,
        "shortterm_display": st_display,
        "shortterm_data": st_data,
        "ai_result": ai_result,
    }
