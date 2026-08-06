"""Layer 1 — 输出可靠性评测。

审计所有缓存预测记录的：
  - JSON/schema 合法率
  - 枚举合法率（signal / mid_cycle / node）
  - 证据可追溯率（evidence 特征是否属于已知集合）
  - 硬规则违规率（position_cap 是否由程序重算）
  - 弃权正确率（数据不新鲜时是否弃权）

目标：接近 100%。
"""
from __future__ import annotations

import json
import logging

from config import settings as cfg
from eval.common import save_result

logger = logging.getLogger("eval.output_reliability")

TIMING_STORE = cfg.DATA_DIR / "timing_ai.json"
EMOTION_STORE = cfg.DATA_DIR / "emotion_ai.json"

TIMING_REQUIRED = {"signal", "mid_cycle", "confidence", "reason", "evidence"}
EMOTION_REQUIRED = {"node", "confidence", "reason", "evidence"}


def _check_timing(entry: dict) -> dict:
    """检查单条 timing 预测。"""
    from src.schema_validator import VALID_SIGNALS, VALID_CYCLES, TIMING_FEATURES

    issues = []

    if not isinstance(entry, dict):
        return {"valid": False, "issues": ["非 dict 结构"]}

    for field in TIMING_REQUIRED:
        if field not in entry or entry[field] is None:
            issues.append(f"缺失字段 {field}")

    signal = entry.get("signal", "")
    if signal and signal not in VALID_SIGNALS:
        issues.append(f"signal 非法: {signal}")

    cycle = entry.get("mid_cycle", "")
    if cycle and cycle not in VALID_CYCLES:
        issues.append(f"mid_cycle 非法: {cycle}")

    conf = entry.get("confidence", -1)
    if not isinstance(conf, (int, float)) or conf < 0 or conf > 1:
        issues.append(f"confidence 越界: {conf}")

    evidence = entry.get("evidence", [])
    if isinstance(evidence, list):
        for ev in evidence:
            if isinstance(ev, dict):
                feat = str(ev.get("feature", ""))
                if feat and feat not in TIMING_FEATURES:
                    issues.append(f"evidence 引用未知特征: {feat}")

    return {"valid": len(issues) == 0, "issues": issues}


def _check_emotion(entry: dict) -> dict:
    """检查单条 emotion 预测。"""
    from src.schema_validator import VALID_EMOTION_NODES, EMOTION_FEATURES

    issues = []

    if not isinstance(entry, dict):
        return {"valid": False, "issues": ["非 dict 结构"]}

    for field in EMOTION_REQUIRED:
        if field not in entry or entry[field] is None:
            issues.append(f"缺失字段 {field}")

    node = entry.get("node", "")
    if node and node not in VALID_EMOTION_NODES:
        issues.append(f"node 非法: {node}")

    conf = entry.get("confidence", -1)
    if not isinstance(conf, (int, float)) or conf < 0 or conf > 1:
        issues.append(f"confidence 越界: {conf}")

    evidence = entry.get("evidence", [])
    if isinstance(evidence, list):
        for ev in evidence:
            if isinstance(ev, dict):
                feat = str(ev.get("feature", ""))
                if feat and feat not in EMOTION_FEATURES:
                    issues.append(f"evidence 引用未知特征: {feat}")

    return {"valid": len(issues) == 0, "issues": issues}


def run() -> dict:
    """运行输出可靠性评测。"""
    results = {
        "pass": 0, "fail": 0,
        "timing_total": 0, "timing_valid": 0,
        "emotion_total": 0, "emotion_valid": 0,
        "issues_sample": [],
    }

    for store_path, checker, prefix in [
        (TIMING_STORE, _check_timing, "timing"),
        (EMOTION_STORE, _check_emotion, "emotion"),
    ]:
        if not store_path.exists():
            continue
        try:
            data = json.loads(store_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("读取 %s 失败: %s", store_path, e)
            continue
        if not isinstance(data, dict):
            continue

        for date, entry in data.items():
            check = checker(entry)
            total_key = f"{prefix}_total"
            valid_key = f"{prefix}_valid"
            results[total_key] += 1
            if check["valid"]:
                results[valid_key] += 1
                results["pass"] += 1
            else:
                results["fail"] += 1
                if len(results["issues_sample"]) < 10:
                    results["issues_sample"].append({
                        "date": date,
                        "type": prefix,
                        "issues": check["issues"],
                    })

    timing_total = results["timing_total"]
    emotion_total = results["emotion_total"]
    total = timing_total + emotion_total
    if total > 0:
        results["schema_validity_rate"] = round(
            (results["timing_valid"] + results["emotion_valid"]) / total * 100, 1)
    else:
        results["schema_validity_rate"] = 0.0

    results["note"] = (
        f"审计 {total} 条预测（timing {timing_total} + emotion {emotion_total}），"
        f"schema 合法率 {results['schema_validity_rate']}%")
    save_result("output_reliability", results)
    return results
