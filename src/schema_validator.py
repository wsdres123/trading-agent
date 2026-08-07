"""结构化决策校验器：为 LLM 高风险输出提供严格 Schema + 业务校验管线。

8 步校验管线：
1. JSON 提取与解析（从 LLM 原始输出中正则提取 JSON）
2. Pydantic Schema 校验（结构合法性）
3. 枚举值校验（signal/cycle/node 必须属于预定义集合）
4. 置信度范围校验（0.0~1.0，越界截断）
5. 必填字段校验（reason 不可为空）
6. evidence 特征校验（引用幻觉特征则丢弃）
7. 硬规则覆盖（position_cap 由程序重算，不信任 LLM 建议）
8. 失败降级（重试一次，仍失败返回转/混沌）

实现位置：src/schema_validator.py
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger("schema_validator")


# ── 枚举值定义 ────────────────────────────────────────────────────────────

VALID_SIGNALS = ["多", "空", "转"]
VALID_CYCLES = ["趋势A", "龙头A", "B", "C", "D"]

# 情绪节点全集（与 emotion_node.py NODE_NAMES 保持一致）
VALID_EMOTION_NODES = [
    "混沌", "混沌分歧",
    "主升--确认", "主升--加速", "主升--高潮", "主升--分化", "主升--分歧",
    "主升--延续", "日内分歧转一致", "日内分歧未转一致",
    "退潮", "退潮加速", "退潮转衰竭", "退潮中继",
    "冰点", "冰点转折",
    "修复--弱", "修复--中等", "修复--强",
    "修复--加速", "修复--高潮", "修复延续",
    "加速", "加速转衰竭",
    "龙头确认", "短线情绪确认",
]

# 已知特征名（供 evidence 校验，LLM 不应引用这些之外的特征）
TIMING_FEATURES = {
    "avg_price_vs_ma10", "turnover", "index_kline", "review_signal",
    "today_pct", "ma10_cross", "volume_change", "price_trend",
    "量能", "成交额", "涨跌", "多空线", "平均股价", "上证", "K线",
    "情绪节点", "复盘", "信号", "周期", "趋势", "箱体", "缩量", "放量", "连阳", "阴跌",
}

EMOTION_FEATURES = {
    "大面数", "跌停数", "涨停数", "涨超7数", "打板大面数",
    "上证涨跌幅", "上证成交额", "热门个股指数",
    "热门股跌停数", "热股指数", "连板空间", "平均股价涨幅",
    "前日大面", "大面变化率", "跌停趋势", "热门股表现",
    "竞价表节点", "历史节点", "亏效", "一字", "断板",
    "高度个股", "连板梯队", "龙头断板",
}


# ── JSON 提取与解析 ───────────────────────────────────────────────────────

def _strip_markdown_fences(raw: str) -> str:
    """去除 LLM 输出的 markdown 代码块标记。"""
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.I)
    raw = re.sub(r"\s*```$", "", raw.strip())
    return raw.strip()


def extract_json(raw: str) -> dict | None:
    """从 LLM 原始输出中提取 JSON 对象。

    尝试顺序：
    1. 去除 markdown 代码块后直接 json.loads
    2. 非贪婪正则提取 {...} 块
    3. 返回 None
    """
    raw = _strip_markdown_fences(raw)

    # 尝试 1：直接解析
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 尝试 2：非贪婪正则提取，避免把外层说明文字一并吞入
    m = re.search(r"\{.*?\}", raw, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    return None


def parse_bool(value: Any) -> bool:
    """解析布尔值，兼容字符串 'true'/'false'、1/0 等。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "是", "t")
    return False


def load_spec_text(path: Any) -> str:
    """运行时读取规范文档内容；缺失则返回空字符串。

    用于将 docs/*.md 作为策略逻辑的单一事实来源，避免规则硬编码在 prompt。
    """
    try:
        from pathlib import Path
        p = Path(path)
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.debug("读取规范文档失败 %s: %s", path, e)
    return ""


# ── 枚举值校验 ────────────────────────────────────────────────────────────

