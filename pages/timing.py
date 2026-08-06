"""指数择时页：中级周期/多空转预判 + 情绪节点 + 热榜分组 + 指数/平均股价日K。"""
import time

import pandas as pd
import streamlit as st

from config import settings as cfg
from src import data, stock_filter as sf
from src.realtime_widget import market_bar_html
from pages.shared import e, sortable_table


def render(current_user: str):

        import plotly.graph_objects as go
        from src import index_timing as it

        # ── 实时行情 bar（WS 增量推送：平均股价/指数随推送跳动，不等整页刷新）──
        st.markdown(market_bar_html(), unsafe_allow_html=True)

        def kline_fig(df: pd.DataFrame, title: str, marks: dict | None = None,
                       height: int = 620,
                       ma_lines: tuple[tuple[int, str, str], ...] | None = None) -> "go.Figure":
            """日K + 可定制均线 + 可选成交量柱；marks={日期:{signal,source}} 多标在K线下方、空/转标在上方。"""
            has_vol = "成交量" in df.columns and df["成交量"].notna().any()
            fig = go.Figure()
            pct = (df["收盘"] / df["收盘"].shift(1) - 1) * 100
            hover = [
                f"{d}<br>开盘 {o:.2f}<br>最高 {h:.2f}<br>最低 {l:.2f}<br>收盘 {c:.2f}"
                + (f"<br>涨跌幅 {p:+.2f}%" if pd.notna(p) else "")
                for d, o, h, l, c, p in zip(df["日期"], df["开盘"], df["最高"],
                                            df["最低"], df["收盘"], pct)
            ]
            fig.add_trace(go.Candlestick(
                x=df["日期"], open=df["开盘"], high=df["最高"], low=df["最低"], close=df["收盘"],
                increasing_line_color=cfg.COLOR_UP, increasing_fillcolor=cfg.COLOR_UP,
                decreasing_line_color=cfg.COLOR_DOWN, decreasing_fillcolor=cfg.COLOR_DOWN,
                name=title, showlegend=False,
                text=hover, hoverinfo="text",
                yaxis="y"))
            ma_default = ((5, "MA5", "#f7d774"), (10, "MA10", "#4dd0e1"),
                           (30, "MA30", "#ba68c8"), (60, "MA60", "#90a4ae"))
            for n, label, color in (ma_lines or ma_default):
                fig.add_trace(go.Scatter(
                    x=df["日期"], y=df["收盘"].rolling(n).mean().round(2),
                    name=label, line=dict(width=1.5, color=color), hoverinfo="skip",
                    yaxis="y"))
            if has_vol:
                vol_colors = [
                    cfg.COLOR_UP if c >= o else cfg.COLOR_DOWN
                    for c, o in zip(df["收盘"], df["开盘"])
                ]
                fig.add_trace(go.Bar(
                    x=df["日期"], y=df["成交量"],
                    name="成交量", marker_color=vol_colors,
                    opacity=0.6, showlegend=False,
                    hovertemplate="%{x}<br>成交量 %{y:.2e}<extra></extra>",
                    yaxis="y2"))
            if marks:
                d2i = {d: i for i, d in enumerate(df["日期"])}
                span = float(df["最高"].max() - df["最低"].min()) or 1.0
                off = span * 0.012
                for sig, color in (("多", cfg.COLOR_UP), ("空", cfg.COLOR_DOWN), ("转", cfg.COLOR_STOCK)):
                    xs, ys, hover = [], [], []
                    for dstr in sorted(marks):
                        info = marks[dstr]
                        if info["signal"] != sig or dstr not in d2i:
                            continue
                        i = d2i[dstr]
                        xs.append(dstr)
                        ys.append(float(df["最低"].iloc[i]) - off if sig == "多"
                                  else float(df["最高"].iloc[i]) + off)
                        hover.append(f"{dstr} {sig}（{info.get('source', '')}）")
                    if xs:
                        fig.add_trace(go.Scatter(
                            x=xs, y=ys, mode="text", text=[sig] * len(xs), name=sig,
                            textfont=dict(color=color, size=10, family=cfg.FONT_FAMILY),
                            hovertext=hover, hoverinfo="text",
                            yaxis="y"))
            price_domain = [0.22, 1.0] if has_vol else [0.0, 1.0]
            vol_domain = [0.0, 0.18]
            fig.update_xaxes(type="category", nticks=10, showgrid=False,
                             tickfont=dict(size=12, color=cfg.COLOR_TEXT),
                             tickangle=-45, rangeslider_visible=False)
            fig.update_layout(
                yaxis=dict(domain=price_domain, gridcolor="#222", showticklabels=True,
                           tickfont=dict(size=12, color=cfg.COLOR_TEXT), side="left"),
                yaxis2=dict(domain=vol_domain, showticklabels=False,
                            gridcolor="#1a1a1a", showgrid=False) if has_vol else {},
                title=dict(text=title, font=dict(size=18, color=cfg.COLOR_TEXT)),
                height=height, paper_bgcolor=cfg.COLOR_BG, plot_bgcolor="#141414",
                font=dict(color=cfg.COLOR_TEXT, size=13, family=cfg.FONT_FAMILY),
                margin=dict(l=55, r=10, t=48, b=60), hovermode="x unified",
                dragmode="pan",
                legend=dict(orientation="h", y=1.06, x=0, bgcolor="rgba(0,0,0,0)"))
            return fig

        PLOTLY_CFG = {"scrollZoom": True, "displaylogo": False,
                       "modeBarButtonsToAdd": ["zoom2d", "resetScale2d"]}

        # ── 今日预判面板 ──────────────────────────────────────────────────────
        if it.should_auto_judge():
            with st.spinner(f"已到尾盘 {it.JUDGE_TIME}，AI 自动判断今日多空转…"):
                it.ai_judge()
        today = time.strftime("%Y-%m-%d")
        rec = it.load_ai_predictions().get(today)
        hist = it.load_history_signals()
        latest_review = hist.iloc[-1] if not hist.empty else None

        c1, c2, c3, c4, c5 = st.columns([1, 1, 2, 1.4, 1])
        sig = (rec or {}).get("signal", "")
        sig_color = it.SIGNAL_COLORS.get(sig, cfg.COLOR_MUTED)
        with c1:
            st.markdown(f'<div class="stat-box"><div class="label">今日预判（{e(today)}）</div>'
                        f'<div class="val" style="color:{e(sig_color)};">{e(sig or "未判断")}</div></div>',
                        unsafe_allow_html=True)
        with c2:
            st.markdown(f'<div class="stat-box"><div class="label">中级周期</div>'
                        f'<div class="val">{e((rec or {}).get("mid_cycle", "-"))}</div></div>',
                        unsafe_allow_html=True)
        with c3:
            st.markdown(f'<div class="stat-box"><div class="label">仓位建议</div>'
                        f'<div class="val" style="font-size:17px;">{e((rec or {}).get("position", "-"))}</div></div>',
                        unsafe_allow_html=True)
        with c4:
            to = it.market_turnover_wanyi()
            to_color = cfg.COLOR_UP if (to or 0) >= 2.5 else cfg.COLOR_TEXT
            st.markdown(f'<div class="stat-box"><div class="label">全市场成交额</div>'
                        f'<div class="val" style="color:{e(to_color)};">'
                        f'{f"{to} 万亿" if to is not None else "-"}</div></div>',
                        unsafe_allow_html=True)
        with c5:
            if st.button("🤖 立即判断", use_container_width=True):
                with st.spinner("AI 判断中…"):
                    r = it.ai_judge(force=True)
                if r.get("error"):
                    st.error(f"判断失败：{r['error']}")
                else:
                    st.rerun()

        if rec and rec.get("reason"):
            st.markdown(f'<span class="chip">AI 依据: <span class="k">{e(rec["reason"])}</span>'
                        f'（{e(rec.get("time", ""))}）</span>', unsafe_allow_html=True)
        hint = it.pattern_hint(it.all_signals())
        if hint:
            st.markdown(f'<span class="chip">📐 组合规则: <span class="k">{e(hint)}</span></span>',
                        unsafe_allow_html=True)
        if latest_review is not None:
            st.markdown(
                f'<span class="chip">📋 复盘表最新（{e(latest_review["日期"])}）: '
                f'指数择时 <span class="k">{e(latest_review["信号"])}</span> · '
                f'中级周期 <span class="k">{e(latest_review["中级周期"])}</span> · '
                f'情绪 {e(latest_review["情绪周期"])} · {e(latest_review["指数"])}</span>',
                unsafe_allow_html=True)

        # ── 情绪节点（AI 学习竞价表 + emotional node.md，初步判断）──────────────
        from src import emotion_node as en

        if en.should_auto_judge():
            with st.spinner(f"已到尾盘 {en.JUDGE_TIME}，AI 自动判断今日情绪节点…"):
                en.ai_judge()
        erec = en.load_predictions().get(today)
        rt_daban = en.daban_damian_count()
        en1, en2, en3, en4 = st.columns([1, 1, 2.4, 1])
        with en1:
            st.markdown(f'<div class="stat-box"><div class="label">今日情绪节点</div>'
                        f'<div class="val" style="color:{e(cfg.COLOR_STOCK)};">'
                        f'{e((erec or {}).get("node") or "未判断")}</div></div>',
                        unsafe_allow_html=True)
        with en2:
            daban_val = rt_daban if rt_daban is not None else \
                (erec or {}).get("stats", {}).get("打板大面数")
            daban_color = cfg.COLOR_DOWN if (daban_val and daban_val >= 10) else \
                (cfg.COLOR_UP if daban_val and daban_val < 10 else cfg.COLOR_MUTED)
            st.markdown(f'<div class="stat-box"><div class="label">打板大面</div>'
                        f'<div class="val" style="color:{e(daban_color)};font-size:26px;">'
                        f'{e(daban_val if daban_val is not None else "—")}</div>'
                        f'<div class="label" style="margin-top:2px;">曾涨停回落&lt;5%</div></div>',
                        unsafe_allow_html=True)
        with en3:
            est = (erec or {}).get("stats", {})
            epv = (erec or {}).get("prev_stats", {})
            if est:
                pm = epv.get("大面数")
                cm = est.get("大面数")
                cmp = ""
                if pm and cm is not None:
                    chg = (cm - pm) / pm * 100
                    cmp = (f' （昨 {e(pm)} → 今 {e(cm)}，'
                            f'{"减少" if chg < 0 else "增加"}{e(f"{abs(chg):.0f}")}%）')
                st.markdown(
                    f'<span class="chip">盘面: '
                    f'大面(跌>7%) <span class="k">{e(cm if cm is not None else "-")}</span>{cmp} · '
                    f'跌停 <span style="color:{e(cfg.COLOR_DOWN)};">{e(est.get("跌停数", "-"))}</span> · '
                    f'涨停 <span style="color:{e(cfg.COLOR_UP)};">{e(est.get("涨停数", "-"))}</span> · '
                    f'涨超7% <span class="k">{e(est.get("涨超7数", "-"))}</span> · '
                    f'高度股(10日>40%) <span class="k">{e(est.get("高度个股数", "-"))}</span></span>',
                    unsafe_allow_html=True)
            if erec and erec.get("reason"):
                st.markdown(f'<span class="chip">🎭 AI 依据: <span class="k">{e(erec["reason"])}</span>'
                            f' ｜ {e(erec.get("advice", ""))}（{e(erec.get("time", ""))}）</span>',
                            unsafe_allow_html=True)
        with en4:
            if st.button("🎭 判断情绪节点", use_container_width=True):
                with st.spinner("AI 学习竞价表历史 + 统计当日盘面…"):
                    er = en.ai_judge(force=True)
                if er.get("error"):
                    st.error(f"判断失败：{er['error']}")
                else:
                    st.rerun()
        auc = en.load_auction_table()
        if not auc.empty:
            al = auc.iloc[-1]
            st.markdown(
                f'<span class="chip">📒 竞价表最新（{e(al["日期"])}）: '
                f'节点 <span class="k">{e(al["节点"])}</span> · 指数 {e(al.get("指数", ""))} · '
                f'小票亏效 {e(al.get("小票亏效", "") or "-")} · '
                f'大票亏效 {e(al.get("大票亏效", "") or "-")} · '
                f'断板 {e(al.get("断板", "") or "-")}</span>', unsafe_allow_html=True)

        # ── 同花顺热榜小窗 ＋ 旁边放AI助手筛选分组（自选1-2个）──────────────────
        hot = data.get_hot_stocks(top=10)
        if not hot.empty:
            hpct = pd.to_numeric(hot.get("涨跌幅"), errors="coerce")
            if isinstance(hpct, pd.Series) and hpct.notna().any():
                hidx = float(hpct.mean())
                hc = cfg.COLOR_UP if hidx > 0 else (cfg.COLOR_DOWN if hidx < 0 else cfg.COLOR_TEXT)
                st.markdown(
                    f'<span class="chip">🔥 同花顺热门个股指数（前10平均涨跌幅）: '
                    f'<span style="color:{e(hc)};font-weight:700;">{hidx:+.2f}%</span> · '
                    f'大跌(≤-7%) <span style="color:{e(cfg.COLOR_DOWN)};">{int((hpct <= -7).sum())}</span> · '
                    f'翻红 <span style="color:{e(cfg.COLOR_UP)};">{int((hpct > 0).sum())}</span> · '
                    f'<span class="k">前10热门股（小窗可上下左右滑动）↓</span></span>',
                    unsafe_allow_html=True)
        hw, gw = st.columns([2.2, 2.8])
        with hw:
            if st.button("🔄", key="idx_hot_rf", help="刷新热门股"):
                data.clear_cache("get_hot_stocks")
                data.clear_cache("get_stock_spot")
                st.rerun()
            if hot.empty:
                st.markdown('<span class="chip">🔥 同花顺热榜: <span class="k">暂不可用</span></span>',
                            unsafe_allow_html=True)
            else:
                hcols = [c for c in ("排名", "代码", "名称", "最新价", "涨跌幅", "成交额(亿)", "板块")
                          if c in hot.columns]
                sortable_table(hot[hcols], pct_cols=("涨跌幅",), stock_cols=("名称",),
                               height=220)
        with gw:
            groups = sf.list_groups(current_user)
            if not groups:
                st.markdown('<div class="muted">（暂无分组——到「AI助手」用一句话筛选并保存分组，'
                            '这里可自选展示 1-2 个分组）</div>', unsafe_allow_html=True)
            else:
                gnames = [g["name"] for g in groups]
                gsel = st.multiselect("展示分组", gnames, default=gnames[:2],
                                       max_selections=2, label_visibility="collapsed",
                                       placeholder="自选要展示的分组（最多2个）",
                                       key="idx_show_groups")
                if gsel:
                    for gc, gn in zip(st.columns(len(gsel)), gsel):
                        g = next(x for x in groups if x["name"] == gn)
                        with gc:
                            st.markdown(
                                f'<span class="chip">📁 <span class="k">{e(gn)}</span> · '
                                f'{len(g.get("stocks", []))}只 · 更新 {e(g.get("updated_at", "-"))}</span>',
                                unsafe_allow_html=True)
                            if st.button("🔄", key=f"idx_gupd_{gn}", help="更新分组"):
                                with st.spinner(f"更新「{gn}」…"):
                                    upd = sf.update_group(gn, username=current_user)
                                if upd:
                                    st.rerun()
                                else:
                                    st.error("更新失败。")
                            gdf = pd.DataFrame(g.get("stocks", []))
                            if gdf.empty:
                                st.markdown('<div class="muted">（空分组，可到AI助手更新）</div>',
                                            unsafe_allow_html=True)
                            else:
                                gshow = [c for c in ("名称", "最新价", "涨跌幅", "涨速",
                                                      "成交额(亿)", "概念板块", "竞价量",
                                                      "自由流通市值(亿)", "ret_5d", "ret_30d")
                                          if c in gdf.columns]
                                sortable_table(gdf[gshow], stock_cols=("名称",),
                                               pct_cols=("涨跌幅", "涨速", "ret_5d", "ret_30d"),
                                               height=178)

        # ── 指数日K（上证带多空转标记，其余可切换；容量1000天，60秒实时刷新）──
        c_idx, c_rf = st.columns([5, 1])
        with c_idx:
            idx_name = st.radio("指数", list(data.INDEX_KLINE_SYMBOLS.keys()),
                                 horizontal=True, label_visibility="collapsed")
        with c_rf:
            if st.button("🔄 刷新行情", use_container_width=True):
                data.clear_cache("get_index_daily")
                st.rerun()
        sym = data.INDEX_KLINE_SYMBOLS[idx_name]
        idf = data.get_index_daily(sym, days=1060)
        if idf.empty:
            st.error("指数日K数据获取失败，请稍后刷新重试。")
        else:
            idf = idf.tail(1000).reset_index(drop=True)
            st.plotly_chart(kline_fig(idf,
                                       f"{idx_name} 日K（{len(idf)}天 · MA5/10/30/60）"),
                            use_container_width=True, theme=None, config=PLOTLY_CFG)

        # ── 平均股价日K（多空线 = MA10，多空转标记在此）──────────────────────────
        avg = it.avg_price_kline(days=360)
        if avg.empty:
            st.warning("平均股价需先构建全市场数据缓存（约1-2分钟，含全部股票兜底数据）。")
            if st.button("🔄 构建全市场数据缓存"):
                prog = st.progress(0.0, text="构建全市场指标缓存…")

                def bcb(done, total):
                    prog.progress(done / total, text=f"拉取历史 {done}/{total}")

                data.build_metrics_cache(progress_cb=bcb)
                prog.empty()
                st.rerun()
        else:
            # 实时数字由 WS 推送（顶部行情 bar），整页只低频重渲染（60s）重算当日K线/信号
            import datetime as dt
            now = dt.datetime.now()
            m = now.hour * 60 + now.minute
            is_trading = data.is_trading_day()
            if is_trading:
                from streamlit_autorefresh import st_autorefresh
                if 565 <= m <= 905 and not 690 < m <= 780:
                    st_autorefresh(interval=60000, key="avg_price_auto")  # 交易时段 60s

            # 每次加载都取最新实时均价（get_stock_spot 有60秒TTL缓存自动节流）
            rt = data.get_realtime_avg_price()
            rt_val = rt.get("avg_price")
            today_str = time.strftime("%Y-%m-%d")
            if rt_val and (len(avg) == 0 or str(avg["日期"].iloc[-1]) != today_str):
                prev_close = float(avg["收盘"].iloc[-1]) if not avg.empty else rt_val
                today_row = pd.DataFrame([{
                    "日期": today_str,
                    "开盘": round(prev_close, 2),
                    "最高": round(max(prev_close, rt_val), 2),
                    "最低": round(min(prev_close, rt_val), 2),
                    "收盘": round(rt_val, 2),
                    "成交量": float("nan"),
                }])
                avg = pd.concat([avg, today_row], ignore_index=True)

            marks = it.all_signals()
            ac1, ac2 = st.columns([5, 1])
            with ac1:
                st.plotly_chart(kline_fig(avg,
                                           f"全市场平均股价 日K（{len(avg)}天 · 多空线=MA10"
                                           f"{' · 多空转标记' if marks else ''}）",
                                           height=520, marks=marks,
                                           ma_lines=((10, "多空线", "#4dd0e1"),)),
                                use_container_width=True, theme=None, config=PLOTLY_CFG)
            with ac2:
                rt_color = cfg.COLOR_STOCK if rt_val else cfg.COLOR_MUTED
                prev_rt = float(avg["收盘"].iloc[-2]) if len(avg) >= 2 else None
                rt_pct = ((rt_val / prev_rt - 1) * 100) if (rt_val and prev_rt) else None
                pct_color = cfg.COLOR_UP if (rt_pct and rt_pct > 0) else (cfg.COLOR_DOWN if (rt_pct and rt_pct < 0) else cfg.COLOR_MUTED)
                st.markdown(
                    f'<div class="stat-box" style="margin-top:60px;">'
                    f'<div class="label">{"🔴 实时" if cfg.is_trading_hours() else "平均"}平均股价</div>'
                    f'<div class="val" style="color:{e(rt_color)};font-size:26px;">'
                    f'{f"{rt_val:.3f}" if rt_val else "—"}</div>'
                    + (f'<div class="val" style="color:{e(pct_color)};font-size:15px;">{rt_pct:+.2f}%</div>' if rt_pct is not None else "")
                    + f'<div class="label" style="margin-top:4px;">覆盖 {e(rt.get("stock_count","—"))} 只<br>{e(rt.get("timestamp",""))}</div>'
                    f'</div>',
                    unsafe_allow_html=True)
                st.markdown('<div style="height:8px;"></div>', unsafe_allow_html=True)
                if st.button("🔄 刷新均价", use_container_width=True):
                    data.clear_cache("get_stock_spot")
                    st.rerun()

        with st.expander("📖 中级周期规范（node_spec.md）"):
            spec = cfg.DOCS_DIR / "node_spec.md"
            if spec.exists():
                st.markdown(spec.read_text(encoding="utf-8"))

        with st.expander("📖 情绪节点规范（emotional node.md）"):
            espec = cfg.DOCS_DIR / "emotional node.md"
            if espec.exists():
                st.markdown(espec.read_text(encoding="utf-8"))
