"""指数择时：中级周期判断（node_spec）+ 每日多/空/转预判（屠龙表复盘）+ 平均股价K线。

- load_history_signals(): 屠龙表-复盘表.csv 指数择时列 → 历史 多/空/转 标记
- ai_judge(): Qwen 基于 node_spec.md + 复盘表近况 + 指数量价特征，判断中级周期与今日信号
  （每日缓存到 .data/timing_ai.json，尾盘 14:57 后页面自动触发）
- avg_price_kline(): 全市场指标缓存的 OHLC 矩阵逐日算术平均 → 平均股价日K
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

import numpy as np
import pandas as pd

from config import settings as cfg
from src import data
from src.data import ttl_cache
from src.schema_validator import load_spec_text

logger = logging.getLogger("index_timing")

REVIEW_CSV = cfg.KNOWLEDGE_DIR / "屠龙表 - 复盘表.csv"
NODE_SPEC = cfg.DOCS_DIR / "node_spec.md"
AI_STORE = cfg.DATA_DIR / "timing_ai.json"

SIGNAL_COLORS = {"多": cfg.COLOR_UP, "空": cfg.COLOR_DOWN, "转": cfg.COLOR_STOCK}
JUDGE_TIME = "14:57"  # 每日尾盘判断时点


# ── 历史信号（屠龙表复盘表）───────────────────────────────────────────────
def load_history_signals() -> pd.DataFrame:
    """→ DataFrame[日期(YYYY-MM-DD), 信号(多/空/转), 中级周期, 情绪周期, 指数]"""
    try:
        df = pd.read_csv(REVIEW_CSV)
    except Exception as e:
        logger.warning("读复盘表失败：%s", e)
        return pd.DataFrame(columns=["日期", "信号", "中级周期", "情绪周期", "指数"])
    df = df[df["指数择时"].notna() & df["日期"].notna()].copy()
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce").dt.strftime("%Y-%m-%d")
    df = df.dropna(subset=["日期"])
    df["信号"] = df["指数择时"].astype(str).str.strip()
    df = df[df["信号"].isin(["多", "空", "转"])]
    out = df.rename(columns={"周期": "情绪周期"})[
        ["日期", "信号", "中级周期", "情绪周期", "指数"]]
    return out.drop_duplicates("日期", keep="last").reset_index(drop=True)


# ── AI 每日预判存取 ───────────────────────────────────────────────────────
def load_ai_predictions() -> dict:
    try:
        return json.loads(AI_STORE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_ai_predictions(d: dict) -> None:
    try:
        AI_STORE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        logger.warning("写AI预判失败：%s", e)


@ttl_cache(cfg.SPOT_TTL)
def all_signals() -> dict:
    """{日期: {signal, source}}：复盘表历史为准，AI 预判补充未复盘的日期，上穿多空线自动补充"转"。"""
    out = {}
    for d, p in load_ai_predictions().items():
        if p.get("signal") in SIGNAL_COLORS:
            out[d] = {"signal": p["signal"], "source": "AI"}
    hist = load_history_signals()
    for _, r in hist.iterrows():
        out[r["日期"]] = {"signal": r["信号"], "source": "复盘"}
    # 平均股价收盘价上穿多空线（MA10）→ 自动补充"转"信号（可覆盖AI预判，不覆盖复盘）
    try:
        avg = avg_price_kline(days=400)
        if not avg.empty and len(avg) >= 11:
            avg = avg.reset_index(drop=True)
            # 追加今日实时均价（如盘中且尚未包含今日）
            import time as _time
            today_str = _time.strftime("%Y-%m-%d")
            if str(avg["日期"].iloc[-1]) != today_str:
                rt = data.get_realtime_avg_price()
                rt_val = rt.get("avg_price")
                if rt_val:
                    prev_close = float(avg["收盘"].iloc[-1])
                    today_row = pd.DataFrame([{
                        "日期": today_str,
                        "开盘": round(prev_close, 2),
                        "最高": round(max(prev_close, rt_val), 2),
                        "最低": round(min(prev_close, rt_val), 2),
                        "收盘": round(rt_val, 2),
                    }])
                    avg = pd.concat([avg, today_row], ignore_index=True)
            # 检测上穿
            ma10 = avg["收盘"].rolling(10, min_periods=10).mean()
            for i in range(1, len(avg)):
                if not (pd.notna(ma10.iloc[i]) and pd.notna(ma10.iloc[i - 1])):
                    continue
                if (avg["收盘"].iloc[i - 1] <= ma10.iloc[i - 1]
                        and avg["收盘"].iloc[i] > ma10.iloc[i]):
                    d = str(avg["日期"].iloc[i])
                    # 上穿多空线可覆盖AI预判，但不覆盖复盘记录
                    if d not in out or out[d].get("source") == "AI":
                        out[d] = {"signal": "转", "source": "上穿多空线"}
    except Exception:
        pass
    return out


# ── 市场特征 ──────────────────────────────────────────────────────────────
def market_turnover_wanyi() -> float | None:
    """全市场成交额（万亿元）。"""
    try:
        spot = data.get_stock_spot()
        if spot.empty or "成交额" not in spot.columns:
            return None
        return round(float(pd.to_numeric(spot["成交额"], errors="coerce").sum()) / 1e12, 2)
    except Exception:
        return None


def pattern_hint(signals) -> str | None:
    """近3日信号组合 → 行情提示（1.md 规则）。signals 可为 all_signals() 的 dict 或信号列表。"""
    if isinstance(signals, dict):
        signals = [signals[d]["signal"] for d in sorted(signals)]
    if len(signals) < 3:
        return None
    s3 = list(signals)[-3:]
    zh, duo, kong = s3.count("转"), s3.count("多"), s3.count("空")
    if zh == 2 and duo == 1:
        return "近3日「俩转一多」→ 易出趋势A周期或C周期行情，关注主线做多机会"
    if duo == 2 and zh == 1:
        return "近3日「俩多一转」→ 易出趋势A周期或C周期行情，关注主线做多机会"
    if zh == 2 and kong == 1:
        return "近3日「俩转一空」→ 易出下跌行情（D周期或B周期），控制仓位"
    if kong == 2 and zh == 1:
        return "近3日「俩空一转」→ 易出下跌行情（D周期或B周期），控制仓位"
    if kong == 3:
        return "近3日连续「空」→ 谨慎，若叠加D周期必须空仓"
    if duo == 3:
        return "近3日连续「多」→ 趋势延续中，回撤线之上持股"
    return None


def _position_rule(signal: str, mid_cycle: str) -> str | None:
    """1.md 硬规则：D 周期无论信号如何必须空仓；空+B 小仓位。"""
    if "D" in mid_cycle:
        return "D周期（下跌周期）→ 必须空仓"
    if signal == "空" and "B" in mid_cycle:
        return "空信号叠加B周期 → 只可小仓位参与"
    return None


# ── AI 判断（中级周期 + 今日多空转）──────────────────────────────────────
def _index_features(days: int = 12) -> str:
    df = data.get_index_daily("sh000001", days=days + 1)
    if df.empty:
        return "（指数数据不可用）"
    df = df.copy()
    df["涨跌幅"] = (df["收盘"] / df["收盘"].shift(1) - 1) * 100
    lines = []
    for _, r in df.tail(days).iterrows():
        pct = f"{r['涨跌幅']:+.2f}%" if pd.notna(r["涨跌幅"]) else "-"
        lines.append(f"{r['日期']} 开{r['开盘']:.0f} 高{r['最高']:.0f} "
                     f"低{r['最低']:.0f} 收{r['收盘']:.0f} 涨跌{pct}")
    return "\n".join(lines)


def _recent_review_rows(n: int = 10) -> str:
    hist = load_history_signals()
    if hist.empty:
        return "（无复盘记录）"
    rows = hist.tail(n)
    return "\n".join(f"{r['日期']} {r['信号']} {r['中级周期']} {r['情绪周期']}"
                     for _, r in rows.iterrows())


def _emotion_node_status() -> str:
    """今日情绪节点预测（如有），用于指数择时参考。"""
    from src import emotion_node as en
    today = datetime.now().strftime("%Y-%m-%d")
    rec = en.load_predictions().get(today)
    if not rec:
        return "（情绪节点未判断）"
    node = rec.get("node", "未判断")
    reason = rec.get("reason", "")
    return f"{today} 情绪节点：{node}（{reason}）"


def _avg_price_status() -> str:
    """平均股价与多空线(MA10)关系：当前价、MA10值、在上方/下方、是否刚上穿、K线形态。"""
    avg = avg_price_kline(days=25)
    if avg.empty or len(avg) < 11:
        return "（平均股价数据不足）"
    avg = avg.reset_index(drop=True)
    # 追加今日实时均价（如盘中且尚未包含今日）
    import time as _time
    today_str = _time.strftime("%Y-%m-%d")
    if str(avg["日期"].iloc[-1]) != today_str:
        rt = data.get_realtime_avg_price()
        rt_val = rt.get("avg_price")
        if rt_val:
            prev_close = float(avg["收盘"].iloc[-1])
            today_row = pd.DataFrame([{
                "日期": today_str,
                "开盘": round(prev_close, 2),
                "最高": round(max(prev_close, rt_val), 2),
                "最低": round(min(prev_close, rt_val), 2),
                "收盘": round(rt_val, 2),
            }])
            avg = pd.concat([avg, today_row], ignore_index=True)
    ma10 = avg["收盘"].rolling(10, min_periods=10).mean()
    close = float(avg["收盘"].iloc[-1])
    open_ = float(avg["开盘"].iloc[-1])
    ma_val = ma10.iloc[-1]
    if pd.isna(ma_val):
        return "（多空线数据不足）"
    ma_val = float(ma_val)
    date = str(avg["日期"].iloc[-1])
    pos = "上方" if close > ma_val else "下方"
    cross = ""
    if len(avg) >= 2 and pd.notna(ma10.iloc[-2]):
        prev_above = float(avg["收盘"].iloc[-2]) > float(ma10.iloc[-2])
        curr_above = close > ma_val
        if not prev_above and curr_above:
            cross = "，今日上穿多空线"
        elif prev_above and not curr_above:
            cross = "，今日下穿多空线"
    # 今日K线形态
    body_pct = abs(close - open_) / open_ * 100 if open_ > 0 else 0
    if body_pct < 0.3:
        candle_type = "，今日十字星（小实体）"
    elif close < open_:
        candle_type = "，今日阴线"
    else:
        candle_type = "，今日阳线"
    # 上涨趋势判断（近5日均在多空线上方 → 处于上涨段）
    trend_note = ""
    if len(avg) >= 6:
        closes_recent = avg["收盘"].iloc[-6:-1]
        ma10_recent = ma10.iloc[-6:-1]
        valid = [(c, m) for c, m in zip(closes_recent, ma10_recent) if pd.notna(m)]
        if valid and all(c > m for c, m in valid):
            trend_note = "，近5日持续在多空线上方（上涨趋势中）"
        elif valid and all(c <= m for c, m in valid):
            trend_note = "，近5日持续在多空线下方（下跌趋势中）"
    return (f"{date} 平均股价{close:.2f}，多空线(MA10){ma_val:.2f}，"
            f"股价在多空线{pos}{cross}{candle_type}{trend_note}")


JUDGE_PROMPT = """A股指数择时。只输出JSON，不要多余文字：
{{"mid_cycle":"(从趋势A/龙头A/B/C/D中选一个)","signal":"多/空/转","position":"仓位一句话","reason":"依据40字内","confidence":0.8,"abstain":false,"evidence":[{{"feature":"特征名","value":"特征值","direction":"bullish/bearish/neutral"}}],"invalidation":["条件失效触发点1","条件失效触发点2"]}}

