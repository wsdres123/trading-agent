"""情绪节点：大模型学习 emotional node.md + 屠龙表-竞价表.csv 历史节点，判断当日情绪节点。

- load_auction_table()：竞价表历史节点（日期/指数/节点/亏效/一字/断板）
- market_stats()：当日盘面统计（大面数/跌停/涨停/高度个股等，供模型判断）
- ai_judge()：结合规范+历史+当日统计输出今日情绪节点（每日缓存，初步判断版）
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime

import numpy as np
import pandas as pd

from config import settings as cfg
from src import data

logger = logging.getLogger("emotion_node")

AUCTION_CSV = cfg.KNOWLEDGE_DIR / "屠龙表 - 竞价表.csv"
EMO_SPEC = cfg.DOCS_DIR / "emotional node.md"
EMO_STORE = cfg.DATA_DIR / "emotion_ai.json"
JUDGE_TIME = "14:57"

# 规范中的节点全集（模型输出约束，与竞价表CSV命名对齐）
NODE_NAMES = ["混沌", "混沌分歧",
              "主升--确认", "主升--加速", "主升--高潮", "主升--分歧",
              "主升--延续", "日内分歧转一致", "日内分歧未转一致",
              "退潮", "退潮加速", "退潮转衰竭", "退潮中继",
              "冰点", "冰点转折",
              "修复--弱", "修复--中等", "修复--强",
              "修复--加速", "修复--高潮", "修复延续",
              "加速", "加速转衰竭",
              "龙头确认", "短线情绪确认"]


def load_auction_table() -> pd.DataFrame:
    """竞价表历史：日期/指数/节点/小票亏效/大票亏效/一字/断板（仅保留有节点标注的行）。"""
    if not AUCTION_CSV.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(AUCTION_CSV, encoding="utf-8-sig")
    except Exception as e:
        logger.warning("竞价表读取失败：%s", e)
        return pd.DataFrame()
    df.columns = [str(c).replace("\n", "").strip() for c in df.columns]
    if "日期" not in df.columns:
        df = df.rename(columns={df.columns[0]: "日期"})
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce").dt.strftime("%Y-%m-%d")
    df = df.dropna(subset=["日期"])
    if "节点" in df.columns:
        df = df[df["节点"].notna() & df["节点"].astype(str).str.strip().ne("")]
    keep = [c for c in ["日期", "指数", "节点", "小票亏效", "大票亏效", "一字", "断板"]
            if c in df.columns]
    return df[keep].fillna("").reset_index(drop=True)


def _limit_pct(code: str) -> float:
    """涨跌停幅度近似：创业板/科创板20%，北交所30%，其余10%（ST未细分，初步版）。"""
    if code.startswith(("30", "68")):
        return 20.0
    if code.startswith(("8", "4", "92")):
        return 30.0
    return 10.0


def daban_damian_count() -> int | None:
    """打板大面数：主板盘中曾触及涨停价，但当前涨幅回落至5%以下的个股数。"""
    try:
        spot = data.get_stock_spot()
        if spot.empty or "最高" not in spot.columns:
            return None
        pct = pd.to_numeric(spot["涨跌幅"], errors="coerce")
        prev_close = pd.to_numeric(spot["最新价"], errors="coerce") - \
            pd.to_numeric(spot["涨跌额"], errors="coerce")
        lim = spot["代码"].astype(str).map(_limit_pct)
        lim_price = (prev_close * (1 + lim / 100)).round(2)
        touched = (pd.to_numeric(spot["最高"], errors="coerce") >= lim_price - 0.01).fillna(False)
        is_main = ~spot["代码"].astype(str).str.startswith(("30", "68", "8", "4", "92"))
        return int((touched & (pct < 5.0) & is_main).sum())
    except Exception:
        return None


def hot_stock_stats() -> dict:
    """同花顺热榜前10热门股表现：热门个股指数(平均涨跌幅) + 明细。"""
    out: dict = {"hot_list": []}
    try:
        hot = data.get_hot_stocks(top=10)
        if hot.empty:
            return out
        pct = pd.to_numeric(hot.get("涨跌幅"), errors="coerce")
        if pct.notna().any():
            out["热门个股指数"] = round(float(pct.mean()), 2)
            out["热门股大跌数"] = int((pct <= -7).sum())
            lim = hot["代码"].astype(str).map(_limit_pct)
            out["热门股跌停数"] = int((pct <= -(lim - 0.2)).sum())
            out["热门股涨停数"] = int((pct >= lim - 0.2).sum())
        out["hot_list"] = hot.to_dict("records")
    except Exception as e:
        logger.warning("热门股统计失败：%s", e)
    return out


def market_stats() -> dict:
    """当日盘面统计：大面数(跌>7%)、跌停/涨停数、涨>7%数、高度个股(10日涨幅>40%)。"""
    out: dict = {}
    try:
        spot = data.get_stock_spot()
        pct = pd.to_numeric(spot["涨跌幅"], errors="coerce")
        lim = spot["代码"].astype(str).map(_limit_pct)
        out["大面数"] = int((pct <= -7).sum())
        out["涨超7数"] = int((pct >= 7).sum())
        out["跌停数"] = int((pct <= -(lim - 0.2)).sum())
        out["涨停数"] = int((pct >= lim - 0.2).sum())
        # 打板大面：主板盘中曾触及涨停价，但当前涨幅回落至5%以下
        if "最高" in spot.columns:
            prev_close = pd.to_numeric(spot["最新价"], errors="coerce") - \
                pd.to_numeric(spot["涨跌额"], errors="coerce")
            lim_price = (prev_close * (1 + lim / 100)).round(2)
            touched = (pd.to_numeric(spot["最高"], errors="coerce") >= lim_price - 0.01).fillna(False)
            is_main = ~spot["代码"].astype(str).str.startswith(("30", "68", "8", "4", "92"))
            out["打板大面数"] = int((touched & (pct < 5.0) & is_main).sum())
    except Exception as e:
        logger.warning("盘面统计失败：%s", e)
    try:
        cache = data.load_metrics_cache()
        if cache is None:
            cache = data.load_metrics_cache(allow_stale=True)
        if cache is not None and "closes" in cache.columns:
            cnt = 0
            for arr in cache["closes"]:
                a = np.asarray(arr, dtype=float)
                if a.size > 10 and a[-11] and not np.isnan(a[-11]) \
                        and (a[-1] / a[-11] - 1) * 100 > 40:
                    cnt += 1
            out["高度个股数"] = cnt
    except Exception as e:
        logger.warning("高度个股统计失败：%s", e)
    try:
        idx = data.get_index_daily("sh000001", days=3)
        if len(idx) >= 2:
            out["上证涨跌幅"] = round(
                float(idx["收盘"].iloc[-1] / idx["收盘"].iloc[-2] - 1) * 100, 2)
    except Exception:
        pass
    try:
        from src import index_timing as it
        rec = it.load_ai_predictions().get(datetime.now().strftime("%Y-%m-%d"), {})
        if rec.get("signal"):
            out["平均股价信号"] = rec["signal"]
        if rec.get("mid_cycle"):
            out["中级周期"] = rec["mid_cycle"]
    except Exception:
        pass
    return out


# ── 历史盘面统计（从 metrics_cache 回算）─────────────────────────────────

_hist_cache: dict | None = None


def _load_hist_arrays() -> dict | None:
    """加载 metrics_cache 并构建交易日→数组索引映射（单次加载，内存复用）。"""
    global _hist_cache
    if _hist_cache is not None:
        return _hist_cache
    cache = data.load_metrics_cache(allow_stale=True)
    if cache is None or "closes" not in cache.columns:
        return None
    idx = data.get_index_daily("sh000001", days=400)
    if idx.empty:
        return None
    trading_dates = idx["日期"].astype(str).str.strip().tolist()
    date_to_pos = {d: i for i, d in enumerate(trading_dates)}
    _hist_cache = {
        "df": cache,
        "trading_dates": trading_dates,
        "date_to_pos": date_to_pos,
    }
    return _hist_cache


def historical_market_stats(date_str: str) -> dict:
    """从 metrics_cache 回算某日的盘面统计（大面/涨停/跌停/打板大面/涨超7）。

    date_str: YYYY-MM-DD
    """
    hc = _load_hist_arrays()
    if hc is None:
        return {}
    date_str = date_str.strip()
    if date_str not in hc["date_to_pos"]:
        return {}
    target_pos = hc["date_to_pos"][date_str]
    df = hc["df"]
    codes = df["代码"].astype(str).values
    lim = np.array([_limit_pct(c) for c in codes])
    is_main = np.array([not c.startswith(("30", "68", "8", "4", "92")) for c in codes])

    last_dates = df["last_date"].astype(str).values
    closes_col = df["closes"].values
    highs_col = df["highs"].values

    pct_all, high_all, prev_close_all, valid = [], [], [], []
    for j in range(len(df)):
        arr_len = len(closes_col[j])
        last_pos = hc["date_to_pos"].get(str(last_dates[j]), -1)
        if last_pos < 0:
            continue
        offset = target_pos - last_pos
        arr_idx = arr_len - 1 + offset
        if arr_idx < 1 or arr_idx >= arr_len:
            continue
        c = np.asarray(closes_col[j], dtype=float)
        h = np.asarray(highs_col[j], dtype=float)
        if np.isnan(c[arr_idx]) or np.isnan(c[arr_idx - 1]) or c[arr_idx - 1] == 0:
            continue
        pct_all.append((c[arr_idx] / c[arr_idx - 1] - 1) * 100)
        high_all.append(h[arr_idx])
        prev_close_all.append(c[arr_idx - 1])
        valid.append(j)

    if not pct_all:
        return {}
    pct = np.array(pct_all)
    highs = np.array(high_all)
    prev_closes = np.array(prev_close_all)
    lim_v = lim[valid]
    main_v = is_main[valid]

    lim_price = (prev_closes * (1 + lim_v / 100)).round(2)
    touched = (highs >= lim_price - 0.01)

    out = {
        "大面数": int(np.sum(pct <= -7)),
        "涨超7数": int(np.sum(pct >= 7)),
        "跌停数": int(np.sum(pct <= -(lim_v - 0.2))),
        "涨停数": int(np.sum(pct >= lim_v - 0.2)),
        "打板大面数": int(np.sum(touched & (pct < 5.0) & main_v)),
    }
    return out


def stats_history(days: int = 10) -> pd.DataFrame:
    """近N个交易日盘面统计序列（大面/涨停/跌停），供模型做相对比较。"""
    try:
        from src import theme_mode
        m = theme_mode._matrices()
    except Exception:
        m = None
    if m is None:
        return pd.DataFrame()
    C, dates, codes = m["C"], m["dates"], m["codes"]
    H = m.get("H")
    lim = np.array([_limit_pct(c) for c in codes])
    is_main = np.array([not c.startswith(("30", "68", "8", "4", "92")) for c in codes])
    today = datetime.now().strftime("%Y-%m-%d")
    rows = []
    for i in range(max(1, C.shape[1] - days), C.shape[1]):
        if dates[i] >= today:   # 当天数据以实时快照为准
            continue
        with np.errstate(invalid="ignore"):
            pct = (C[:, i] / C[:, i - 1] - 1) * 100
        row = {"日期": dates[i],
               "大面数": int(np.nansum(pct <= -7)),
               "涨超7数": int(np.nansum(pct >= 7)),
               "跌停数": int(np.nansum(pct <= -(lim - 0.2))),
               "涨停数": int(np.nansum(pct >= lim - 0.2))}
        if H is not None and H.size and i > 0:
            with np.errstate(invalid="ignore"):
                hi_pct = (H[:, i] / C[:, i - 1] - 1) * 100
            row["打板大面数"] = int(np.nansum(
                (hi_pct >= lim - 0.2) & (pct < 5.0) & is_main))
        rows.append(row)
    return pd.DataFrame(rows)


def load_predictions() -> dict:
    if EMO_STORE.exists():
        try:
            return json.loads(EMO_STORE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_predictions(d: dict) -> None:
    EMO_STORE.parent.mkdir(parents=True, exist_ok=True)
    EMO_STORE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


JUDGE_PROMPT = """A股情绪节点判断。只输出JSON：
{{"node":"节点名","reason":"依据40字内","advice":"操作提示一句"}}
节点选：{nodes}

