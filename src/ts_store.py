"""DuckDB-backed time series store for K-line and snapshot data.

将历史K线从 Redis 迁移到本地 parquet + DuckDB，Redis 只存最新实时快照。
目录结构：
  .data/ts/index_daily/{symbol}.parquet
  .data/ts/ths_index/{code}.parquet
  .data/ts/snapshots/{YYYY-MM-DD}.parquet
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import pandas as pd

from config import settings as cfg

logger = logging.getLogger("ts_store")

_store = None


class TimeSeriesStore:
    """DuckDB-backed time series store."""

    def __init__(self, base_dir: Path):
        self._base = base_dir
        self._idx_dir = base_dir / "index_daily"
        self._ths_dir = base_dir / "ths_index"
        self._snap_dir = base_dir / "snapshots"
        for d in (self._idx_dir, self._ths_dir, self._snap_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._conn = None

    def _get_conn(self):
        if self._conn is None:
            try:
                import duckdb
                self._conn = duckdb.connect(str(self._base / "ts.duckdb"))
            except Exception as e:
                logger.warning("DuckDB unavailable, ts_store degraded: %s", e)
                return None
        return self._conn

    # ── 指数日K ──────────────────────────────────────────────────────────
    def save_index_daily(self, symbol: str, df: pd.DataFrame) -> None:
        """保存指数日K到 parquet（覆盖写入）。"""
        if df.empty:
            return
        path = self._idx_dir / f"{symbol}.parquet"
        try:
            df.to_parquet(path, index=False)
        except Exception as e:
            logger.warning("save_index_daily %s failed: %s", symbol, e)

    def load_index_daily(self, symbol: str, days: int = 380) -> Optional[pd.DataFrame]:
        """从 parquet 读取指数日K。数据不足时返回 None。"""
        path = self._idx_dir / f"{symbol}.parquet"
        if not path.exists():
            return None
        try:
            df = pd.read_parquet(path)
            if df.empty or len(df) < days * 0.8:
                return None
            return df.tail(days).reset_index(drop=True)
        except Exception as e:
            logger.debug("load_index_daily %s failed: %s", symbol, e)
            return None

    def index_daily_freshness(self, symbol: str) -> Optional[str]:
        """返回本地 parquet 最后一条日期（YYYY-MM-DD），不存在返回 None。"""
        path = self._idx_dir / f"{symbol}.parquet"
        if not path.exists():
            return None
        try:
            df = pd.read_parquet(path, columns=["日期"])
            return str(df["日期"].iloc[-1]).strip() if not df.empty else None
        except Exception:
            return None

    # ── 同花顺指数日K ────────────────────────────────────────────────────
    def save_ths_index(self, code: str, df: pd.DataFrame) -> None:
        if df.empty:
            return
        path = self._ths_dir / f"{code}.parquet"
        try:
            df.to_parquet(path, index=False)
        except Exception as e:
            logger.warning("save_ths_index %s failed: %s", code, e)

    def load_ths_index(self, code: str, days: int = 200) -> Optional[pd.DataFrame]:
        path = self._ths_dir / f"{code}.parquet"
        if not path.exists():
            return None
        try:
            df = pd.read_parquet(path)
            if df.empty:
                return None
            return df.tail(days).reset_index(drop=True)
        except Exception:
            return None

    # ── 每日快照 ─────────────────────────────────────────────────────────
    def save_daily_snapshot(self, date: str, df: pd.DataFrame) -> None:
        """保存全市场收盘快照到 parquet。"""
        if df.empty:
            return
        path = self._snap_dir / f"{date}.parquet"
        try:
            df.to_parquet(path, index=False)
            logger.info("Daily snapshot saved: %s (%d rows)", date, len(df))
        except Exception as e:
            logger.warning("save_daily_snapshot %s failed: %s", date, e)

    def has_daily_snapshot(self, date: str) -> bool:
        return (self._snap_dir / f"{date}.parquet").exists()

    def load_daily_snapshot(self, date: str) -> Optional[pd.DataFrame]:
        path = self._snap_dir / f"{date}.parquet"
        if not path.exists():
            return None
        try:
            return pd.read_parquet(path)
        except Exception:
            return None

    # ── DuckDB SQL 查询 ──────────────────────────────────────────────────
    def query(self, sql: str) -> pd.DataFrame:
        """执行 DuckDB SQL（可直接查询 parquet 文件）。"""
        conn = self._get_conn()
        if conn is None:
            return pd.DataFrame()
        try:
            return conn.execute(sql).fetchdf()
        except Exception as e:
            logger.warning("DuckDB query failed: %s", e)
            return pd.DataFrame()

    # ── 向量化计算（DuckDB SQL 直接在 parquet 上算）─────────────────────

    _INDEX_NAMES = {
        "sh000001": "上证指数", "sz399001": "深证成指",
        "sz399006": "创业板指", "sh000688": "科创50",
        "sh000300": "沪深300",
    }

    def get_index_name(self, symbol: str) -> str:
        """返回指数中文名，未知代码返回代码本身。"""
        return self._INDEX_NAMES.get(symbol, symbol)

    def calc_ma(self, symbol: str, period: int = 20) -> Optional[float]:
        """N日均线（DuckDB向量化SQL）。"""
        path = self._idx_dir / f"{symbol}.parquet"
        if not path.exists():
            return None
        conn = self._get_conn()
        if conn is None:
            return None
        try:
            sql = (
                f"SELECT AVG(\"收盘\") AS ma FROM ("
                f"SELECT \"收盘\", ROW_NUMBER() OVER (ORDER BY \"日期\" DESC) AS rn"
                f" FROM '{path}') WHERE rn <= {int(period)}"
            )
            df = conn.execute(sql).fetchdf()
            if df.empty or df["ma"].iloc[0] is None:
                return None
            return float(df["ma"].iloc[0])
        except Exception as e:
            logger.warning("calc_ma %s period=%d failed: %s", symbol, period, e)
            return None

    def calc_returns(self, symbol: str, days: int = 20) -> Optional[float]:
        """N日涨跌幅（百分比）。"""
        path = self._idx_dir / f"{symbol}.parquet"
        if not path.exists():
            return None
        conn = self._get_conn()
        if conn is None:
            return None
        try:
            n = int(days) + 1
            sql = (
                f"WITH ranked AS ("
                f"SELECT \"收盘\", ROW_NUMBER() OVER (ORDER BY \"日期\" DESC) AS rn"
                f" FROM '{path}')"
                f" SELECT"
                f"  (SELECT \"收盘\" FROM ranked WHERE rn = 1) AS latest,"
                f"  (SELECT \"收盘\" FROM ranked WHERE rn = {n}) AS base"
            )
            df = conn.execute(sql).fetchdf()
            if df.empty or df["base"].iloc[0] is None or df["latest"].iloc[0] is None:
                return None
            latest, base = float(df["latest"].iloc[0]), float(df["base"].iloc[0])
            return ((latest / base) - 1) * 100 if base > 0 else None
        except Exception as e:
            logger.warning("calc_returns %s days=%d failed: %s", symbol, days, e)
            return None

    def calc_volatility(self, symbol: str, days: int = 20) -> Optional[float]:
        """N日年化波动率（百分比）。"""
        path = self._idx_dir / f"{symbol}.parquet"
        if not path.exists():
            return None
        conn = self._get_conn()
        if conn is None:
            return None
        try:
            sql = (
                f"WITH returns AS ("
                f"SELECT \"收盘\", LAG(\"收盘\", 1) OVER (ORDER BY \"日期\") AS prev, \"日期\""
                f" FROM '{path}'),"
                f" daily_ret AS ("
                f"SELECT (\"收盘\" / prev - 1) AS ret"
                f" FROM returns WHERE prev IS NOT NULL"
                f" ORDER BY \"日期\" DESC LIMIT {int(days)})"
                f" SELECT STDDEV_SAMP(ret) * SQRT(252) * 100 AS vol FROM daily_ret"
            )
            df = conn.execute(sql).fetchdf()
            if df.empty or df["vol"].iloc[0] is None:
                return None
            return float(df["vol"].iloc[0])
        except Exception as e:
            logger.warning("calc_volatility %s days=%d failed: %s", symbol, days, e)
            return None

    def get_index_stats(self, symbol: str) -> Optional[dict]:
        """指数综合统计：收盘、MA5/10/20/60、5日/20日涨幅、百日高低（单条SQL）。"""
        path = self._idx_dir / f"{symbol}.parquet"
        if not path.exists():
            return None
        conn = self._get_conn()
        if conn is None:
            return None
        try:
            sql = (
                f"WITH ranked AS ("
                f"SELECT \"日期\", \"收盘\", \"最高\", \"最低\","
                f" ROW_NUMBER() OVER (ORDER BY \"日期\" DESC) AS rn"
                f" FROM '{path}')"
                f" SELECT"
                f"  (SELECT \"日期\" FROM ranked WHERE rn = 1) AS dt,"
                f"  (SELECT \"收盘\" FROM ranked WHERE rn = 1) AS close,"
                f"  (SELECT AVG(\"收盘\") FROM ranked WHERE rn <= 5) AS ma5,"
                f"  (SELECT AVG(\"收盘\") FROM ranked WHERE rn <= 10) AS ma10,"
                f"  (SELECT AVG(\"收盘\") FROM ranked WHERE rn <= 20) AS ma20,"
                f"  (SELECT AVG(\"收盘\") FROM ranked WHERE rn <= 60) AS ma60,"
                f"  (SELECT \"收盘\" FROM ranked WHERE rn = 6) AS base_5d,"
                f"  (SELECT \"收盘\" FROM ranked WHERE rn = 21) AS base_20d,"
                f"  (SELECT MAX(\"最高\") FROM ranked WHERE rn <= 100) AS high_100d,"
                f"  (SELECT MIN(\"最低\") FROM ranked WHERE rn <= 100) AS low_100d"
            )
            df = conn.execute(sql).fetchdf()
            if df.empty:
                return None
            r = df.iloc[0]

            def _f(val):
                return float(val) if val is not None else None

            close = _f(r["close"])
            base_5d = _f(r["base_5d"])
            base_20d = _f(r["base_20d"])
            return {
                "date": str(r["dt"]).strip(),
                "close": close,
                "ma5": _f(r["ma5"]),
                "ma10": _f(r["ma10"]),
                "ma20": _f(r["ma20"]),
                "ma60": _f(r["ma60"]),
                "ret_5d": ((close / base_5d - 1) * 100) if close and base_5d else None,
                "ret_20d": ((close / base_20d - 1) * 100) if close and base_20d else None,
                "high_100d": _f(r["high_100d"]),
                "low_100d": _f(r["low_100d"]),
            }
        except Exception as e:
            logger.warning("get_index_stats %s failed: %s", symbol, e)
            return None


def get_store() -> TimeSeriesStore:
    """获取全局 TimeSeriesStore 单例。"""
    global _store
    if _store is None:
        _store = TimeSeriesStore(cfg.TS_DIR)
    return _store
