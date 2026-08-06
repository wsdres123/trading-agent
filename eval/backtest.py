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
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from config import settings as cfg
from src import index_timing, emotion_node, data, predictions

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


# 与 src/predictions.py 保持一致
PROMPT_VERSION = "v1.0"
FEATURE_VERSION = "v1.0"


def _call_llm(prompt: str, max_tokens: int = 500,
              models: list[str] | None = None) -> str:
    """调用 LLM 返回原始文本（供 schema_validator 校验）。

    支持模型降级链：默认 [qwen-plus, cfg.QWEN_CHAT_MODEL]，与生产 ai_judge 保持一致。
    统一走 src.llm_gateway，复用超时/重试/限流/token 统计。
    """
    from src.llm_gateway import call_llm
    if models is None:
        models = ["qwen-plus", cfg.QWEN_CHAT_MODEL]
    for use_model in models:
        raw = call_llm(
            prompt=prompt,
            model=use_model,
            temperature=0.1,
            max_tokens=max_tokens,
            timeout=30.0,
            retries=0,
        )
        if raw is not None:
            return raw, use_model
        logger.warning("  模型 %s 调用失败", use_model)
    raise RuntimeError("LLM 全部模型调用失败")


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

    today = datetime.now().strftime("%Y-%m-%d")
    hist = hist[hist["日期"] != today]
    targets = hist.tail(days + 10).head(days)

    store = _load_store(TIMING_STORE)
    existing = set(store.keys())
    todo = [r for _, r in targets.iterrows() if r["日期"] not in existing]

    if not todo:
        logger.info("择时回测: 无新增（已有 %d 天缓存）", len(existing))
        return {"skipped": len(existing)}

    logger.info("择时回测: %d 天待处理（使用统一特征构建器 + qwen-plus）", len(todo))
    correct_sig, correct_cyc, total, errors = 0, 0, 0, 0

    # 导入统一特征构建器和校验器
    from src.feature_provider import FeatureProvider
    from src.schema_validator import validate_timing_decision

    for i, row in enumerate(todo):
        date = row["日期"]
        expected_sig = row.get("信号", "")
        expected_cyc = str(row.get("中级周期", ""))

        # 使用统一特征构建器（回测模式）
        provider = FeatureProvider(date_str=date)
        features = provider.build_timing_features()

        # ── 防止未来信息泄漏校验 ──
        try:
            idx_daily = data.get_index_daily("sh000001", days=5)
            if not idx_daily.empty:
                idx_daily = idx_daily[idx_daily["日期"].astype(str).str[:10] <= date]
                last_date = str(idx_daily.iloc[-1]["日期"])[:10]
                if last_date and last_date > date:
                    logger.warning("  %s 泄漏警告: 特征数据包含未来日期 %s", date, last_date)
        except Exception:
            pass

        prompt = index_timing.JUDGE_PROMPT.format(**features)

        try:
            raw, model_used = _call_llm(prompt, models=["qwen-plus", cfg.QWEN_CHAT_MODEL])
            validated = validate_timing_decision(raw, position_rule_fn=index_timing._position_rule)
            store[date] = {
                "mid_cycle": validated["mid_cycle"],
                "signal": validated["signal"],
                "position": validated["position"],
                "reason": validated["reason"],
                "confidence": validated["confidence"],
                "abstain": validated.get("abstain", False),
                "evidence": validated["evidence"],
                "invalidation": validated.get("invalidation", []),
                "position_cap": validated["position_cap"],
                "time": "15:00",
                "turnover_wanyi": features.get("turnover", 0),
                "prompt_version": PROMPT_VERSION,
                "feature_version": FEATURE_VERSION,
                "model_id": model_used,
                "source": "backtest",
            }

            total += 1
            if store[date]["signal"] == expected_sig:
                correct_sig += 1
            if store[date]["mid_cycle"] == expected_cyc:
                correct_cyc += 1

            # 持久化到预测日志（供 Layer 3 评测）
            try:
                predictions.append_prediction(
                    type="timing", date=date, signal=store[date]["signal"],
                    confidence=store[date]["confidence"],
                    evidence_snapshot=store[date]["evidence"],
                    position_cap=store[date]["position_cap"],
                    abstain=store[date]["abstain"],
                    model_id=model_used,
                    source="backtest",
                    extra={"mid_cycle": store[date]["mid_cycle"]},
                )
            except Exception as e:
                logger.debug("预测持久化失败 %s: %s", date, e)
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

