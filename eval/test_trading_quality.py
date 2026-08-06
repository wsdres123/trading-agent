"""Layer 3 — 真实交易质量评测。

从 .data/predictions.jsonl 加载预测记录，计算/回填未来收益，然后按任务评估：

  timing  → sh000001 上证指数 1/3/5 日方向命中率（多→涨 / 空→跌 / 转→变）
  emotion → 883958 连板指数 1/3/5 日平均溢价
  shortterm → 883958 连板指数 1/3/5 日收益 + 失败率（负收益=失败）
"""
from __future__ import annotations

import logging

from eval.common import save_result

logger = logging.getLogger("eval.trading_quality")


def run() -> dict:
    """运行真实交易质量评测。"""
    from src import predictions

    # 先回填缺失的 future_outcome
    try:
        updated = predictions.update_future_outcomes()
        if updated:
            logger.info("回填 %d 条预测的 future_outcome", updated)
    except Exception as e:
        logger.warning("回填 future_outcome 失败: %s", e)

    records = predictions.load_predictions()
    results = {
        "pass": 0, "fail": 0,
        "total": len(records),
        "timing": {}, "emotion": {}, "shortterm": {},
    }

    # 按类型分组
    by_type: dict[str, list[dict]] = {}
    for r in records:
        t = r.get("type", "unknown")
        by_type.setdefault(t, []).append(r)

    # ── timing: 方向命中率 ──────────────────────────────────────────
    timing_preds = by_type.get("timing", [])
    timing_stats = _eval_timing(timing_preds)
    results["timing"] = timing_stats

    # ── emotion: 883958 平均溢价 ────────────────────────────────────
    emotion_preds = by_type.get("emotion", [])
    emotion_stats = _eval_emotion(emotion_preds)
    results["emotion"] = emotion_stats

    # ── shortterm: 收益 + 失败率 ───────────────────────────────────
    short_preds = by_type.get("shortterm", [])
    short_stats = _eval_shortterm(short_preds)
    results["shortterm"] = short_stats

    total_eval = (
        timing_stats.get("evaluated", 0)
        + emotion_stats.get("evaluated", 0)
        + short_stats.get("evaluated", 0)
    )
    results["pass"] = total_eval
    results["fail"] = len(records) - total_eval

    results["note"] = (
        f"共 {len(records)} 条预测，"
        f"timing {len(timing_preds)} / emotion {len(emotion_preds)} / "
        f"shortterm {len(short_preds)}。"
        f"已评估 {total_eval} 条。")
    save_result("trading_quality", results)
    return results


def _eval_timing(preds: list[dict]) -> dict:
    """timing 预测：1/3/5 日方向命中率。"""
    stats = {"total": len(preds), "evaluated": 0}
    for period in ("d1", "d3", "d5"):
        stats[period] = {"hits": 0, "total": 0, "rate": 0.0}

    for p in preds:
        fo = p.get("future_outcome")
        if not fo or not isinstance(fo, dict):
            continue
        hits = fo.get("direction_hit", {})
        if not hits:
            continue
        stats["evaluated"] += 1
        for period in ("d1", "d3", "d5"):
            h = hits.get(period)
            if h is not None:
                stats[period]["total"] += 1
                if h:
                    stats[period]["hits"] += 1

    for period in ("d1", "d3", "d5"):
        t = stats[period]["total"]
        if t > 0:
            stats[period]["rate"] = round(stats[period]["hits"] / t * 100, 1)

    return stats


def _eval_emotion(preds: list[dict]) -> dict:
    """emotion 预测：883958 1/3/5 日平均溢价。"""
    stats = {"total": len(preds), "evaluated": 0}
    for period in ("d1", "d3", "d5"):
        stats[period] = {"returns": [], "avg": 0.0}

    for p in preds:
        fo = p.get("future_outcome")
        if not fo or not isinstance(fo, dict):
            continue
        returns = fo.get("returns", {})
        if not returns:
            continue
        stats["evaluated"] += 1
        for period in ("d1", "d3", "d5"):
            r = returns.get(period)
            if r and isinstance(r, dict):
                stats[period]["returns"].append(r.get("return_pct", 0))

    for period in ("d1", "d3", "d5"):
        rets = stats[period]["returns"]
        if rets:
            stats[period]["avg"] = round(sum(rets) / len(rets), 2)

    return stats


def _eval_shortterm(preds: list[dict]) -> dict:
    """shortterm 预测：883958 1/3/5 日收益 + 失败率。"""
    stats = {"total": len(preds), "evaluated": 0}
    for period in ("d1", "d3", "d5"):
        stats[period] = {"returns": [], "avg": 0.0, "failure_rate": 0.0}

    for p in preds:
        fo = p.get("future_outcome")
        if not fo or not isinstance(fo, dict):
            continue
        returns = fo.get("returns", {})
        if not returns:
            continue
        stats["evaluated"] += 1
        for period in ("d1", "d3", "d5"):
            r = returns.get(period)
            if r and isinstance(r, dict):
                stats[period]["returns"].append(r.get("return_pct", 0))

    for period in ("d1", "d3", "d5"):
        rets = stats[period]["returns"]
        if rets:
            stats[period]["avg"] = round(sum(rets) / len(rets), 2)
            failures = sum(1 for r in rets if r < 0)
            stats[period]["failure_rate"] = round(failures / len(rets) * 100, 1)

    return stats
