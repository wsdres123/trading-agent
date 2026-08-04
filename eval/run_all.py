"""评测汇总：运行所有维度并生成综合报告。

用法：
  python -m eval.run_all                  # 运行全部（跳过 LLM 调用类）
  python -m eval.run_all --all            # 含 LLM 调用（费用）
  python -m eval.run_all --regression     # 与上次结果回归对比
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.common import RESULTS_DIR

MODULES_NO_LLM = [
    ("数据准确性", "eval.test_data_accuracy"),
    ("情绪节点", "eval.test_emotion"),
    ("指数择时", "eval.test_timing"),
    ("筛选NLP解析", "eval.test_filter_nlp"),
    ("RAG检索质量", "eval.test_rag_recall"),
    ("性能基准", "eval.test_benchmark"),
]

MODULES_LLM = [
    ("LLM输出质量", "eval.test_llm_quality"),
    ("工具路由", "eval.test_tool_routing"),
]


def _run_module(name: str, module_path: str) -> dict:
    import importlib
    t0 = time.perf_counter()
    try:
        mod = importlib.import_module(module_path)
        result = mod.run()
        elapsed = round(time.perf_counter() - t0, 2)
        return {"name": name, "result": result, "elapsed_s": elapsed, "status": "ok"}
    except Exception as e:
        elapsed = round(time.perf_counter() - t0, 2)
        return {"name": name, "error": str(e), "elapsed_s": elapsed, "status": "error"}


def _load_latest_result(name_key: str) -> dict | None:
    """加载最近一次同名结果用于回归对比。"""
    files = sorted(RESULTS_DIR.glob(f"*_{name_key}.json"), reverse=True)
    # 跳过最新的（就是当前这次），取倒数第二个
    if len(files) >= 2:
        try:
            return json.loads(files[1].read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def main():
    parser = argparse.ArgumentParser(description="劫财AI交易 评测汇总")
    parser.add_argument("--all", action="store_true", help="包含 LLM 调用类评测（有费用）")
    parser.add_argument("--regression", action="store_true", help="与上次结果回归对比")
    args = parser.parse_args()

    modules = list(MODULES_NO_LLM)
    if args.all:
        modules.extend(MODULES_LLM)

    print("=" * 60)
    print("  劫财AI交易 — 评测报告")
    print(f"  时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  模块数: {len(modules)}")
    print("=" * 60)

    report = {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "modules": []}
    total_pass = 0
    total_fail = 0

    for name, module_path in modules:
        print(f"\n▶ {name}...", end=" ", flush=True)
        entry = _run_module(name, module_path)
        report["modules"].append(entry)

        r = entry.get("result", {})
        p = r.get("pass", 0)
        f = r.get("fail", 0)
        total_pass += p
        total_fail += f
        elapsed = entry["elapsed_s"]
        status = "✓" if entry["status"] == "ok" and f == 0 else "✗"
        print(f"{status} pass={p} fail={f} ({elapsed}s)")

        # 显示关键指标
        if "accuracy" in r:
            print(f"  准确率: {r['accuracy']:.1%}")
        if "recall_accuracy" in r:
            print(f"  召回准确率: {r['recall_accuracy']:.1%}")
        if "overall_avg" in r:
            print(f"  LLM 质量均分: {r['overall_avg']}/5")
        if "benchmarks" in r:
            for b in r["benchmarks"]:
                s = "✓" if b.get("status") == "pass" else "✗"
                print(f"  {s} {b['name']}: {b.get('avg_ms', '-')}ms (阈值 {b.get('threshold_ms', '-')}ms)")

        # 回归对比
        if args.regression and r:
            key_map = {
                "筛选NLP解析": "filter_nlp",
                "RAG检索质量": "rag_recall",
            }
            for label, key in key_map.items():
                if name == label:
                    prev = _load_latest_result(key)
                    if prev and "accuracy" in prev and "accuracy" in r:
                        delta = r["accuracy"] - prev["accuracy"]
                        flag = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
                        print(f"  回归: {flag} 上次 {prev['accuracy']:.1%} → 本次 {r['accuracy']:.1%}")

    print("\n" + "=" * 60)
    print(f"  总计: pass={total_pass} fail={total_fail}")
    print(f"  通过率: {total_pass / (total_pass + total_fail):.1%}" if (total_pass + total_fail) else "  无结果")
    print("=" * 60)

    # 保存汇总报告
    ts = time.strftime("%Y%m%d_%H%M%S")
    report_path = RESULTS_DIR / f"{ts}_summary.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n报告已保存: {report_path}")


if __name__ == "__main__":
    main()