字段说明：
- signal: 多(做多)/空(做空)/转(转折观望/不确定)
- mid_cycle: 中级周期阶段
- confidence: 0.0~1.0，对判断的置信度
- abstain: true表示不确定
- evidence: 支撑判断的关键特征，feature必须是输入中出现的特征
- invalidation: 什么情况下这个判断会失效（反转条件）

规则：
1. 量能≥2.5万亿连阳→趋势A/C；缩量阴跌→D；箱体→B；空+D必须空仓；空+B小仓。
2. signal延续复盘近期标注，除非量价明显反转。
3. 多空转核心规则（来自1.md规范）：
   - 平均股价一段下跌后上穿多空线 → 出"转"字（上穿是最强转折信号）
   - 平均股价一轮上涨后跌破多空线 → 出"转"字（下穿也是转折信号，转为观望/空）
   - 上涨趋势中出现十字星或阴线 → 出"转"字（警示可能顶部，转为观望）
   - 一般至少2转1多，或3转1多，才能确定一轮主升（多个转信号后才升级为多）
4. 平均股价在多空线下方且未上穿时，若无转折迹象，signal应为空。
5. 平均股价上穿多空线出转后，若后续持续在多空线上方且无十字星/阴线，signal可升级为多。
6. 情绪节点为「主升--确认/主升--加速/主升--延续/主升--高潮」时，支持signal为转/多，不单独因量能不足而下修为空。
7. 量能不足只影响仓位上限和中级周期判定，不能单独把「转」压成「空」。
不确定时设abstain=true或confidence<0.5，signal仍必须为多/空/转之一，不要输出观望。

