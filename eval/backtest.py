"""回测：对历史日期调用 LLM 判断情绪节点/指数择时，写入 _ai.json 缓存。

用法:
    python -m eval.backtest              # 回测最近100天
    python -m eval.backtest --days 50    # 回测最近50天
    python -m eval.backtest --force      # 清除旧回测数据重新跑
    python -m eval.backtest --only timing   # 仅择时
    python -m eval.backtest --only emotion  # 仅情绪
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from config import settings as cfg
from src import index_timing, emotion_node, data

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("eval.backtest")

ROOT = Path(__file__).resolve().parent.parent
TIMING_STORE = ROOT / ".data" / "timing_ai.json"
EMOTION_STORE = ROOT / ".data" / "emotion_ai.json"
REVIEW_CSV = cfg.KNOWLEDGE_DIR / "屠龙表 - 复盘表.csv"
AUCTION_CSV = cfg.KNOWLEDGE_DIR / "屠龙表 - 竞价表.csv"


def _norm_date(s: str) -> str:
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(s).strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return str(s).strip()


def _load_store(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _save_store(path: Path, d: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def _clear_backtest_entries(path: Path) -> int:
    """删除 source=backtest 的条目，保留真实 ai_judge 结果。"""
    store = _load_store(path)
    before = len(store)
    store = {k: v for k, v in store.items() if v.get("source") != "backtest"}
    removed = before - len(store)
    if removed:
        _save_store(path, store)
    return removed


def _call_llm(prompt: str, max_tokens: int = 300) -> dict:
    from openai import OpenAI
    client = OpenAI(api_key=cfg.QWEN_API_KEY, base_url=cfg.QWEN_BASE_URL)
    resp = client.chat.completions.create(
        model=cfg.QWEN_TURBO_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1, max_tokens=max_tokens,
    )
    raw = resp.choices[0].message.content.strip()
    m = re.search(r"\{.*\}", raw, re.S)
    return json.loads(m.group(0) if m else raw)


def _load_auction_full() -> pd.DataFrame:
    """加载完整竞价表（含所有列）。"""
    df = pd.read_csv(AUCTION_CSV, encoding="utf-8-sig")
    df.columns = [str(c).replace("\n", "").strip() for c in df.columns]
    df["日期_norm"] = df["日期"].astype(str).apply(_norm_date)
    return df[df["日期_norm"].str.match(r"\d{4}-\d{2}-\d{2}")].copy()


def _load_review_full() -> pd.DataFrame:
    """加载完整复盘表（含所有列）。"""
    df = pd.read_csv(REVIEW_CSV)
    if "日期" not in df.columns:
        return pd.DataFrame()
    df["日期_norm"] = df["日期"].astype(str).apply(_norm_date)
    return df[df["日期_norm"].str.match(r"\d{4}-\d{2}-\d{2}")].copy()


# ── 指数择时回测 ──────────────────────────────────────────────────────────

def backtest_timing(days: int = 100, force: bool = False) -> dict:
    if force:
        n = _clear_backtest_entries(TIMING_STORE)
        logger.info("清除 %d 条旧择时回测", n)

    hist = index_timing.load_history_signals()
    if hist.empty:
        return {"error": "无历史数据"}

    idx = data.get_index_daily("sh000001", days=days + 30)
    if idx.empty:
        return {"error": "指数数据不可用"}

    idx["日期"] = idx["日期"].astype(str).str.strip()

    review = _load_review_full()
    review_map = {}
    for _, r in review.iterrows():
        d = r["日期_norm"]
        info = {}
        if pd.notna(r.get("指数特点")):
            info["指数特点"] = str(r["指数特点"])
        if pd.notna(r.get("大盘指数")):
            info["大盘指数"] = f"{float(r['大盘指数']):+.2f}%"
        if pd.notna(r.get("微盘指数")):
            info["微盘指数"] = f"{float(r['微盘指数']):+.2f}%"
        if pd.notna(r.get("日内强势题材")):
            info["强势题材"] = str(r["日内强势题材"])
        review_map[d] = info

    auction = _load_auction_full()
    auction_map = {}
    for _, r in auction.iterrows():
        d = r["日期_norm"]
        info = {}
        if pd.notna(r.get("量能")):
            info["量能"] = str(r["量能"])
        if pd.notna(r.get("大周期")):
            info["大周期"] = str(r["大周期"])
        auction_map[d] = info

    today = datetime.now().strftime("%Y-%m-%d")
    hist = hist[hist["日期"] != today]
    targets = hist.tail(days + 10).head(days)

    store = _load_store(TIMING_STORE)
    existing = set(store.keys())
    todo = [r for _, r in targets.iterrows() if r["日期"] not in existing]

    if not todo:
        logger.info("择时回测: 无新增（已有 %d 天缓存）", len(existing))
        return {"skipped": len(existing)}

    logger.info("择时回测: %d 天待处理", len(todo))
    correct_sig, correct_cyc, total, errors = 0, 0, 0, 0

    for i, row in enumerate(todo):
        date = row["日期"]
        expected_sig = row.get("信号", "")
        expected_cyc = str(row.get("中级周期", ""))

        idx_slice = idx[idx["日期"] <= date].tail(13).copy()
        if len(idx_slice) < 2:
            continue

        idx_slice["涨跌幅"] = (idx_slice["收盘"] / idx_slice["收盘"].shift(1) - 1) * 100
        kline_lines = []
        for _, r in idx_slice.iterrows():
            pct = f"{r['涨跌幅']:+.2f}%" if pd.notna(r["涨跌幅"]) else "-"
            kline_lines.append(
                f"{r['日期']} 开{r['开盘']:.0f} 高{r['最高']:.0f} "
                f"低{r['最低']:.0f} 收{r['收盘']:.0f} 涨跌{pct}")

        review_before = hist[hist["日期"] < date].tail(10)
        review_lines = "\n".join(
            f"{r['日期']} {r['信号']} {r['中级周期']} {r['情绪周期']}"
            for _, r in review_before.iterrows()) or "（无历史记录）"

        last_close = float(idx_slice["收盘"].iloc[-1])
        prev_close = float(idx_slice["收盘"].iloc[-2])
        today_pct = f"{(last_close / prev_close - 1) * 100:+.2f}%"

        rv = review_map.get(date, {})
        au = auction_map.get(date, {})
        extra = []
        if rv.get("指数特点"):
            extra.append(f"指数特点: {rv['指数特点']}")
        if rv.get("大盘指数"):
            extra.append(f"大盘指数: {rv['大盘指数']}")
        if rv.get("微盘指数"):
            extra.append(f"微盘指数: {rv['微盘指数']}")
        if rv.get("强势题材"):
            extra.append(f"强势题材: {rv['强势题材']}")
        if au.get("量能"):
            extra.append(f"成交额: {au['量能']}")
        if au.get("大周期"):
            extra.append(f"大周期: {au['大周期']}")
        extra_str = " ".join(extra) if extra else ""

        prompt = f"""A股指数择时。只输出JSON，不要多余文字：
{{"mid_cycle":"(从趋势A/龙头A/B/C/D中选一个)","signal":"多/空/转","position":"仓位一句话","reason":"依据40字内"}}
规则：量能≥2.5万亿连阳→趋势A/C；缩量阴跌→D；箱体→B；空+D必须空仓；空+B小仓。
signal延续复盘近期标注，除非量价明显反转。平均股价在多空线下方且未上穿时signal不能为转/多，应为空。

