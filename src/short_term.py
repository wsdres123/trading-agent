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

import pandas as pd

from config import settings as cfg
from src import data, ths_data

logger = logging.getLogger("short_term")

SHORT_SPEC = cfg.DOCS_DIR / "Short-term.md"
BIDDING_CSV = cfg.KNOWLEDGE_DIR / "屠龙表 - 竞价表.csv"

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


def calc_lianban_signal(code: str = "883958", n: int = 8) -> dict:
    """883958 连板溢价信号：连阴>=3天后跳空高开拉红。

    返回 {triggered, prev_green_red_days, gap_up, turn_red, close, prev_close, klines}
    """
    df = data.get_ths_index_daily(code, days=n + 5)
    result = {"triggered": False, "prev_green_red_days": 0, "gap_up": False,
              "turn_red": False, "close": None, "prev_close": None,
              "klines": []}
    if df is None or df.empty or len(df) < 4:
        return result

    df = df.sort_values("日期").reset_index(drop=True)
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


def calc_weipan_safe(code: str = "883418") -> dict:
    """883418 微盘股跌幅是否 > -1%（即不深跌）。

    返回 {safe, change_pct, close, prev_close}
    """
    df = data.get_ths_index_daily(code, days=5)
    result = {"safe": True, "change_pct": 0.0, "close": None, "prev_close": None}
    if df is None or df.empty or len(df) < 2:
        return result
    df = df.sort_values("日期").reset_index(drop=True)
    today_close = float(df.iloc[-1]["收盘"])
    prev_close = float(df.iloc[-2]["收盘"])
    change_pct = (today_close / prev_close - 1) * 100 if prev_close else 0.0
    result["change_pct"] = round(change_pct, 2)
    result["close"] = today_close
    result["prev_close"] = prev_close
    result["safe"] = change_pct > -1.0
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

    lianban = calc_lianban_signal("883958")
    weipan = calc_weipan_safe("883418")

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
        "ladder_count": len(ladder) if not ladder.empty else 0,
        "ladder_summary": ladder_summary,
    }


# ── 大模型判断（核心 — 起变信号判断）──────────────────────────────────────
AI_PROMPT = """你是A股短线情绪交易员，擅长连板梯队与情绪周期判断。请先学习【短线模式规范】（Short-term.md），
再结合【竞价表历史】和【当日证据】，判断今日是否出现"起变信号"，并匹配对应短线模式。

起变信号标准（需综合判断，不要求全部硬性满足，你可以根据经验加权判断）：
1. 连板空间 > 4板（空间压制被打破）
2. 高度板如果是一字板（封板<09:30且炸板=0），该票不能作为候选，但起变信号仍可触发——看连板天梯中是否有非一字板票出现分歧转一致
3. 883958（昨日连板指数）连阴3天后跳空高开拉红（连板溢价率高）
4. 883418（微盘股指数）跌幅不大于-1%（微盘股不深跌）
5. 行情种类偏向接力，情绪红色1天

4种短线模式：
1. 突破空间压制：2周内连板空间压制4板，今天暴量分歧转一致回封站上5板，带动板块回流
   买点：暴量分歧转一致回封站上5板  卖点：30分钟不上水或次日退潮  仓位：5层
2. 分歧转一致：情绪分歧2-3天后有修复预期，连板指数弱2-3天，个股爆量分歧转一致带动板块回流
   买点：爆量分歧转一致上板  卖点：次日不能高开弱转强则兑现  仓位：3层以下
3. 新题材同身位：竞价大单一字封单30亿+，最好3个10亿+同板块，新题材有想象力，做首板同身位换手板
   买点：有一字做首个换手打板  卖点：次日一字封单减少则走  仓位：3层以下
4. 补涨：高度龙头断板当天或爆量当天（龙头不能跌超-5%），做1进2不同板块或同板块首板补涨
   买点：龙头断板当天做补涨  卖点：次日强转弱则走  仓位：3层

候选股选择规则（重要）：
- 一字板（封板时间<09:30且炸板次数=0）不能作为候选股，一字板是资金锁仓不是换手博弈
- 分歧转一致模式：从连板天梯中找炸板次数>0的票（说明盘中分歧后回封=分歧转一致），优先高连板数
- 突破空间压制模式：找站上新高度的票，但必须是非一字板（有换手有炸板=真博弈）
- 候选股必须来自连板天梯数据，给出代码和名称

请严格输出以下JSON（不要输出JSON以外的任何内容）：
{{
  "is_signal": true/false,
  "signal_reason": "判断起变信号的理由（必填，100字内）",
  "cycle_type": "行情种类判断（接力/趋势/混沌等）",
  "modes": [
    {{
      "mode": "模式名",
      "triggered": true/false,
      "candidates": [{{"code": "代码", "name": "名称", "reason": "入选理由"}}],
      "buy_point": "买点",
      "sell_point": "卖点",
      "position": "仓位建议"
    }}
  ],
  "summary": "总结与操作建议（150字内）"
}}

只返回 triggered=true 的模式。如果无起变信号，is_signal=false，modes返回空数组，在summary说明原因。

【短线模式规范】
{spec}

【竞价表历史（近15行）】
{bidding}

【当日证据】
{evidence}
"""


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
    lines.append(f"涨停池总数: {ev.get('ladder_count', 0)}只")
    lines.append("连板天梯(前20):")
    for s in ev.get("ladder_summary", []):
        lines.append(f"  {s['名称']}({s['代码']}) {s['连板数']}板 "
                     f"行业={s['所属行业']} 封板={s['封板时间']} "
                     f"炸板={s['炸板次数']} 封板资金={s['封板资金(亿)']}亿 "
                     f"流通市值={s['自由流通市值(亿)']}亿")
    return "\n".join(lines)