策略规范（单一事实来源）：
{spec}

复盘表近况：
{review}

上证近期K线：
{kline}

平均股价与多空线：
{avg_status}

情绪节点：
{emotion_node}

今日{today} 成交额{turnover}万亿 涨跌{today_pct}
"""

REVIEW_PROMPT_TIMING = """你是A股择时复核员，请根据以下**同一份结构化特征**独立判断中级周期与多空信号。
这是对抗式复核——你不知道第一模型的结论，需要独立给出判断。

请重点回答：
1. 是否违反硬规则（空+D必须空仓、空+B小仓）？
2. 哪一条证据不足？
3. 有哪些反例会推翻你的结论？
4. 是否应弃权（数据矛盾或信息不足时设abstain=true）？
5. 需要什么条件才能升级仓位？

请严格输出JSON：
{{
  "signal": "多/空/转",
  "mid_cycle": "趋势A/龙头A/B/C/D",
  "confidence": 0.0~1.0,
  "abstain": false,
  "reason": "独立判断理由（100字内）",
  "evidence": [{{"feature": "特征名", "value": "值", "direction": "多/空/中性"}}],
  "invalidation": ["失效条件"],
  "position": "仓位建议",
  "position_cap": 0.0~1.0
}}
不确定时设abstain=true，不要强行输出伪确定结论。

