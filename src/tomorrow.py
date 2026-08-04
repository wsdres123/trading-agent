"""明日推演：汇总指数择时/情绪节点/主线模式/盘面统计，大模型推演明日走势与操作计划。

- gather_context()：收集今日各模块判断与近日数据
- ai_deduce()：输出明日 多空转/情绪节点/主线关注/仓位/操作计划/风险（每日缓存）
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta

import pandas as pd

from config import settings as cfg
from src import data

logger = logging.getLogger("tomorrow")

DEDUCE_STORE = cfg.DATA_DIR / "tomorrow_ai.json"


def load_deductions() -> dict:
    if DEDUCE_STORE.exists():
        try:
            return json.loads(DEDUCE_STORE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_deductions(d: dict) -> None:
    DEDUCE_STORE.parent.mkdir(parents=True, exist_ok=True)
    DEDUCE_STORE.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                            encoding="utf-8")


def gather_context() -> dict:
    """收集推演所需的各模块现状（尽量走缓存，失败逐项降级）。"""
    ctx: dict = {}
    today = datetime.now().strftime("%Y-%m-%d")

    try:
        from src import index_timing as it
        rec = it.load_ai_predictions().get(today, {})
        ctx["今日择时"] = {k: rec.get(k) for k in ("signal", "mid_cycle", "position", "reason")
                          if rec.get(k)}
        hist = it.load_history_signals()
        if not hist.empty:
            ctx["复盘表近10日"] = [
                f"{r['日期']} 择时{r['信号']} 周期{r['中级周期']} 情绪{r['情绪周期']}"
                for _, r in hist.tail(10).iterrows()]
        ctx["全市场成交额_万亿"] = it.market_turnover_wanyi()
        sigs = it.all_signals()
        recent = sorted(sigs)[-3:]
        ctx["近3日多空转"] = [f"{d}:{sigs[d]['signal']}" for d in recent]
        hint = it.pattern_hint(sigs)
        if hint:
            ctx["组合规则提示"] = hint
    except Exception as e:
        logger.warning("择时上下文失败：%s", e)

    try:
        from src import emotion_node as en
        rec = en.load_predictions().get(today, {})
        if rec.get("node"):
            ctx["今日情绪节点"] = {k: rec.get(k) for k in ("node", "reason")}
        hs = en.stats_history(8)
        if not hs.empty:
            ctx["近日盘面统计"] = [
                f"{r['日期']} 大面{r['大面数']} 跌停{r['跌停数']} 涨停{r['涨停数']}"
                for _, r in hs.iterrows()]
        ctx["今日盘面"] = rec.get("stats") or en.market_stats()
        auc = en.load_auction_table()
        if not auc.empty:
            ctx["竞价表近5日节点"] = [
                f"{r['日期']} {r['节点']}" for _, r in auc.tail(5).iterrows()]
    except Exception as e:
        logger.warning("情绪上下文失败：%s", e)

    try:
        from src import theme_mode as tm
        start = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
        r = tm.detect(start, today)
        if r.get("has_mainline"):
            ml = r["mainlines"][0]
            ctx["主线现状"] = (
                f"唯一主线[{ml['board']}] {ml['start']}~{ml['end']} 连续{ml['days']}天"
                f"{'（进行中）' if ml['ongoing'] else '（已结束）'}；"
                f"核心：{'、'.join(s['名称'] for s in ml['core']) or '无'}；"
                f"关联板块：{'、'.join(ml.get('related', [])[:5]) or '无'}")
        elif not r.get("error"):
            ctx["主线现状"] = "近45日无主线"
    except Exception as e:
        logger.warning("主线上下文失败：%s", e)

    try:
        idx = data.get_index_daily("sh000001", days=9)
        if not idx.empty:
            idx = idx.copy()
            idx["涨跌幅"] = (idx["收盘"] / idx["收盘"].shift(1) - 1) * 100
            ctx["上证近8日"] = [
                f"{r['日期']} 收{r['收盘']:.0f}"
                + (f" {r['涨跌幅']:+.2f}%" if pd.notna(r["涨跌幅"]) else "")
                for _, r in idx.tail(8).iterrows()]
    except Exception as e:
        logger.warning("指数上下文失败：%s", e)
    return ctx


DEDUCE_PROMPT = """你是A股复盘推演专家。基于【今日市场全景】做明日推演，只输出 JSON：
{{"signal": "多|空|转", "emotion_node": "明日最可能的情绪节点",
"mainline": "明日主线/板块关注点，60字内",
"position": "明日仓位建议一句话",
"plan": "明日操作计划，分竞价/盘中/尾盘三段，150字内",
"risk": "最大风险点与应对，60字内"}}

推演要点：
- 结合中级周期与多空转组合规则：俩转一多/俩多一转→趋势A或C；俩转一空/俩空一转→D或B；
  空+D必须空仓，空+B只可小仓位。
- 情绪节点按连续性推演：退潮后大面大减→修复；修复不完全→弱修复延续或再退潮；
  主升分歧次日常见分歧转一致或修复。大面数看相对变化。
- B/D周期不可能有主线；有主线时关注核心个股动向，无主线时以观察为主。
- 推演是概率判断，给出最可能情景即可，语言口语化。

【今日市场全景】
今日日期：{today}
{context}
"""


def ai_deduce(force: bool = False) -> dict:
    """推演明日。每日缓存；force=True 重新推演。"""
    today = datetime.now().strftime("%Y-%m-%d")
    store = load_deductions()
    if not force and today in store:
        return store[today]
    if not cfg.QWEN_API_KEY:
        return {"error": "未配置 QWEN_API_KEY"}

    ctx = gather_context()
    ctx_lines = "\n".join(
        f"{k}：{json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v}"
        for k, v in ctx.items())
    prompt = DEDUCE_PROMPT.format(today=today, context=ctx_lines)
    try:
        from openai import OpenAI
        client = OpenAI(api_key=cfg.QWEN_API_KEY, base_url=cfg.QWEN_BASE_URL)
        resp = client.chat.completions.create(
            model=cfg.QWEN_PLUS_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            extra_body={"enable_thinking": False})
        raw = resp.choices[0].message.content.strip()
        m = re.search(r"\{.*\}", raw, re.S)
        result = json.loads(m.group(0) if m else raw)
    except Exception as e:
        logger.error("明日推演失败：%s", e)
        return {"error": str(e)}
    result = {k: str(result.get(k, "")).strip()
              for k in ("signal", "emotion_node", "mainline", "position", "plan", "risk")}
    result["context"] = ctx
    result["time"] = datetime.now().strftime("%H:%M")
    store[today] = result
    _save_deductions(store)
    return result