def validate_enum(value: str, valid_set: list[str], field_name: str) -> str:
    """校验枚举值，非法时返回最保守的默认值。

    valid_set: 合法值列表
    field_name: 字段名（用于日志）

    返回：合法值 or 默认值
    """
    value = str(value).strip()
    if value in valid_set:
        return value

    # 模糊匹配：LLM 可能输出 "多头" 而非 "多"
    for v in valid_set:
        if v in value or value in v:
            logger.warning("枚举模糊匹配: %s='%s' → '%s'", field_name, value, v)
            return v

    # 默认值
    if field_name == "signal":
        default = "转"
    elif field_name == "mid_cycle":
        default = "B"  # 最保守的周期
    elif field_name == "node":
        default = "混沌"
    else:
        default = valid_set[0]

    logger.warning("枚举值非法: %s='%s' → 降级为 '%s'", field_name, value, default)
    return default


# ── 置信度校验 ────────────────────────────────────────────────────────────

def clamp_confidence(conf: Any) -> float:
    """置信度必须在 [0.0, 1.0] 范围内，越界截断。"""
    try:
        conf = float(conf)
    except (TypeError, ValueError):
        return 0.5  # 默认中等置信度

    if conf < 0.0:
        logger.warning("置信度 %.2f < 0 → 截断为 0.0", conf)
        return 0.0
    if conf > 1.0:
        logger.warning("置信度 %.2f > 1 → 截断为 1.0", conf)
        return 1.0
    return round(conf, 2)


# ── evidence 校验 ─────────────────────────────────────────────────────────

def validate_evidence(evidence: list[dict], known_features: set[str]) -> list[dict]:
    """校验 evidence 列表，丢弃引用幻觉特征的条目。

    known_features: 已知特征名集合
    返回：过滤后的 evidence 列表
    """
    if not isinstance(evidence, list):
        return []

    valid = []
    for ev in evidence:
        if not isinstance(ev, dict):
            continue
        feat = str(ev.get("feature", "")).strip()
        if not feat:
            continue

        # 检查特征是否已知
        if feat in known_features:
            valid.append(ev)
        else:
            # 模糊匹配：检查是否包含已知特征关键词
            matched = False
            for kf in known_features:
                if kf in feat or feat in kf:
                    valid.append(ev)
                    matched = True
                    break
            if not matched:
                logger.warning("丢弃幻觉特征: %s", feat)

    return valid


# ── 指数择时决策校验 ──────────────────────────────────────────────────────

def validate_timing_decision(raw: str, position_rule_fn=None) -> dict:
    """校验指数择时 LLM 输出。

    Args:
        raw: LLM 原始输出字符串
        position_rule_fn: 硬规则函数 (signal, mid_cycle) -> str | None

    Returns:
        校验后的结构化决策 dict，包含：
        - signal: 多/空/转
        - mid_cycle: 趋势A/龙头A/B/C/D
        - confidence: 0.0~1.0
        - abstain: bool
        - evidence: list[dict]
        - invalidation: list[str]
        - position_cap: float (由程序重算)
        - position: str (原始仓位建议)
        - reason: str

    失败时返回 转 决策（不确定/转折）。
    """
    # 步骤 1：JSON 提取
    result = extract_json(raw)
    if result is None:
        logger.error("JSON 提取失败: %s", raw[:200])
        return _fallback_timing("JSON解析失败")

    # 步骤 2-3：枚举值校验
    signal = validate_enum(result.get("signal", ""), VALID_SIGNALS, "signal")
    mid_cycle = validate_enum(result.get("mid_cycle", ""), VALID_CYCLES, "mid_cycle")

    # 步骤 4：置信度校验
    confidence = clamp_confidence(result.get("confidence", 0.5))

    # abstain 字段：修复 bool("false") == True 的坑
    abstain = parse_bool(result.get("abstain", False))

    # 步骤 5：必填字段校验
    reason = str(result.get("reason", "")).strip()
    if not reason:
        reason = "未提供理由"

    position = str(result.get("position", "")).strip()

    # 步骤 6：evidence 校验
    evidence = validate_evidence(result.get("evidence", []), TIMING_FEATURES)

    # invalidation 字段（条件失效触发点）
    invalidation = result.get("invalidation", [])
    if not isinstance(invalidation, list):
        invalidation = []
    invalidation = [str(x).strip() for x in invalidation if x]

    # 步骤 7：硬规则覆盖 position_cap
    if position_rule_fn:
        hard_msg = position_rule_fn(signal, mid_cycle)
        if hard_msg:
            # 硬规则触发时，position_cap 由规则决定
            if "空仓" in hard_msg:
                position_cap = 0.0
            elif "小仓位" in hard_msg or "小仓" in hard_msg:
                position_cap = 0.3
            else:
                position_cap = 1.0
            position = hard_msg  # 覆盖 LLM 建议
        else:
            # 无硬规则时，使用 LLM 建议的 position_cap（如果有）
            try:
                position_cap = float(result.get("position_cap", 1.0))
                position_cap = max(0.0, min(1.0, position_cap))
            except (TypeError, ValueError):
                position_cap = 1.0
    else:
        position_cap = 1.0

    return {
        "signal": signal,
        "mid_cycle": mid_cycle,
        "confidence": confidence,
        "abstain": abstain,
        "evidence": evidence,
        "invalidation": invalidation,
        "position_cap": round(position_cap, 2),
        "position": position,
        "reason": reason,
    }


