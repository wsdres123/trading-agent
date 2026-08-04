"""维度 6：LLM 输出质量 — LLM-as-Judge 自动打分。"""
from __future__ import annotations

import json
import re

from eval.common import save_result

JUDGE_PROMPT = """你是评测员，对以下AI回答打分(1-5)：
评分维度：
1. 事实准确性：是否引用了正确数据
2. 可操作性：是否给出了具体建议
3. 简洁性：是否废话过多
4. 风险意识：是否提示了风险

用户问题：{question}
AI回答：{answer}
请只输出JSON：{{"factual": N, "actionable": N, "concise": N, "risk_aware": N}}"""

QUESTIONS = [
    "今天大盘怎么样？",
    "半导体板块还能追吗？",
    "我现在满仓怎么办？",
]


def _judge(question: str, answer: str) -> dict:
    from openai import OpenAI
    from config import settings as cfg
    if not cfg.QWEN_API_KEY:
        return {"factual": 0, "actionable": 0, "concise": 0, "risk_aware": 0, "error": "无API Key"}
    client = OpenAI(api_key=cfg.QWEN_API_KEY, base_url=cfg.QWEN_BASE_URL)
    try:
        resp = client.chat.completions.create(
            model=cfg.QWEN_TURBO_MODEL,
            messages=[{"role": "user", "content": JUDGE_PROMPT.format(
                question=question, answer=answer)}],
            temperature=0.0, max_tokens=200,
        )
        raw = resp.choices[0].message.content.strip()
        m = re.search(r"\{.*\}", raw, re.S)
        return json.loads(m.group(0) if m else raw)
    except Exception as e:
        return {"factual": 0, "actionable": 0, "concise": 0, "risk_aware": 0, "error": str(e)}


def run() -> dict:
    from src import ai_assistant

    results = {"total": len(QUESTIONS), "scores": [], "pass": 0, "fail": 0}

    for q in QUESTIONS:
        try:
            answer = ai_assistant.chat(q)
            scores = _judge(q, answer)
            avg = sum(scores.get(k, 0) for k in ("factual", "actionable", "concise", "risk_aware")) / 4
            results["scores"].append({
                "question": q, "scores": scores, "avg": round(avg, 2),
                "status": "pass" if scores.get("factual", 0) >= 3 else "fail",
            })
            if scores.get("factual", 0) >= 3:
                results["pass"] += 1
            else:
                results["fail"] += 1
        except Exception as e:
            results["fail"] += 1
            results["scores"].append({"question": q, "error": str(e), "status": "error"})

    if results["scores"]:
        all_avgs = [s.get("avg", 0) for s in results["scores"] if "avg" in s]
        results["overall_avg"] = round(sum(all_avgs) / len(all_avgs), 2) if all_avgs else 0

    save_result("llm_quality", results)
    return results
