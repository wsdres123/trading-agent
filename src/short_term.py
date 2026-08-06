"""短线模式：基于 Short-term.md 的起变信号判断与连板模式匹配。

核心流程：
1. 收集当日规则证据（连板空间/高度板一字/883958连板指数/883418微盘股）
2. Qwen 大模型学习 Short-term.md + 竞价表历史 + 当日证据，判断起变信号
3. 起变信号出现时展示 4 种模式（突破空间压制/分歧转一致/新题材同身位/补涨）

数据源：
- 连板天梯：同花顺 fuyao API limit-up-ladder（30天历史）；akshare stock_zt_pool_em（近20天，含封板资金/首封时间/炸板次数等明细）
- 883958/883418 日K：data.get_ths_index_daily（同花顺指数日K）
- 大模型：Qwen qwen-turbo via OpenAI client
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter

import pandas as pd

from config import settings as cfg
from src import data, ths_data
from src.schema_validator import load_spec_text

logger = logging.getLogger("short_term")

SHORT_SPEC = cfg.DOCS_DIR / "Short-term.md"
BIDDING_CSV = cfg.KNOWLEDGE_DIR / "屠龙表 - 竞价表.csv"
SIGNAL_STORE = cfg.DATA_DIR / "short_term_signal.json"


def _load_signal_state() -> dict:
    """读取缓存的起变信号状态。"""
    try:
        if SIGNAL_STORE.exists():
            return json.loads(SIGNAL_STORE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_signal_state(state: dict) -> None:
    """保存起变信号状态到 .data/short_term_signal.json。"""
    SIGNAL_STORE.parent.mkdir(parents=True, exist_ok=True)
    SIGNAL_STORE.write_text(
        json.dumps(state, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

# 4 种短线模式定义（来自 Short-term.md + 屠龙表-短线模式.csv）
MODES_DEF = [
    {
        "mode": "突破空间压制",
        "condition": "2周内连板空间一直压制在4板，今天有个股暴量分歧转一致（换手放量回封）站上5板，"
                     "并且带动对应题材板块回流，情绪修复",
        "buy_point": "暴量分歧转一致回封站上5板时打板",
        "sell_point": "30分钟不上水，或次日情绪退潮",
        "position": "5层",
    },
    {
        "mode": "分歧转一致",
        "condition": "情绪分歧2-3天后有修复预期，连板指数弱2-3天，个股爆量分歧转一致带动板块回流，"
                     "同时板块低位有首板助攻情绪",
        "buy_point": "爆量分歧转一致上板即买点",
        "sell_point": "次日不能高开弱转强，或爆量平开则兑现",
        "position": "3层以下",
    },
    {
        "mode": "新题材同身位",
        "condition": "竞价开始突然有大单一字封单30亿以上的票，最好有3个10亿以上同板块个股，"
                     "新题材有想象力，做首板同身位换手板",
        "buy_point": "有一字做首个换手，20cm套利，新高小票最好，打板或竞价直接买",
        "sell_point": "次日一字封单减少竞价卖，次日超预期有封单则格局",
        "position": "3层以下",
    },
    {
        "mode": "补涨",
        "condition": "高度龙头断板当天或爆量当天，龙头不能大负反馈（跌超-5%），"
                     "做1进2不同板块或和龙头同板块的首板补涨，博弈空间溢价",
        "buy_point": "龙头断板/爆量当天，做1进2或首板补涨",
        "sell_point": "次日强转弱则走",
        "position": "3层",
    },
]


# ── 数据获取层 ─────────────────────────────────────────────────────────────
@data.ttl_cache(cfg.BOARD_TTL)
def get_zt_pool(date_str: str) -> pd.DataFrame:
    """涨停池（akshare stock_zt_pool_em）。

    date_str: YYYY-MM-DD 或 YYYYMMDD
    返回列：代码/名称/连板数/所属行业/封板资金/首次封板时间/炸板次数/涨停统计/换手率
    """
    raw = date_str.replace("-", "")
    try:
        data._need_akshare()
        import akshare as ak
        df = ak.stock_zt_pool_em(date=raw)
    except Exception as e:
        logger.warning("涨停池获取失败 %s: %s", raw, e)
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()

    # 列名归一化（akshare 版本差异兼容）
    col_map = {}
    for c in df.columns:
        cl = c.strip()
        if "代码" in cl or cl == "code":
            col_map[c] = "代码"
        elif "名称" in cl or cl == "name":
            col_map[c] = "名称"
        elif "连板" in cl:
            col_map[c] = "连板数"
        elif "所属行业" in cl or "行业" in cl:
            col_map[c] = "所属行业"
        elif "封板资金" in cl:
            col_map[c] = "封板资金"
        elif "首次封板" in cl:
            col_map[c] = "首次封板时间"
        elif "炸板" in cl:
            col_map[c] = "炸板次数"
        elif "涨停统计" in cl:
            col_map[c] = "涨停统计"
        elif "换手" in cl:
            col_map[c] = "换手率"
        elif "流通市值" in cl:
            col_map[c] = "流通市值"
        elif "涨跌幅" in cl:
            col_map[c] = "涨跌幅"
    df = df.rename(columns=col_map)

    # 连板数转 int
    if "连板数" in df.columns:
        df["连板数"] = pd.to_numeric(df["连板数"], errors="coerce").fillna(0).astype(int)
    return df.reset_index(drop=True)


def _fmt_time(t: str) -> str:
    """'092500' / '09:25:00' → '9:25'"""
    t = re.sub(r"[:\-]", "", str(t).strip())
    if len(t) >= 4:
        return f"{int(t[:2])}:{t[2:4]}"
    return str(t)


@data.ttl_cache(cfg.BOARD_TTL)
def _fetch_ladder_ths() -> list:
    """从同花顺 fuyao API 获取连板天梯（30天数据一次返回，缓存1天）。"""
    resp = ths_data._request("a-share/special-data/limit-up-ladder")
    return resp.get("item", []) if resp else []


def get_ladder_ths(date_str: str) -> pd.DataFrame:
    """同花顺连板天梯（指定日期）。返回 代码/名称/连板数/次日封板/信号等级。"""
    target = date_str.replace("-", "")
    for it in _fetch_ladder_ths():
        d = str(it.get("date", "")).replace("-", "")
        if d == target:
            rows = []
            boards = it.get("boards", {})
            for level in ("seven_over", "six_board", "five_board", "four_board",
                          "three_board", "two_board"):
                for s in boards.get(level, []):
                    rows.append({
                        "代码": str(s.get("ticker", "")),
                        "名称": str(s.get("name", "")),
                        "连板数": int(s.get("board_num", 0)),
                        "次日封板": "是" if s.get("seal_nextday") else "否",
                        "信号等级": int(s.get("sign_level", 0)),
                    })
            df = pd.DataFrame(rows)
            if not df.empty:
                df = df.sort_values("连板数", ascending=False).reset_index(drop=True)
            return df
    return pd.DataFrame()


@data.ttl_cache(cfg.SPOT_TTL)
def _fetch_zt_pool_ths() -> list:
    """从同花顺 limit-up-pool 获取当日全部涨停（含首板），分页获取。"""
    all_items = []
    for page in (1, 2, 3, 4):
        resp = ths_data._request("a-share/special-data/limit-up-pool", {"page": page})
        items = resp.get("item", []) if resp else []
        if not items:
            break
        all_items.extend(items)
        if page >= resp.get("pagination", {}).get("pages", 1):
            break
    return all_items


def get_zt_pool_ths() -> pd.DataFrame:
    """同花顺当日涨停池（含首板）。"""
    rows = []
    for s in _fetch_zt_pool_ths():
        rows.append({
            "代码": str(s.get("ticker", "")),
            "名称": str(s.get("name", "")),
            "连板数": int(s.get("continue_day_cnt", 1)),
            "封板时间": str(s.get("limit_up_time", "")),
            "涨停原因": str(s.get("limit_up_reason", "")),
            "封板资金(亿)": round(float(s.get("seal_money", 0) or 0) / 1e8, 2),
            "涨跌幅": float(s.get("price_change_ratio_pct", 0) or 0),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("连板数", ascending=False).reset_index(drop=True)
    return df


def _ladder_show(df: pd.DataFrame) -> pd.DataFrame:
    """选列并按连板数降序。"""
    show_cols = [c for c in ["代码", "名称", "连板数", "所属行业", "封板资金(亿)",
                             "封板时间", "炸板次数", "自由流通市值(亿)", "涨跌幅",
                             "涨停原因", "次日封板", "信号等级"]
                 if c in df.columns]
    return df[show_cols].sort_values("连板数", ascending=False).reset_index(drop=True)


def get_ladder(date_str: str) -> pd.DataFrame:
    """连板天梯（含首板）。当日用 limit-up-pool，历史用 limit-up-ladder(2板+) + akshare首板。"""
    ths_ladder = get_ladder_ths(date_str)
    ak_df = get_zt_pool(date_str)

    # akshare 明细列归一化
    ak = pd.DataFrame()
    if not ak_df.empty:
        ak = ak_df.copy()
        if "封板资金" in ak.columns:
            ak["封板资金(亿)"] = (pd.to_numeric(ak["封板资金"], errors="coerce") / 1e8).round(2)
        if "流通市值" in ak.columns:
            ak["自由流通市值(亿)"] = (pd.to_numeric(ak["流通市值"], errors="coerce") / 1e8).round(1)
        if "首次封板时间" in ak.columns:
            ak["封板时间"] = ak["首次封板时间"].apply(_fmt_time)
        ak = ak[[c for c in ["代码", "名称", "连板数", "所属行业", "封板资金(亿)",
                             "封板时间", "炸板次数", "自由流通市值(亿)", "涨跌幅"] if c in ak.columns]]

    # 判断是否最新交易日（limit-up-pool 只有当日数据）
    items = _fetch_ladder_ths()
    latest = str(items[0]["date"]).replace("-", "") if items else ""
    is_latest = date_str.replace("-", "") == latest

    # 当日：用 limit-up-pool（含首板 + 涨停时间/原因/封板资金）
    if is_latest:
        pool = get_zt_pool_ths()
        if not pool.empty:
            df = pool
            if not ak.empty:
                extra = [c for c in ["代码", "所属行业", "炸板次数", "自由流通市值(亿)"] if c in ak.columns]
                if len(extra) > 1:
                    df = df.merge(ak[extra], on="代码", how="left")
            return _ladder_show(df)

    # 历史：THS ladder(2板+) 为基础，合并 akshare 明细 + 追加首板
    if not ths_ladder.empty:
        df = ths_ladder
        if not ak.empty:
            merge_cols = [c for c in ["代码", "所属行业", "封板资金(亿)", "封板时间",
                                      "炸板次数", "自由流通市值(亿)", "涨跌幅"] if c in ak.columns]
            if len(merge_cols) > 1:
                df = df.merge(ak[merge_cols], on="代码", how="left")
            # 追加首板（THS ladder 没有1板）
            first = ak[ak["连板数"] == 1]
            if not first.empty:
                new_first = first[~first["代码"].isin(set(df["代码"]))]
                if not new_first.empty:
                    df = pd.concat([df, new_first], ignore_index=True)
        return _ladder_show(df)

    # THS 无数据，用 akshare
    if not ak.empty:
        return _ladder_show(ak)
    return pd.DataFrame()


# ── 信号计算层 ─────────────────────────────────────────────────────────────
def is_one_line_board(row) -> bool:
    """是否一字板：封板时间<'09:30' AND 炸板次数==0。

    股权变更票不算一字板。
    """
    if isinstance(row, dict):
        first_time = str(row.get("首次封板时间") or row.get("封板时间") or "")
        breaks = row.get("炸板次数", 1)
        stat = str(row.get("涨停统计") or row.get("涨停原因") or "")
    else:
        first_time = str(row.get("首次封板时间", "") or row.get("封板时间", ""))
        breaks = row.get("炸板次数", 1)
        stat = str(row.get("涨停统计", "") or row.get("涨停原因", ""))

    if "股权" in stat or "变更" in stat or "更名" in stat:
        return False
    try:
        breaks = int(breaks) if pd.notna(breaks) else 1
    except (ValueError, TypeError):
        breaks = 1
    ft = re.sub(r"[:\-]", "", first_time)
    return bool(first_time) and bool(ft) and ft < "093000" and breaks == 0


def calc_space(zt_df: pd.DataFrame) -> int:
    """连板空间 = 涨停池中最大连板数。"""
    if zt_df is None or zt_df.empty or "连板数" not in zt_df.columns:
        return 0
    return int(zt_df["连板数"].max())


def calc_lianban_signal(code: str = "883958", n: int = 8,
                        date_str: str | None = None) -> dict:
    """883958 连板溢价信号：连阴>=3天后跳空高开拉红。

    date_str: 分析日期，仅取该日及之前的K线（None则用最新数据）。
    返回 {triggered, prev_green_red_days, gap_up, turn_red, close, prev_close, klines}
    """
    df = data.get_ths_index_daily(code, days=n + 5)
    result = {"triggered": False, "prev_green_red_days": 0, "gap_up": False,
              "turn_red": False, "close": None, "prev_close": None,
              "klines": []}
    if df is None or df.empty or len(df) < 4:
        return result

    df = df.sort_values("日期").reset_index(drop=True)
    if date_str:
        df = df[df["日期"].astype(str).str[:10] <= date_str].reset_index(drop=True)
    if df.empty or len(df) < 4:
        return result

    today = df.iloc[-1]
    result["close"] = float(today["收盘"])
    result["prev_close"] = float(df.iloc[-2]["收盘"]) if len(df) >= 2 else None

    # K线摘要供模型参考
    for _, r in df.tail(n).iterrows():
        result["klines"].append({
            "日期": str(r["日期"]), "开盘": float(r["开盘"]),
            "最高": float(r["最高"]), "最低": float(r["最低"]),
            "收盘": float(r["收盘"]),
        })

    # 连阴天数（收盘<开盘 为阴线）
    green_red_days = 0
    for i in range(len(df) - 2, -1, -1):
        if df.iloc[i]["收盘"] < df.iloc[i]["开盘"]:
            green_red_days += 1
        else:
            break
    result["prev_green_red_days"] = green_red_days

    # 跳空高开 = 今日开盘 > 昨日收盘
    gap_up = float(today["开盘"]) > float(df.iloc[-2]["收盘"]) if len(df) >= 2 else False
    # 拉红 = 今日收盘 > 今日开盘
    turn_red = float(today["收盘"]) > float(today["开盘"])
    result["gap_up"] = bool(gap_up)
    result["turn_red"] = bool(turn_red)
    result["triggered"] = green_red_days >= 3 and bool(gap_up) and bool(turn_red)
    return result


def calc_weipan_safe(code: str = "883418",
                     date_str: str | None = None) -> dict:
    """883418 微盘股跌幅是否 > -1%（即不深跌）。

    date_str: 分析日期，仅取该日及之前的K线（None则用最新数据）。
    返回 {safe, change_pct, close, prev_close}
    """
    df = data.get_ths_index_daily(code, days=5)
    result = {"safe": True, "change_pct": 0.0, "close": None, "prev_close": None}
    if df is None or df.empty or len(df) < 2:
        return result
    df = df.sort_values("日期").reset_index(drop=True)
    if date_str:
        df = df[df["日期"].astype(str).str[:10] <= date_str].reset_index(drop=True)
    if df.empty or len(df) < 2:
        return result
    today_close = float(df.iloc[-1]["收盘"])
    prev_close = float(df.iloc[-2]["收盘"])
    change_pct = (today_close / prev_close - 1) * 100 if prev_close else 0.0
    result["change_pct"] = round(change_pct, 2)
    result["close"] = today_close
    result["prev_close"] = prev_close
    result["safe"] = change_pct > -1.0
    return result

def calc_continuation_signal(code: str = "883958",
                              date_str: str | None = None) -> dict:
    """延续信号：起变信号出现第2天，连板指数还能维持3个点溢价。

    date_str: 分析日期，仅取该日及之前的K线（None则用最新数据）。
    返回 {triggered, premium_pct, yesterday_signal_date}
    """
    result = {"triggered": False, "premium_pct": 0.0, "yesterday_signal_date": ""}
    state = _load_signal_state()
    signal_date = state.get("signal_date", "")
    if not signal_date:
        return result

    df = data.get_ths_index_daily(code, days=8)
    if df is None or df.empty or len(df) < 2:
        return result
    df = df.sort_values("日期").reset_index(drop=True)
    if date_str:
        df = df[df["日期"].astype(str).str[:10] <= date_str].reset_index(drop=True)
    if df.empty or len(df) < 2:
        return result
    last_date = str(df.iloc[-1]["日期"])[:10]
    if last_date <= signal_date:
        return result
    trading_days_after = sum(
        1 for d in df["日期"] if str(d)[:10] > signal_date
    )
    if trading_days_after != 1:
        return result
    today_close = float(df.iloc[-1]["收盘"])
    prev_close = float(df.iloc[-2]["收盘"])
    premium_pct = (today_close / prev_close - 1) * 100 if prev_close else 0.0
    result["premium_pct"] = round(premium_pct, 2)
    result["yesterday_signal_date"] = signal_date
    result["triggered"] = premium_pct >= 3.0
    return result


# ── 证据汇总 ───────────────────────────────────────────────────────────────
def build_evidence(date_str: str) -> dict:
    """汇总当日所有规则证据（不判断，只收集）。"""
    zt = get_zt_pool(date_str)
    ladder = get_ladder(date_str)
    space = calc_space(zt) if not zt.empty else calc_space(ladder)

    # 高度板（最大连板数的票）
    high_board = None
    high_is_one_line = None
    if space > 0:
        if not zt.empty:
            high_rows = zt[zt["连板数"] == space]
            if not high_rows.empty:
                high_board = high_rows.iloc[0]
                high_is_one_line = is_one_line_board(high_board)
        if high_board is None and not ladder.empty:
            high_rows = ladder[ladder["连板数"] == space]
            if not high_rows.empty:
                high_board = high_rows.iloc[0]
                high_is_one_line = is_one_line_board(high_board)

    lianban = calc_lianban_signal("883958", date_str=date_str)
    weipan = calc_weipan_safe("883418", date_str=date_str)
    continuation = calc_continuation_signal("883958", date_str=date_str)

    # 连板天梯摘要（前8只）
    ladder_summary = []
    if not ladder.empty:
        for _, r in ladder.head(8).iterrows():
            ladder_summary.append({
                "代码": str(r.get("代码", "")),
                "名称": str(r.get("名称", "")),
                "连板数": int(r.get("连板数", 0)),
                "所属行业": str(r.get("所属行业", "")),
                "封板资金(亿)": str(r.get("封板资金(亿)", "")),
                "封板时间": str(r.get("封板时间", "")),
                "炸板次数": str(r.get("炸板次数", "")),
                "自由流通市值(亿)": str(r.get("自由流通市值(亿)", "")),
            })

    return {
        "date": date_str,
        "space": space,
        "high_board": {
            "代码": str(high_board.get("代码", "")) if high_board is not None else "",
            "名称": str(high_board.get("名称", "")) if high_board is not None else "",
            "连板数": int(space),
            "所属行业": str(high_board.get("所属行业", "")) if high_board is not None else "",
            "首次封板时间": str(high_board.get("首次封板时间", "")) if high_board is not None else "",
            "炸板次数": str(high_board.get("炸板次数", "")) if high_board is not None else "",
            "换手率": str(high_board.get("换手率", "")) if high_board is not None else "",
        } if high_board is not None else None,
        "high_board_is_one_line": high_is_one_line,
        "lianban_883958": lianban,
        "weipan_883418": weipan,
        "continuation_883958": continuation,
        "ladder_count": len(ladder) if not ladder.empty else 0,
        "ladder_summary": ladder_summary,
    }


# ── 三层决策：硬门控 + 特征评分 + LLM 裁决 ─────────────────────────────
def hard_gate(evidence: dict) -> dict:
    """第一层：硬门控。事实性条件由程序判定，不依赖模型。"""
    lb = evidence.get("lianban_883958", {})
    wp = evidence.get("weipan_883418", {})
    cont = evidence.get("continuation_883958", {})
    space = evidence.get("space", 0)

    high_is_one_line = evidence.get("high_board_is_one_line", False)

    # 起变硬门控：连板空间>4 + 连板指数触发 + 微盘安全 + 高度板非一字板（有换手）
    is_signal = (space > 4
                 and lb.get("triggered", False)
                 and wp.get("safe", True)
                 and not high_is_one_line)

    is_continuation = cont.get("triggered", False)

    reasons = []
    if is_signal:
        reasons.append(f"连板空间{space}>4板")
        reasons.append("883958连阴后跳空高开拉红")
        reasons.append(f"微盘股{'安全' if wp.get('safe') else '不安全'}")
    if is_continuation:
        reasons.append(f"883958维持{cont.get('premium_pct', 0)}%溢价")

    return {
        "is_signal": is_signal,
        "is_continuation": is_continuation,
        "high_is_one_line": high_is_one_line,
        "premium_pct": cont.get("premium_pct", 0.0),
        "reason": "；".join(reasons) if reasons else "未满足起变/延续硬条件",
    }


def score_modes(evidence: dict) -> list[dict]:
    """第二层：特征评分。模式匹配得分 + 候选筛选由程序完成。"""
    ladder = evidence.get("ladder_summary", [])
    space = evidence.get("space", 0)
    high_is_one_line = evidence.get("high_board_is_one_line", False)
    results = []

    # 模式1: 突破空间压制
    hb = evidence.get("high_board")
    breakout_score = 0
    breakout_candidates = []
    if hb and not high_is_one_line and space >= 5:
        try:
            breaks = int(hb.get("炸板次数", 0))
        except (ValueError, TypeError):
            breaks = 0
        if breaks > 0:
            breakout_score = 1.0
            breakout_candidates.append({
                "code": hb.get("代码", ""), "name": hb.get("名称", ""),
                "boards": space, "reason": f"站上新高度{space}板 炸板{breaks}次（非一字有换手）"
            })
    results.append({
        "mode": "突破空间压制", "score": breakout_score,
        "candidates": breakout_candidates,
        "buy_point": "暴量分歧转一致回封站上5板",
        "sell_point": "30分钟不上水或次日退潮", "position": "5层",
    })

    # 模式2: 分歧转一致
    divergence_candidates = []
    for s in ladder:
        try:
            breaks = int(s.get("炸板次数", 0))
        except (ValueError, TypeError):
            breaks = 0
        if breaks > 0 and not is_one_line_board(s):
            divergence_candidates.append({
                "code": s["代码"], "name": s["名称"],
                "boards": s["连板数"],
                "reason": f"{s['连板数']}板 炸板{breaks}次（分歧后回封）"
            })
    divergence_candidates.sort(key=lambda x: -x["boards"])
    div_score = min(1.0, len(divergence_candidates) / 3)
    results.append({
        "mode": "分歧转一致", "score": div_score,
        "candidates": divergence_candidates[:5],
        "buy_point": "爆量分歧转一致上板",
        "sell_point": "次日不能高开弱转强则兑现", "position": "3层以下",
    })

    # 模式3: 新题材同身位
    yizi_candidates = []
    for s in ladder:
        if is_one_line_board(s):
            try:
                amount = float(s.get("封板资金(亿)", 0))
            except (ValueError, TypeError):
                amount = 0
            if amount >= 10:
                yizi_candidates.append({
                    "code": s["代码"], "name": s["名称"],
                    "boards": s["连板数"],
                    "amount_yi": amount,
                    "industry": s.get("所属行业", ""),
                    "reason": f"一字板 封单{amount:.0f}亿 [{s.get('所属行业', '')}]"
                })
    board_counts = Counter(s["industry"] for s in yizi_candidates)
    has_cluster = any(c >= 3 for c in board_counts.values())
    has_big = any(s.get("amount_yi", 0) >= 30 for s in yizi_candidates)
    newtheme_score = 0.0
    if has_big:
        newtheme_score += 0.5
    if has_cluster:
        newtheme_score += 0.5
    results.append({
        "mode": "新题材同身位", "score": newtheme_score,
        "candidates": yizi_candidates[:5],
        "buy_point": "有一字做首个换手打板",
        "sell_point": "次日一字封单减少则走", "position": "3层以下",
    })

    # 模式4: 补涨
    buzhang_candidates = []
    for s in ladder:
        if s["连板数"] < space and s["连板数"] >= 2:
            buzhang_candidates.append({
                "code": s["代码"], "name": s["名称"],
                "boards": s["连板数"],
                "reason": f"{s['连板数']}板 低于空间{space}板（补涨候选）"
            })
    buzhang_candidates.sort(key=lambda x: -x["boards"])
    buzhang_score = min(1.0, len(buzhang_candidates) / 3) if space >= 5 else 0
    results.append({
        "mode": "补涨", "score": buzhang_score,
        "candidates": buzhang_candidates[:5],
        "buy_point": "龙头断板当天做补涨",
        "sell_point": "次日强转弱则走", "position": "3层",
    })

    return results


def _assemble_result(gate: dict, modes: list[dict], llm_result: dict | None) -> dict:
    """组装三层决策最终结果。"""
    triggered_modes = []
    for m in modes:
        if m["score"] > 0 and m["candidates"]:
            triggered_modes.append({
                "mode": m["mode"], "triggered": True,
                "candidates": m["candidates"],
                "buy_point": m["buy_point"],
                "sell_point": m["sell_point"],
                "position": m["position"],
            })

    result = {
        "is_signal": gate["is_signal"],
        "is_continuation": gate["is_continuation"],
        "signal_reason": gate["reason"],
        "continuation_reason": (
            f"起变信号后883958仍维持{gate.get('premium_pct', 0)}%溢价，延续行情"
            if gate["is_continuation"] else ""
        ),
        "cycle_type": "接力" if gate["is_signal"] or gate["is_continuation"] else "未知",
        "modes": triggered_modes,
        "summary": "",
        "gate_reason": gate["reason"],
        "mode_scores": {m["mode"]: m["score"] for m in modes},
    }

    if llm_result:
        result["summary"] = llm_result.get("summary", "")
        if llm_result.get("signal_reason"):
            result["signal_reason"] = llm_result["signal_reason"]
        if llm_result.get("cycle_type"):
            result["cycle_type"] = llm_result["cycle_type"]
        result["confidence"] = llm_result.get("confidence", 0.8)
    else:
        if gate["is_signal"]:
            result["summary"] = "起变信号出现，关注4种模式机会。"
        elif gate["is_continuation"]:
            result["summary"] = f"延续信号触发：{result['continuation_reason']}"
        else:
            result["summary"] = "未满足起变信号条件。"
        result["confidence"] = 1.0 if gate["is_signal"] else 0.5

    return result


AI_ADJUDICATE_PROMPT = """你是A股短线情绪交易员。程序已完成硬门控和模式评分，请对边界案例做裁决。

