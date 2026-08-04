"""维度 5：RAG 检索质量 — 验证知识库能否召回相关内容。"""
from __future__ import annotations

from eval.common import load_jsonl, save_result


def run() -> dict:
    cases = load_jsonl("rag_queries.jsonl")
    if not cases:
        return {"error": "无评测数据", "pass": 0, "fail": 0}

    from src import knowledge

    correct = 0
    total = len(cases)
    details = []

    for case in cases:
        query = case["query"]
        keywords = case["expected_keywords"]
        try:
            chunks = knowledge.search(query, k=5)
            combined = " ".join(chunks)
            hits = [kw for kw in keywords if kw in combined]
            recall = len(hits) / len(keywords) if keywords else 1.0
            if recall >= 0.5:
                correct += 1
                details.append({"query": query, "status": "pass", "recall": round(recall, 2)})
            else:
                details.append({
                    "query": query, "status": "fail", "recall": round(recall, 2),
                    "missed": [kw for kw in keywords if kw not in combined],
                })
        except Exception as e:
            details.append({"query": query, "status": "error", "error": str(e)})

    accuracy = correct / total if total else 0
    results = {
        "total": total,
        "correct": correct,
        "recall_accuracy": round(accuracy, 4),
        "pass": correct,
        "fail": total - correct,
        "details": [d for d in details if d["status"] != "pass"],
    }

    save_result("rag_recall", results)
    return results
