"""AI 助手：Qwen 对话（RAG 知识 + 实时行情工具调用）+ 筛选条件解析。

- chat(): RAG 检索交易系统知识 + 按需调用行情工具(指数/个股/历史/板块/大盘)，
  既能回答屠龙表/周期/心态类问题，也能回答"今天上证涨多少"等实时数据问题。
- chat_stream(): 流式输出，配合 st.write_stream() 降低感知延迟。
- parse_filter_conditions(): 自然语言 → 结构化 JSON 条件；失败回退关键字识别。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Generator, List

from openai import OpenAI

from config import settings as cfg
from src import knowledge, market_tools

logger = logging.getLogger("ai_assistant")

_client = OpenAI(api_key=cfg.QWEN_API_KEY, base_url=cfg.QWEN_BASE_URL) if cfg.QWEN_API_KEY else None

MAX_TOOL_ROUNDS = 3

# ── 固定前缀（Prompt Caching 友好，每次调用相同前缀命中缓存）─────────────
_SYSTEM_PREFIX = """你是「劫财AI交易」的专属助手，服务一位 A 股短线/趋势交易者。回答分两类处理：

1. 交易系统/规则/周期/心态类问题（如趋势A周期、主线筛选、屠龙表、空仓纪律）：
   严格依据下方【知识库片段】回答；知识库未覆盖则如实说明，不要编造交易规则。

2. 实时行情/个股数据/大盘/板块/通用金融知识类问题（如"今天上证涨多少""贵州茅台现价"）：
   调用提供的工具获取实时数据，结合自身金融知识作答。数据为 A 股，涨为正数、跌为负数。

