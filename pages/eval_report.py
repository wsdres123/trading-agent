"""评测报告页：运行 eval/ 各维度评测并展示结果/历史报告。"""
import json
from pathlib import Path

import pandas as pd
import streamlit as st

from pages.shared import e



def render_eval_results(results: dict, eval_dir):
    """渲染评测结果面板。"""
    import json as json

    # ── 总览卡片 ──────────────────────────────────────────────────────────
    total_p = sum(r.get("pass", 0) for r in results.values())
    total_f = sum(r.get("fail", 0) for r in results.values())
    total = total_p + total_f

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("总通过", total_p)
    with c2:
        st.metric("总失败", total_f)
    with c3:
        rate = f"{total_p / total:.0%}" if total else "-"
        st.metric("通过率", rate)
    with c4:
        st.metric("评测维度", len(results))

    # ── 数据完整性检查 ────────────────────────────────────────────────────
    DATA_TARGETS = [
        ("数据准确性", None, 0, ""),
        ("情绪节点", "emotion_cases.jsonl", 100,
         "需标注情绪转折节点(高潮/冰点/修复)，来源: 复盘表.csv"),
        ("指数择时", "timing_cases.jsonl", 100,
         "需标注每日择时信号(买入/卖出/观望)，来源: 复盘表.csv"),
        ("输出可靠性", None, 0, "依赖 timing_ai.json / emotion_ai.json"),
        ("真实交易质量", None, 0, "依赖 .data/predictions.jsonl 与未来收益回填"),
        ("筛选NLP", "filter_nlp.jsonl", 50,
         "需手写自然语言→筛选条件映射用例"),
        ("RAG检索", "rag_queries.jsonl", 30,
         "需手写查询语句及期望命中的知识条目"),
        ("性能基准", None, 0, ""),
        ("工具路由", None, 30,
         "需手写问题→期望工具映射，当前仅5条硬编码在test_tool_routing.py"),
    ]
    ds_dir = eval_dir / "datasets"
    ds_counts = {}
    if ds_dir.exists():
        for fn in ds_dir.glob("*.jsonl"):
            with open(fn, encoding="utf-8") as f:
                ds_counts[fn.name] = sum(1 for _ in f)

    with st.expander("📋 数据完整性检查", expanded=True):
        comp_cols = st.columns([2, 1, 1, 4])
        with comp_cols[0]:
            st.markdown("**评测维度**")
        with comp_cols[1]:
            st.markdown("**已有**")
        with comp_cols[2]:
            st.markdown("**目标**")
        with comp_cols[3]:
            st.markdown("**补充建议**")

        for name, fname, target, hint in DATA_TARGETS:
            cc1, cc2, cc3, cc4 = st.columns([2, 1, 1, 4])
            with cc1:
                st.markdown(f"{name}")
            with cc2:
                if fname:
                    cur_count = ds_counts.get(fname, 0)
                    st.markdown(f"**{cur_count}** 条")
                else:
                    cur_count = None
                    st.markdown("—")
            with cc3:
                if target > 0:
                    st.markdown(f"{target} 条")
                else:
                    st.markdown("—")
            with cc4:
                if target > 0 and cur_count is not None:
                    pct = min(cur_count / target, 1.0)
                    status = "✅" if pct >= 1.0 else ("🟡" if pct >= 0.5 else "🔴")
                    if pct < 1.0:
                        gap = target - cur_count
                        st.markdown(f"{status} <span style='color:#e67e22'>还需补充 ~{e(gap)} 条</span>  {e(hint)}", unsafe_allow_html=True)
                    else:
                        st.markdown(f"{status} 数据充足")
                    st.progress(pct)
                else:
                    st.caption(hint)

    # ── 各维度详情 ────────────────────────────────────────────────────────
    for dim_name, r in results.items():
        if not isinstance(r, dict):
            continue
        p = r.get("pass", 0)
        f = r.get("fail", 0)
        icon = "🟢" if f == 0 and p > 0 else ("🔴" if f > 0 else "⚪")

        with st.expander(f"{icon} {dim_name}  —  pass={p}  fail={f}", expanded=(f > 0)):
            # 关键指标
            metrics_row = []
            if "accuracy" in r:
                metrics_row.append(f"准确率: **{r['accuracy']:.1%}**")
            if "recall_accuracy" in r:
                metrics_row.append(f"召回准确率: **{r['recall_accuracy']:.1%}**")
            if "overall_avg" in r:
                metrics_row.append(f"LLM质量均分: **{r['overall_avg']}/5**")
            if "schema_validity_rate" in r:
                metrics_row.append(f"schema合法率: **{r['schema_validity_rate']}%**")
            if "total" in r:
                metrics_row.append(f"总用例: {r['total']}")
            if "correct" in r:
                metrics_row.append(f"正确: {r['correct']}")
            if metrics_row:
                st.markdown("  ·  ".join(metrics_row))

            # 性能基准表
            if "benchmarks" in r:
                bdf = pd.DataFrame(r["benchmarks"])
                if not bdf.empty:
                    bdf_show = bdf[["name", "avg_ms", "threshold_ms", "status"]].copy()
                    bdf_show.columns = ["测试项", "平均耗时(ms)", "阈值(ms)", "状态"]
                    st.dataframe(bdf_show, use_container_width=True, hide_index=True)

            # LLM 质量评分
            if "scores" in r:
                sdf = pd.DataFrame(r["scores"])
                if not sdf.empty:
                    show_cols = [c for c in ["question", "avg", "status"] if c in sdf.columns]
                    if "scores" in sdf.columns:
                        for sk in ("factual", "actionable", "concise", "risk_aware"):
                            sdf[sk] = sdf["scores"].apply(
                                lambda x: x.get(sk, "-") if isinstance(x, dict) else "-")
                            show_cols.append(sk)
                    st.dataframe(sdf[show_cols], use_container_width=True, hide_index=True)

            # 节点/信号分布
            if "node_distribution" in r:
                st.markdown("**节点分布:**")
                ndf = pd.DataFrame(list(r["node_distribution"].items()), columns=["节点", "数量"])
                st.dataframe(ndf, use_container_width=True, hide_index=True)
            if "signal_distribution" in r:
                st.markdown("**信号分布:**")
                sdf2 = pd.DataFrame(list(r["signal_distribution"].items()), columns=["信号", "数量"])
                st.dataframe(sdf2, use_container_width=True, hide_index=True)

            # 真实交易质量：timing / emotion / shortterm
            for tq_type in ("timing", "emotion", "shortterm"):
                if tq_type not in r:
                    continue
                tq = r[tq_type]
                if not isinstance(tq, dict):
                    continue
                st.markdown(f"**{tq_type} 未来收益评估:**")
                rows = []
                for per in ("d1", "d3", "d5"):
                    if per not in tq:
                        continue
                    pv = tq[per]
                    if isinstance(pv, dict):
                        row = {"周期": per, "样本": pv.get("total", "-")}
                        if "rate" in pv:
                            row["命中率"] = f"{pv['rate']}%"
                        if "avg" in pv:
                            row["平均收益(%)"] = pv["avg"]
                        if "failure_rate" in pv:
                            row["失败率"] = f"{pv['failure_rate']}%"
                        rows.append(row)
                if rows:
                    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

            # 输出可靠性：样例问题
            if "issues_sample" in r and r["issues_sample"]:
                st.markdown("**schema 问题样例:**")
                for iss in r["issues_sample"][:5]:
                    txt = json.dumps(iss, ensure_ascii=False)
                    if len(txt) > 200:
                        txt = txt[:200] + "…"
                    st.markdown(f"- `{txt}`")

            # 失败详情
            details = r.get("details", [])
            if details:
                fail_only = [d for d in details if d.get("status") != "pass"]
                if fail_only:
                    st.markdown(f"**失败用例 ({len(fail_only)}):**")
                    for d in fail_only[:10]:
                        txt = json.dumps(d, ensure_ascii=False)
                        if len(txt) > 200:
                            txt = txt[:200] + "…"
                        st.markdown(f"- `{txt}`")

            # 备注
            for note_key in ("accuracy_note", "note"):
                if note_key in r:
                    st.caption(r[note_key])

            # 错误
            if "error" in r:
                st.error(r["error"])