判断规则：
1. 多空转核心规则：
   - 平均股价下跌后上穿多空线 → 转；平均股价上涨后下穿多空线 → 转；上涨中出现十字星/阴线 → 转。
   - 至少2转1多或3转1多，才能确认一轮主升并判多。
2. 平均股价上穿多空线出转后，后续持续在线上且无十字星/阴线 → 可升级为多。
3. 情绪节点为「主升--确认/主升--加速/主升--延续/主升--高潮」 → 支持signal为转/多，量能不足不单独压空。
4. 量能不足只影响仓位上限和中级周期，不单独把「转」压成「空」。

策略规范（单一事实来源）：
{spec}

复盘表近况：
{review}

上证近期K线：
{kline}

平均股价与多空线：
{avg_status}

情绪节点：
{emotion_node}

今日{today} 成交额{turnover}万亿 涨跌{today_pct}
"""


def ai_judge(force: bool = False) -> dict:
    """判断今日 中级周期+多空转。每日缓存；force=True 重新判断。

    使用结构化校验器：validate_timing_decision() 对 LLM 输出进行
    枚举校验、置信度截断、evidence 过滤、硬规则覆盖 position_cap。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    store = load_ai_predictions()
    if not force and today in store:
        return store[today]

    if not cfg.QWEN_API_KEY:
        return {"error": "未配置 QWEN_API_KEY"}

    # 使用统一特征构建器
    from src.feature_provider import FeatureProvider
    from src.schema_validator import validate_timing_decision, dual_stage_review
    from src.llm_gateway import call_llm

    provider = FeatureProvider(date_str=None)  # 实时模式
    features = provider.build_timing_features()

    spec_text = load_spec_text(NODE_SPEC)
    prompt = JUDGE_PROMPT.format(**features, spec=spec_text)
    review_prompt = REVIEW_PROMPT_TIMING.format(**features, spec=spec_text)

    def _call_llm(model: str, llm_prompt: str = "") -> str | None:
        """调用 LLM 返回原始文本，失败返回 None。"""
        return call_llm(
            prompt=llm_prompt or prompt,
            model=model,
            temperature=0.1,
            max_tokens=500,
            timeout=30.0,
            retries=0,
        )

    def _call_review_llm(_model: str, llm_prompt: str = "") -> str | None:
        """复核阶段使用更快模型 + 更短超时，避免 qwen3.7-max 拖慢整体响应。"""
        return call_llm(
            prompt=llm_prompt or prompt,
            model="qwen-plus",
            temperature=0.1,
            max_tokens=500,
            timeout=15.0,
            retries=0,
        )

    avg_status = features.get("avg_status", "")

    # 统计近期 转 信号天数（用于 2转1多 门槛检查）
    sorted_dates_all = sorted(store.keys())
    recent_signals = [store[d].get("signal") for d in sorted_dates_all[-6:]
                      if store[d].get("signal") in ("多", "空", "转")]

    def _apply_hard_crossover(res: dict) -> dict:
        """硬规则：上穿/下穿多空线当日必须出转；2转1多门槛：多个转后才能判多。"""
        sig = res.get("signal", "转")
        forced_reason = ""
        if "今日上穿多空线" in avg_status and sig != "转":
            forced_reason = "上穿多空线当日硬规则→转"
        elif "今日下穿多空线" in avg_status and sig not in ("转", "空"):
            forced_reason = "下穿多空线当日硬规则→转"
        elif ("上涨趋势中" in avg_status
              and ("今日阴线" in avg_status or "今日十字星" in avg_status)
              and sig == "多"):
            forced_reason = "上涨中出现阴线/十字星→转"
        elif sig == "多":
            # 2转1多门槛：近6日里需要至少2个转（或1个转+1个多）才可升为多
            zhuans = sum(1 for s in recent_signals if s == "转")
            if zhuans < 2:
                forced_reason = f"近期仅{zhuans}个转信号，需≥2转才可判多→改为转"
        if forced_reason:
            res = dict(res)
            res["signal"] = "转"
            res["reason"] = forced_reason + "；" + res.get("reason", "")
            res["confidence"] = min(0.85, res.get("confidence", 0.8))
            logger.info("硬规则覆盖 signal→转: %s", forced_reason)
        return res

    # 尝试主模型 + 兜底模型，每个最多重试 1 次（共 4 次调用）
    validated = None
    for model in ["qwen-plus", cfg.QWEN_CHAT_MODEL]:
        for attempt in range(2):
            raw = _call_llm(model, prompt)
            if raw is None:
                continue
            result = validate_timing_decision(raw, position_rule_fn=_position_rule)
            result = _apply_hard_crossover(result)
            if not result.get("abstain") and result.get("confidence", 0) >= 0.5:
                validated = result
                break
            if attempt == 0:
                logger.info("校验返回弃权或低置信，重试一次 (%s)", model)
        if validated and not validated.get("abstain"):
            break

    if validated is None:
        logger.error("AI 择时判断全部失败")
        return {"error": "LLM 调用与校验均失败"}

    # 双阶段复核：低置信/弃权/前后跳变 → 触发 qwen3.7-max 对抗式复核
    sorted_dates = sorted(store.keys(), reverse=True)
    prev_result = store[sorted_dates[0]] if sorted_dates and sorted_dates[0] != today else None

    def _validate_for_review(raw: str) -> dict:
        return _apply_hard_crossover(
            validate_timing_decision(raw, position_rule_fn=_position_rule)
        )

    validated = dual_stage_review(
        call_llm=_call_review_llm,
        validate_fn=_validate_for_review,
        stage1_result=validated,
        prev_result=prev_result,
        key_field="signal",
        review_prompt=review_prompt,
    )
    # 最终兜底：复核可能重置 signal，再过一次硬规则
    validated = _apply_hard_crossover(validated)

    # 组装最终结果（向后兼容 + 新增字段）
    result = {
        "mid_cycle": validated["mid_cycle"],
        "signal": validated["signal"],
        "position": validated["position"],
        "reason": validated["reason"],
        "confidence": validated["confidence"],
        "abstain": validated["abstain"],
        "evidence": validated["evidence"],
        "invalidation": validated["invalidation"],
        "position_cap": validated["position_cap"],
        "time": datetime.now().strftime("%H:%M"),
        "turnover_wanyi": features["turnover"],
        "reviewed": validated.get("reviewed", False),
        "agreed": validated.get("agreed", None),
    }
    if validated.get("review_note"):
        result["review_note"] = validated["review_note"]
    if validated.get("review_reason"):
        result["review_reason"] = validated["review_reason"]
    store[today] = result
    _save_ai_predictions(store)
    try:
        from src import predictions
        predictions.append_prediction(
            type="timing", date=today, signal=result["signal"],
            confidence=result["confidence"],
            evidence_snapshot=result.get("evidence", []),
            position_cap=result.get("position_cap"),
            abstain=result.get("abstain", False),
            model_id="qwen-plus",
            extra={"mid_cycle": result.get("mid_cycle")},
        )
    except Exception:
        pass
    return result