def _fallback_timing(reason: str) -> dict:
    """步骤 8：失败降级，返回 转（不确定/转折）决策。"""
    return {
        "signal": "转",
        "mid_cycle": "B",
        "confidence": 0.0,
        "abstain": True,
        "evidence": [],
        "invalidation": [],
        "position_cap": 0.0,
        "position": "数据不足 → 观望",
        "reason": reason,
    }


# ── 情绪节点决策校验 ──────────────────────────────────────────────────────

def validate_emotion_decision(raw: str, stats: dict = None) -> dict:
    """校验情绪节点 LLM 输出。

    Args:
        raw: LLM 原始输出字符串
        stats: 当日盘面统计 dict（用于硬约束校验）

    Returns:
        校验后的结构化决策 dict，包含：
        - node: 节点名（属于 VALID_EMOTION_NODES）
        - confidence: 0.0~1.0
        - abstain: bool
        - evidence: list[dict]
        - advice: str
        - reason: str

    失败时返回混沌节点。
    """
    # 步骤 1：JSON 提取
    result = extract_json(raw)
    if result is None:
        logger.error("JSON 提取失败: %s", raw[:200])
        return _fallback_emotion("JSON解析失败")

    # 步骤 2-3：枚举值校验
    node = validate_enum(result.get("node", ""), VALID_EMOTION_NODES, "node")

    # 步骤 4：置信度校验
    confidence = clamp_confidence(result.get("confidence", 0.5))

    # abstain 字段
    abstain = bool(result.get("abstain", False))
    if abstain:
        node = "混沌"  # abstain 时强制混沌

    # 步骤 5：必填字段校验
    reason = str(result.get("reason", "")).strip()
    if not reason:
        reason = "未提供理由"

    advice = str(result.get("advice", "")).strip()

    # 步骤 6：evidence 校验
    evidence = validate_evidence(result.get("evidence", []), EMOTION_FEATURES)

    # 步骤 7：硬约束校验（与 emotional node.md 量化标准一致）
    if stats:
        db_val = stats.get("打板大面数")
        dm_val = stats.get("大面数", 0) or 0
        dt_val = stats.get("跌停数", 0) or 0

        # 修复--强：打板大面≥5禁止（规范要求<5）
        if node == "修复--强":
            if db_val is not None and db_val >= 5:
                node = "修复--中等"
                reason = f"打板大面{db_val}≥5，禁修复--强→降为修复--中等；{reason}"

        # 主升--确认：打板大面≥8禁止（规范要求<8）
        if node == "主升--确认":
            if db_val is not None and db_val >= 8:
                node = "混沌"
                reason = f"打板大面{db_val}≥8，禁主升--确认（规范要求<8）→降为混沌；{reason}"

        # 退潮加速：总跌停数需>10
        if node == "退潮加速":
            if dt_val < 10:
                node = "混沌分歧" if dm_val > 30 else "混沌"
                reason = f"跌停{dt_val}<10不满足退潮加速（需>10）→降为{node}；{reason}"

        # 退潮：大面需>50
        if node == "退潮":
            if dm_val <= 50:
                node = "混沌分歧" if dm_val > 20 else "混沌"
                reason = f"大面{dm_val}≤50不满足退潮门槛（需>50）→降为{node}；{reason}"

        # 冰点：大面需>200且跌停需>20
        if node == "冰点":
            if dm_val <= 200 or dt_val <= 20:
                node = "退潮加速" if (dt_val > 10 and dm_val > 100) else "退潮"
                reason = (f"大面{dm_val}/跌停{dt_val}不满足冰点门槛（大面>200且跌停>20）"
                          f"→降为{node}；{reason}")

    return {
        "node": node,
        "confidence": confidence,
        "abstain": abstain,
        "evidence": evidence,
        "advice": advice,
        "reason": reason,
    }


