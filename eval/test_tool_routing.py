"""维度 7：工具调用正确性 — 验证 AI 是否调用了正确的工具。"""
from __future__ import annotations

from eval.common import save_result

CASES = [
    {"question": "今天上证涨了多少", "expected_tool": "get_index_quote"},
    {"question": "贵州茅台现在多少钱", "expected_tool": "get_stock_quote"},
    {"question": "半导体板块表现怎么样", "expected_tool": "get_sector_quote"},
    {"question": "今天大盘怎么样", "expected_tool": "get_market_overview"},
    {"question": "贵州茅台最近走势如何", "expected_tool": "get_stock_history"},
]


def _capture_tool_calls(question: str) -> list[dict]:
    """拦截工具调用，记录调用的工具名和参数，不实际执行。"""
    from openai import OpenAI
    from config import settings as cfg
    from src import ai_assistant, market_tools

    if not cfg.QWEN_API_KEY:
        return []

    calls = []
    try:
        ctx_chunks = []
        messages = [
            {"role": "system", "content": ai_assistant.SYSTEM_PROMPT.format(context="")},
            {"role": "user", "content": question},
        ]
        client = OpenAI(api_key=cfg.QWEN_API_KEY, base_url=cfg.QWEN_BASE_URL)
        resp = client.chat.completions.create(
            model=cfg.QWEN_PLUS_MODEL,
            messages=messages,
            tools=market_tools.TOOL_SCHEMAS,
            tool_choice="auto",
            temperature=0.4,
            extra_body={"enable_thinking": False},
        )
        msg = resp.choices[0].message
        if msg.tool_calls:
            for tc in msg.tool_calls:
                calls.append({
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                })
    except Exception:
        pass
    return calls


def run() -> dict:
    results = {"total": len(CASES), "correct": 0, "pass": 0, "fail": 0, "details": []}

    for case in CASES:
        calls = _capture_tool_calls(case["question"])
        if calls and calls[0]["name"] == case["expected_tool"]:
            results["correct"] += 1
            results["pass"] += 1
            results["details"].append({
                "question": case["question"],
                "status": "pass",
                "called": calls[0]["name"],
            })
        else:
            results["fail"] += 1
            results["details"].append({
                "question": case["question"],
                "status": "fail",
                "expected": case["expected_tool"],
                "called": calls[0]["name"] if calls else "none",
            })

    results["accuracy"] = round(results["correct"] / results["total"], 4) if results["total"] else 0
    save_result("tool_routing", results)
    return results