复盘表近况：
{review_lines}

上证近期K线：
{chr(10).join(kline_lines)}

今日{date} 涨跌{today_pct} {extra_str}
"""
        try:
            result = _call_llm(prompt)
            store[date] = {
                "mid_cycle": str(result.get("mid_cycle", "")).strip(),
                "signal": str(result.get("signal", "")).strip(),
                "position": str(result.get("position", "")).strip(),
                "reason": str(result.get("reason", "")).strip(),
                "source": "backtest",
            }
            total += 1
            if store[date]["signal"] == expected_sig:
                correct_sig += 1
            if store[date]["mid_cycle"] == expected_cyc:
                correct_cyc += 1
        except Exception as e:
            logger.warning("  %s 失败: %s", date, e)
            errors += 1

        if (i + 1) % 10 == 0:
            logger.info("  进度 %d/%d (信号 %.0f%%, 周期 %.0f%%)",
                        i + 1, len(todo),
                        correct_sig / total * 100 if total else 0,
                        correct_cyc / total * 100 if total else 0)
        time.sleep(0.3)

    _save_store(TIMING_STORE, store)
    result = {
        "backtested": total,
        "correct_signal": correct_sig,
        "correct_cycle": correct_cyc,
        "signal_accuracy": correct_sig / total if total else 0,
        "cycle_accuracy": correct_cyc / total if total else 0,
        "errors": errors,
        "total_cached": len(store),
    }
    logger.info("择时回测完成: %s", result)
    return result


# ── 情绪节点回测 ──────────────────────────────────────────────────────────

_NODE_ALIAS = {
    "弱修复": "修复--弱", "中等修复": "修复--中等", "强修复": "修复--强",
    "修复弱": "修复--弱", "修复中等": "修复--中等", "修复强": "修复--强",
    "加速修复": "修复--加速", "高潮修复": "修复--高潮",
    "主升分歧": "主升--分歧", "主升确认": "主升--确认",
    "主升加速": "主升--加速", "主升高潮": "主升--高潮",
    "主升延续": "主升--延续", "主升修复": "修复--弱",
    "分歧转一致": "日内分歧转一致", "分歧未转一致": "日内分歧未转一致",
}


def _node_match(ai: str, exp: str) -> bool:
    if ai == exp or exp in ai or ai in exp:
        return True
    ai_n = _NODE_ALIAS.get(ai, ai)
    exp_n = _NODE_ALIAS.get(exp, exp)
    return ai_n == exp_n or exp_n in ai_n or ai_n in exp_n


def backtest_emotion(days: int = 100, force: bool = False) -> dict:
    if force:
        n = _clear_backtest_entries(EMOTION_STORE)
        logger.info("清除 %d 条旧情绪回测", n)

    auction = _load_auction_full()
    if auction.empty:
        return {"error": "无竞价表数据"}

    review = _load_review_full()
    review_by_date = {}
    for _, r in review.iterrows():
        d = r["日期_norm"]
        info = {}
        if pd.notna(r.get("大盘指数")):
            info["大盘指数"] = f"{float(r['大盘指数']):+.2f}%"
        if pd.notna(r.get("微盘指数")):
            info["微盘指数"] = f"{float(r['微盘指数']):+.2f}%"
        if pd.notna(r.get("情绪节点")):
            info["复盘节点"] = str(r["情绪节点"])
        if pd.notna(r.get("指数")):
            info["指数描述"] = str(r["指数"])
        review_by_date[d] = info

    node_names = emotion_node.NODE_NAMES if hasattr(emotion_node, "NODE_NAMES") else []
    if not node_names:
        return {"error": "NODE_NAMES 不可用"}

    today = datetime.now().strftime("%Y-%m-%d")
    auction = auction[auction["日期_norm"] != today]
    targets = auction.tail(days + 10).head(days)

    store = _load_store(EMOTION_STORE)
    existing = set(store.keys())
    todo = [r for _, r in targets.iterrows() if r["日期_norm"] not in existing]

    if not todo:
        logger.info("情绪回测: 无新增（已有 %d 天缓存）", len(existing))
        return {"skipped": len(existing)}

    logger.info("情绪回测: %d 天待处理", len(todo))
    correct, total, errors = 0, 0, 0

    for i, row in enumerate(todo):
        date = row["日期_norm"]
        expected = str(row.get("节点", "")).strip()
        if not expected:
            continue

        # 竞价表：该日之前15条（含丰富字段）
        before = auction[auction["日期_norm"] < date].tail(15)
        hist_lines = []
        for _, r in before.iterrows():
            parts = [r["日期_norm"], str(r.get("节点", ""))]
            if pd.notna(r.get("指数")):
                parts.append(f"指数{r['指数']}")
            if pd.notna(r.get("小票亏效")):
                parts.append(f"小亏{r['小票亏效']}")
            if pd.notna(r.get("大票亏效")):
                parts.append(f"大亏{r['大票亏效']}")
            if pd.notna(r.get("一字")):
                parts.append(f"一字{r['一字']}")
            if pd.notna(r.get("断板")):
                parts.append(f"断板:{r['断板']}")
            hist_lines.append(" ".join(parts))
        hist_text = "\n".join(hist_lines) if hist_lines else "（无历史标注）"

        # 从 metrics_cache 回算当日和前日盘面统计
        mstats = emotion_node.historical_market_stats(date)
        mstats_str = ""
        if mstats:
            mstats_str = (f"今日盘面: 大面{mstats['大面数']} 涨超7:{mstats['涨超7数']} "
                          f"跌停{mstats['跌停数']} 涨停{mstats['涨停数']} "
                          f"打板大面{mstats['打板大面数']}")
        prev_date_row = auction[auction["日期_norm"] < date].tail(1)
        prev_mstats_str = ""
        if not prev_date_row.empty:
            prev_d = prev_date_row.iloc[0]["日期_norm"]
            pm = emotion_node.historical_market_stats(prev_d)
            if pm:
                prev_mstats_str = (f"前日盘面: 大面{pm['大面数']} 涨超7:{pm['涨超7数']} "
                                   f"跌停{pm['跌停数']} 涨停{pm['涨停数']} "
                                   f"打板大面{pm['打板大面数']}")

        # 近5日盘面统计序列（供趋势判断）
        recent_dates = auction[auction["日期_norm"] < date].tail(5)["日期_norm"].tolist()
        recent_stats_parts = []
        for rd in recent_dates:
            rm = emotion_node.historical_market_stats(rd)
            if rm:
                recent_stats_parts.append(
                    f"{rd} 大面{rm['大面数']} 跌停{rm['跌停数']} 涨停{rm['涨停数']}")
        recent_stats_str = "\n".join(recent_stats_parts) if recent_stats_parts else ""

        # 前日竞价表的大面数（用于修复类判断）
        prev_day = auction[auction["日期_norm"] < date].tail(1)
        prev_damian = ""
        if not prev_day.empty:
            pr = prev_day.iloc[0]
            for col in ["尾盘面数", "3分钟大面数", "竞价大面数"]:
                if pd.notna(pr.get(col)):
                    prev_damian += f" 前日{col}={int(pr[col])}"

        # 当日竞价表的信息
        idx_desc = str(row.get("指数", "")) if pd.notna(row.get("指数")) else ""
        today_extra = []
        if pd.notna(row.get("小票亏效")):
            today_extra.append(f"小票亏效:{row['小票亏效']}")
        if pd.notna(row.get("大票亏效")):
            today_extra.append(f"大票亏效:{row['大票亏效']}")
        if pd.notna(row.get("一字")):
            today_extra.append(f"一字:{row['一字']}")
        if pd.notna(row.get("断板")):
            today_extra.append(f"断板:{row['断板']}")
        for col in ["尾盘面数", "3分钟大面数", "竞价大面数"]:
            if pd.notna(row.get(col)):
                today_extra.append(f"{col}={int(row[col])}")
        if pd.notna(row.get("大周期")):
            today_extra.append(f"大周期:{row['大周期']}")
        today_str = " ".join(today_extra) if today_extra else ""

        # 复盘表补充
        rv = review_by_date.get(date, {})
        rv_parts = []
        if rv.get("大盘指数"):
            rv_parts.append(f"大盘{rv['大盘指数']}")
        if rv.get("微盘指数"):
            rv_parts.append(f"微盘{rv['微盘指数']}")
        rv_str = " ".join(rv_parts) if rv_parts else ""

        prompt = f"""A股情绪节点判断。只输出JSON：
{{"node":"节点名","reason":"依据40字内","advice":"操作提示一句"}}
节点选：{"、".join(node_names)}

