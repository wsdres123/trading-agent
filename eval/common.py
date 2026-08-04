"""eval 模块公共工具：加载 JSONL、计时、结果记录。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

DATASETS_DIR = Path(__file__).parent / "datasets"
RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)


def load_jsonl(name: str) -> list[dict]:
    """加载 datasets/ 下的 JSONL 文件。"""
    path = DATASETS_DIR / name
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def benchmark(fn, *args, repeats: int = 3, **kwargs) -> float:
    """返回 fn 平均执行时间（ms）。"""
    times = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn(*args, **kwargs)
        times.append((time.perf_counter() - t0) * 1000)
    return sum(times) / len(times)


def save_result(name: str, metrics: dict[str, Any]) -> Path:
    """保存单次评测结果到 results/{timestamp}_{name}.json。"""
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = RESULTS_DIR / f"{ts}_{name}.json"
    path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
