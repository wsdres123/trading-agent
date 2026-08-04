"""维度 1：数据准确性 — 自动断言，无需标准答案。"""
from __future__ import annotations

import pandas as pd

from eval.common import save_result


def run() -> dict:
    """执行所有数据准确性检查，返回 {pass: N, fail: N, details: [...]}。"""
    results = {"pass": 0, "fail": 0, "details": []}

    def _check(name: str, fn):
        try:
            fn()
            results["pass"] += 1
            results["details"].append({"test": name, "status": "pass"})
        except AssertionError as e:
            results["fail"] += 1
            results["details"].append({"test": name, "status": "fail", "error": str(e)})
        except Exception as e:
            results["fail"] += 1
            results["details"].append({"test": name, "status": "error", "error": str(e)})

    def test_spot_columns():
        from src import data
        df = data.get_stock_spot()
        required = {"代码", "名称", "最新价", "涨跌幅", "成交额"}
        assert required.issubset(set(df.columns)), f"缺少字段: {required - set(df.columns)}"
        assert len(df) > 4000, f"A股数量不足: {len(df)}"

    def test_index_range():
        from src import data
        idx = data.get_index_spot()
        if idx.empty:
            return
        pct = pd.to_numeric(idx["涨跌幅"], errors="coerce").dropna()
        assert (pct.abs() < 12).all(), f"指数涨跌幅超±12%: {pct[pct.abs() >= 12].tolist()}"

    def test_hist_continuity():
        from src import data
        df = data.get_stock_hist("000001", days=60)
        if df is None or df.empty:
            return
        if "date" in df.columns:
            dates = pd.to_datetime(df["date"])
        elif "日期" in df.columns:
            dates = pd.to_datetime(df["日期"])
        else:
            return
        gaps = dates.diff().dt.days.dropna()
        assert (gaps <= 5).all(), f"日K线间隔异常(>5天): max gap={gaps.max()}"

    def test_hot_stocks_not_empty():
        from src import data
        df = data.get_hot_stocks(top=10)
        assert not df.empty, "热榜数据为空"
        assert "代码" in df.columns, "热榜缺少代码列"

    def test_redis_cache_works():
        from src.redis_cache import redis_get, redis_set
        test_key = "eval:test:ping"
        redis_set(test_key, {"ok": True}, 60)
        val = redis_get(test_key)
        assert val is not None, "Redis 读写失败"
        assert val.get("ok") is True

    _check("全市场快照字段完整性", test_spot_columns)
    _check("指数涨跌幅合理性", test_index_range)
    _check("日K线连续性", test_hist_continuity)
    _check("热榜数据可用性", test_hot_stocks_not_empty)
    _check("Redis缓存读写", test_redis_cache_works)

    save_result("data_accuracy", results)
    return results
