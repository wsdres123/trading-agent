"""预测持久化 + 未来收益追踪（评测体系 Layer 3 基础设施）。

每条预测记录追加写入 .data/predictions.jsonl，包含完整 metadata：
  prediction_id / as_of_timestamp / data_cutoff_timestamp / type /
  model_id / prompt_version / feature_version / date /
  signal / node / confidence / evidence_snapshot / position_cap / abstain /
  future_outcome (初始为 null，由 update_future_outcomes() 回填)

未来收益基准：
  timing → sh000001 上证指数 1/3/5 日方向命中率
  emotion → 883958 连板指数 1/3/5 日溢价
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime

import pandas as pd

from config import settings as cfg
from src import data

logger = logging.getLogger("predictions")

PREDICTION_STORE = cfg.DATA_DIR / "predictions.jsonl"

PROMPT_VERSION = "v1.0"
FEATURE_VERSION = "v1.0"


def append_prediction(
    type: str,
    date: str,
    signal: str | None = None,
    node: str | None = None,
    confidence: float = 0.0,
    evidence_snapshot: list | None = None,
    position_cap: float | None = None,
    abstain: bool = False,
    model_id: str = "",
    source: str = "production",
    extra: dict | None = None,
) -> str | None:
    """追加一条预测记录到 .data/predictions.jsonl。

    按 (type, date, source) 去重：已存在则更新记录，避免重复。
    返回 prediction_id；若未写入则返回 None。
    """
    PREDICTION_STORE.parent.mkdir(parents=True, exist_ok=True)

    records = load_predictions()
    existing_idx = None
    for i, r in enumerate(records):
        if r.get("type") == type and r.get("date") == date and r.get("source") == source:
            existing_idx = i
            break

    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    if existing_idx is not None:
        pid = records[existing_idx].get("prediction_id",
                                        f"{type}_{date}_{uuid.uuid4().hex[:8]}")
    else:
        pid = f"{type}_{date}_{uuid.uuid4().hex[:8]}"

    record = {
        "prediction_id": pid,
        "as_of_timestamp": now,
        "data_cutoff_timestamp": f"{date}T15:00:00",
        "type": type,
        "model_id": model_id or cfg.QWEN_PLUS_MODEL,
        "prompt_version": PROMPT_VERSION,
        "feature_version": FEATURE_VERSION,
        "date": date,
        "source": source,
        "signal": signal,
        "node": node,
        "confidence": round(confidence, 4),
        "evidence_snapshot": evidence_snapshot or [],
        "position_cap": position_cap,
        "abstain": abstain,
        "future_outcome": None,
    }
    if extra:
        record.update(extra)

    if existing_idx is not None:
        records[existing_idx] = record
        PREDICTION_STORE.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8",
        )
        return pid

    with open(PREDICTION_STORE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return pid


def load_predictions(
    date_str: str | None = None,
    type: str | None = None,
) -> list[dict]:
    """加载预测记录，可按日期/类型筛选。"""
    if not PREDICTION_STORE.exists():
        return []
    records = []
    for line in PREDICTION_STORE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if date_str and r.get("date") != date_str:
            continue
        if type and r.get("type") != type:
            continue
        records.append(r)
    return records


def _compute_index_returns(
    index_df: pd.DataFrame,
    signal_date: str,
    periods: tuple[int, ...] = (1, 3, 5),
) -> dict:
    """计算指数在 signal_date 后 N 日的收益率。

    返回 {d1: {return_pct, direction}, d3: {...}, d5: {...}}
    """
    result = {}
    if index_df is None or index_df.empty:
        return {f"d{p}": None for p in periods}

    df = index_df.sort_values("日期").reset_index(drop=True)
    df["日期"] = df["日期"].astype(str).str[:10]

    idx = df.index[df["日期"] == signal_date].tolist()
    if not idx:
        signal_date_norm = signal_date.replace("-", "")
        idx = df.index[df["日期"].str.replace("-", "") == signal_date_norm].tolist()
    if not idx:
        return {f"d{p}": None for p in periods}

    pos = idx[0]
    base_close = float(df.iloc[pos]["收盘"])

    for p in periods:
        target_pos = pos + p
        if target_pos >= len(df):
            result[f"d{p}"] = None
            continue
        target_close = float(df.iloc[target_pos]["收盘"])
        ret_pct = round((target_close / base_close - 1) * 100, 2)
        if ret_pct > 0.1:
            direction = "up"
        elif ret_pct < -0.1:
            direction = "down"
        else:
            direction = "flat"
        result[f"d{p}"] = {
            "return_pct": ret_pct,
            "direction": direction,
            "target_date": str(df.iloc[target_pos]["日期"]),
        }
    return result


def compute_future_outcome(prediction: dict) -> dict | None:
    """为单条预测计算未来收益。

    timing: sh000001 上证指数 1/3/5 日方向
    emotion: 883958 连板指数 1/3/5 日溢价
    shortterm: 883958 连板指数 1/3/5 日方向
    """
    ptype = prediction.get("type", "")
    date = prediction.get("date", "")
    if not date:
        return None

    periods = (1, 3, 5)

    if ptype == "timing":
        idx_df = data.get_index_daily("sh000001", days=400)
        returns = _compute_index_returns(idx_df, date, periods)
        signal = prediction.get("signal", "")
        hit = {}
        for p in periods:
            r = returns.get(f"d{p}")
            if r is None:
                hit[f"d{p}"] = None
            elif signal == "多":
                hit[f"d{p}"] = r["direction"] == "up"
            elif signal == "空":
                hit[f"d{p}"] = r["direction"] == "down"
            elif signal == "转":
                hit[f"d{p}"] = r["direction"] in ("up", "down")
            else:
                hit[f"d{p}"] = None
        return {"returns": returns, "direction_hit": hit, "benchmark": "sh000001"}

    if ptype in ("emotion", "shortterm"):
        idx_df = data.get_ths_index_daily("883958", days=400)
        returns = _compute_index_returns(idx_df, date, periods)
        return {"returns": returns, "benchmark": "883958"}

    return None


def update_future_outcomes(max_days: int = 90) -> int:
    """扫描所有 future_outcome 为 null 的预测，计算并回填。

    只处理 date 距今 >= 1 个交易日的记录（确保有至少 1 天未来数据）。
    返回更新条数。
    """
    records = load_predictions()
    if not records:
        return 0

    updated = 0
    lines = []
    for r in records:
        if r.get("future_outcome") is not None:
            lines.append(json.dumps(r, ensure_ascii=False))
            continue
        outcome = compute_future_outcome(r)
        if outcome is not None:
            r["future_outcome"] = outcome
            updated += 1
        lines.append(json.dumps(r, ensure_ascii=False))

    if updated > 0:
        PREDICTION_STORE.write_text(
            "\n".join(lines) + "\n", encoding="utf-8")

    return updated
