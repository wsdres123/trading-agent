"""维度 8：性能基准 — 关键操作延迟和吞吐测量。"""
from __future__ import annotations

import time

from eval.common import benchmark, save_result


def run() -> dict:
    from src import data

    results = {"pass": 0, "fail": 0, "benchmarks": []}

    def _bench(name: str, fn, *args, threshold_ms: float, **kwargs):
        ms = benchmark(fn, *args, **kwargs)
        ok = ms <= threshold_ms
        results["benchmarks"].append({
            "name": name,
            "avg_ms": round(ms, 1),
            "threshold_ms": threshold_ms,
            "status": "pass" if ok else "fail",
        })
        if ok:
            results["pass"] += 1
        else:
            results["fail"] += 1

    _bench("get_stock_spot (Redis命中)", data.get_stock_spot, threshold_ms=200)
    _bench("get_index_spot (Redis命中)", data.get_index_spot, threshold_ms=200)
    _bench("get_hot_stocks (Redis命中)", data.get_hot_stocks, 10, threshold_ms=100)
    _bench("get_stock_quote_fast", data.get_stock_quote_fast, "000001", threshold_ms=500)

    try:
        from src import knowledge
        _bench("knowledge.search", knowledge.search, "趋势A周期", threshold_ms=3000)
    except Exception as e:
        results["benchmarks"].append({"name": "knowledge.search", "error": str(e), "status": "error"})
        results["fail"] += 1

    try:
        from src.ai_assistant import parse_filter_conditions
        _bench("parse_filter_conditions (本地关键字)", parse_filter_conditions,
               "百日新高且成交额大于30亿", threshold_ms=50)
    except Exception as e:
        results["benchmarks"].append({"name": "parse_filter_conditions", "error": str(e), "status": "error"})
        results["fail"] += 1

    try:
        from src.redis_cache import redis_get, redis_set
        redis_set("eval:bench:ping", {"v": 1}, 60)
        _bench("redis_set + redis_get", lambda: redis_get("eval:bench:ping"), threshold_ms=10)
    except Exception as e:
        results["benchmarks"].append({"name": "redis roundtrip", "error": str(e), "status": "error"})
        results["fail"] += 1

    save_result("benchmark", results)
    return results