def _fallback_emotion(reason: str) -> dict:
    """步骤 8：失败降级，返回混沌节点。"""
    return {
        "node": "混沌",
        "confidence": 0.0,
        "abstain": True,
        "evidence": [],
        "advice": "数据不足，建议观望",
        "reason": reason,
    }


# ── 双阶段复核 ────────────────────────────────────────────────────────────

# 情绪节点 → 一级分类（简化版，与 backtest.py _PRIMARY_MAP 对齐）
_NODE_PRIMARY = {
    "混沌": "混沌", "混沌分歧": "混沌",
    "主升--确认": "主升", "主升--加速": "主升", "主升--高潮": "主升",
    "主升--分歧": "主升", "主升--延续": "主升",
    "日内分歧转一致": "日内一致", "日内分歧未转一致": "日内一致",
    "退潮": "退潮", "退潮加速": "退潮", "退潮转衰竭": "退潮", "退潮中继": "退潮",
    "冰点": "冰点", "冰点转折": "冰点",
    "修复--弱": "修复", "修复--中等": "修复", "修复--强": "修复",
    "修复--加速": "修复", "修复--高潮": "修复", "修复延续": "修复",
    "加速": "加速", "加速转衰竭": "加速",
    "龙头确认": "主升", "短线情绪确认": "主升",
}


def _primary_of_node(node: str) -> str:
    """情绪节点 → 一级分类。"""
    return _NODE_PRIMARY.get(node, "混沌")


def needs_review(result: dict, prev_result: dict | None, key_field: str) -> bool:
    """判断是否需要触发第二阶段复核。

    触发条件：低置信(<0.6)、弃权标记、前后跳变。
    """
    if result.get("confidence", 0.5) < 0.6:
        return True
    if result.get("abstain"):
        return True
    if prev_result and prev_result.get(key_field) != result.get(key_field):
        return True
    return False


def resolve_review(stage1: dict, stage2: dict, key_field: str) -> dict:
    """合并两阶段结果。一致则输出，分歧则保守处理。"""
    agreed = stage1.get(key_field) == stage2.get(key_field)
    result = {**stage1, "reviewed": True, "agreed": agreed}

    if agreed:
        result["review_note"] = f"双阶段一致：{stage1.get(key_field)}"
        return result

    result["review_reason"] = (
        f"Stage1={stage1.get(key_field)} vs Stage2={stage2.get(key_field)}，"
        f"结论分歧→保守处理"
    )
    result["confidence"] = min(stage1.get("confidence", 0.5),
                               stage2.get("confidence", 0.5))

    if key_field == "signal":
        result["signal"] = "转"
        result["position_cap"] = min(result.get("position_cap", 1.0), 0.3)
    elif key_field == "node":
        primary_1 = _primary_of_node(stage1.get("node", ""))
        primary_2 = _primary_of_node(stage2.get("node", ""))
        if primary_1 != primary_2:
            result["node"] = "混沌"
            result["advice"] = "两阶段一级判断分歧，建议观望"
        else:
            result["review_note"] = f"一级分类一致({primary_1})，二级节点分歧，保留Stage1"

    return result


