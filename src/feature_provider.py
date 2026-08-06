"""统一特征构建器：确保线上与回测使用完全相同的特征构建逻辑。

核心原则：
- 线上和回测只允许数据来源不同，不允许决策逻辑、prompt、模型版本不同
- 所有特征构建函数统一接口：输入日期，输出特征字典
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import pandas as pd

from src import data, index_timing

logger = logging.getLogger(__name__)


class FeatureProvider:
    """统一特征构建器，支持实时模式和历史回测模式。"""

    def __init__(self, date_str: Optional[str] = None):
        """
        Args:
            date_str: 指定日期（YYYY-MM-DD）。None 表示使用实时数据。
        """
        self.date_str = date_str
        self.is_realtime = date_str is None

    def get_index_features(self, days: int = 12) -> str:
        """获取指数K线特征（格式化文本）。"""
        if self.is_realtime:
            return index_timing._index_features(days=days)

        # 回测模式：从历史数据切片
        idx = data.get_index_daily("sh000001", days=days + 50)
        if idx.empty:
            return "（指数数据不可用）"

        idx = idx[idx["日期"] <= self.date_str].tail(days + 1).copy()
        if len(idx) < 2:
            return "（指数数据不足）"

        idx["涨跌幅"] = (idx["收盘"] / idx["收盘"].shift(1) - 1) * 100
        lines = []
        for _, r in idx.iterrows():
            pct = f"{r['涨跌幅']:+.2f}%" if pd.notna(r["涨跌幅"]) else "-"
            lines.append(
                f"{r['日期']} 开{r['开盘']:.0f} 高{r['最高']:.0f} "
                f"低{r['最低']:.0f} 收{r['收盘']:.0f} 涨跌{pct}")
        return "\n".join(lines[-days:])

    def get_review_rows(self, n: int = 10) -> str:
        """获取复盘表近N行（不含当日）。"""
        if self.is_realtime:
            return index_timing._recent_review_rows(n=n)

        # 回测模式
        hist = index_timing.load_history_signals()
        if hist.empty:
            return "（无历史记录）"
        before = hist[hist["日期"] < self.date_str].tail(n)
        if before.empty:
            return "（无历史记录）"
        return "\n".join(
            f"{r['日期']} {r['信号']} {r['中级周期']} {r['情绪周期']}"
            for _, r in before.iterrows())

    def get_avg_price_status(self) -> str:
        """获取平均股价与多空线(MA10)关系。"""
        if self.is_realtime:
            return index_timing._avg_price_status()

        # 回测模式：从 metrics_cache 构建
        cache = data.load_metrics_cache(allow_stale=True)
        if cache is None or not {"opens", "lows", "closes", "highs"}.issubset(cache.columns):
            return "（平均股价数据不可用）"

        # 构建历史平均股价序列
        import numpy as np
        def mat(col: str) -> np.ndarray | None:
            arrs = [a if a is not None and len(a) else [] for a in cache[col].tolist()]
            L = max((len(a) for a in arrs), default=0)
            if L == 0:
                return None
            M = np.full((len(arrs), L), np.nan)
            for i, a in enumerate(arrs):
                M[i, L - len(a):] = a
            return M

        C = mat("closes")
        if C is None or C.shape[1] < 30:
            return "（平均股价数据不足）"

        # 获取日期轴
        idx = data.get_index_daily("sh000001", days=C.shape[1] + 30)
        dates = idx["日期"].tolist() if not idx.empty else []
        last_date = None
        if "last_date" in cache.columns:
            try:
                last_date = str(cache["last_date"].dropna().mode().iloc[0])[:10]
            except Exception:
                pass

        if dates and last_date in dates:
            end_idx = dates.index(last_date) + 1
            target_idx = None
            for i, d in enumerate(dates[:end_idx]):
                if str(d) >= self.date_str:
                    target_idx = i
                    break
            if target_idx is None:
                return "（目标日期数据不可用）"

            n_cols = C.shape[1]
            col_offset = end_idx - n_cols  # 矩阵第 0 列在 dates 中的索引
            target_col = target_idx - col_offset
            if target_col < 0 or target_col >= n_cols:
                return "（目标日期在缓存范围外）"

            # 取目标日期及之前20天，严格按矩阵列索引切片，避免用末端未来数据
            start_col = max(0, target_col - 19)
            seg_dates = dates[start_col + col_offset:target_col + col_offset + 1]
            closes = np.nanmean(C[:, start_col:target_col + 1], axis=0)
        else:
            return "（日期对齐失败）"

        if len(closes) < 11:
            return "（平均股价数据不足）"

        # 计算MA10
        ma10 = pd.Series(closes).rolling(10, min_periods=10).mean()
        close = float(closes[-1])
        ma_val = ma10.iloc[-1]
        if pd.isna(ma_val):
            return "（多空线数据不足）"
        ma_val = float(ma_val)
        date = str(seg_dates[-1])
        pos = "上方" if close > ma_val else "下方"
        cross = ""
        if len(closes) >= 2 and pd.notna(ma10.iloc[-2]):
            prev_above = float(closes[-2]) > float(ma10.iloc[-2])
            curr_above = close > ma_val
            if not prev_above and curr_above:
                cross = "，今日上穿多空线"
            elif prev_above and not curr_above:
                cross = "，今日下穿多空线"
        return f"{date} 平均股价{close:.2f}，多空线(MA10){ma_val:.2f}，股价在多空线{pos}{cross}"

    def get_turnover_wanyi(self) -> Optional[float]:
        """获取全市场成交额（万亿元）。"""
        if self.is_realtime:
            return index_timing.market_turnover_wanyi()

        # 回测模式：从竞价表读取
        # TODO: 从竞价表CSV读取当日量能字段
        return None

    def get_emotion_node_status(self) -> str:
        """获取今日情绪节点状态（仅实时模式）。"""
        if self.is_realtime:
            return index_timing._emotion_node_status()
        return "（回测模式暂不注入情绪节点）"

    def get_today_pct(self) -> str:
        """获取今日涨跌幅。"""
        if self.is_realtime:
            idx = data.get_index_daily("sh000001", days=3)
            if len(idx) >= 2:
                return f"{(idx['收盘'].iloc[-1] / idx['收盘'].iloc[-2] - 1) * 100:+.2f}%"
            return "-"

        # 回测模式
        idx = data.get_index_daily("sh000001", days=50)
        idx = idx[idx["日期"] <= self.date_str]
        if len(idx) >= 2:
            last = float(idx["收盘"].iloc[-1])
            prev = float(idx["收盘"].iloc[-2])
            return f"{(last / prev - 1) * 100:+.2f}%"
        return "-"

    def build_timing_features(self) -> dict:
        """构建指数择时所需的全部特征。"""
        return {
            "review": self.get_review_rows(),
            "kline": self.get_index_features(),
            "avg_status": self.get_avg_price_status(),
            "emotion_node": self.get_emotion_node_status(),
            "turnover": self.get_turnover_wanyi(),
            "today_pct": self.get_today_pct(),
            "today": self.date_str if not self.is_realtime else datetime.now().strftime("%Y-%m-%d"),
        }
