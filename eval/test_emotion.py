"""维度 2：情绪节点 — AI判断 vs 复盘表标注对比。"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from eval.common import load_jsonl, save_result

logger = logging.getLogger("eval.emotion")

_AI_CACHE = Path(__file__).resolve().parent.parent / ".data" / "emotion_ai.json"


def _norm_date(s: str) -> str:
    """统一日期为 YYYY-MM-DD。"""
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s


_NODE_ALIAS = {
    "弱修复": "修复--弱", "中等修复": "修复--中等", "强修复": "修复--强",
    "修复弱": "修复--弱", "修复中等": "修复--中等", "修复强": "修复--强",
    "加速修复": "修复--加速", "高潮修复": "修复--高潮",
    "主升分歧": "主升--分歧", "主升确认": "主升--确认",
    "主升加速": "主升--加速", "主升高潮": "主升--高潮",
    "主升延续": "主升--延续", "主升修复": "修复--弱",
    "分歧": "主升--分歧", "高潮": "主升--高潮", "延续": "主升--延续",
    "确认": "主升--确认", "加速": "主升--加速",
    "分歧转一致": "日内分歧转一致", "分歧未转一致": "日内分歧未转一致",
}


def _norm_node(s: str) -> str:
    return _NODE_ALIAS.get(s, s)


def _load_ai_cache() -> dict:
    if not _AI_CACHE.exists():
        return {}
    raw = json.loads(_AI_CACHE.read_text(encoding="utf-8"))
    return {_norm_date(k): v for k, v in raw.items()}


def run(target_accuracy: float = 0.6) -> dict:
    cases = load_jsonl("emotion_cases.jsonl")[:100]
    if not cases:
        return {"error": "无评测数据", "pass": 0, "fail": 0}

    ai_cache = _load_ai_cache()
    results = {
        "total": len(cases),
        "matched": 0,
        "correct": 0,
        "wrong": 0,
        "pass": 0,
        "fail": 0,
    }

    node_dist = {}
    details = []

    for case in cases:
        expected = case.get("expected_node", "")
        date = _norm_date(case.get("date", ""))
        if expected:
            node_dist[expected] = node_dist.get(expected, 0) + 1

        ai = ai_cache.get(date)
        if not ai:
            continue
        ai_node = ai.get("node", "")
        if not ai_node:
            continue

        results["matched"] += 1
        ai_norm = _norm_node(ai_node)
        exp_norm = _norm_node(expected)
        match = (ai_norm == exp_norm) or (exp_norm in ai_norm) or (ai_norm in exp_norm)
        if match:
            results["correct"] += 1
            details.append({"date": date, "expected": expected, "ai": ai_node, "status": "pass"})
        else:
            results["wrong"] += 1
            details.append({"date": date, "expected": expected, "ai": ai_node, "status": "fail"})

    results["node_distribution"] = dict(sorted(node_dist.items(), key=lambda x: -x[1]))
    results["details"] = details

    if results["matched"] > 0:
        acc = results["correct"] / results["matched"]
        results["accuracy"] = acc
        results["pass"] = results["correct"]
        results["fail"] = results["wrong"]
        results["accuracy_note"] = (
            f"AI缓存匹配 {results['matched']}/{results['total']} 天，"
            f"准确率 {acc:.1%}（目标 {target_accuracy:.0%}）。"
            f"覆盖天数随每日运行自动增长。"
        )
    else:
        results["accuracy_note"] = (
            f"AI缓存中无匹配日期（缓存 {len(ai_cache)} 天，数据集 {results['total']} 天）。"
            f"需持续运行 ai_judge() 积累每日预测数据。"
        )
        results["pass"] = 0
        results["fail"] = 0

    save_result("emotion", results)
    return results