def ai_judge(evidence: dict) -> dict:
    """Qwen 大模型判断起变信号 + 模式触发。

    返回 {is_signal, signal_reason, cycle_type, modes[], summary}
    失败时回退到规则判断。
    """
    spec = SHORT_SPEC.read_text(encoding="utf-8") if SHORT_SPEC.exists() else ""
    bidding = _load_bidding_history()
    ev_text = _evidence_to_text(evidence)

    # 规则回退判断
    def _rule_fallback() -> dict:
        lb = evidence.get("lianban_883958", {})
        wp = evidence.get("weipan_883418", {})
        is_sig = (evidence["space"] > 4
                  and lb.get("triggered", False)
                  and wp.get("safe", True))
        triggered_modes = []
        if is_sig:
            for m in MODES_DEF:
                triggered_modes.append({
                    "mode": m["mode"], "triggered": True,
                    "candidates": [],
                    "buy_point": m["buy_point"],
                    "sell_point": m["sell_point"],
                    "position": m["position"],
                })
        return {
            "is_signal": is_sig,
            "signal_reason": "（规则回退）" + (
                "连板空间>4板且高度板非一字且883958触发且微盘股安全" if is_sig
                else "未满足起变信号条件"),
            "cycle_type": "接力" if is_sig else "未知",
            "modes": triggered_modes,
            "summary": "规则回退判断，模型调用失败。" if not is_sig
                       else "起变信号出现，关注4种模式机会。",
        }

    if not cfg.QWEN_API_KEY:
        return _rule_fallback()

    from openai import OpenAI
    client = OpenAI(api_key=cfg.QWEN_API_KEY, base_url=cfg.QWEN_BASE_URL)
    prompt = AI_PROMPT.format(spec=spec, bidding=bidding, evidence=ev_text)

    # qwen-plus 优先（快且质量好），失败回退3.7-max
    for _model in ("qwen-plus", cfg.QWEN_CHAT_MODEL):
        try:
            resp = client.chat.completions.create(
                model=_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=600)
            raw = resp.choices[0].message.content.strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            result = json.loads(raw)
            if "modes" in result:
                result["modes"] = [m for m in result["modes"] if m.get("triggered")]
            return result
        except Exception as e:
            logger.warning("AI (%s) 起变信号判读失败: %s", _model, e)
            continue
    return _rule_fallback()


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
def detect(date_str: str) -> dict:
    """短线模式主入口。

    1. build_evidence(date_str) — 收集规则证据
    2. ai_judge(evidence) — 模型判断起变信号
    3. 返回 {date, evidence, ai_result, ladder_df, scan_marks}
    """
    evidence = build_evidence(date_str)
    ai_result = ai_judge(evidence)
    ladder = get_ladder(date_str)
    scan_marks = scan_signals("883958", days=120)
    return {
        "date": date_str,
        "evidence": evidence,
        "ai_result": ai_result,
        "ladder_df": ladder,
        "scan_marks": scan_marks,
    }
