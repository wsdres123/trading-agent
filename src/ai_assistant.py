"""AI 助手：Qwen 对话（RAG 知识 + 实时行情工具调用）+ 筛选条件解析。

- chat(): RAG 检索交易系统知识 + 按需调用行情工具(指数/个股/历史/板块/大盘)，
  既能回答屠龙表/周期/心态类问题，也能回答"今天上证涨多少"等实时数据问题。
- parse_filter_conditions(): 自然语言 → 结构化 JSON 条件；失败回退关键字识别。
"""
from __future__ import annotations

import json
import logging
import re
from typing import List

from openai import OpenAI

from config import settings as cfg
from src import knowledge, market_tools

logger = logging.getLogger("ai_assistant")

_client = OpenAI(api_key=cfg.QWEN_API_KEY, base_url=cfg.QWEN_BASE_URL) if cfg.QWEN_API_KEY else None

MAX_TOOL_ROUNDS = 5

SYSTEM_PROMPT = """你是「劫财AI交易」的专属助手，服务一位 A 股短线/趋势交易者。回答分两类处理：

1. 交易系统/规则/周期/心态类问题（如趋势A周期、主线筛选、屠龙表、空仓纪律）：
   严格依据下方【知识库片段】回答；知识库未覆盖则如实说明，不要编造交易规则。

2. 实时行情/个股数据/大盘/板块/通用金融知识类问题（如"今天上证涨多少""贵州茅台现价"）：
   调用提供的工具获取实时数据，结合自身金融知识作答。数据为 A 股，涨为正数、跌为负数。

风格：简洁、要点化、可操作；回答行情务必给出具体数字与涨跌幅。若用户问个股筛选，
可建议其使用「个股分组筛选」功能。工具返回的数据即视为权威，不要凭空臆测数值。
【知识库片段】
{context}"""

FILTER_SCHEMA_INSTRUCTIONS = """把用户对个股筛选的自然语言描述解析成 JSON，字段如下，只输出 JSON：
{
  "conditions": [
    {"field": "close_gt_ma", "ma": 5},
    {"field": "close_gt_ma", "ma": 5, "days": 5},
    {"field": "return_ndays", "days": 30, "min_pct": 40},
    {"field": "return_ndays", "days": 5, "min_pct": 10},
    {"field": "new_high", "days": 100},
    {"field": "free_float_cap", "min_yi": 30},
    {"field": "sector", "name": "半导体"},
    {"field": "board", "name": "主板"},
    {"field": "board", "name": "北交所", "exclude": true}
  ]
}
规则：
- field 取值仅限：close_gt_ma / return_ndays / new_high / free_float_cap / sector / board。
- close_gt_ma：收盘价大于 N 日均线，ma 为整数(5/10/20)；若要求"连续 M 日收盘价在 N 日线上/上方"，加 days=M。
- return_ndays：N 日涨幅大于 X%，days 与 min_pct 为数字；"5日涨幅大于10%"→days=5,min_pct=10；"日涨幅/今日涨幅/当日涨幅大于2%"→days=1,min_pct=2。
- new_high：N 日新高，days 为整数；"百日新高"→days=100。
- free_float_cap：自由流通市值大于 X 亿，min_yi 为数字；单位统一为"亿"。
- sector：属于某行业/概念板块，name 为板块名（如 半导体、光伏）。
- board：市场板块，name 仅限 主板/创业板/科创板/北交所；"非北交所""剔除北交所""不要北交所"→加 "exclude": true。
- 只输出 JSON，不要解释。无法识别的条件请忽略。"""


def _ensure_client():
    if not _client:
        raise RuntimeError("未配置 QWEN_API_KEY，AI 助手不可用。请在 .env 设置 DASHSCOPE_API_KEY。")


