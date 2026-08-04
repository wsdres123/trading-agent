"""维度 4：选股筛选 NLP 解析 — 验证自然语言→JSON 条件转换。"""
from __future__ import annotations

from eval.common import load_jsonl, save_result


def _conditions_match(actual: list, expected: list) -> tuple[bool, str]:
    """检查实际解析结果是否覆盖所有期望条件。"""
    if not expected:
        return True, ""
    misses = []
    for exp in expected:
        found = False
        for act in actual:
            if act.get("field") != exp.get("field"):
                continue
            ok = True
            for k, v in exp.items():
                if k == "field":
                    continue
                if act.get(k) != v:
                    ok = False
                    break
            if ok:
                found = True
                break
        if not found:
            misses.append(exp)
    if misses:
        return False, f"漏解析: {misses}"
    return True, ""


def run() -> dict:
    cases = load_jsonl("filter_nlp.jsonl")
    if not cases:
        return {"error": "无评测数据", "pass": 0, "fail": 0}

    from src.ai_assistant import parse_filter_conditions

    correct = 0
    total = len(cases)
    details = []

    for case in cases:
        text = case["input"]
        expected = case["expected"]
        try:
            result = parse_filter_conditions(text)
            actual = result.get("conditions", [])
            ok, reason = _conditions_match(actual, expected)
            if ok:
                correct += 1
                details.append({"input": text, "status": "pass"})
            else:
                details.append({"input": text, "status": "fail", "reason": reason, "actual": actual})
        except Exception as e:
            details.append({"input": text, "status": "error", "error": str(e)})

    accuracy = correct / total if total else 0
    results = {
        "total": total,
        "correct": correct,
        "accuracy": round(accuracy, 4),
        "pass": correct,
        "fail": total - correct,
        "details": [d for d in details if d["status"] != "pass"],
    }

    save_result("filter_nlp", results)
    return results
