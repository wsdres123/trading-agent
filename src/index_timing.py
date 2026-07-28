"""指数择时：中级周期判断（node_spec）+ 每日多/空/转预判（屠龙表复盘）+ 平均股价K线。

- load_history_signals(): 屠龙表-复盘表.csv 指数择时列 → 历史 多/空/转 标记
- ai_judge(): Qwen 基于 node_spec.md + 复盘表近况 + 指数量价特征，判断中级周期与今日信号
  （每日缓存到 .data/timing_ai.json，尾盘 14:57 后页面自动触发）
- avg_price_kline(): 全市场指标缓存的 OHLC 矩阵逐日算术平均 → 平均股价日K
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


def all_signals() -> dict:
    """{日期: {signal, source}}：复盘表历史为准，AI 预判补充未复盘的日期。"""
    out = {}
    for d, p in load_ai_predictions().items():
        if p.get("signal") in SIGNAL_COLORS:
            out[d] = {"signal": p["signal"], "source": "AI"}
    hist = load_history_signals()
    for _, r in hist.iterrows():
        out[r["日期"]] = {"signal": r["信号"], "source": "复盘"}
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
    """1.md 硬规则：空+D 必须空仓；空+B 小仓位。"""
    if signal == "空" and "D" in mid_cycle:
        return "空信号叠加D周期 → 必须空仓"
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


def _recent_review_rows(n: int = 25) -> str:
    hist = load_history_signals()
    if hist.empty:
        return "（无复盘记录）"
    rows = hist.tail(n)
    return "\n".join(f"{r['日期']} 指数择时={r['信号']} 中级周期={r['中级周期']} "
                     f"情绪周期={r['情绪周期']} 指数形态={r['指数']}"
                     for _, r in rows.iterrows())


JUDGE_PROMPT = """你是A股指数择时研究员。请依据下面的【中级周期规范】与【复盘表近况】，
结合【上证指数近期量价】与【今日市场数据】，输出今日判断，只输出 JSON：
{{"mid_cycle": "趋势A|龙头A|B|C|D", "signal": "多|空|转", "position": "仓位建议一句话", "reason": "判断依据，80字内"}}

判断要点：
- mid_cycle 按规范识别当前所处中级周期（量能≥2.5万亿看趋势A/C；缩量阴跌看D；箱体震荡看B）。
- signal 参考复盘表"指数择时"列的历史标注习惯来预判今天：多=看多，空=看空，转=转折/过渡。
- 硬规则：空叠加D周期必须空仓；空叠加B周期只可小仓位。
- 复盘表最近几天的标注具有很强的连续性参考价值，除非量价出现明显反转不要轻易跳变。

【中级周期规范】
{spec}

【复盘表近况（旧→新）】
{review}

【上证指数近期量价（旧→新）】
{kline}

【今日市场数据】
今日日期：{today}；全市场成交额：{turnover}万亿；上证今日涨跌幅：{today_pct}
"""


def ai_judge(force: bool = False) -> dict:
    """判断今日 中级周期+多空转。每日缓存；force=True 重新判断。"""
    today = datetime.now().strftime("%Y-%m-%d")
    store = load_ai_predictions()
    if not force and today in store:
        return store[today]

    from openai import OpenAI
    if not cfg.QWEN_API_KEY:
        return {"error": "未配置 QWEN_API_KEY"}
    spec = NODE_SPEC.read_text(encoding="utf-8") if NODE_SPEC.exists() else ""
    idx = data.get_index_daily("sh000001", days=3)
    today_pct = "-"
    if len(idx) >= 2:
        today_pct = f"{(idx['收盘'].iloc[-1] / idx['收盘'].iloc[-2] - 1) * 100:+.2f}%"
    turnover = market_turnover_wanyi()
    prompt = JUDGE_PROMPT.format(
        spec=spec, review=_recent_review_rows(), kline=_index_features(),
        today=today, turnover=turnover if turnover is not None else "未知",
        today_pct=today_pct)
    try:
        client = OpenAI(api_key=cfg.QWEN_API_KEY, base_url=cfg.QWEN_BASE_URL)
        resp = client.chat.completions.create(
            model=cfg.QWEN_CHAT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1)
        raw = resp.choices[0].message.content.strip()
        m = re.search(r"\{.*\}", raw, re.S)
        result = json.loads(m.group(0) if m else raw)
    except Exception as e:
        logger.error("AI 择时判断失败：%s", e)
        return {"error": str(e)}
    result = {
        "mid_cycle": str(result.get("mid_cycle", "")).strip(),
        "signal": str(result.get("signal", "")).strip(),
        "position": str(result.get("position", "")).strip(),
        "reason": str(result.get("reason", "")).strip(),
        "time": datetime.now().strftime("%H:%M"),
        "turnover_wanyi": turnover,
    }
    hard = _position_rule(result["signal"], result["mid_cycle"])
    if hard:
        result["position"] = hard
    store[today] = result
    _save_ai_predictions(store)
    return result


def should_auto_judge() -> bool:
    """尾盘 14:57 后且今日尚无判断 → 自动触发。"""
    now = datetime.now()
    if now.strftime("%H:%M") < JUDGE_TIME or now.weekday() >= 5:
        return False
    return now.strftime("%Y-%m-%d") not in load_ai_predictions()


# ── 平均股价日K ───────────────────────────────────────────────────────────
def avg_price_kline(days: int = 360) -> pd.DataFrame:
    """全市场逐日算术平均 OHLC。依赖 390 日版指标缓存（缺列则返回空表提示重建）。"""
    cache = data.load_metrics_cache()
    if cache is None:
        cache = data.load_metrics_cache(allow_stale=True)
    cols = ["日期", "开盘", "最高", "最低", "收盘"]
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