【硬门控结果】
{gate}

【模式评分与候选】
{modes}

【竞价表历史（近15行）】
{bidding}

策略规范（单一事实来源）：
{spec}

请对以上评分和候选做可读解释与排序（150字内），输出JSON：
{{
  "recommended_modes": ["模式名"],
  "signal_reason": "补充判断理由",
  "summary": "总结与操作建议（150字内）",
  "confidence": 0.8,
  "cycle_type": "接力/趋势/混沌"
}}
只返回JSON。"""

REVIEW_PROMPT_SHORT = """你是A股短线复核员。程序已完成硬门控和模式评分，请根据**同一份数据**独立裁决。
这是对抗式复核——你不知道第一模型的结论。

【硬门控结果】
{gate}

【模式评分与候选】
{modes}

请独立判断并输出JSON：
{{
  "recommended_modes": ["模式名"],
  "signal_reason": "独立判断理由",
  "summary": "独立操作建议（150字内）",
  "confidence": 0.8,
  "cycle_type": "接力/趋势/混沌"
}}
策略规范（单一事实来源）：
{spec}

重点：1)哪些模式候选证据不足？2)有什么反例？3)是否应弃权？
只返回JSON。"""


def _parse_llm_json(raw: str) -> dict | None:
    """解析 LLM 返回的 JSON，去除 markdown 包裹。"""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _llm_adjudicate(evidence: dict, gate: dict, modes: list[dict]) -> dict | None:
    """第三层：LLM 裁决边界案例（含双阶段复核）。"""
    bidding = _load_bidding_history()
    gate_text = gate["reason"]
    mode_lines = []
    for m in modes:
        if m["score"] > 0:
            mode_lines.append(f"  {m['mode']} (score={m['score']:.1f})")
            for c in m.get("candidates", []):
                mode_lines.append(f"    - {c['name']}({c['code']}) "
                                  f"{c.get('boards', '?')}板 {c.get('reason', '')}")
    modes_text = "\n".join(mode_lines) if mode_lines else "  无触发模式"

    spec_text = load_spec_text(SHORT_SPEC)
    prompt = AI_ADJUDICATE_PROMPT.format(gate=gate_text, modes=modes_text,
                                         bidding=bidding, spec=spec_text)
    review_prompt = REVIEW_PROMPT_SHORT.format(gate=gate_text, modes=modes_text,
                                               spec=spec_text)

    from src.llm_gateway import call_llm

    def _call(model: str, llm_prompt: str) -> dict | None:
        try:
            raw = call_llm(
                prompt=llm_prompt,
                model=model,
                temperature=0.2,
                max_tokens=400,
                timeout=30.0,
                retries=0,
            )
            if raw is None:
                return None
            return _parse_llm_json(raw)
        except Exception as e:
            logger.warning("LLM 裁决失败 (%s): %s", model, e)
            return None

    # Stage 1: qwen-plus 初判
    stage1 = None
    for _model in ("qwen-plus", cfg.QWEN_CHAT_MODEL):
        stage1 = _call(_model, prompt)
        if stage1:
            break
    if stage1 is None:
        return None

    # 双阶段复核：低置信触发
    confidence = stage1.get("confidence", 0.5)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.5

    if confidence >= 0.6:
        return stage1

    logger.info("短线决策低置信(%.2f)，触发双阶段复核", confidence)
    stage2 = _call(cfg.QWEN_CHAT_MODEL, review_prompt)
    if stage2 is None:
        return stage1

    # 比较 recommended_modes
    modes1 = set(stage1.get("recommended_modes", []))
    modes2 = set(stage2.get("recommended_modes", []))
    agreed = bool(modes1 & modes2) if modes1 and modes2 else False

    result = {**stage1, "reviewed": True, "agreed": agreed}
    if not agreed:
        result["recommended_modes"] = list(modes1 & modes2) if modes1 & modes2 else list(modes1)
        result["confidence"] = min(confidence, float(stage2.get("confidence", 0.5)))
        result["review_reason"] = "两阶段推荐模式分歧，取交集并降置信"
    else:
        result["review_note"] = f"双阶段一致：{list(modes1 & modes2)}"

    return result


def _load_bidding_history(n: int = 15) -> str:
    """读取竞价表历史近 n 行，转为文本供模型参考。"""
    try:
        df = pd.read_csv(BIDDING_CSV, encoding="utf-8")
    except Exception as e:
        logger.warning("读取竞价表失败: %s", e)
        return ""
    if df.empty:
        return ""
    tail = df.tail(n)
    lines = []
    for _, r in tail.iterrows():
        date_val = str(r.iloc[0]) if len(r) > 0 else ""
        parts = [f"{c}:{r[c]}" for c in df.columns[1:12] if pd.notna(r.get(c)) and str(r.get(c)).strip()]
        if parts:
            lines.append(f"{date_val} | {' | '.join(parts)}")
    return "\n".join(lines)


def _evidence_to_text(ev: dict) -> str:
    """将证据 dict 转为文本。"""
    lines = [f"日期: {ev['date']}"]
    lines.append(f"连板空间: {ev['space']}板")
    hb = ev.get("high_board")
    if hb:
        lines.append(f"高度板: {hb['名称']}({hb['代码']}) {hb['连板数']}板 "
                     f"行业={hb.get('所属行业', '')} 首封={hb.get('首次封板时间', '')} "
                     f"炸板={hb.get('炸板次数', '')} 换手={hb.get('换手率', '')}")
        _ol = ev.get("high_board_is_one_line")
        _ol_txt = "是" if _ol else ("否" if _ol is False else "未知（无明细数据）")
        lines.append(f"高度板是否一字板: {_ol_txt}")
    lb = ev.get("lianban_883958", {})
    lines.append(f"883958连板指数: 连阴{lb.get('prev_green_red_days', 0)}天 "
                 f"跳空高开={'是' if lb.get('gap_up') else '否'} "
                 f"拉红={'是' if lb.get('turn_red') else '否'} "
                 f"信号触发={'是' if lb.get('triggered') else '否'}")
    wp = ev.get("weipan_883418", {})
    lines.append(f"883418微盘股: 涨跌幅={wp.get('change_pct', 0)}% "
                 f"安全(不深跌)={'是' if wp.get('safe') else '否'}")
    cont = ev.get("continuation_883958", {})
    if cont.get("yesterday_signal_date"):
        lines.append(f"延续信号检测: 起变日期={cont['yesterday_signal_date']} "
                     f"今日883958溢价={cont.get('premium_pct', 0)}% "
                     f"延续触发={'是' if cont.get('triggered') else '否'}")
    lines.append(f"涨停池总数: {ev.get('ladder_count', 0)}只")
    lines.append("连板天梯(前20):")
    for s in ev.get("ladder_summary", []):
        lines.append(f"  {s['名称']}({s['代码']}) {s['连板数']}板 "
                     f"行业={s['所属行业']} 封板={s['封板时间']} "
                     f"炸板={s['炸板次数']} 封板资金={s['封板资金(亿)']}亿 "
                     f"流通市值={s['自由流通市值(亿)']}亿")
    return "\n".join(lines)


def ai_judge(evidence: dict) -> dict:
    """三层决策：硬门控 → 特征评分 → LLM 裁决。

    返回 {is_signal, is_continuation, signal_reason, continuation_reason,
          cycle_type, modes[], summary, gate_reason, mode_scores, confidence}
    """
    gate = hard_gate(evidence)
    modes = score_modes(evidence) if gate["is_signal"] else []

    if not cfg.QWEN_API_KEY:
        return _assemble_result(gate, modes, None)

    needs_llm = any(0.3 < m["score"] < 0.7 for m in modes)

    if not needs_llm and gate["is_signal"]:
        return _assemble_result(gate, modes, None)

    if not gate["is_signal"] and not gate["is_continuation"]:
        return _assemble_result(gate, modes, None)

    llm_result = _llm_adjudicate(evidence, gate, modes)
    return _assemble_result(gate, modes, llm_result)


# ── 轻量扫描（K线标记用）──────────────────────────────────────────────────
def scan_signals(code: str = "883958", days: int = 120) -> list[dict]:
    """扫描最近 days 天的起变候选日（连阴>=3天后跳空高开拉红）。

    返回 [{date, type: 'candidate'}]
    """
    df = data.get_ths_index_daily(code, days=days + 10)
    if df is None or df.empty:
        return []
    df = df.sort_values("日期").reset_index(drop=True)
    marks = []
    for i in range(3, len(df)):
        # 前3天连阴
        green_red = all(df.iloc[j]["收盘"] < df.iloc[j]["开盘"] for j in range(i - 3, i))
        if not green_red:
            continue
        today = df.iloc[i]
        prev = df.iloc[i - 1]
        gap_up = float(today["开盘"]) > float(prev["收盘"])
        turn_red = float(today["收盘"]) > float(today["开盘"])
        if gap_up and turn_red:
            marks.append({"date": str(today["日期"]), "type": "candidate"})
    return marks


# ── 入口函数 ───────────────────────────────────────────────────────────────
def _detect_raw(date_str: str) -> dict:
    """短线模式原始判断（无去重）。

    1. build_evidence(date_str) — 收集规则证据
    2. ai_judge(evidence) — 模型判断起变信号
    3. 返回 {date, evidence, ai_result, ladder_df, scan_marks}
    """
    evidence = build_evidence(date_str)
    ai_result = ai_judge(evidence)
    try:
        from src import predictions as _pred
        if ai_result.get("is_signal"):
            _pred.append_prediction(
                type="shortterm", date=date_str, signal="起变",
                confidence=ai_result.get("confidence", 1.0),
                evidence_snapshot=[{"reason": ai_result.get("signal_reason", "")}],
            )
        elif ai_result.get("is_continuation"):
            _pred.append_prediction(
                type="shortterm", date=date_str, signal="延续",
                confidence=ai_result.get("confidence", 0.8),
                evidence_snapshot=[{"reason": ai_result.get("continuation_reason", "")}],
            )
    except Exception:
        pass
    ladder = get_ladder(date_str)
    scan_marks = scan_signals("883958", days=120)
    return {
        "date": date_str,
        "evidence": evidence,
        "ai_result": ai_result,
        "ladder_df": ladder,
        "scan_marks": scan_marks,
    }


def detect(date_str: str) -> dict:
    """短线模式主入口（带去重）。

    起变信号一旦触发，在同一883958周期内不重复出现。
    当883958经历新一轮"连阴→跳空高开拉红"后周期重置。
    延续信号不受去重影响，独立展示。
    """
    result = _detect_raw(date_str)
    ai_result = result.get("ai_result", {})
    evidence = result.get("evidence", {})
    is_cont = ai_result.get("is_continuation", False)
    cont_reason = ai_result.get("continuation_reason", "")

    if not ai_result.get("is_signal"):
        # 无起变信号，但可能有延续信号——保留延续信息
        return result

    state = _load_signal_state()
    cached_date = state.get("signal_date", "")

    if not cached_date:
        lb = evidence.get("lianban_883958", {})
        lb_date = ""
        if lb.get("triggered") and lb.get("klines"):
            lb_date = str(lb["klines"][-1]["日期"])
        _save_signal_state({"signal_date": date_str, "lianban_trigger_date": lb_date})
        return result

    # 回测历史日期（早于或等于已缓存日期）不应用去重
    if date_str <= cached_date:
        return result

    lb = evidence.get("lianban_883958", {})
    current_lb = ""
    if lb.get("triggered") and lb.get("klines"):
        current_lb = str(lb["klines"][-1]["日期"])
    cached_lb = state.get("lianban_trigger_date", "")

    if current_lb and current_lb != cached_lb:
        _save_signal_state({"signal_date": date_str, "lianban_trigger_date": current_lb})
        return result

    # 起变信号被去重，但保留延续信号信息
    ai_result["is_signal"] = False
    ai_result["signal_reason"] = f"已于 {cached_date} 起变"
    ai_result["modes"] = []
    base_summary = (
        f"起变信号已于 {cached_date} 触发，当前仍处于同一周期，"
        f"等待883958连板指数新一轮连阴→跳空高开拉红后再关注。"
    )
    if is_cont and cont_reason:
        base_summary += f"\n【延续信号】{cont_reason}"
    ai_result["summary"] = base_summary
    return result
