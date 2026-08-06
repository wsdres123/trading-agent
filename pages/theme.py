"""主线模式页：区间主线板块识别 + 成交前10指数 + AI 判断 + 核心/补涨个股。"""
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import settings as cfg
from src import data, theme_mode as tm
from pages.shared import e, sortable_table


def render():

    
        st.markdown("### 🧭 主线模式（趋势A/C周期 · 主线板块与核心/补涨个股）")

        # 同花顺热股指数（当前参考，无历史序列，仅辅助当前判断）
        try:
            hot = data.get_hot_stocks(top=10)
            if not hot.empty:
                hpct = pd.to_numeric(hot.get("涨跌幅"), errors="coerce").dropna()
                if len(hpct):
                    hidx = float(hpct.mean())
                    hc = cfg.COLOR_UP if hidx > 0 else (cfg.COLOR_DOWN if hidx < 0 else cfg.COLOR_TEXT)
                    st.markdown(
                        f'<span class="chip">🔥 同花顺热股指数(当前参考): '
                        f'<span style="color:{e(hc)};font-weight:700;">{hidx:+.2f}%</span> · '
                        f'前10平均涨跌幅（无历史，仅当前判断参考）</span>', unsafe_allow_html=True)
        except Exception:
            pass

        # 表单内改日期不触发页面重跑（否则日历一选就整页刷新，感觉卡死）
        with st.form("tm_form", border=False):
            tc1, tc2, tc3, tc4 = st.columns([1.4, 1.4, 1.2, 4])
            with tc1:
                t_start = st.date_input("开始日期", value=date.today() - timedelta(days=60),
                                         max_value=date.today())
            with tc2:
                t_end = st.date_input("结束日期", value=date.today(),
                                       max_value=date.today())
            with tc3:
                st.markdown('<div style="height:28px;"></div>', unsafe_allow_html=True)
                t_go = st.form_submit_button("🔍 识别主线", use_container_width=True,
                                              type="primary")

        # 仅点按钮或首次进入才计算，改日期不自动重算（避免界面卡顿）
        if t_go or "tm_res" not in st.session_state:
            with st.spinner("识别主线：逐日筛选候选票 → 共同板块 → 连续强度…"):
                st.session_state["tm_res"] = tm.detect(str(t_start), str(t_end))
                st.session_state.pop("tm_ai", None)
        tr = st.session_state.get("tm_res", {})

        if tr.get("error") == "need_cache":
            st.warning("主线识别需先构建全市场数据缓存（含成交量序列，约1-2分钟）。"
                       "旧版缓存缺成交量，也需重建。")
            if st.button("🔄 构建全市场数据缓存", key="tm_build"):
                prog = st.progress(0.0, text="构建全市场指标缓存…")

                def tm_bcb(done, total):
                    prog.progress(done / total, text=f"拉取历史 {done}/{total}")

                data.build_metrics_cache(progress_cb=tm_bcb)
                prog.empty()
                st.session_state.pop("tm_res", None)
                st.rerun()
        elif tr.get("error") == "no_days":
            st.error("所选区间内没有可用交易日（缓存序列约390天，请缩小/调整区间）。")
        elif tr:
            gid = tr.get("gate_open_days", 0)
            gtot = tr.get("gate_total_days", 0)
            gcolor = cfg.COLOR_UP if gid > 0 else cfg.COLOR_MUTED
            jc, kc = st.columns([1.35, 1])
            with jc:
                # ── 主线判断结论（主线唯一；B/D周期日不计入连续强度）────────────
                if tr["has_mainline"]:
                    ml0 = tr["mainlines"][0]
                    st.markdown(
                        f'<div class="stat-box"><div class="label">主线判断'
                        f'（{e(tr["start"])} ~ {e(tr["end"])} · 主线唯一 · B/D周期无主线）</div>'
                        f'<div class="val" style="color:{e(cfg.COLOR_UP)};">✅ 唯一主线：'
                        f'{e(ml0["board"])}{"（进行中）" if ml0["ongoing"] else "（已结束）"}'
                        f'</div></div>', unsafe_allow_html=True)
                    rel, sec = ml0.get("related", []), ml0.get("secondary", [])
                    if rel:
                        st.markdown(f'<span class="chip">🔗 关联板块（成员重叠，共{len(rel)}个）: '
                                    f'<span class="k">{e("、".join(rel[:8]))}'
                                    f'{"等" if len(rel) > 8 else ""}</span></span>',
                                    unsafe_allow_html=True)
                    if sec:
                        st.markdown(f'<span class="chip">📎 次强板块（非主线）: '
                                    f'<span class="k">{e("、".join(sec[:8]))}</span></span>',
                                    unsafe_allow_html=True)
                else:
                    gate_note = ("；且成交前10指数全程下行（开启参考偏弱）"
                                  if gid == 0 else "")
                    st.markdown(
                        f'<div class="stat-box"><div class="label">主线判断'
                        f'（{e(tr["start"])} ~ {e(tr["end"])}）</div>'
                        f'<div class="val" style="color:{e(cfg.COLOR_MUTED)};">❌ 无主线：'
                        f'区间内无板块满足「30日涨幅>50% + 成交额>30亿 的票 ≥3只 且连续强≥5天」'
                        f'（B/D周期日不计入）{e(gate_note)}</div></div>', unsafe_allow_html=True)
            with kc:
                # ── 成交前10指数 K线（同花顺883902，主线开启参考，缩小置右）────
                kc1, kc2 = st.columns([4, 1])
                with kc1:
                    st.markdown(
                        f'<span class="chip">📊 成交前10指数(同花顺883902): '
                        f'<span style="color:{e(gcolor)};font-weight:700;">上升趋势 {gid}/{gtot} 日</span></span>',
                        unsafe_allow_html=True)
                with kc2:
                    if st.button("🔄 刷新", key="refresh_ths_idx", use_container_width=True):
                        data.clear_cache("get_ths_index_daily")
                        with st.spinner("刷新成交前10指数…"):
                            st.session_state["tm_res"] = tm.detect(str(t_start), str(t_end))
                        st.session_state.pop("tm_ai", None)
                        st.rerun()
                idf = tr.get("turnover_idx")
                if idf is not None and not idf.empty:
                    idf = idf.dropna(subset=["收盘"])
                    if not idf.empty:
                        ifig = go.Figure(go.Candlestick(
                            x=idf["日期"], open=idf["开盘"], high=idf["最高"],
                            low=idf["最低"], close=idf["收盘"],
                            increasing=dict(line=dict(color=cfg.COLOR_UP), fillcolor=cfg.COLOR_UP),
                            decreasing=dict(line=dict(color=cfg.COLOR_DOWN), fillcolor=cfg.COLOR_DOWN),
                            name="成交前10指数"))
                        if idf["MA5"].notna().any():
                            ifig.add_trace(go.Scatter(x=idf["日期"], y=idf["MA5"],
                                                       mode="lines", name="MA5",
                                                       line=dict(color=cfg.COLOR_TEXT, width=1.2, dash="dot")))
                        ifig.update_xaxes(type="category", nticks=8, tickangle=-45, showgrid=False,
                                           rangeslider_visible=False,
                                           tickfont=dict(size=9, color=cfg.COLOR_TEXT))
                        ifig.update_yaxes(gridcolor="#222", tickfont=dict(size=9, color=cfg.COLOR_TEXT))
                        ifig.update_layout(
                            title=dict(text="成交前10指数(同花顺883902)·主线开启参考",
                                       font=dict(size=12, color=cfg.COLOR_TEXT)),
                            height=250, paper_bgcolor=cfg.COLOR_BG, plot_bgcolor="#141414",
                            font=dict(color=cfg.COLOR_TEXT, size=10, family=cfg.FONT_FAMILY),
                            margin=dict(l=36, r=8, t=34, b=38),
                            showlegend=False)
                        st.plotly_chart(ifig, use_container_width=True, theme=None,
                                        config={"displaylogo": False})
            # ── AI 最终判断（大模型学习 theme_spec.md 后给出结论）───────────
            if "tm_ai" not in st.session_state:
                with st.spinner("🤖 AI 学习 theme_spec.md 并判断主线中…"):
                    st.session_state["tm_ai"] = tm.ai_analyze(tr)
            if st.session_state.get("tm_ai"):
                st.markdown(f'<span class="chip">🤖 AI 主线判断: '
                            f'<span class="k">{e(st.session_state["tm_ai"])}</span></span>',
                            unsafe_allow_html=True)

            # ── 各主线板块明细 ──────────────────────────────────────────────
            for ml in tr.get("mainlines", []):
                st.markdown(
                    f'<span class="chip">🔥 主线 <span class="k">{e(ml["board"])}</span> · '
                    f'{e(ml["start"])} ~ {e(ml["end"])} 连续强 {ml["days"]} 天'
                    f'{"（进行中）" if ml["ongoing"] else "（已结束）"} · '
                    f'单日峰值 {ml["max_count"]} 只 · '
                    f'门槛{"✅开启" if ml.get("gate_open") else "⚠未开启"}</span>',
                    unsafe_allow_html=True)
                cc1, cc2 = st.columns(2)
                with cc1:
                    st.markdown(f'**⚔️ 趋势核心（阵眼，成交额≥{tm.CORE_AMOUNT_YI:.0f}亿）'
                                '· 卖点：尾盘破5日线/退潮/跌停/主升2次分歧**')
                    sortable_table(pd.DataFrame(ml["core"]), pct_cols=("区间涨幅",))
                with cc2:
                    st.markdown('**🚀 趋势补涨 · 卖点：尾盘破3日线/退潮**')
                    sortable_table(pd.DataFrame(ml["follow"]), pct_cols=("区间涨幅",))

            # ── 每日候选强度时间轴 ──────────────────────────────────────────
            dd = tr.get("daily")
            if dd is not None and not dd.empty:
                tfig = go.Figure(go.Bar(
                    x=dd["日期"], y=dd["候选数"],
                    marker_color=cfg.COLOR_STOCK,
                    hovertext=[f"{r['日期']}<br>候选 {r['候选数']} 只<br>{r['强板块'] or '无强板块'}"
                               for _, r in dd.iterrows()],
                    hoverinfo="text"))
                tfig.update_xaxes(type="category", nticks=12, tickangle=-45, showgrid=False,
                                   tickfont=dict(size=12, color=cfg.COLOR_TEXT))
                tfig.update_yaxes(gridcolor="#222",
                                   tickfont=dict(size=12, color=cfg.COLOR_TEXT))
                tfig.update_layout(
                    title=dict(text="每日主线候选强度（30日涨幅>50% 且 成交额>30亿 的票数）",
                               font=dict(size=15, color=cfg.COLOR_TEXT)),
                    height=300, paper_bgcolor=cfg.COLOR_BG, plot_bgcolor="#141414",
                    font=dict(color=cfg.COLOR_TEXT, size=13, family=cfg.FONT_FAMILY),
                    margin=dict(l=45, r=10, t=42, b=55))
                st.plotly_chart(tfig, use_container_width=True, theme=None,
                                config={"displaylogo": False})
                with st.expander("📋 每日强板块明细"):
                    sortable_table(dd, stock_cols=(), pct_cols=())

        with st.expander("📖 主线筛选规范（theme_spec.md）"):
            tspec = cfg.DOCS_DIR / "theme_spec.md"
            if tspec.exists():
                st.markdown(tspec.read_text(encoding="utf-8"))
