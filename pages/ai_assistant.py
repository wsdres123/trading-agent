"""AI 助手页：智能问答（RAG+工具调用）+ 个股分组筛选（自然语言→条件→选股→存组）。"""
import time

import pandas as pd
import streamlit as st

from config import settings as cfg
from src import data, ai_assistant as ai, stock_filter as sf
from pages.shared import e, conds_chips, cond_labels, sortable_big_table


def render(current_user: str):
        st.markdown("### 🤖 AI 助手")
        ai_mode = st.radio("AI助手模式", ["💬 智能问答", "🔬 个股分组筛选"],
                            horizontal=True, label_visibility="collapsed")

        # ── 智能问答 ──────────────────────────────────────────────────────────
        if ai_mode.startswith("💬"):
            if "messages" not in st.session_state:
                st.session_state.messages = [{
                    "role": "assistant",
                    "content": "你好，我是劫财AI交易助手。可向我提问：\n"
                               "- 交易系统：屠龙表周期、主线筛选、心态管理\n"
                               "- 实时行情：如「今天上证涨多少」「贵州茅台现价」「半导体板块表现」\n"
                               "也可在「个股分组筛选」用自然语言描述条件来选股。"
                }]
            for msg in st.session_state.messages:
                with st.chat_message(msg["role"]):
                    st.markdown(msg["content"])
            q = st.chat_input("向AI助手提问…")
            if q:
                st.session_state.messages.append({"role": "user", "content": q})
                with st.chat_message("user"):
                    st.markdown(q)
                with st.chat_message("assistant"):
                    ans = st.write_stream(ai.chat_stream(q, history=st.session_state.messages))
                st.session_state.messages.append({"role": "assistant", "content": ans})

        # ── 个股分组筛选 ──────────────────────────────────────────────────────
        else:
            st.markdown("用自然语言描述筛选条件，AI 解析后实时筛股并保存为可更新的分组。")
            st.caption("示例：今日成交额大于30亿 · 换手率大于5% · 流通市值30到200亿 · 总市值大于100亿 · "
                       "股价5到50元 · 收盘价大于5日均线 · 跌破20日线 · 30天涨幅大于40% · 5日跌幅大于5% · "
                       "百日新高 · 60日新低 · 连涨3天 · 均线多头排列 · 量比大于2 · 距高点回撤10%以内 · "
                       "属于半导体板块 · 剔除北交所")

            # ── 数据缓存：筛选秒出的关键 ──
            cs = data.metrics_cache_status()
            if cs["exists"]:
                badge = "已就绪" if cs["fresh"] else "已过期"
                st.markdown(
                    f'<span class="chip">数据缓存: <span class="k">{e(badge)}</span> · '
                    f'{e(cs["rows"])}只 · 截至 {e(cs["mtime"])} · {e(cs["age_min"])}分钟前</span>',
                    unsafe_allow_html=True)
            else:
                st.markdown(
                    '<span class="chip">数据缓存: <span class="k">未构建</span>'
                    '（首次筛选自动构建，约15秒，之后毫秒级）</span>', unsafe_allow_html=True)
            cb1, cb2 = st.columns([4, 1])
            with cb2:
                build_label = "🔄 构建数据缓存" if not cs["exists"] else "🔄 更新数据缓存"
                if st.button(build_label, key="build_cache", use_container_width=True):
                    prog = st.progress(0.0, text="构建全市场指标缓存（约15秒）…")

                    def bcb(done, total):
                        prog.progress(done / total, text=f"拉取历史 {done}/{total}")

                    data.build_metrics_cache(progress_cb=bcb)
                    prog.empty()
                    st.success("数据缓存构建完成，筛选将秒出。")
                    st.rerun()

            col_a, col_b = st.columns([4, 1])
            with col_a:
                desc = st.text_area("筛选条件描述", height=90, key="filter_desc",
                                    placeholder="在此输入筛选条件…")
            with col_b:
                st.write("")
                st.write("")
                run_btn = st.button("🔍 解析并筛选", use_container_width=True)

            if run_btn:
                if not desc.strip():
                    st.warning("请输入筛选条件描述。")
                else:
                    with st.spinner("AI 解析条件中…"):
                        parsed = ai.parse_filter_conditions(desc)
                    st.session_state["parsed_conds"] = parsed["conditions"]
                    if not parsed["conditions"]:
                        st.error("未能解析出任何条件，请换一种描述。")
                    else:
                        st.markdown("**解析出的条件：**")
                        st.markdown(conds_chips(parsed["conditions"]), unsafe_allow_html=True)

            conds = st.session_state.get("parsed_conds")
            if conds:
                st.markdown("---")
                if st.button("▶️ 执行筛选", key="exec_filter"):
                    if data.load_metrics_cache(allow_stale=True) is None:
                        prog = st.progress(0.0, text="首次使用，自动构建数据缓存（约15秒）…")

                        def bcb2(done, total):
                            prog.progress(done / total, text=f"构建缓存 {done}/{total}")

                        data.build_metrics_cache(progress_cb=bcb2)
                        prog.empty()
                    if data.load_metrics_cache(allow_stale=True) is None:
                        st.error("数据缓存构建失败，请检查网络后重试。")
                    else:
                        t0 = time.time()
                        res = sf.run_filter(conds)
                        dt = time.time() - t0
                        if not res.empty:
                            with st.spinner(f"命中 {len(res)} 只，正在补充涨速/竞价量/封单额/概念板块…"):
                                res = sf.finalize_results(res)
                        st.session_state["filter_result"] = res
                        st.success(f"筛选完成（耗时 {dt*1000:.0f} 毫秒，命中 {len(res)} 只）。")

            res = st.session_state.get("filter_result")
            if res is not None and not res.empty:
                st.markdown("#### 筛选结果")
                st.caption("点击列标题可排序")
                sortable_big_table(res)
                with st.form("save_group_form"):
                    gname = st.text_input("分组名称", placeholder="如：半导体强势股")
                    save_btn = st.form_submit_button("💾 保存为分组", use_container_width=False)
                    if save_btn:
                        if not gname.strip():
                            st.warning("请输入分组名称。")
                        else:
                            sf.save_group(gname.strip(), conds or [], res.to_dict("records"),
                                          username=current_user)
                            st.success(f"已保存分组「{gname.strip()}」。")

            # ── 已存分组 ──────────────────────────────────────────────────────
            st.markdown("---")
            st.markdown("#### 已保存分组")
            groups = sf.list_groups(current_user)
            if not groups:
                st.markdown('<span class="muted">暂无分组，筛选后可保存。</span>', unsafe_allow_html=True)
            for g in groups:
                with st.container():
                    gc1, gc2, gc3, gc4 = st.columns([3, 1, 1, 1])
                    with gc1:
                        chips = "".join(
                            '<span class="chip">' + e(l) + "</span>" for l in cond_labels(g["conditions"])
                        )
                        st.markdown(
                            f'<div class="stat-box"><span style="font-size:18px;font-weight:700;'
                            f'color:{e(cfg.COLOR_STOCK)}">{e(g["name"])}</span>'
                            f'<br><span class="muted">更新于 {e(g.get("updated_at",""))}</span>'
                            f'<br>{chips}'
                            f'<br><span class="muted">含 {e(len(g.get("stocks",[])))} 只个股</span></div>',
                            unsafe_allow_html=True)
                    with gc2:
                        if st.button("👁 查看", key=f"view_{g['name']}"):
                            st.session_state["view_group"] = g["name"]
                    with gc3:
                        if st.button("🔄 更新", key=f"upd_{g['name']}"):
                            prog = st.progress(0.0, text="更新中…")
                            upd = sf.update_group(g["name"], progress_cb=lambda d, t: prog.progress(d / t),
                                                  username=current_user)
                            prog.empty()
                            if upd:
                                st.success(f"已刷新「{g['name']}」，现有 {len(upd['stocks'])} 只。")
                            else:
                                st.error("更新失败。")
                    with gc4:
                        if st.button("🗑 删除", key=f"del_{g['name']}"):
                            sf.delete_group(g["name"], username=current_user)
                            st.success(f"已删除「{g['name']}」。")
                            st.rerun()
                    if st.session_state.get("view_group") == g["name"]:
                        gdf = pd.DataFrame(g.get("stocks", []))
                        show_cols = [c for c in ["代码", "名称", "最新价", "涨跌幅", "涨速", "竞价量",
                                                 "涨停封单额", "自由流通市值(亿)", "成交额(亿)", "概念板块",
                                                 "成交额", "流通市值_亿"] if c in gdf.columns]
                        sortable_big_table(gdf[show_cols] if show_cols else gdf)
