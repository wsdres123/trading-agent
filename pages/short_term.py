"""短线模式页：883958 连板指数 + 起变/延续信号 + 连板天梯 + 触发模式。"""
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import settings as cfg
from src import data, short_term as stm
from pages.shared import e, sortable_table


def render():
    
    
    
        st.markdown("### ⚡ 短线模式（连板梯队 · 起变信号 · 情绪博弈）")

        with st.form("st_form", border=False):
            sc1, sc2 = st.columns([1.4, 4])
            with sc1:
                st_date = st.date_input("分析日期", value=date.today(),
                                         max_value=date.today(), key="st_date_input")
            with sc2:
                st.markdown('<div style="height:28px;"></div>', unsafe_allow_html=True)
                st_go = st.form_submit_button("🔍 分析起变信号", use_container_width=True,
                                               type="primary")

        if st_go or "st_res" not in st.session_state:
            with st.spinner("收集连板证据 → AI 判断起变信号…"):
                st.session_state["st_res"] = stm.detect(str(st_date))
                st.session_state["st_date_key"] = str(st_date)
        sr = st.session_state.get("st_res", {})
        sev = sr.get("evidence", {})
        sai = sr.get("ai_result", {})
        sld = sr.get("ladder_df")
        smarks = sr.get("scan_marks", [])

        # ── 883958 连板指数 K线 + 连板天梯（平行展示）──────────────────
        kl, lr = st.columns([1.5, 1])
        with kl:
            st.markdown('<span class="chip">📊 883958 连板指数 · '
                        '起变候选（橙色▲）</span>', unsafe_allow_html=True)
            sk = data.get_ths_index_daily("883958", days=120)
            if sk is not None and not sk.empty:
                sk = sk.sort_values("日期").reset_index(drop=True)
                last_d = pd.to_datetime(str(sk.iloc[-1]["日期"])[:10])
                range_start = (last_d - pd.Timedelta(days=60)).strftime("%Y-%m-%d")
                sfig = go.Figure(go.Candlestick(
                    x=sk["日期"], open=sk["开盘"], high=sk["最高"],
                    low=sk["最低"], close=sk["收盘"],
                    increasing=dict(line=dict(color=cfg.COLOR_UP), fillcolor=cfg.COLOR_UP),
                    decreasing=dict(line=dict(color=cfg.COLOR_DOWN), fillcolor=cfg.COLOR_DOWN),
                    name="883958"))
                cand_dates = {m["date"] for m in smarks}
                if cand_dates:
                    cand_rows = sk[sk["日期"].isin(cand_dates)]
                    if not cand_rows.empty:
                        sfig.add_trace(go.Scatter(
                            x=cand_rows["日期"], y=cand_rows["最低"] * 0.995,
                            mode="markers", name="起变候选",
                            marker=dict(symbol="triangle-up", size=10, color="orange"),
                            hovertext=[f"起变候选 {d}" for d in cand_rows["日期"]],
                            hoverinfo="text"))
                st_ds = str(st_date)
                if st_ds in sk["日期"].values:
                    tr = sk[sk["日期"] == st_ds].iloc[0]
                    sfig.add_trace(go.Scatter(
                        x=[st_ds], y=[float(tr["最高"]) * 1.01],
                        mode="markers", name="当日",
                        marker=dict(symbol="arrow-down", size=14, color="red"),
                        hovertext=f"当日 {st_ds}", hoverinfo="text"))
                sfig.update_xaxes(
                    type="date", nticks=8, tickangle=-45, showgrid=False,
                    range=[range_start, str(sk.iloc[-1]["日期"])[:10]],
                    rangeslider=dict(visible=True, thickness=0.06),
                    rangeselector=dict(
                        buttons=[
                            dict(count=1, label="1月", step="month", stepmode="backward"),
                            dict(count=3, label="3月", step="month", stepmode="backward"),
                            dict(label="全部", step="all")],
                        bgcolor=cfg.COLOR_BG, activecolor="#444",
                        font=dict(size=9, color=cfg.COLOR_TEXT)),
                    rangebreaks=[dict(bounds=["sat", "mon"])],
                    tickfont=dict(size=9, color=cfg.COLOR_TEXT))
                sfig.update_yaxes(gridcolor="#222", tickfont=dict(size=9, color=cfg.COLOR_TEXT))
                sfig.update_layout(
                    height=320, paper_bgcolor=cfg.COLOR_BG, plot_bgcolor="#141414",
                    font=dict(color=cfg.COLOR_TEXT, size=10, family=cfg.FONT_FAMILY),
                    margin=dict(l=36, r=8, t=6, b=8), showlegend=False,
                    dragmode="zoom")
                st.plotly_chart(sfig, use_container_width=True, theme=None,
                                config={"displaylogo": False, "scrollZoom": True})

            # ── AI 起变信号判断（紧贴K线下方）──────────────────────
            if sr:
                is_sig = sai.get("is_signal", False)
                is_cont = sai.get("is_continuation", False)
                sig_color = cfg.COLOR_UP if is_sig else (cfg.COLOR_STOCK if is_cont else cfg.COLOR_MUTED)
                sig_icon = "✅" if is_sig else ("🔄" if is_cont else "❌")
                sig_text = ("起变" if is_sig
                             else ("延续" if is_cont else "无信号"))
                st.markdown(
                    f'<div class="stat-box"><div class="label">883958 信号</div>'
                    f'<div class="val" style="color:{e(sig_color)};">{sig_icon} '
                    f'{e(sig_text)}</div></div>',
                    unsafe_allow_html=True)
                if is_sig and sai.get("signal_reason"):
                    st.markdown(f'<span class="chip">📝 {e(sai["signal_reason"])}</span>',
                                unsafe_allow_html=True)
                    gate = sai.get("gate_reason", "")
                    if gate:
                        st.markdown(f'<span class="chip" style="font-size:11px;">🔍 {e(gate)}</span>',
                                    unsafe_allow_html=True)
                if is_cont and sai.get("continuation_reason"):
                    st.markdown(
                        f'<span class="chip" style="border-color:{e(cfg.COLOR_STOCK)};">'
                        f'🔄 {e(sai["continuation_reason"])}</span>',
                        unsafe_allow_html=True)
        with lr:
            if not sr:
                st.info("点击「分析起变信号」查看信号判断。")
            else:
                st.markdown(
                    f'<span class="chip">🪜 连板天梯 · {sev.get("ladder_count", 0)}只 · '
                    f'空间{e(sev.get("space", 0))}板</span>', unsafe_allow_html=True)
                if sld is not None and not sld.empty:
                    sortable_table(sld, stock_cols=("名称",), pct_cols=("涨跌幅",))
                else:
                    st.markdown(
                        '<div class="muted">⚠️ 无涨停池数据。东方财富涨停池接口仅保留近20个交易日数据，'
                        '更早的日期无法获取。883958/883418指数K线仍有历史数据可分析。</div>',
                        unsafe_allow_html=True)

        if sr:
            # ── 触发模式卡片 ─────────────────────────────────────────
            for sm in sai.get("modes", []):
                if not sm.get("triggered"):
                    continue
                st.markdown(
                    f'<span class="chip">🔥 <span class="k">{e(sm.get("mode", ""))}</span> · '
                    f'买点: {e(sm.get("buy_point", "-"))} · '
                    f'卖点: {e(sm.get("sell_point", "-"))} · '
                    f'仓位: {e(sm.get("position", "-"))}</span>',
                    unsafe_allow_html=True)
                cands = sm.get("candidates", [])
                if cands:
                    cdf = pd.DataFrame(cands)
                    show = [c for c in ("code", "name", "reason") if c in cdf.columns]
                    if show:
                        st.dataframe(cdf[show], use_container_width=True, hide_index=True,
                                     height=min(38 * (len(cdf) + 1) + 4, 200))

            if sai.get("summary"):
                st.markdown(f'<span class="chip">📋 总结: '
                            f'<span class="k">{e(sai["summary"])}</span></span>',
                            unsafe_allow_html=True)

            # ── 证据明细 ──────────────────────────────────────────────
            with st.expander("📋 当日证据明细"):
                hb = sev.get("high_board")
                lb = sev.get("lianban_883958", {})
                wp = sev.get("weipan_883418", {})
                st.markdown(f"**日期**: {sev.get('date', '-')}")
                st.markdown(f"**连板空间**: {sev.get('space', 0)}板")
                if hb:
                    st.markdown(
                        f"**高度板**: {hb.get('名称', '-')}({hb.get('代码', '-')}) "
                        f"{hb.get('连板数', 0)}板 · 行业={hb.get('所属行业', '-')} · "
                        f"首封={hb.get('首次封板时间', '-')} · "
                        f"炸板={hb.get('炸板次数', '-')} · 换手={hb.get('换手率', '-')}")
                    st.markdown(
                        f"**高度板是否一字板**: "
                        f"{'是' if sev.get('high_board_is_one_line') else '否'}")
                st.markdown(
                    f"**883958连板指数**: 连阴{lb.get('prev_green_red_days', 0)}天 · "
                    f"跳空高开={'是' if lb.get('gap_up') else '否'} · "
                    f"拉红={'是' if lb.get('turn_red') else '否'} · "
                    f"信号触发={'是' if lb.get('triggered') else '否'}")
                prem = sev.get("lianban_premium_883958", {})
                st.markdown(
                    f"**883958当日溢价**: 涨跌幅={prem.get('change_pct', 0)}% · "
                    f"溢价>3%={'是' if prem.get('triggered') else '否'}")
                st.markdown(
                    f"**883418微盘股**: 涨跌幅={wp.get('change_pct', 0)}% · "
                    f"安全={'是' if wp.get('safe') else '否'}")
                cont = sev.get("continuation_883958", {})
                if cont.get("yesterday_signal_date"):
                    st.markdown(
                        f"**延续信号检测**: 起变日期={cont['yesterday_signal_date']} · "
                        f"今日883958溢价={cont.get('premium_pct', 0)}% · "
                        f"延续触发={'是' if cont.get('triggered') else '否'}")
                st.markdown(f"**涨停池总数**: {sev.get('ladder_count', 0)}只")
                klines = lb.get("klines", [])
                if klines:
                    st.markdown("**883958 近期K线:**")
                    st.dataframe(pd.DataFrame(klines), use_container_width=True,
                                 hide_index=True)

        with st.expander("📖 短线模式规范（Short-term.md）"):
            sspec = cfg.DOCS_DIR / "Short-term.md"
            if sspec.exists():
                st.markdown(sspec.read_text(encoding="utf-8"))