# ── 完整 raw_label → canonical_label 映射 ──
# 覆盖竞价表 43 种 + 复盘表 37 种原始标签
_RAW_LABEL_MAP = {
    # 修复类别名
    "弱修复": "修复--弱", "中等修复": "修复--中等", "强修复": "修复--强",
    "修复弱": "修复--弱", "修复中等": "修复--中等", "修复强": "修复--强",
    "加速修复": "修复--加速", "高潮修复": "修复--高潮", "修复高潮": "修复--高潮",
    "中等局部修复": "修复--中等",
    "反弹第一天": "修复--弱",
    "反弹加速": "修复--加速",
    "反弹加速转衰竭": "加速转衰竭",
    "修复后反杀": "退潮",
    # 主升类别名
    "主升分歧": "主升--分歧", "主升确认": "主升--确认",
    "主升加速": "主升--加速", "主升高潮": "主升--高潮",
    "主升延续": "主升--延续", "主升修复": "修复--弱",
    "主升-延续": "主升--延续", "主升-加速转衰竭": "加速转衰竭",
    "主升": "主升--确认", "情绪主升": "主升--确认",
    "9板以上补涨加速期": "主升--加速",
    "癫狂": "主升--高潮",
    "高潮": "主升--高潮",
    # 退潮类别名
    "大退潮": "退潮加速", "小退潮": "退潮",
    # 加速类
    "分歧转一致": "日内分歧转一致", "分歧未转一致": "日内分歧未转一致",
}

# ── 一级分类定义 ──
_PRIMARY_CATEGORIES = ["主升", "修复", "混沌", "退潮", "冰点", "加速", "日内一致"]

_PRIMARY_MAP = {}
for _n in emotion_node.NODE_NAMES:
    if "主升" in _n:
        _PRIMARY_MAP[_n] = "主升"
    elif "修复" in _n or _n == "修复延续":
        _PRIMARY_MAP[_n] = "修复"
    elif "混沌" in _n:
        _PRIMARY_MAP[_n] = "混沌"
    elif "退潮" in _n:
        _PRIMARY_MAP[_n] = "退潮"
    elif "冰点" in _n:
        _PRIMARY_MAP[_n] = "冰点"
    elif "加速" in _n and "修复" not in _n and "主升" not in _n:
        _PRIMARY_MAP[_n] = "加速"
    elif "分歧转一致" in _n or "分歧未转一致" in _n:
        _PRIMARY_MAP[_n] = "日内一致"
    elif _n in ("龙头确认", "短线情绪确认"):
        _PRIMARY_MAP[_n] = "主升"
    else:
        _PRIMARY_MAP[_n] = "其他"


def canonicalize(raw: str) -> str | None:
    """将原始标签规范化为生产节点名。处理双标签、别名。无法映射时返回 None。"""
    raw = raw.strip()
    if not raw:
        return None

    valid_set = set(emotion_node.NODE_NAMES)

    if raw in valid_set:
        return raw

    if raw in _RAW_LABEL_MAP:
        return _RAW_LABEL_MAP[raw]

    if "," in raw or "，" in raw:
        parts = [p.strip() for p in raw.replace("，", ",").split(",")]
        for p in parts:
            c = canonicalize(p)
            if c and c in valid_set:
                return c
        return None

    for c in valid_set:
        if c in raw or raw in c:
            return c

    return None


def primary_of(node: str) -> str:
    """返回节点的一级分类。"""
    return _PRIMARY_MAP.get(node, "其他")


def _node_match(ai: str, exp: str) -> bool:
    """向后兼容的节点匹配（用于回测循环内快速判断）。"""
    if ai == exp or exp in ai or ai in exp:
        return True
    ai_c = canonicalize(ai)
    exp_c = canonicalize(exp)
    if ai_c and exp_c:
        return ai_c == exp_c
    return False


# ── 评测指标计算 ──