def should_auto_judge() -> bool:
    """尾盘 14:57 后且今日尚无判断 → 自动触发。"""
    now = datetime.now()
    if now.strftime("%H:%M") < JUDGE_TIME or now.weekday() >= 5:
        return False
    return now.strftime("%Y-%m-%d") not in load_ai_predictions()


# ── 平均股价日K ───────────────────────────────────────────────────────────
@ttl_cache(cfg.SPOT_TTL)
def avg_price_kline(days: int = 360) -> pd.DataFrame:
    """全市场逐日算术平均 OHLC + 合计成交量。依赖指标缓存（含兜底数据，覆盖全部股票）。"""
    cache = data.load_metrics_cache()
    cols = ["日期", "开盘", "最高", "最低", "收盘", "成交量"]
    if cache is None or not {"opens", "lows", "closes", "highs"}.issubset(cache.columns):
        return pd.DataFrame(columns=cols)

    def mat(col: str) -> np.ndarray | None:
        arrs = [a if a is not None and len(a) else [] for a in cache[col].tolist()]
        L = max((len(a) for a in arrs), default=0)
        if L == 0:
            return None
        M = np.full((len(arrs), L), np.nan)
        for i, a in enumerate(arrs):
            M[i, L - len(a):] = a
        return M

    O, H, L_, C = mat("opens"), mat("highs"), mat("lows"), mat("closes")
    if C is None or C.shape[1] < 30:
        return pd.DataFrame(columns=cols)
    n = min(days, C.shape[1])
    df = pd.DataFrame({
        "开盘": np.nanmean(O[:, -n:], axis=0), "最高": np.nanmean(H[:, -n:], axis=0),
        "最低": np.nanmean(L_[:, -n:], axis=0), "收盘": np.nanmean(C[:, -n:], axis=0),
    }).round(2)
    # 合计成交量（每日所有个股成交量之和）
    if "volumes" in cache.columns:
        V = mat("volumes")
        if V is not None and V.shape[1] >= n:
            df["成交量"] = np.nansum(V[:, -n:], axis=0).round(0).astype(float)
        else:
            df["成交量"] = np.nan
    else:
        df["成交量"] = np.nan
    # 日期轴：与上证指数交易日对齐（缓存序列右对齐，末日=缓存 last_date）
    idx = data.get_index_daily("sh000001", days=days + 30)
    dates = idx["日期"].tolist() if not idx.empty else []
    last_date = None
    if "last_date" in cache.columns:
        try:
            last_date = str(cache["last_date"].dropna().mode().iloc[0])[:10]
        except Exception:
            last_date = None
    if dates and last_date in dates:
        end = dates.index(last_date) + 1
        seg = dates[max(0, end - n):end]
    else:
        seg = dates[-n:]
    if len(seg) == n:
        df.insert(0, "日期", seg)
    else:
        df.insert(0, "日期", [f"T-{n - 1 - i}" for i in range(n)])
    return df.reset_index(drop=True)
