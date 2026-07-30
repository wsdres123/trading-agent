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

# 规范中的节点全集（模型输出约束）
NODE_NAMES = ["混沌", "混沌分歧",
              "主升确认", "主升加速", "主升高潮", "主升分歧", "主升分歧转一致",
              "主升修复", "主升延续",
              "退潮", "退潮加速", "退潮转衰竭", "冰点",
              "弱修复", "中等修复", "强修复"]


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
    lim = np.array([_limit_pct(c) for c in codes])
    today = datetime.now().strftime("%Y-%m-%d")
    rows = []
    for i in range(max(1, C.shape[1] - days), C.shape[1]):
        if dates[i] >= today:   # 当天数据以实时快照为准
            continue
        with np.errstate(invalid="ignore"):
            pct = (C[:, i] / C[:, i - 1] - 1) * 100
        rows.append({"日期": dates[i],
                     "大面数": int(np.nansum(pct <= -7)),
                     "涨超7数": int(np.nansum(pct >= 7)),
                     "跌停数": int(np.nansum(pct <= -(lim - 0.2))),
                     "涨停数": int(np.nansum(pct >= lim - 0.2))})
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


JUDGE_PROMPT = """你是A股情绪周期研究员。请先学习【情绪节点规范】（emotional node.md），
再参考【竞价表历史节点标注】的判定习惯，结合【近日盘面统计序列】与【今日盘面统计】，
判断今天处于哪个情绪节点。只输出 JSON：
{{"node": "节点名", "reason": "判断依据，80字内", "advice": "操作提示一句话"}}

要求：
- node 必须从以下节点中选一个：{nodes}
- 大面数是相对的，必须与前几日比较，不能只看今天绝对值：
  昨天大面很多、今天大幅减少 → 修复类（减少50%以下=弱修复，约70%=中等修复，90%以上=强修复）；
  今天比昨天明显增加 → 退潮类。
- 判定标准以规范为准（大面数变化、跌停数、赚亏钱效应、主线/核心个股状态、平均股价信号）。
- 热门股表现很关键：热门股批量大跌/跌停→退潮类；热门股高开/涨停加速→主升类；
  热门股分歧但未大面→主升分歧或分歧转一致。
- 历史标注具有连续性参考价值：昨天退潮今天大面大减→修复类；昨天主升今天首次分歧→主升分歧。
- 规则暂不完备，属初步判断，拿不准时选择更保守（偏混沌/修复）的节点。

【情绪节点规范】
{spec}

【竞价表历史节点标注（旧→新）】
{history}

【近日盘面统计序列（旧→新，用于相对比较）】
{recent}

【今日盘面统计】
今日日期：{today}
{stats}

【热门股表现（同花顺热榜前10）】
{hot}
"""


def ai_judge(force: bool = False) -> dict:
    """判断今日情绪节点。每日缓存；force=True 重新判断。"""
    today = datetime.now().strftime("%Y-%m-%d")
    store = load_predictions()
    if not force and today in store:
        return store[today]

    if not cfg.QWEN_API_KEY:
        return {"error": "未配置 QWEN_API_KEY"}
    spec = EMO_SPEC.read_text(encoding="utf-8") if EMO_SPEC.exists() else ""
    hist = load_auction_table()
    hist_lines = "\n".join(
        f"{r['日期']} 指数[{r.get('指数', '')}] 节点[{r['节点']}] "
        f"小票亏效[{r.get('小票亏效', '')}] 大票亏效[{r.get('大票亏效', '')}] "
        f"一字[{r.get('一字', '')}] 断板[{r.get('断板', '')}]"
        for _, r in hist.tail(40).iterrows()) or "（无历史标注）"
    stats = market_stats()
    stats_lines = "\n".join(f"{k}：{v}" for k, v in stats.items()) or "（盘面数据不可用）"
    hist_stats = stats_history(10)
    recent_lines = "\n".join(
        f"{r['日期']} 大面{r['大面数']} 跌停{r['跌停数']} 涨停{r['涨停数']} 涨超7%{r['涨超7数']}"
        for _, r in hist_stats.iterrows()) or "（无历史统计）"
    hot = hot_stock_stats()
    _hot_rows = []
    for h in hot.get("hot_list", []):
        p = h.get("涨跌幅")
        p_str = f"{p:+.2f}%" if pd.notna(p) else "-"
        amt = h.get("成交额(亿)")
        amt_str = f"{amt}亿" if pd.notna(amt) else "-"
        _hot_rows.append(f"{h.get('排名', '')}. {h.get('名称', '')}({h.get('代码', '')}) "
                         f"涨跌幅{p_str} 成交额{amt_str} 板块[{h.get('板块', '-')}]")
    hot_lines = "\n".join(_hot_rows) or "（热榜不可用）"
    if hot.get("热门个股指数") is not None:
        hot_lines = (f"热门个股指数（前10平均涨跌幅）：{hot['热门个股指数']:+.2f}% ｜ "
                     f"大跌(≤-7%) {hot.get('热门股大跌数', 0)} · "
                     f"跌停 {hot.get('热门股跌停数', 0)} · "
                     f"涨停 {hot.get('热门股涨停数', 0)}\n" + hot_lines)
    prompt = JUDGE_PROMPT.format(nodes="、".join(NODE_NAMES), spec=spec,
                                 history=hist_lines, recent=recent_lines,
                                 today=today, stats=stats_lines, hot=hot_lines)
    try:
        from openai import OpenAI
        client = OpenAI(api_key=cfg.QWEN_API_KEY, base_url=cfg.QWEN_BASE_URL)
        resp = client.chat.completions.create(
            model=cfg.QWEN_CHAT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1)
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
    store[today] = result
    _save_predictions(store)
    return result


def should_auto_judge() -> bool:
    """尾盘 14:57 后且今日尚无判断 → 自动触发。"""
    now = datetime.now()
    if now.strftime("%H:%M") < JUDGE_TIME or now.weekday() >= 5:
        return False
    return now.strftime("%Y-%m-%d") not in load_predictions()
