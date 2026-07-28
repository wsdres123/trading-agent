"""AI 助手的实时行情工具集（function calling）。

把 data.py 的能力包装成一组模型可调用的工具：指数/个股/历史/板块/大盘总览。
每个工具返回「给模型阅读」的简洁文本，由模型据此组织最终回答。
"""
from __future__ import annotations

import json
import logging
from typing import Any

import pandas as pd

from src import data

logger = logging.getLogger("market_tools")

_MAIN_INDICES = ["上证指数", "深证成指", "创业板指", "科创50", "沪深300", "北证50"]


def _fmt_pct(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "-"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def _yi(v) -> str:
    try:
        return f"{float(v) / 1e8:,.0f}亿"
    except (TypeError, ValueError):
        return "-"


# ── 工具实现 ──────────────────────────────────────────────────────────────
def get_index_quote(name: str = "") -> str:
    """主要股指实时行情。name 留空返回主要指数列表。"""
    df = data.get_index_spot()
    if df.empty:
        return "实时指数数据获取失败，请稍后重试。"
    if name:
        hit = df[df["名称"].astype(str).str.contains(name, na=False, regex=False)]
        if hit.empty:
            return f"未找到包含「{name}」的指数。可选：{'、'.join(_MAIN_INDICES)}"
        df = hit
    else:
        df = df[df["名称"].isin(_MAIN_INDICES)] if "名称" in df else df
    lines = []
    for _, r in df.iterrows():
        lines.append(
            f"{r.get('名称','')} 最新价 {r.get('最新价','-')} "
            f"涨跌幅 {_fmt_pct(r.get('涨跌幅'))} 涨跌额 {r.get('涨跌额','-')} "
            f"成交额 {_yi(r.get('成交额'))}"
        )
    return "\n".join(lines) or "无指数数据"


def _local_name_table() -> pd.DataFrame | None:
    """本地代码/名称表：过期的指标缓存也可用（名称不会过期）。"""
    if data.METRICS_CACHE.exists():
        try:
            return pd.read_parquet(data.METRICS_CACHE,
                                   columns=["代码", "名称", "流通市值_亿"])
        except Exception:
            pass
    return None


def _resolve_codes(key: str) -> list[tuple[str, float | None]]:
    """名称/代码 → [(6位代码, 流通市值_亿), ...]。优先本地表，零联网。"""
    key = str(key).strip()
    if key.isdigit():
        return [(key.zfill(6), None)]
    for df in (_local_name_table(), data.get_stock_list()):
        if df is None or df.empty or "名称" not in df.columns:
            continue
        hit = df[df["名称"].astype(str).str.contains(key, na=False, regex=False)].head(5)
        if not hit.empty:
            cap = "流通市值_亿" if "流通市值_亿" in hit.columns else None
            return [(str(r["代码"]).zfill(6), r[cap] if cap else None)
                    for _, r in hit.iterrows()]
    return []


def get_stock_quote(code_or_name: str = "") -> str:
    """个股实时行情。可传代码或名称（支持模糊）。新浪单股直查，毫秒级。"""
    if not code_or_name:
        return "请提供股票代码或名称。"
    candidates = _resolve_codes(code_or_name)
    if not candidates:
        return f"未找到匹配「{code_or_name}」的个股。"
    lines = []
    for code, cap in candidates:
        q = data.get_stock_quote_fast(code)
        if not q:
            continue
        line = (
            f"{q['代码']} {q['名称']} 最新价 {q['最新价']} "
            f"涨跌幅 {_fmt_pct(q['涨跌幅'])} 涨跌额 {q['涨跌额']} "
            f"今开 {q['今开']} 最高 {q['最高']} 最低 {q['最低']} "
            f"成交额 {_yi(q['成交额'])}"
        )
        if cap is not None and pd.notna(cap):
            line += f" 流通市值 {cap}亿"
        lines.append(line)
    if not lines:
        return f"获取「{code_or_name}」实时行情失败，请稍后重试。"
    return "\n".join(lines)


def get_stock_history(code: str, days: int = 30) -> str:
    """个股近 N 日 K 线摘要：收盘/均线/涨幅/是否百日新高。"""
    code = str(code).strip().zfill(6)
    m = data.get_stock_metrics(code)
    if not m:
        return f"获取 {code} 历史数据失败。"
    parts = [
        f"{code} 最新收盘 {m['close']}",
        f"MA5={m['ma5']:.2f} MA10={m['ma10']:.2f} MA20={m['ma20']:.2f}",
    ]
    if m["ret_5d"] is not None:
        parts.append(f"5日涨幅 {_fmt_pct(m['ret_5d'])}")
    if m["ret_30d"] is not None:
        parts.append(f"30日涨幅 {_fmt_pct(m['ret_30d'])}")
    parts.append(f"近100日最高 {m['high_100d']:.2f} {'(今日收盘为百日新高)' if m['is_100d_new_high'] else ''}")
    if days not in (5, 30) and days > 0:
        ret = data._calc_nday_return(code, days)
        if ret is not None:
            parts.append(f"{days}日涨幅 {_fmt_pct(ret)}")
    return "；".join(parts)


def get_sector_quote(name: str = "") -> str:
    """板块行情。传 name 返回该板块及其成分前列；不传返回今日涨幅前列板块。"""
    if name:
        codes = data.sector_to_codes(name)
        if not codes:
            return f"未找到板块「{name}」。"
        spot = data.get_stock_spot()
        if spot.empty:
            return f"板块「{name}」含 {len(codes)} 只成分股，实时行情获取失败。"
        cons = spot[spot["代码"].astype(str).str.zfill(6).isin(codes)]
        up = int((cons["涨跌幅"] > 0).sum()) if "涨跌幅" in cons else 0
        down = int((cons["涨跌幅"] < 0).sum()) if "涨跌幅" in cons else 0
        top = cons.nlargest(8, "涨跌幅") if "涨跌幅" in cons else cons.head(8)
        lines = [f"板块「{name}」共 {len(cons)} 只，涨 {up} 跌 {down}", "涨幅前列："]
        for _, r in top.iterrows():
            lines.append(f"  {r.get('代码','')} {r.get('名称','')} {_fmt_pct(r.get('涨跌幅'))}")
        return "\n".join(lines)
    # 不传：返回行业板块涨幅前 10
    boards = data.get_industry_boards()
    if boards.empty:
        return "板块数据获取失败。"
    if "涨跌幅" in boards.columns:
        boards = boards.sort_values("涨跌幅", ascending=False)
    lines = ["今日行业板块涨幅前列："]
    for _, r in boards.head(10).iterrows():
        lines.append(
            f"  {r.get('板块名称','')} {_fmt_pct(r.get('涨跌幅'))} "
            f"上涨{r.get('上涨家数','-')} 下跌{r.get('下跌家数','-')}"
        )
    return "\n".join(lines)


def get_market_overview() -> str:
    """大盘总览：主要指数 + 涨跌家数 + 涨停/跌停 + 总成交额。"""
    idx = get_index_quote()
    spot = data.get_stock_spot()
    if spot.empty:
        return idx + "\n（个股明细获取失败）"
    up = int((spot["涨跌幅"] > 0).sum()) if "涨跌幅" in spot else 0
    down = int((spot["涨跌幅"] < 0).sum()) if "涨跌幅" in spot else 0
    flat = len(spot) - up - down
    zt = int((spot["涨跌幅"] >= 9.8).sum()) if "涨跌幅" in spot else 0
    dt = int((spot["涨跌幅"] <= -9.8).sum()) if "涨跌幅" in spot else 0
    total_amt = _yi(spot["成交额"].sum()) if "成交额" in spot else "-"
    return (
        f"{idx}\n"
        f"全 A：涨 {up} 跌 {down} 平 {flat}；涨停 {zt} 跌停 {dt}；"
        f"两市总成交额 {total_amt}"
    )


# ── 工具 schema（OpenAI function-calling 格式）──────────────────────────────
TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "get_index_quote",
        "description": "获取主要股指（上证指数/深证成指/创业板指/科创50/沪深300/北证50等）的实时行情。name 留空返回主要指数列表。",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "指数名称关键词，如 上证指数、创业板指"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "get_stock_quote",
        "description": "获取个股实时行情（最新价/涨跌幅/成交额/换手率/流通市值）。可传6位代码或名称。",
        "parameters": {"type": "object", "properties": {
            "code_or_name": {"type": "string", "description": "6位股票代码或名称关键词，如 600519 或 贵州茅台"}},
            "required": ["code_or_name"]}}},
    {"type": "function", "function": {
        "name": "get_stock_history",
        "description": "获取个股近 N 日 K 线摘要：收盘价、MA5/10/20、5日/30日涨幅、近100日最高、是否百日新高。",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string", "description": "6位股票代码"},
            "days": {"type": "integer", "description": "回看天数，默认30"}},
            "required": ["code"]}}},
    {"type": "function", "function": {
        "name": "get_sector_quote",
        "description": "获取板块行情。传 name 返回该板块成分股及涨幅前列；不传返回今日行业板块涨幅前列。",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "板块或概念名称，如 半导体、光通信"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "get_market_overview",
        "description": "获取大盘总览：主要指数、全市场涨跌家数、涨停/跌停数、两市总成交额。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
]

_DISPATCH = {
    "get_index_quote": get_index_quote,
    "get_stock_quote": get_stock_quote,
    "get_stock_history": get_stock_history,
    "get_sector_quote": get_sector_quote,
    "get_market_overview": get_market_overview,
}


def execute(tool_name: str, arguments: dict[str, Any] | str) -> str:
    """执行工具调用，返回结果文本。arguments 可为 dict 或 JSON 字符串。"""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            arguments = {}
    fn = _DISPATCH.get(tool_name)
    if not fn:
        return f"未知工具：{tool_name}"
    try:
        return str(fn(**arguments))
    except TypeError as e:
        return f"工具参数错误：{e}"
    except Exception as e:
        logger.warning("工具 %s 执行失败：%s", tool_name, e)
        return f"工具执行失败：{e}"