def chat(question: str, history: List[dict] | None = None) -> str:
    """RAG + 实时行情工具的问答。history 为 [{role,user/assistant, content}]。"""
    _ensure_client()
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

    try:
        for _ in range(MAX_TOOL_ROUNDS):
            resp = _client.chat.completions.create(
                model=cfg.QWEN_CHAT_MODEL, messages=messages,
                tools=market_tools.TOOL_SCHEMAS, tool_choice="auto",
                temperature=0.4,
            )
            msg = resp.choices[0].message
            if not msg.tool_calls:
                return (msg.content or "").strip()
            # 把助手消息（含 tool_calls）原样回填，再追加各工具结果
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
    except Exception as e:
        logger.error("Qwen 调用失败：%s", e)
        return f"AI 暂时不可用：{e}"


def parse_filter_conditions(text: str) -> dict:
    """自然语言 → {conditions:[...]}。模型失败回退关键字识别。"""
    _ensure_client()
    try:
        resp = _client.chat.completions.create(
            model=cfg.QWEN_CHAT_MODEL,
            messages=[
                {"role": "system", "content": FILTER_SCHEMA_INSTRUCTIONS},
                {"role": "user", "content": text},
            ],
            temperature=0.0,
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
    out = []
    for c in conds:
        if not isinstance(c, dict):
            continue
        f = c.get("field")
        if f == "close_gt_ma" and c.get("ma"):
            item = {"field": "close_gt_ma", "ma": int(c["ma"])}
            if c.get("days") and int(c["days"]) > 1:
                item["days"] = int(c["days"])
            out.append(item)
        elif f == "return_ndays" and c.get("days"):
            out.append({"field": "return_ndays", "days": int(c["days"]),
                        "min_pct": float(c.get("min_pct", 0))})
        elif f == "new_high" and c.get("days"):
            out.append({"field": "new_high", "days": int(c["days"])})
        elif f == "free_float_cap" and c.get("min_yi") is not None:
            out.append({"field": "free_float_cap", "min_yi": float(c["min_yi"])})
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
    # N日均线（避开已被"连续"匹配的片段）
    for m in re.finditer(r"(\d+|[一二三五二十三十百])\s*日(?:均线|线上|线之上)", text):
        if any(s <= m.start() < e for s, e in used_ma_spans):
            continue
        n = _cn_to_num(m.group(1))
        if n and not any(c.get("field") == "close_gt_ma" and c.get("ma") == n for c in conds):
            conds.append({"field": "close_gt_ma", "ma": n})
    # N日涨幅大于X%
    for m in re.finditer(r"(\d+|[一二三五二十三十百])\s*[日天].*?涨幅.*?(\d+(?:\.\d+)?)\s*%", text):
        n = _cn_to_num(m.group(1))
        if n:
            conds.append({"field": "return_ndays", "days": n, "min_pct": float(m.group(2))})
    # 今日/当日/日涨幅大于X%（无数字前缀 → days=1）
    for m in re.finditer(r"(?:今日|当日|(?<![\d一二三五十])日)\s*涨幅[^%]*?(\d+(?:\.\d+)?)\s*%", text):
        if not any(c.get("field") == "return_ndays" and c.get("min_pct") == float(m.group(1))
                   for c in conds):
            conds.append({"field": "return_ndays", "days": 1, "min_pct": float(m.group(1))})
    # 百日新高 / N日新高
    if "百日新高" in text or "100日新高" in text:
        conds.append({"field": "new_high", "days": 100})
    for m in re.finditer(r"(\d+)\s*日新高", text):
        conds.append({"field": "new_high", "days": int(m.group(1))})
    # 流通市值大于X亿
    for m in re.finditer(r"(?:自由)?流通市值.*?(\d+(?:\.\d+)?)\s*亿", text):
        conds.append({"field": "free_float_cap", "min_yi": float(m.group(1))})
    # 板块
    m = re.search(r"属于(.+?)板块", text) or re.search(r"(.+?)板块", text)
    if m:
        name = m.group(1).strip()
        if name and len(name) <= 8:
            conds.append({"field": "sector", "name": name})
    return conds
