"""维度 3：指数择时信号 — AI判断 vs 复盘表标注对比。"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from eval.common import load_jsonl, save_result

logger = logging.getLogger("eval.timing")

_AI_CACHE = Path(__file__).resolve().parent.parent / ".data" / "timing_ai.json"


def _norm_date(s: str) -> str:
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s


def _load_ai_cache() -> dict:
    if not _AI_CACHE.exists():
        return {}
    raw = json.loads(_AI_CACHE.read_text(encoding="utf-8"))
    return {_norm_date(k): v for k, v in raw.items()}


def run(target_accuracy: float = 0.6) -> dict:
    cases = load_jsonl("timing_cases.jsonl")[:100]
    if not cases:
        return {"error": "无评测数据", "pass": 0, "fail": 0}

    ai_cache = _load_ai_cache()
    results = {
        "total": len(cases),
        "matched_signal": 0,
        "correct_signal": 0,
        "matched_cycle": 0,
        "correct_cycle": 0,
        "pass": 0,
        "fail": 0,
    }

    signal_dist = {}
    details = []

    for case in cases:
        date = _norm_date(case.get("date", ""))
        expected_sig = case.get("expected_signal", "")
        expected_cyc = case.get("expected_cycle", "")

        if expected_sig:
            signal_dist[expected_sig] = signal_dist.get(expected_sig, 0) + 1

        ai = ai_cache.get(date)
        if not ai:
            continue

        ai_sig = ai.get("signal", "")
        ai_cyc = ai.get("mid_cycle", "")

        if ai_sig:
            results["matched_signal"] += 1
            sig_match = (ai_sig == expected_sig) or (expected_sig in ai_sig)
            if sig_match:
                results["correct_signal"] += 1

        if ai_cyc:
            results["matched_cycle"] += 1
            cyc_match = (ai_cyc == expected_cyc) or (expected_cyc in ai_cyc)
            if cyc_match:
                results["correct_cycle"] += 1

        if ai_sig or ai_cyc:
            status = "pass" if (
                (ai_sig and (ai_sig == expected_sig or expected_sig in ai_sig))
                or (ai_cyc and (ai_cyc == expected_cyc or expected_cyc in ai_cyc))
            ) else "fail"
            details.append({
                "date": date,
                "expected_signal": expected_sig,
                "ai_signal": ai_sig,
                "expected_cycle": expected_cyc,
                "ai_cycle": ai_cyc,
                "status": status,
            })

    results["signal_distribution"] = dict(sorted(signal_dist.items(), key=lambda x: -x[1]))
    results["details"] = details

    total_matched = max(results["matched_signal"], results["matched_cycle"])
    if total_matched > 0:
        sig_acc = results["correct_signal"] / results["matched_signal"] if results["matched_signal"] else 0
        cyc_acc = results["correct_cycle"] / results["matched_cycle"] if results["matched_cycle"] else 0
        avg_acc = (sig_acc + cyc_acc) / 2 if (results["matched_signal"] and results["matched_cycle"]) else max(sig_acc, cyc_acc)
        results["accuracy"] = avg_acc
        results["signal_accuracy"] = sig_acc
        results["cycle_accuracy"] = cyc_acc

        results["pass"] = results["correct_signal"] + results["correct_cycle"]
        results["fail"] = (
            (results["matched_signal"] - results["correct_signal"])
            + (results["matched_cycle"] - results["correct_cycle"])
        )

        results["note"] = (
            f"AI缓存匹配 {total_matched}/{results['total']} 天。"
            f"信号准确率 {sig_acc:.1%}({results['correct_signal']}/{results['matched_signal']})，"
            f"周期准确率 {cyc_acc:.1%}({results['correct_cycle']}/{results['matched_cycle']})。"
        )
    else:
        results["note"] = (
            f"AI缓存中无匹配日期（缓存 {len(ai_cache)} 天，数据集 {results['total']} 天）。"
            f"需持续运行 ai_judge() 积累每日预测数据。"
        )
        results["pass"] = 0
        results["fail"] = 0

    save_result("timing", results)
    return results
