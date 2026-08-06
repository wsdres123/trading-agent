"""个股模式页：主线/短线/庄股三 tab + AI 买卖点判断 + 点击个股看分时图。"""
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import settings as cfg
from src import data, single_stock as ss
from pages.shared import e, sortable_table


def render():

    
        st.markdown("### 🎯 个股模式（主线/短线/庄股 · AI判断买卖点）")

        # 周期判断
        cyc = ss.current_cycle()
        if cyc == "D":
            st.markdown(
                f'<div class="stat-box" style="border-color:{e(cfg.COLOR_DOWN)};">'
                f'<div class="label">⚠️ 当前中级周期 D</div>'
                f'<div class="val" style="color:{e(cfg.COLOR_DOWN)};">D周期不弹个股 — 空仓为主</div>'
                f'</div>', unsafe_allow_html=True)
        else:
            cyc_color = cfg.COLOR_UP if cyc in ("A", "B", "C") else cfg.COLOR_MUTED
            st.markdown(
                f'<div class="stat-box">'
                f'<div class="label">当前中级周期 {e(cyc or "未知")}</div>'
                f'<div class="val" style="color:{e(cyc_color)};">'
                f'{"✅ 可弹个股" if cyc in ("A", "B", "C") else "待确认"}</div>'
                f'</div>', unsafe_allow_html=True)

        # 日期选择 + 运行按钮（单日，连板天梯仅支持30天内）
        with st.form("ss_form", border=False):
            sc1, sc2, sc3 = st.columns([1.5, 1.2, 5])
            with sc1:
                ss_date = st.date_input("选择日期（30天内）",
                                         value=date.today(),
                                         min_value=date.today() - timedelta(days=29),
                                         max_value=date.today())
            with sc2:
                st.markdown('<div style="height:28px;"></div>', unsafe_allow_html=True)
                ss_go = st.form_submit_button("🔍 筛选+AI判断", use_container_width=True,
                                              type="primary")
            with sc3:
                st.markdown(
                    f'<div class="muted" style="padding-top:28px;">'
                    f'📅 {e(ss_date)} · 连板天梯仅支持30天内数据'
                    f'</div>', unsafe_allow_html=True)

        if ss_go or "ss_result" not in st.session_state:
            with st.spinner("正在筛选庄股 + 主线/短线候选 + AI判断…"):
                st.session_state["ss_result"] = ss.run(str(ss_date))
                st.session_state.pop("ss_intra_code", None)
        res = st.session_state.get("ss_result", {})

        # AI 总结
        ai = res.get("ai_result", {})
        if ai.get("summary"):
            st.markdown(
                f'<div class="stat-box" style="margin-bottom:6px;">'
                f'<div class="label">🤖 AI 个股判断总结</div>'
                f'<div style="color:{e(cfg.COLOR_TEXT)};margin-top:4px;white-space:pre-wrap;">'
                f'{e(ai["summary"])}</div></div>', unsafe_allow_html=True)

        # 三个子板块
        ss_tabs = st.tabs(["📈 主线模式个股", "⚡ 短线模式个股", "💰 庄股"])

        # ── 主线模式个股 ──
        with ss_tabs[0]:
            ml_data = res.get("mainline_data", {})
            if ml_data.get("error") == "need_cache":
                st.warning("主线个股需先构建全市场数据缓存（约1-2分钟）。")
                if st.button("🔄 构建缓存", key="ss_build"):
                    prog = st.progress(0.0, text="构建全市场指标缓存…")

                    def ss_bcb(done, total):
                        prog.progress(done / total, text=f"拉取历史 {done}/{total}")

                    data.build_metrics_cache(progress_cb=ss_bcb)
                    prog.empty()
                    st.session_state.pop("ss_result", None)
                    st.rerun()
            elif ml_data.get("has_mainline"):
                st.markdown(
                    f'<span class="chip">主线: <span class="k">{e(ml_data["board"])}</span>'
                    f' · {e(ml_data["start"])}~{e(ml_data["end"])}</span>',
                    unsafe_allow_html=True)
                ml_disp = res.get("mainline_display")
                if ml_disp is not None and not ml_disp.empty:
                    core_codes = set(ml_data.get("core_codes", []))
                    follow_codes = set(ml_data.get("follow_codes", []))
                    core_df = ml_disp[ml_disp["代码"].isin(core_codes)]
                    follow_df = ml_disp[ml_disp["代码"].isin(follow_codes)]
                    if not core_df.empty:
                        st.markdown("**核心容量个股：**")
                        sortable_table(core_df, pct_cols=("涨跌幅", "涨速"))
                    if not follow_df.empty:
                        st.markdown("**补涨前5：**")
                        sortable_table(follow_df, pct_cols=("涨跌幅", "涨速"))
                else:
                    st.markdown('<div class="muted">（主线个股无实时数据）</div>',
                                unsafe_allow_html=True)
                ai_ml = ai.get("mainline", [])
                if ai_ml:
                    with st.expander("🤖 AI 主线个股判断", expanded=True):
                        for s in ai_ml:
                            vc = (cfg.COLOR_UP if "可做" in s.get("verdict", "")
                                   else cfg.COLOR_DOWN if "回避" in s.get("verdict", "")
                                   else cfg.COLOR_MUTED)
                            st.markdown(
                                f'**<span class="stk">{e(s.get("name", ""))}</span>'
                                f'({e(s.get("code", ""))})** — '
                                f'<span style="color:{e(vc)};font-weight:600;">{e(s.get("verdict", ""))}</span>'
                                f' [{e(s.get("type", ""))}]',
                                unsafe_allow_html=True)
                            st.markdown(
                                f'<span class="chip">买: <span class="k">{e(s.get("buy", ""))}</span></span>'
                                f'<span class="chip">卖: <span class="k">{e(s.get("sell", ""))}</span></span>',
                                unsafe_allow_html=True)
                            st.markdown(f'<div class="muted">{e(s.get("reason", ""))}</div>',
                                        unsafe_allow_html=True)
            else:
                st.markdown(
                    '<div class="muted">（当前区间无主线，不弹主线个股。'
                    '主线一般在A/C周期出现。）</div>',
                    unsafe_allow_html=True)

        # ── 短线模式个股 ──
        with ss_tabs[1]:
            st_data = res.get("shortterm_data", {})
            st_sig = st_data.get("is_signal", False)
            st_cont = st_data.get("is_continuation", False)
            if st_sig or st_cont:
                if st_sig:
                    st.markdown(
                        f'<span class="chip">起变信号: <span class="k">已出现 ✅</span></span>'
                        f'<span class="chip">📝 {e(st_data.get("signal_reason", ""))}</span>',
                        unsafe_allow_html=True)
                if st_cont:
                    st.markdown(
                        f'<span class="chip" style="border-color:{e(cfg.COLOR_STOCK)};">'
                        f'延续信号: <span class="k">已触发 🔄</span></span>'
                        f'<span class="chip">🔄 {e(st_data.get("continuation_reason", ""))}</span>',
                        unsafe_allow_html=True)
                for m in st_data.get("modes", []):
                    st.markdown(
                        f'<div class="stat-box" style="margin-top:4px;">'
                        f'<div class="label">🔥 {e(m.get("mode", ""))}</div>'
                        f'<div style="font-size:14px;margin-top:2px;">'
                        f'买: <span class="stk">{e(m.get("buy_point", ""))}</span> | '
                        f'卖: <span class="stk">{e(m.get("sell_point", ""))}</span> | '
                        f'仓位: <span class="stk">{e(m.get("position", ""))}</span>'
                        f'</div></div>', unsafe_allow_html=True)
                st_disp = res.get("shortterm_display")
                if st_disp is not None and not st_disp.empty:
                    sortable_table(st_disp, pct_cols=("涨跌幅", "涨速"))
                else:
                    st.markdown('<div class="muted">（短线候选无实时数据）</div>',
                                unsafe_allow_html=True)
            else:
                st.markdown(
                    f'<div class="muted">（今日无起变信号 — '
                    f'{e(st_data.get("signal_reason", ""))}）</div>',
                    unsafe_allow_html=True)
            ai_st = ai.get("shortterm", [])
            if ai_st:
                with st.expander("🤖 AI 短线个股判断", expanded=True):
                    for s in ai_st:
                        vc = (cfg.COLOR_UP if "可做" in s.get("verdict", "")
                               else cfg.COLOR_DOWN if "回避" in s.get("verdict", "")
                               else cfg.COLOR_MUTED)
                        st.markdown(
                            f'**<span class="stk">{e(s.get("name", ""))}</span>'
                            f'({e(s.get("code", ""))})** — '
                            f'<span style="color:{e(vc)};font-weight:600;">{e(s.get("verdict", ""))}</span>'
                            f' [{e(s.get("mode", ""))}]',
                            unsafe_allow_html=True)
                        st.markdown(
                            f'<span class="chip">买: <span class="k">{e(s.get("buy", ""))}</span></span>'
                            f'<span class="chip">卖: <span class="k">{e(s.get("sell", ""))}</span></span>',
                            unsafe_allow_html=True)
                        st.markdown(f'<div class="muted">{e(s.get("reason", ""))}</div>',
                                    unsafe_allow_html=True)

        # ── 庄股 ──
        with ss_tabs[2]:
            st.markdown(
                '<span class="chip">筛选: <span class="k">连续5日收盘&gt;MA5 · 自由流通&gt;30亿'
                ' · 5日涨&gt;15% · 主板 · 30日涨&gt;40%</span></span>',
                unsafe_allow_html=True)
            zg_df = res.get("zhuanggu")
            if zg_df is not None and not zg_df.empty:
                st.markdown(f'<span class="chip">命中: <span class="k">{e(len(zg_df))}只</span></span>',
                            unsafe_allow_html=True)
                zg_show = zg_df[[c for c in ss.DISPLAY_COLS if c in zg_df.columns]]
                sortable_table(zg_show, pct_cols=("涨跌幅", "涨速"))
            else:
                st.markdown('<div class="muted">（无符合条件的庄股）</div>',
                            unsafe_allow_html=True)
            ai_zg = ai.get("zhuanggu", [])
            if ai_zg:
                with st.expander("🤖 AI 庄股判断", expanded=True):
                    for s in ai_zg:
                        vc = (cfg.COLOR_UP if "可做" in s.get("verdict", "")
                               else cfg.COLOR_DOWN if "回避" in s.get("verdict", "")
                               else cfg.COLOR_MUTED)
                        st.markdown(
                            f'**<span class="stk">{e(s.get("name", ""))}</span>'
                            f'({e(s.get("code", ""))})** — '
                            f'<span style="color:{e(vc)};font-weight:600;">{e(s.get("verdict", ""))}</span>',
                            unsafe_allow_html=True)
                        st.markdown(
                            f'<span class="chip">买: <span class="k">{e(s.get("buy", ""))}</span></span>'
                            f'<span class="chip">卖: <span class="k">{e(s.get("sell", ""))}</span></span>'
                            f'<span class="chip">⚠️ {e(s.get("risk", ""))}</span>',
                            unsafe_allow_html=True)
                        st.markdown(f'<div class="muted">{e(s.get("reason", ""))}</div>',
                                    unsafe_allow_html=True)

        # ── 分时图 ──
        stock_pairs: list[tuple[str, str]] = []
        for df_key in ("zhuanggu", "mainline_display", "shortterm_display"):
            df = res.get(df_key)
            if df is not None and not df.empty:
                for _, r in df.iterrows():
                    stock_pairs.append((str(r["代码"]), str(r.get("名称", ""))))
        stock_pairs = list(dict.fromkeys(stock_pairs))
        if stock_pairs:
            st.markdown("---")
            st.markdown("##### 📈 点击个股查看分时图")
            npr = 6
            for rs in range(0, len(stock_pairs), npr):
                chunk = stock_pairs[rs:rs + npr]
                cols = st.columns(npr)
                for j, (code, name) in enumerate(chunk):
                    with cols[j]:
                        is_sel = st.session_state.get("ss_intra_code") == code
                        if st.button(name, key=f"intra_btn_{code}",
                                     use_container_width=True,
                                     type="primary" if is_sel else "secondary"):
                            st.session_state["ss_intra_code"] = code
            sel_code = st.session_state.get("ss_intra_code")
            if sel_code:
                name_disp = next((n for c, n in stock_pairs
                                   if c == sel_code), sel_code)
                hc, tc, rc = st.columns([1, 4, 1])
                with hc:
                    if st.button("✕ 关闭", key="intra_close",
                                 use_container_width=True):
                        st.session_state.pop("ss_intra_code", None)
                with tc:
                    st.markdown(
                        f'<div style="text-align:center;padding-top:6px">'
                        f'<span class="stk">{e(name_disp)}</span> '
                        f'<span class="muted">{e(sel_code)}</span></div>',
                        unsafe_allow_html=True)
                with rc:
                    st.button("🔄 刷新", key="intra_refresh",
                              use_container_width=True)
                chart_col, _ = st.columns([1, 1])
                with chart_col:
                    with st.spinner("加载分时数据…"):
                        intra = ss.get_intraday(sel_code)
                    if not intra.empty:
                        from plotly.subplots import make_subplots
                        fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                                             vertical_spacing=0.04,
                                             row_heights=[0.70, 0.30],
                                             specs=[[{"secondary_y": True}],
                                                    [{"secondary_y": False}]])
                        pc = (float(intra["昨收"].iloc[0])
                               if "昨收" in intra.columns else 0.0)
                        fig.add_trace(go.Scatter(
                            x=intra["时间"], y=intra["价格"],
                            mode="lines", name="价格",
                            line=dict(color="#ffffff", width=1.2),
                            fill="tozeroy" if not pc else None,
                            showlegend=False), row=1, col=1, secondary_y=False)
                        if pc:
                            fig.add_trace(go.Scatter(
                                x=intra["时间"], y=intra["价格"],
                                mode="lines", showlegend=False,
                                line=dict(width=0),
                                fill="tonexty",
                                fillcolor="rgba(255,59,59,0.10)",
                                hoverinfo="skip"), row=1, col=1, secondary_y=False)
                            fig.add_trace(go.Scatter(
                                x=[intra["时间"].iloc[0], intra["时间"].iloc[-1]],
                                y=[pc, pc],
                                mode="lines", name="昨收",
                                line=dict(color="#ffffff", width=0.8,
                                          dash="dash"),
                                showlegend=False), row=1, col=1, secondary_y=False)
                        if "均价" in intra.columns:
                            fig.add_trace(go.Scatter(
                                x=intra["时间"], y=intra["均价"],
                                mode="lines", name="均价",
                                line=dict(color=cfg.COLOR_STOCK, width=0.8,
                                          dash="dot"),
                                showlegend=False), row=1, col=1, secondary_y=False)
                        if pc and "涨幅" in intra.columns:
                            fig.add_trace(go.Scatter(
                                x=intra["时间"], y=intra["涨幅"],
                                mode="lines", showlegend=False,
                                line=dict(width=0),
                                hovertemplate="%{y:.2f}%<extra></extra>",
                                hoverinfo="y"), row=1, col=1, secondary_y=True)
                        if "成交量" in intra.columns:
                            px = intra["价格"].ffill()
                            prev = [px.iloc[0]] + list(px.iloc[:-1])
                            vcolors = [
                                cfg.COLOR_UP if (pd.notna(p) and p >= pp)
                                else (cfg.COLOR_DOWN if pd.notna(p) else "rgba(0,0,0,0)")
                                for p, pp in zip(intra["价格"], prev)
                            ]
                            fig.add_trace(go.Bar(
                                x=intra["时间"], y=intra["成交量"],
                                name="量", marker=dict(color=vcolors),
                                showlegend=False), row=2, col=1)
                        fig.update_layout(
                            height=280, paper_bgcolor=cfg.COLOR_BG,
                            plot_bgcolor=cfg.COLOR_PANEL,
                            font=dict(color=cfg.COLOR_TEXT, size=9),
                            margin=dict(l=42, r=36, t=4, b=4),
                            showlegend=False)
                        fig.update_xaxes(showticklabels=False, row=1, col=1)
                        tick_vals = ["09:30", "10:30", "11:30",
                                      "13:00", "14:00", "15:00"]
                        fig.update_xaxes(tickangle=0, tickvals=tick_vals,
                                          row=2, col=1)
                        fig.update_yaxes(gridcolor="#2a2a2a", row=1, col=1,
                                          secondary_y=False)
                        fig.update_yaxes(gridcolor="#2a2a2a", row=2, col=1)
                        if pc and "涨幅" in intra.columns:
                            valid_pct = intra["涨幅"].dropna()
                            if not valid_pct.empty:
                                min_pct = float(valid_pct.min()) - 0.5
                                max_pct = float(valid_pct.max()) + 0.5
                                fig.update_yaxes(
                                    secondary_y=True, row=1, col=1,
                                    range=[min_pct, max_pct],
                                    tickformat=".1f",
                                    ticksuffix="%",
                                    gridcolor="rgba(0,0,0,0)",
                                    showgrid=False)
                        fig.add_vline(x="12:00", line_width=0.5,
                                       line_dash="dot", line_color="#555555",
                                       row=1, col=1)
                        fig.add_vline(x="12:00", line_width=0.5,
                                       line_dash="dot", line_color="#555555",
                                       row=2, col=1)
                        st.plotly_chart(fig, use_container_width=True,
                                        theme=None)
                    else:
                        st.markdown(
                            '<div class="muted">（无分时数据，可能非交易时段）</div>',
                            unsafe_allow_html=True)

        # 规范文档
        with st.expander("📖 个股模式规范（single stock.md）"):
            sspec = cfg.DOCS_DIR / "single stock.md"
            if sspec.exists():
                st.markdown(sspec.read_text(encoding="utf-8"))