def _compute_metrics(predictions: dict, ground_truth: dict) -> dict:
    """计算情绪节点评测的完整指标。

    Args:
        predictions: {日期: 预测节点}
        ground_truth: {日期: 标准答案节点（已规范化）}

    Returns:
        包含覆盖率、一级/二级 Macro-F1、混淆矩阵、高风险错误率、弃权比例的指标字典
    """
    from collections import Counter

    common_dates = sorted(set(predictions.keys()) & set(ground_truth.keys()))
    total_gt = len(ground_truth)
    n = len(common_dates)

    if n == 0:
        return {"coverage": 0, "evaluated": 0, "total_gt": total_gt}

    y_true = [ground_truth[d] for d in common_dates]
    y_pred = [predictions[d] for d in common_dates]

    # ── 二级精确匹配 ──
    correct_2 = sum(1 for t, p in zip(y_true, y_pred) if t == p)
    accuracy_2 = correct_2 / n

    # ── 一级匹配 ──
    y_true_1 = [primary_of(t) for t in y_true]
    y_pred_1 = [primary_of(p) for p in y_pred]
    correct_1 = sum(1 for t, p in zip(y_true_1, y_pred_1) if t == p)
    accuracy_1 = correct_1 / n

    # ── Macro-F1（二级） ──
    all_nodes_2 = sorted(set(y_true) | set(y_pred))
    f1_scores_2 = {}
    for node in all_nodes_2:
        tp = sum(1 for t, p in zip(y_true, y_pred) if t == node and p == node)
        fp = sum(1 for t, p in zip(y_true, y_pred) if t != node and p == node)
        fn = sum(1 for t, p in zip(y_true, y_pred) if t == node and p != node)
        prec = tp / (tp + fp) if (tp + fp) else 0
        rec = tp / (tp + fn) if (tp + fn) else 0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0
        f1_scores_2[node] = f1
    macro_f1_2 = sum(f1_scores_2.values()) / len(f1_scores_2) if f1_scores_2 else 0

    # ── Macro-F1（一级） ──
    all_nodes_1 = sorted(set(y_true_1) | set(y_pred_1))
    f1_scores_1 = {}
    for cat in all_nodes_1:
        tp = sum(1 for t, p in zip(y_true_1, y_pred_1) if t == cat and p == cat)
        fp = sum(1 for t, p in zip(y_true_1, y_pred_1) if t != cat and p == cat)
        fn = sum(1 for t, p in zip(y_true_1, y_pred_1) if t == cat and p != cat)
        prec = tp / (tp + fp) if (tp + fp) else 0
        rec = tp / (tp + fn) if (tp + fn) else 0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0
        f1_scores_1[cat] = f1
    macro_f1_1 = sum(f1_scores_1.values()) / len(f1_scores_1) if f1_scores_1 else 0

    # ── 混淆矩阵（二级 top-10 错误对） ──
    confusion = Counter()
    for t, p in zip(y_true, y_pred):
        if t != p:
            confusion[(t, p)] += 1
    top_confusion = confusion.most_common(10)

    # ── 高风险错误率 ──
    # 高风险错误：真实=退潮/冰点 但预测=主升/修复（进攻信号），或反之
    HIGH_RISK_PAIRS = [
        ({"退潮", "冰点"}, {"主升", "修复"}),
        ({"主升", "修复"}, {"退潮", "冰点"}),
    ]
    high_risk_errors = 0
    high_risk_total = 0
    for t, p in zip(y_true, y_pred):
        t1, p1 = primary_of(t), primary_of(p)
        if t1 == p1:
            continue
        for danger_set, wrong_set in HIGH_RISK_PAIRS:
            if t1 in danger_set and p1 in wrong_set:
                high_risk_errors += 1
                break
        if t1 in {"退潮", "冰点", "主升", "修复"}:
            high_risk_total += 1
    high_risk_rate = high_risk_errors / high_risk_total if high_risk_total else 0

    return {
        "coverage": n / total_gt if total_gt else 0,
        "evaluated": n,
        "total_gt": total_gt,
        "accuracy_1": round(accuracy_1, 4),
        "accuracy_2": round(accuracy_2, 4),
        "macro_f1_1": round(macro_f1_1, 4),
        "macro_f1_2": round(macro_f1_2, 4),
        "f1_by_primary": {k: round(v, 4) for k, v in sorted(f1_scores_1.items())},
        "f1_by_node": {k: round(v, 4) for k, v in sorted(f1_scores_2.items()) if v > 0},
        "top_confusion": [(f"{t}→{p}", c) for (t, p), c in top_confusion],
        "high_risk_error_rate": round(high_risk_rate, 4),
        "high_risk_errors": high_risk_errors,
        "high_risk_total": high_risk_total,
    }


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

    # 构建标准答案集（仅保留可规范化的标签）
    ground_truth = {}
    unmapped = {}
    for _, row in auction.iterrows():
        d = row["日期_norm"]
        raw_label = str(row.get("节点", "")).strip()
        if not raw_label:
            continue
        canon = canonicalize(raw_label)
        if canon:
            ground_truth[d] = canon
        else:
            unmapped[raw_label] = unmapped.get(raw_label, 0) + 1
    if unmapped:
        logger.warning("无法映射的标签: %s", unmapped)

    store = _load_store(EMOTION_STORE)
    existing = set(store.keys())
    todo = [r for _, r in targets.iterrows()
            if r["日期_norm"] not in existing and r["日期_norm"] in ground_truth]

    if not todo:
        logger.info("情绪回测: 无新增（已有 %d 天缓存）", len(existing))
    else:
        logger.info("情绪回测: %d 天待处理（标准答案 %d 天，覆盖率目标 %d 天）",
                     len(todo), len(ground_truth), days)
        correct, total, errors = 0, 0, 0

        for i, row in enumerate(todo):
            date = row["日期_norm"]
            expected = ground_truth[date]

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

            # 从 metrics_cache 回算当日和前日盘面统计（按时点股票池过滤，消除幸存者偏差）
            pool_df = data.get_point_in_time_pool(str(date))
            pool_codes = set(pool_df["代码"].astype(str)) if not pool_df.empty else None
            mstats = emotion_node.historical_market_stats(date, pool_codes=pool_codes)
            mstats_str = ""
            if mstats:
                mstats_str = (f"今日盘面: 大面{mstats['大面数']} 涨超7:{mstats['涨超7数']} "
                              f"跌停{mstats['跌停数']} 涨停{mstats['涨停数']} "
                              f"打板大面{mstats['打板大面数']}")
            prev_date_row = auction[auction["日期_norm"] < date].tail(1)
            prev_mstats_str = ""
            pm = None
            if not prev_date_row.empty:
                prev_d = prev_date_row.iloc[0]["日期_norm"]
                pool_prev = data.get_point_in_time_pool(str(prev_d))
                pool_codes_prev = set(pool_prev["代码"].astype(str)) if not pool_prev.empty else None
                pm = emotion_node.historical_market_stats(prev_d, pool_codes=pool_codes_prev)
                if pm:
                    prev_mstats_str = (f"前日盘面: 大面{pm['大面数']} 涨超7:{pm['涨超7数']} "
                                       f"跌停{pm['跌停数']} 涨停{pm['涨停数']} "
                                       f"打板大面{pm['打板大面数']}")

            # 近5日盘面统计序列
            recent_dates = auction[auction["日期_norm"] < date].tail(5)["日期_norm"].tolist()
            recent_stats_parts = []
            for rd in recent_dates:
                pool_rd = data.get_point_in_time_pool(str(rd))
                pool_codes_rd = set(pool_rd["代码"].astype(str)) if not pool_rd.empty else None
                rm = emotion_node.historical_market_stats(rd, pool_codes=pool_codes_rd)
                if rm:
                    recent_stats_parts.append(
                        f"{rd} 大面{rm['大面数']} 跌停{rm['跌停数']} 涨停{rm['涨停数']}")
            recent_stats_str = "\n".join(recent_stats_parts) if recent_stats_parts else ""

            # 前日竞价表的大面数
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

            # 构建 prompt 所需变量
            from src.schema_validator import validate_emotion_decision
            _db = mstats.get("打板大面数") if mstats else None
            if _db is not None and _db >= 10:
                daban_constraint = f"打板大面{_db}个，禁强修复（上限中等修复）且禁主升--确认"
            elif _db is not None:
                daban_constraint = f"打板大面{_db}个，无限制"
            else:
                daban_constraint = "打板大面数据不可用"

            compare_parts = []
            if prev_mstats_str:
                compare_parts.append(prev_mstats_str)
            if idx_desc:
                compare_parts.append(f"指数{idx_desc}")
            if prev_damian:
                compare_parts.append(prev_damian.strip())
            if today_str:
                compare_parts.append(today_str)
            if rv_str:
                compare_parts.append(rv_str)
            compare_str = "\n".join(compare_parts) if compare_parts else ""

            prompt = emotion_node.JUDGE_PROMPT.format(
                nodes="、".join(node_names),
                history=hist_text, recent=recent_stats_str,
                today=date, daban_constraint=daban_constraint,
                stats=mstats_str, hot="（回测无热榜数据）",
                compare=compare_str)

            # ── 防止未来信息泄漏校验 ──
            if mstats:
                stats_date = str(mstats.get("日期", ""))[:10]
                if stats_date and stats_date > date:
                    logger.warning("  %s 泄漏警告: market stats 来自未来 %s", date, stats_date)

            try:
                raw, model_used = _call_llm(prompt, models=["qwen-plus", cfg.QWEN_CHAT_MODEL])
                validated = validate_emotion_decision(raw, stats=mstats)
                ai_node = validated["node"]
                store[date] = {
                    "node": ai_node,
                    "reason": validated["reason"],
                    "advice": validated["advice"],
                    "confidence": validated["confidence"],
                    "abstain": validated.get("abstain", False),
                    "evidence": validated["evidence"],
                    "stats": mstats,
                    "prev_stats": pm if not prev_date_row.empty else {},
                    "time": "15:00",
                    "prompt_version": PROMPT_VERSION,
                    "feature_version": FEATURE_VERSION,
                    "model_id": model_used,
                    "source": "backtest",
                }
                total += 1
                if _node_match(ai_node, expected):
                    correct += 1

                # 持久化到预测日志（供 Layer 3 评测）
                try:
                    predictions.append_prediction(
                        type="emotion", date=date, node=ai_node,
                        confidence=validated["confidence"],
                        evidence_snapshot=validated["evidence"],
                        abstain=validated.get("abstain", False),
                        model_id=model_used,
                        source="backtest",
                    )
                except Exception as e:
                    logger.debug("预测持久化失败 %s: %s", date, e)
            except Exception as e:
                logger.warning("  %s 失败: %s", date, e)
                errors += 1

            if (i + 1) % 10 == 0:
                logger.info("  进度 %d/%d (准确率 %.0f%%)",
                            i + 1, len(todo),
                            correct / total * 100 if total else 0)
            time.sleep(0.3)

        _save_store(EMOTION_STORE, store)

    # ── 评测指标（基于日期交集，含全量缓存） ──
    predictions = {}
    abstain_count = 0
    for d, v in store.items():
        if v.get("source") != "backtest":
            continue
        predictions[d] = v.get("node", "混沌")
        if v.get("abstain"):
            abstain_count += 1

    metrics = _compute_metrics(predictions, ground_truth)
    metrics["abstain_ratio"] = (abstain_count / len(predictions)
                                if predictions else 0)
    metrics["total_cached"] = len(store)
    metrics["unmapped_labels"] = unmapped

    logger.info("情绪回测评测:")
    logger.info("  覆盖率: %d/%d = %.1f%%",
                metrics["evaluated"], metrics["total_gt"],
                metrics["coverage"] * 100)
    logger.info("  一级准确率: %.1f%%  Macro-F1: %.4f",
                metrics.get("accuracy_1", 0) * 100, metrics.get("macro_f1_1", 0))
    logger.info("  二级准确率: %.1f%%  Macro-F1: %.4f",
                metrics.get("accuracy_2", 0) * 100, metrics.get("macro_f1_2", 0))
    logger.info("  高风险错误率: %.1f%% (%d/%d)",
                metrics.get("high_risk_error_rate", 0) * 100,
                metrics.get("high_risk_errors", 0),
                metrics.get("high_risk_total", 0))
    if metrics.get("top_confusion"):
        logger.info("  Top-5 混淆:")
        for pair, cnt in metrics["top_confusion"][:5]:
            logger.info("    %s: %d", pair, cnt)
    if metrics.get("f1_by_primary"):
        logger.info("  一级 F1: %s", metrics["f1_by_primary"])

    return metrics


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
        if name == "emotion" and "evaluated" in r:
            print(f"  覆盖率: {r.get('evaluated', 0)}/{r.get('total_gt', 0)}"
                  f" = {r.get('coverage', 0):.1%}")
            print(f"  一级准确率: {r.get('accuracy_1', 0):.1%}  "
                  f"Macro-F1: {r.get('macro_f1_1', 0):.4f}")
            print(f"  二级准确率: {r.get('accuracy_2', 0):.1%}  "
                  f"Macro-F1: {r.get('macro_f1_2', 0):.4f}")
            print(f"  高风险错误率: {r.get('high_risk_error_rate', 0):.1%}"
                  f" ({r.get('high_risk_errors', 0)}/{r.get('high_risk_total', 0)})")
            print(f"  弃权比例: {r.get('abstain_ratio', 0):.1%}")
            if r.get("f1_by_primary"):
                print(f"  一级 F1: {r['f1_by_primary']}")
            if r.get("top_confusion"):
                print("  Top-5 混淆:")
                for pair, cnt in r["top_confusion"][:5]:
                    print(f"    {pair}: {cnt}")
            if r.get("unmapped_labels"):
                print(f"  未映射标签: {r['unmapped_labels']}")
        else:
            for k, v in r.items():
                if isinstance(v, float):
                    print(f"  {k}: {v:.1%}")
                elif isinstance(v, (dict, list)):
                    print(f"  {k}: {v}")
                else:
                    print(f"  {k}: {v}")