# ── 评测报告 ───────────────────────────────────────────────────────────────


def render():
    
        st.markdown("### 📊 评测报告")

        eval_dir = Path(__file__).parent / "eval"
        results_dir = eval_dir / "results"

        # ── 运行评测 ──────────────────────────────────────────────────────────
        ev_col1, ev_col2, ev_col3, ev_col4 = st.columns([2, 2, 2, 3])
        with ev_col1:
            run_no_llm = st.button("▶ 运行评测（无费用）", use_container_width=True)
        with ev_col2:
            run_all = st.button("▶ 运行全部（含LLM）", use_container_width=True)
        with ev_col3:
            run_filter = st.button("▶ 仅筛选NLP", use_container_width=True)
        with ev_col4:
            st.caption("无费用 = 数据准确性 + 情绪/择时数据集 + 输出可靠性 + 真实交易质量 + 筛选NLP + RAG检索 + 性能基准")

        if run_no_llm or run_all or run_filter:
            with st.spinner("评测运行中…"):
                if run_filter:
                    from eval.test_filter_nlp import run as run_fn
                    res = {"筛选NLP解析": run_fn()}
                else:
                    res = {}
                    modules = [
                        ("数据准确性", "eval.test_data_accuracy"),
                        ("情绪节点", "eval.test_emotion"),
                        ("指数择时", "eval.test_timing"),
                        ("输出可靠性", "eval.test_output_reliability"),
                        ("真实交易质量", "eval.test_trading_quality"),
                        ("筛选NLP解析", "eval.test_filter_nlp"),
                        ("RAG检索质量", "eval.test_rag_recall"),
                        ("性能基准", "eval.test_benchmark"),
                    ]
                    if run_all:
                        modules.extend([
                            ("LLM输出质量", "eval.test_llm_quality"),
                            ("工具路由", "eval.test_tool_routing"),
                        ])
                    import importlib as il
                    for name, mod_path in modules:
                        try:
                            mod = il.import_module(mod_path)
                            res[name] = mod.run()
                        except Exception as e:
                            res[name] = {"error": str(e), "pass": 0, "fail": 0}
            st.session_state["eval_results"] = res
            st.rerun()

        # ── 展示结果 ──────────────────────────────────────────────────────────
        cur = st.session_state.get("eval_results")

        # 历史报告选择
        hist_files = sorted(results_dir.glob("*summary.json"), reverse=True) if results_dir.exists() else []
        if not cur and not hist_files:
            st.info("暂无评测结果。点击上方按钮运行评测。")
        else:
            # Tab: 当前结果 / 历史报告
            tab_labels = ["当前结果"]
            if hist_files:
                tab_labels.append("历史报告")
            tabs = st.tabs(tab_labels)

            with tabs[0]:
                if cur:
                    render_eval_results(cur, eval_dir)
                else:
                    st.info("本次会话尚无评测结果。")

            if len(tabs) > 1:
                with tabs[1]:
                    sel = st.selectbox("选择历史报告", [f.name for f in hist_files[:20]])
                    if sel:
                        hdata = json.loads((results_dir / sel).read_text(encoding="utf-8"))
                        ts = hdata.get("timestamp", "")
                        st.caption(f"报告时间: {ts}")
                        hres = {}
                        for m in hdata.get("modules", []):
                            hres[m["name"]] = m.get("result", {"error": m.get("error", "未知")})
                        render_eval_results(hres, eval_dir)