风格：简洁、要点化、可操作；回答行情务必给出具体数字与涨跌幅。若用户问个股筛选，
可建议其使用「个股分组筛选」功能。工具返回的数据即视为权威，不要凭空臆测数值。
"""

SYSTEM_PROMPT = _SYSTEM_PREFIX + "【知识库片段】\n{context}"

FILTER_SCHEMA_INSTRUCTIONS = """把用户对个股筛选的自然语言描述解析成 JSON，字段如下，只输出 JSON：
{
  "conditions": [
    {"field": "close_gt_ma", "ma": 5},
    {"field": "close_gt_ma", "ma": 5, "days": 5},
    {"field": "close_lt_ma", "ma": 20},
    {"field": "return_ndays", "days": 30, "min_pct": 40},
    {"field": "return_ndays", "days": 5, "max_pct": -5},
    {"field": "new_high", "days": 100},
    {"field": "new_low", "days": 60},
    {"field": "free_float_cap", "min_yi": 30, "max_yi": 200},
    {"field": "total_cap", "min_yi": 100},
    {"field": "amount", "min_yi": 30},
    {"field": "turnover_rate", "min_pct": 5},
    {"field": "price", "min": 5, "max": 50},
    {"field": "volume_surge", "ratio": 2},
    {"field": "consecutive_up", "days": 3},
    {"field": "consecutive_down", "days": 3},
    {"field": "ma_bullish"},
    {"field": "ma_bearish"},
    {"field": "drawdown_from_high", "days": 100, "max_pct": 10},
    {"field": "sector", "name": "半导体"},
    {"field": "board", "name": "主板"},
    {"field": "board", "name": "北交所", "exclude": true}
  ]
}
规则：
- field 取值仅限上例出现的字段名，无法识别的条件请忽略。
- close_gt_ma：收盘价大于 N 日均线，ma 为整数(5/10/20)；若要求"连续 M 日收盘价在 N 日线上/上方"，加 days=M。
- close_lt_ma：收盘价低于/跌破 N 日均线；"跌破20日线""20日线下方"→ma=20，同样支持 days=M。
- return_ndays：N 日涨幅区间，days 为数字；"5日涨幅大于10%"→days=5,min_pct=10；"日涨幅/今日涨幅/当日涨幅大于2%"→days=1,min_pct=2；"涨幅小于20%"→max_pct=20；"5日跌幅大于5%"→max_pct=-5；"今日下跌"→days=1,max_pct=0。
- new_high / new_low：N 日新高/新低，days 为整数；"百日新高"→days=100。
- free_float_cap：自由流通市值，"大于X亿"→min_yi=X，"小于X亿"→max_yi=X，单位统一为"亿"。
- total_cap：总市值，min_yi/max_yi，单位亿。
- amount：今日成交额，"成交额大于30亿"→min_yi=30，"成交额小于5亿"→max_yi=5，单位亿。
- turnover_rate：今日换手率百分比，"换手率大于5%"→min_pct=5，"换手率小于20%"→max_pct=20。
- price：股价区间(元)，"股价大于5元"→min=5，"股价10到50元"→min=10,max=50。
- volume_surge：放量/量比，今日成交量 ≥ 5日均量×ratio；"量比大于2""放量2倍"→ratio=2。
- consecutive_up / consecutive_down：连续上涨/下跌 N 天，days=N。
- ma_bullish / ma_bearish：均线多头排列(5>10>20) / 空头排列(5<10<20)，无参数。
- drawdown_from_high：距 N 日最高点的回撤百分比，"距百日高点回撤10%以内"→days=100,max_pct=10；"回撤超过30%"→min_pct=30。
- sector：属于某行业/概念板块，name 为板块名（如 半导体、光伏）。
- board：市场板块，name 仅限 主板/创业板/科创板/北交所；"非北交所""剔除北交所""不要北交所"→加 "exclude": true。
- 只输出 JSON，不要解释。"""


def _ensure_client():
    if not _client:
        raise RuntimeError("未配置 QWEN_API_KEY，AI 助手不可用。请在 .env 设置 DASHSCOPE_API_KEY。")


def _build_messages(question: str, history: List[dict] | None) -> list:
    """构建消息列表：固定前缀走 Prompt Caching，动态 RAG 上下文追加。"""
    try:
        ctx_chunks = knowledge.search(question, k=5)
    except Exception as e:
        logger.warning("知识检索失败，退化为无上下文回答：%s", e)
        ctx_chunks = []
    context = "\n\n---\n\n".join(ctx_chunks) if ctx_chunks else "（知识库暂无相关内容）"
    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(context=context)}]
    for h in history or []:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": question})
    return messages


def _run_tool_rounds(messages: list) -> str:
    """执行工具调用轮次，返回最终文本。提前终止：连续2轮未产生工具调用即退出。"""
    unused_rounds = 0
    for _ in range(MAX_TOOL_ROUNDS):
        resp = _client.chat.completions.create(
            model=cfg.QWEN_PLUS_MODEL, messages=messages,
            tools=market_tools.TOOL_SCHEMAS, tool_choice="auto",
            temperature=0.4,
            extra_body={"enable_thinking": False},
        )
        msg = resp.choices[0].message
        if not msg.tool_calls:
            if unused_rounds >= 1:
                return (msg.content or "").strip()
            unused_rounds += 1
            return (msg.content or "").strip()
        unused_rounds = 0
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [{
                "id": tc.id, "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            } for tc in msg.tool_calls],
        })
        for tc in msg.tool_calls:
            result = market_tools.execute(tc.function.name, tc.function.arguments)
            logger.info("工具调用 %s(%s) → %s", tc.function.name, tc.function.arguments, result[:120])
            messages.append({
                "role": "tool", "tool_call_id": tc.id,
                "name": tc.function.name, "content": result,
            })
    return "（工具调用轮次超限，请简化问题后重试。）"


def chat(question: str, history: List[dict] | None = None) -> str:
    """RAG + 实时行情工具的问答（同步，返回完整文本）。"""
    _ensure_client()
    messages = _build_messages(question, history)
    try:
        return _run_tool_rounds(messages)
    except Exception as e:
        logger.error("Qwen 调用失败：%s", e)
        return f"AI 暂时不可用：{e}"


def chat_stream(question: str, history: List[dict] | None = None) -> Generator[str, None, None]:
    """流式问答：先执行工具调用轮次（非流式），最终回答用流式输出。
    配合 st.write_stream() 使用，用户 1-2 秒内可见首字。"""
    _ensure_client()
    messages = _build_messages(question, history)
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            resp = _client.chat.completions.create(
                model=cfg.QWEN_PLUS_MODEL, messages=messages,
                tools=market_tools.TOOL_SCHEMAS, tool_choice="auto",
                temperature=0.4,
                extra_body={"enable_thinking": False},
            )
            msg = resp.choices[0].message
            if not msg.tool_calls:
                break
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [{
                    "id": tc.id, "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                } for tc in msg.tool_calls],
            })
            for tc in msg.tool_calls:
                result = market_tools.execute(tc.function.name, tc.function.arguments)
                logger.info("工具调用 %s(%s) → %s", tc.function.name, tc.function.arguments, result[:120])
                messages.append({
                    "role": "tool", "tool_call_id": tc.id,
                    "name": tc.function.name, "content": result,
                })
        else:
            yield "（工具调用轮次超限，请简化问题后重试。）"
            return
        # 最终回答走流式
        stream = _client.chat.completions.create(
            model=cfg.QWEN_PLUS_MODEL, messages=messages,
            temperature=0.4,
            stream=True,
            extra_body={"enable_thinking": False},
        )
        for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content
    except Exception as e:
        logger.error("Qwen 流式调用失败：%s", e)
        yield f"AI 暂时不可用：{e}"


def parse_filter_conditions(text: str) -> dict:
    """自然语言 → {conditions:[...]}。优先本地关键字识别（毫秒级），识别不出再走模型。"""
    local = _keyword_fallback(text)
    if local:
        return {"conditions": local}
    _ensure_client()
    try:
        resp = _client.chat.completions.create(
            model=cfg.QWEN_TURBO_MODEL,
            messages=[
                {"role": "system", "content": FILTER_SCHEMA_INSTRUCTIONS},
                {"role": "user", "content": text},
            ],
            temperature=0.0,
            extra_body={"enable_thinking": False},
        )
        raw = resp.choices[0].message.content.strip()
        raw = _extract_json(raw)
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("conditions"), list):
            return {"conditions": _normalize_conditions(data["conditions"])}
    except Exception as e:
        logger.warning("模型解析失败，回退关键字识别：%s", e)
    return {"conditions": _keyword_fallback(text)}


def _extract_json(raw: str) -> str:
    """从可能含 ```json 围栏或多余文字的回复里提取 JSON。"""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    m = re.search(r"\{.*\}", raw, re.S)
    return m.group(0) if m else raw


def _normalize_conditions(conds: list) -> list:
    def _rng(c, item, lo_key, hi_key):
        """拷贝区间参数，至少一端存在才有效。"""
        ok = False
        for k in (lo_key, hi_key):
            if c.get(k) is not None:
                item[k] = float(c[k])
                ok = True
        return ok

    out = []
    for c in conds:
        if not isinstance(c, dict):
            continue
        f = c.get("field")
        if f in ("close_gt_ma", "close_lt_ma") and c.get("ma"):
            item = {"field": f, "ma": int(c["ma"])}
            if c.get("days") and int(c["days"]) > 1:
                item["days"] = int(c["days"])
            out.append(item)
        elif f == "return_ndays" and c.get("days"):
            item = {"field": "return_ndays", "days": int(c["days"])}
            if not _rng(c, item, "min_pct", "max_pct"):
                item["min_pct"] = 0.0
            out.append(item)
        elif f in ("new_high", "new_low") and c.get("days"):
            out.append({"field": f, "days": int(c["days"])})
        elif f in ("free_float_cap", "total_cap", "amount"):
            item = {"field": f}
            if _rng(c, item, "min_yi", "max_yi"):
                out.append(item)
        elif f == "turnover_rate":
            item = {"field": f}
            if _rng(c, item, "min_pct", "max_pct"):
                out.append(item)
        elif f == "price":
            item = {"field": f}
            if _rng(c, item, "min", "max"):
                out.append(item)
        elif f == "volume_surge" and c.get("ratio"):
            out.append({"field": "volume_surge", "ratio": float(c["ratio"])})
        elif f in ("consecutive_up", "consecutive_down") and c.get("days"):
            out.append({"field": f, "days": int(c["days"])})
        elif f in ("ma_bullish", "ma_bearish"):
            out.append({"field": f})
        elif f == "drawdown_from_high":
            item = {"field": f, "days": int(c.get("days", 100) or 100)}
            if _rng(c, item, "min_pct", "max_pct"):
                out.append(item)
        elif f == "sector" and c.get("name"):
            out.append({"field": "sector", "name": str(c["name"])})
        elif f == "board" and c.get("name") in ("主板", "创业板", "科创板", "北交所"):
            item = {"field": "board", "name": str(c["name"])}
            if c.get("exclude"):
                item["exclude"] = True
            out.append(item)
    return out


_CN_NUM = {"一": 1, "二": 2, "三": 3, "五": 5, "十": 10, "二十": 20, "三十": 30,
           "五十": 50, "百": 100, "百日": 100}


def _cn_to_num(s: str) -> int | None:
    if s.isdigit():
        return int(s)
    return _CN_NUM.get(s)


_LT_WORDS = ("小于", "低于", "不超过", "不足", "以下", "以内")
_OP = r"(大于|超过|高于|不低于|小于|低于|不超过|不足)?"
_SUF = r"\s*(以上|以下|以内)?"


def _is_lt(op: str | None, suffix: str | None = None) -> bool:
    return (op or "") in _LT_WORDS or (suffix or "") in _LT_WORDS


def _keyword_fallback(text: str) -> list:
    """模型不可用时的关键字识别，保证即开即用。"""
    conds = []
    # 市场板块：主板 / 非(剔除/排除/不要)北交所|创业板|科创板（支持"剔除A和B"）
    _B = r"(?:北交所|创业板|科创板|主板)"
    for m in re.finditer(rf"(?:非|剔除|排除|不要|不含|去掉)\s*({_B}(?:\s*[、和及,，]\s*{_B})*)", text):
        for b in re.findall(_B, m.group(1)):
            conds.append({"field": "board", "name": b, "exclude": True})
    _excluded = {c["name"] for c in conds if c.get("field") == "board"}
    for b in ("主板", "创业板", "科创板", "北交所"):
        if b in text and b not in _excluded:
            conds.append({"field": "board", "name": b})
    # 连续M日收盘在N日线上
    used_ma_spans = []
    for m in re.finditer(r"连续\s*(\d+|[一二三五十]+)\s*[日天].*?(\d+|[一二三五十]+)\s*日(?:均线|线)", text):
        md, n = _cn_to_num(m.group(1)), _cn_to_num(m.group(2))
        if md and n:
            conds.append({"field": "close_gt_ma", "ma": n, "days": md})
            used_ma_spans.append(m.span())
    # 跌破N日线 / N日线下方 → close_lt_ma（先匹配并记录区间，避免被误判为线上）
    for m in re.finditer(r"跌破\s*(\d+|[一二三五二十三十百])\s*日(?:均线|线)", text):
        n = _cn_to_num(m.group(1))
        if n:
            conds.append({"field": "close_lt_ma", "ma": n})
            used_ma_spans.append(m.span())
    for m in re.finditer(r"(\d+|[一二三五二十三十百])\s*日(?:均线|线)\s*(?:下方|之下|以下)", text):
        n = _cn_to_num(m.group(1))
        if n and not any(c.get("field") == "close_lt_ma" and c.get("ma") == n for c in conds):
            conds.append({"field": "close_lt_ma", "ma": n})
            used_ma_spans.append(m.span())
    # N日均线（避开已被"连续"/"跌破"匹配的片段）
    for m in re.finditer(r"(\d+|[一二三五二十三十百])\s*日(?:均线|线上|线之上)", text):
        if any(s <= m.start() < e for s, e in used_ma_spans):
            continue
        n = _cn_to_num(m.group(1))
        if n and not any(c.get("field") in ("close_gt_ma", "close_lt_ma") and c.get("ma") == n
                         for c in conds):
            conds.append({"field": "close_gt_ma", "ma": n})
    # N日涨幅大于/小于X%（数字与"涨幅"间不得跨标点，避免误吞前文的"5日均线，"）
    for m in re.finditer(rf"(\d+|[一二三五二十三十百])\s*[日天][^%，,。；;]*?涨幅\s*{_OP}[^0-9%]*?(\d+(?:\.\d+)?)\s*%", text):
        n = _cn_to_num(m.group(1))
        if n:
            key = "max_pct" if _is_lt(m.group(2)) else "min_pct"
            conds.append({"field": "return_ndays", "days": n, key: float(m.group(3))})
    # N日跌幅大于/小于X%（跌幅→负值涨幅）
    for m in re.finditer(rf"(\d+|[一二三五二十三十百])\s*[日天][^%，,。；;]*?跌幅\s*{_OP}[^0-9%]*?(\d+(?:\.\d+)?)\s*%", text):
        n = _cn_to_num(m.group(1))
        if n:
            key = "min_pct" if _is_lt(m.group(2)) else "max_pct"
            conds.append({"field": "return_ndays", "days": n, key: -float(m.group(3))})
    # 今日/当日/日涨幅（无数字前缀 → days=1）
    for m in re.finditer(rf"(?:今日|当日|(?<![\d一二三五十])日)\s*涨幅\s*{_OP}[^0-9%]*?(\d+(?:\.\d+)?)\s*%", text):
        key = "max_pct" if _is_lt(m.group(1)) else "min_pct"
        if not any(c.get("field") == "return_ndays" and c.get(key) == float(m.group(2))
                   for c in conds):
            conds.append({"field": "return_ndays", "days": 1, key: float(m.group(2))})
    for m in re.finditer(rf"(?:今日|当日|(?<![\d一二三五十])日)\s*跌幅\s*{_OP}[^0-9%]*?(\d+(?:\.\d+)?)\s*%", text):
        key = "min_pct" if _is_lt(m.group(1)) else "max_pct"
        conds.append({"field": "return_ndays", "days": 1, key: -float(m.group(2))})
    # 百日新高 / N日新高 / 新低
    if "百日新高" in text or "100日新高" in text:
        conds.append({"field": "new_high", "days": 100})
    for m in re.finditer(r"(\d+)\s*日新高", text):
        conds.append({"field": "new_high", "days": int(m.group(1))})
    if "百日新低" in text or "100日新低" in text:
        conds.append({"field": "new_low", "days": 100})
    for m in re.finditer(r"(\d+)\s*日新低", text):
        conds.append({"field": "new_low", "days": int(m.group(1))})
    # 亿元区间写法："成交额10到50亿""流通市值30-200亿"
    for field, kw in (("amount", "成交额"), ("total_cap", "总市值"),
                      ("free_float_cap", r"(?:自由)?流通市值")):
        for m in re.finditer(rf"{kw}\s*(?:在)?\s*(\d+(?:\.\d+)?)\s*(?:亿)?\s*(?:到|至|[-~])\s*(\d+(?:\.\d+)?)\s*亿", text):
            conds.append({"field": field, "min_yi": float(m.group(1)),
                          "max_yi": float(m.group(2))})
    # 今日成交额大于/小于X亿
    for m in re.finditer(rf"成交额\s*(?:在)?\s*{_OP}\s*(\d+(?:\.\d+)?)\s*亿{_SUF}", text):
        key = "max_yi" if _is_lt(m.group(1), m.group(3)) else "min_yi"
        conds.append({"field": "amount", key: float(m.group(2))})
    # 换手率大于/小于X%
    for m in re.finditer(rf"换手率?\s*{_OP}\s*(\d+(?:\.\d+)?)\s*%?", text):
        key = "max_pct" if _is_lt(m.group(1)) else "min_pct"
        conds.append({"field": "turnover_rate", key: float(m.group(2))})
    # 总市值大于/小于X亿
    for m in re.finditer(rf"总市值\s*(?:在)?\s*{_OP}\s*(\d+(?:\.\d+)?)\s*亿{_SUF}", text):
        key = "max_yi" if _is_lt(m.group(1), m.group(3)) else "min_yi"
        conds.append({"field": "total_cap", key: float(m.group(2))})
    # 流通市值大于/小于X亿
    for m in re.finditer(rf"(?:自由)?流通市值\s*(?:在)?\s*{_OP}\s*(\d+(?:\.\d+)?)\s*亿{_SUF}", text):
        key = "max_yi" if _is_lt(m.group(1), m.group(3)) else "min_yi"
        conds.append({"field": "free_float_cap", key: float(m.group(2))})
    # 股价区间 / 股价大于X元
    for m in re.finditer(r"(?:股价|价格|现价)\s*(?:在)?\s*(\d+(?:\.\d+)?)\s*(?:元)?\s*(?:到|至|[-~])\s*(\d+(?:\.\d+)?)\s*元", text):
        conds.append({"field": "price", "min": float(m.group(1)), "max": float(m.group(2))})
    for m in re.finditer(rf"(?:股价|价格|现价)\s*{_OP}\s*(\d+(?:\.\d+)?)\s*元{_SUF}", text):
        if m.group(1) or m.group(3):
            key = "max" if _is_lt(m.group(1), m.group(3)) else "min"
            if not any(c.get("field") == "price" for c in conds):
                conds.append({"field": "price", key: float(m.group(2))})
    # 量比大于X / 放量X倍
    for m in re.finditer(r"量比\s*(?:大于|超过|高于)?\s*(\d+(?:\.\d+)?)", text):
        conds.append({"field": "volume_surge", "ratio": float(m.group(1))})
    for m in re.finditer(r"放量\s*(\d+(?:\.\d+)?)\s*倍", text):
        if not any(c.get("field") == "volume_surge" for c in conds):
            conds.append({"field": "volume_surge", "ratio": float(m.group(1))})
    # 连涨/连跌N天
    for m in re.finditer(r"(?:连续上涨|连涨)\s*(\d+|[一二三五十]+)\s*[日天]?", text):
        n = _cn_to_num(m.group(1))
        if n:
            conds.append({"field": "consecutive_up", "days": n})
    for m in re.finditer(r"(?:连续下跌|连跌)\s*(\d+|[一二三五十]+)\s*[日天]?", text):
        n = _cn_to_num(m.group(1))
        if n:
            conds.append({"field": "consecutive_down", "days": n})
    # 均线多头/空头排列
    if "多头排列" in text:
        conds.append({"field": "ma_bullish"})
    if "空头排列" in text:
        conds.append({"field": "ma_bearish"})
    # 距高点回撤
    for m in re.finditer(rf"回撤\s*{_OP}\s*(\d+(?:\.\d+)?)\s*%{_SUF}", text):
        key = "max_pct" if _is_lt(m.group(1), m.group(3)) else "min_pct"
        conds.append({"field": "drawdown_from_high", "days": 100, key: float(m.group(2))})
    # 板块
    m = re.search(r"属于(.+?)板块", text) or re.search(r"(.+?)板块", text)
    if m:
        name = m.group(1).strip()
        if name and len(name) <= 8:
            conds.append({"field": "sector", "name": name})
    return conds