核心规则（按优先级）：
0. 默认节点是混沌或混沌分歧。只有明确信号才能判其他节点。
1. 修复类判断：前日大面多(≥30)且今日大面骤减→修复--弱(减50%)、修复--中等(减70%)、修复--强(减90%)。打板大面≥10禁修复--强。前日非大面日不可判修复。
2. 退潮严格门槛（缺一不可）：今日大面>50 AND 跌停>10 AND 涨停数明显少于近期均值。仅大面高不等于退潮！大面高但涨停也多→混沌分歧。
3. 冰点：连续2日以上大面>100或跌停>20，市场极度恐慌。
4. 日内分歧转一致：当日涨停数较多(>80)但大面也不少(>30)，多空分歧大但最终多头占优。
5. 主升类(主升--确认/分歧/延续/加速/高潮)：要求连续多日涨停数>80且大面<30，市场整体强势。
6. 修复类次日不可跳到退潮加速。
7. 前日大面低基数(≤10)时按绝对值：大面<100且跌停<10→混沌或混沌分歧。
8. 拿不准一律选混沌。

竞价表历史（旧→新）：
{hist_text}

近日盘面统计：
{recent_stats_str}

{prev_mstats_str}
{mstats_str}

今日{date} 指数{idx_desc}{prev_damian}
{today_str}
{rv_str}
"""
        try:
            result = _call_llm(prompt)
            ai_node = str(result.get("node", "")).strip()
            store[date] = {
                "node": ai_node,
                "reason": str(result.get("reason", "")).strip(),
                "advice": str(result.get("advice", "")).strip(),
                "source": "backtest",
            }
            total += 1
            if _node_match(ai_node, expected):
                correct += 1
        except Exception as e:
            logger.warning("  %s 失败: %s", date, e)
            errors += 1

        if (i + 1) % 10 == 0:
            logger.info("  进度 %d/%d (准确率 %.0f%%)",
                        i + 1, len(todo),
                        correct / total * 100 if total else 0)
        time.sleep(0.3)

    _save_store(EMOTION_STORE, store)
    result = {
        "backtested": total,
        "correct": correct,
        "accuracy": correct / total if total else 0,
        "errors": errors,
        "total_cached": len(store),
    }
    logger.info("情绪回测完成: %s", result)
    return result


# ── CLI ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="回测情绪节点/指数择时")
    parser.add_argument("--days", type=int, default=100, help="回测天数")
    parser.add_argument("--only", choices=["timing", "emotion"], help="仅回测某一类")
    parser.add_argument("--force", action="store_true", help="清除旧回测数据重新跑")
    args = parser.parse_args()

    results = {}
    if args.only != "emotion":
        results["timing"] = backtest_timing(args.days, force=args.force)
    if args.only != "timing":
        results["emotion"] = backtest_emotion(args.days, force=args.force)

    print("\n=== 回测结果 ===")
    for name, r in results.items():
        print(f"\n{name}:")
        for k, v in r.items():
            if isinstance(v, float):
                print(f"  {k}: {v:.1%}")
            else:
                print(f"  {k}: {v}")