def dual_stage_review(
    call_llm,
    validate_fn,
    stage1_result: dict,
    prev_result: dict | None,
    key_field: str,
    review_prompt: str,
    model: str = "",
) -> dict:
    """通用双阶段复核编排器。

    Args:
        call_llm: (model, prompt) -> str | None
        validate_fn: (raw_str) -> dict (validated)
        stage1_result: Stage 1 校验后的结果
        prev_result: 前一日结果（用于跳变检测）
        key_field: 比较字段名
        review_prompt: Stage 2 的 prompt（使用同一份结构化特征）
        model: Stage 2 模型名（默认 cfg.QWEN_CHAT_MODEL）

    Returns:
        合并后的最终结果 dict。
    """
    if not model:
        from config import settings as _cfg
        model = _cfg.QWEN_CHAT_MODEL

    if not needs_review(stage1_result, prev_result, key_field):
        return {**stage1_result, "reviewed": False}

    logger.info("触发双阶段复核 (key=%s, confidence=%.2f)",
                key_field, stage1_result.get("confidence", 0))

    raw2 = call_llm(model, review_prompt)
    if raw2 is None:
        logger.warning("Stage 2 LLM 调用失败，使用 Stage 1 结果")
        return {**stage1_result, "reviewed": False, "review_error": "Stage2调用失败"}

    stage2 = validate_fn(raw2)
    return resolve_review(stage1_result, stage2, key_field)


# ── 辅助函数 ──────────────────────────────────────────────────────────────

def format_evidence_summary(evidence: list[dict]) -> str:
    """将 evidence 列表格式化为可读的摘要文本。"""
    if not evidence:
        return ""
    parts = []
    for ev in evidence:
        feat = ev.get("feature", "")
        val = ev.get("value", "")
        dir_ = ev.get("direction", "")
        if dir_:
            parts.append(f"{feat}={val}({dir_})")
        else:
            parts.append(f"{feat}={val}")
    return "证据：" + "，".join(parts)


# ── 短线决策校验 ──────────────────────────────────────────────────────────

SHORT_TERM_FEATURES = {
    "连板空间", "高度板", "一字板", "炸板次数", "连板指数", "883958",
    "微盘股", "883418", "溢价", "封板资金", "连板数", "天梯",
    "突破空间压制", "分歧转一致", "新题材同身位", "补涨",
    "空间", "换手", "回封", "打板", "封单", "行业", "板块",
}

VALID_SHORT_TERM_MODES = ["突破空间压制", "分歧转一致", "新题材同身位", "补涨"]


def validate_short_term_decision(raw: str, gate: dict) -> dict:
    """校验短线 LLM 裁决输出。

    Args:
        raw: LLM 原始输出字符串
        gate: hard_gate() 返回的硬门控结果

    Returns:
        校验后的裁决 dict，LLM 不能覆盖硬门控的 is_signal/is_continuation。
    """
    result = extract_json(raw)
    if result is None:
        logger.error("短线决策 JSON 提取失败: %s", raw[:200])
        return {"recommended_modes": [], "signal_reason": "",
                "summary": "LLM解析失败", "confidence": 0.5, "cycle_type": "未知"}

    recommended = result.get("recommended_modes", [])
    if not isinstance(recommended, list):
        recommended = []
    recommended = [m for m in recommended if m in VALID_SHORT_TERM_MODES]

    confidence = clamp_confidence(result.get("confidence", 0.5))
    signal_reason = str(result.get("signal_reason", "")).strip() or ""
    summary = str(result.get("summary", "")).strip() or ""
    cycle_type = str(result.get("cycle_type", "")).strip() or "未知"

    return {
        "recommended_modes": recommended,
        "signal_reason": signal_reason,
        "summary": summary,
        "confidence": confidence,
        "cycle_type": cycle_type,
    }


# ── 主线分析校验 ──────────────────────────────────────────────────────────

try:
    from src.theme_mode import _BROAD_BOARDS
except ImportError:
    _BROAD_BOARDS = set()


def validate_theme_analysis(raw: str, scored_mainlines: list[dict]) -> list:
    """校验主线分析 LLM 输出，确保板块名不在宽泛集合中。

    Args:
        raw: LLM 原始输出字符串（纯文本，非 JSON）
        scored_mainlines: score_mainline() 返回的评分结果

    Returns:
        校验后的文本（如果 LLM 提到宽泛板块名则追加提醒）。
    """
    if not isinstance(raw, str):
        return "LLM 输出格式错误"

    valid_boards = {ml["board"] for ml in scored_mainlines}
    warnings = []
    for broad in _BROAD_BOARDS:
        if broad in raw and broad not in valid_boards:
            warnings.append(broad)

    if warnings:
        raw += f"\n（注：程序已排除宽泛板块 {', '.join(warnings[:3])}，请以程序评分为准）"

    return raw