核心规则（按优先级）：
0. 打板大面硬约束（不可被大面减幅覆盖）：≥10禁修复--强（仅修复--强有此限制）。
1. 先看昨日是否大面多(≥30)。若是，今日大面骤减→修复类：减50%→修复--弱，减70%→修复--中等，减90%→修复--强。但不得超出规则0的上限。不能判为主升。
2. 昨日大面少且今日涨停大增/高度板加速→主升类。打板大面≥10则主升质量打折。
3. 退潮绝对门槛：退潮要求大面>50且热门股跌停且龙头断板；退潮加速要求跌停>10且存在一字缩量跌停。不满足门槛不得判退潮类。
4. 节点流转约束：修复类次日不可能直接跳到退潮加速，中间至少经历混沌或退潮。冰点后的修复如果失败，优先判混沌/混沌分歧而非退潮加速。
5. 前日大面低基数(≤10)时，相对增幅无意义，必须按绝对值判断：大面<100且跌停<10→混沌/混沌分歧。
6. 热门股批量大跌/跌停→退潮；热门股涨停加速→主升--加速。
7. 拿不准选保守(混沌/修复)。

竞价表历史：
{history}

近日盘面（旧→新）：
{recent}

今日{today}（{daban_constraint}）
{stats}
{compare}

热门股：
{hot}
"""


def _daman_compare(hist_stats: pd.DataFrame, today_stats: dict) -> str:
    """前日大面→今日大面对比，输出提示行。"""
    if hist_stats.empty or not today_stats:
        return ""
    prev = hist_stats.iloc[-1]
    prev_dm = int(prev["大面数"]) if pd.notna(prev["大面数"]) else 0
    today_dm = int(today_stats.get("大面数", 0)) if today_stats.get("大面数") is not None else 0
    today_dt = int(today_stats.get("跌停数", 0)) if today_stats.get("跌停数") is not None else 0
    # 低基数场景：前日大面≤10时，相对增幅无意义，按绝对值+跌停数判断
    if prev_dm <= 10:
        if today_dm <= 20:
            tag = "混沌"
        elif today_dm <= 100:
            tag = "混沌分歧" if today_dt < 10 else "退潮"
        elif today_dm <= 200:
            tag = "退潮" if today_dt >= 10 else "混沌分歧"
        else:
            tag = "冰点"
        return (f"前日大面{prev_dm}（低基数）→今日大面{today_dm}，跌停{today_dt}，"
                f"按绝对值判断→{tag}")
    chg = (today_dm - prev_dm) / prev_dm * 100
    if chg < 0:
        tag = "修复--弱" if abs(chg) < 50 else ("修复--中等" if abs(chg) < 70 else "修复--强")
        return f"前日大面{prev_dm}→今日大面{today_dm}，减少{abs(chg):.0f}%→{tag}"
    if chg > 0:
        if today_dm <= 100 and today_dt < 10:
            return (f"前日大面{prev_dm}→今日大面{today_dm}，增加{chg:.0f}%，"
                    f"但绝对值不高且跌停{today_dt}<10→混沌分歧")
        return f"前日大面{prev_dm}→今日大面{today_dm}，增加{chg:.0f}%→退潮"
    return f"前日大面{prev_dm}→今日大面{today_dm}，持平"


def ai_judge(force: bool = False) -> dict:
    """判断今日情绪节点。每日缓存；force=True 重新判断。"""
    today = datetime.now().strftime("%Y-%m-%d")
    store = load_predictions()
    if not force and today in store:
        return store[today]

    if not cfg.QWEN_API_KEY:
        return {"error": "未配置 QWEN_API_KEY"}
    hist = load_auction_table()
    hist_lines = "\n".join(
        f"{r['日期']} {r['节点']} {r.get('指数', '')}"
        for _, r in hist.tail(15).iterrows()) or "（无历史标注）"
    stats = market_stats()
    stats_lines = "\n".join(f"{k}：{v}" for k, v in stats.items()) or "（盘面数据不可用）"
    _db = stats.get("打板大面数")
    if _db is not None and _db >= 10:
        _db_constr = f"打板大面{_db}个，禁强修复（上限中等修复）"
    elif _db is not None:
        _db_constr = f"打板大面{_db}个，无限制"
    else:
        _db_constr = "打板大面数据不可用"
    hist_stats = stats_history(10)
    _db_col = "打板大面数" in hist_stats.columns
    recent_lines = "\n".join(
        f"{r['日期']} 大面{r['大面数']} 跌停{r['跌停数']} 涨停{r['涨停数']}"
        + (f" 打板大面{int(r['打板大面数'])}" if _db_col and pd.notna(r['打板大面数']) else "")
        for _, r in hist_stats.iterrows()) or "（无历史统计）"
    hot = hot_stock_stats()
    _hot_rows = []
    for h in (hot.get("hot_list") or [])[:5]:
        p = h.get("涨跌幅")
        p_str = f"{p:+.2f}%" if pd.notna(p) else "-"
        _hot_rows.append(f"{h.get('名称', '')} {p_str} [{h.get('板块', '-')}]")
    hot_lines = "\n".join(_hot_rows) or "（热榜不可用）"
    if hot.get("热门个股指数") is not None:
        hot_lines = f"热指{hot['热门个股指数']:+.2f}%\n" + hot_lines
    prompt = JUDGE_PROMPT.format(nodes="、".join(NODE_NAMES),
                                 history=hist_lines, recent=recent_lines,
                                 today=today, daban_constraint=_db_constr,
                                 stats=stats_lines, hot=hot_lines,
                                 compare=_daman_compare(hist_stats, stats))
    try:
        from openai import OpenAI
        client = OpenAI(api_key=cfg.QWEN_API_KEY, base_url=cfg.QWEN_BASE_URL)
        resp = client.chat.completions.create(
            model="qwen-plus",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=300)
        raw = resp.choices[0].message.content.strip()
        m = re.search(r"\{.*\}", raw, re.S)
        result = json.loads(m.group(0) if m else raw)
    except Exception:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=cfg.QWEN_API_KEY, base_url=cfg.QWEN_BASE_URL)
            resp = client.chat.completions.create(
                model=cfg.QWEN_CHAT_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1, max_tokens=300)
            raw = resp.choices[0].message.content.strip()
            m = re.search(r"\{.*\}", raw, re.S)
            result = json.loads(m.group(0) if m else raw)
        except Exception as e:
            logger.error("AI 情绪节点判断失败：%s", e)
            return {"error": str(e)}
    result = {
        "node": str(result.get("node", "")).strip(),
        "reason": str(result.get("reason", "")).strip(),
        "advice": str(result.get("advice", "")).strip(),
        "stats": stats,
        "prev_stats": hist_stats.iloc[-1].to_dict() if not hist_stats.empty else {},
        "time": datetime.now().strftime("%H:%M"),
    }
    # 硬约束：打板大面≥10禁修复--强（只有修复--强有此限制，AI可能算错，代码兜底）
    _db_val = stats.get("打板大面数")
    _node = result["node"]
    if _db_val is not None and _db_val >= 10 and _node == "修复--强":
        result["node"] = "修复--中等"
        result["reason"] = f"打板大面{_db_val}≥10，修复--强不可→降修复--中等；{result['reason']}"
    # 硬约束：退潮加速要求跌停>10（规范明确，AI可能算错，代码兜底）
    _dt_val = stats.get("跌停数")
    if _dt_val is not None and _dt_val < 10 and result["node"] == "退潮加速":
        _dm_val = stats.get("大面数", 0) or 0
        result["node"] = "混沌分歧" if _dm_val > 30 else "混沌"
        result["reason"] = (f"跌停{_dt_val}<10不满足退潮加速条件→降{result['node']}；"
                            f"{result['reason']}")
    store[today] = result
    _save_predictions(store)
    return result


def should_auto_judge() -> bool:
    """尾盘 14:57 后且今日尚无判断 → 自动触发。"""
    now = datetime.now()
    if now.strftime("%H:%M") < JUDGE_TIME or now.weekday() >= 5:
        return False
    return now.strftime("%Y-%m-%d") not in load_predictions()
