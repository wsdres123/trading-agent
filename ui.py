"""劫财AI交易 — UI 入口（全部界面集中于此）。

启动：bash run.sh  （自动设置 LD_LIBRARY_PATH 后 streamlit run ui.py）
四大功能（顶部 tab）：指数择时 / 主线模式 / 个股模式 / AI助手
本期实现 AI助手 全功能，其余三个为占位页。
配色：黑底 / 个股黄字 / 其他白字 / 涨红跌绿 / 字体偏大。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import streamlit as st
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from config import settings as cfg
from src import data, knowledge, ai_assistant as ai, stock_filter as sf

# ── 页面配置 ──────────────────────────────────────────────────────────────
st.set_page_config(page_title="劫财AI交易", page_icon="📊", layout="wide",
                   initial_sidebar_state="collapsed")

# ── 全局样式（黑底/白字/个股黄/涨红跌绿/字体偏大）─────────────────────────
st.markdown(f"""
<style>
    html, body, [class*="css"] {{
        font-family: {cfg.FONT_FAMILY};
        font-size: {cfg.FONT_SIZE}px;
    }}
    .stApp {{ background-color: {cfg.COLOR_BG}; color: {cfg.COLOR_TEXT}; }}
    h1, h2, h3 {{ color: {cfg.COLOR_TEXT}; }}
    .title-bar {{
        background: linear-gradient(90deg, #1a1a1a, #2a2210);
        border-left: 6px solid {cfg.COLOR_STOCK};
        padding: 14px 20px; border-radius: 8px; margin-bottom: 18px;
    }}
    .title-bar h1 {{ margin: 0; font-size: 30px; }}
    .title-bar .sub {{ color: {cfg.COLOR_MUTED}; font-size: 14px; margin-top: 4px; }}
    .stock-table {{ width: 100%; border-collapse: collapse; font-size: {cfg.FONT_SIZE}px; }}
    .stock-table th {{
        background: #242424; color: {cfg.COLOR_TEXT}; text-align: left;
        padding: 8px 10px; border-bottom: 2px solid #333; white-space: nowrap;
    }}
    .stock-table td {{ padding: 7px 10px; border-bottom: 1px solid #222; color: {cfg.COLOR_TEXT}; }}
    .stock-table tr:hover td {{ background: #1f1f1f; }}
    .stk {{ color: {cfg.COLOR_STOCK}; font-weight: 600; }}
    .up {{ color: {cfg.COLOR_UP}; font-weight: 600; }}
    .down {{ color: {cfg.COLOR_DOWN}; font-weight: 600; }}
    .muted {{ color: {cfg.COLOR_MUTED}; }}
    .chip {{
        display: inline-block; background: #242424; color: {cfg.COLOR_TEXT};
        border: 1px solid #333; border-radius: 12px; padding: 2px 10px;
        margin: 2px 4px 2px 0; font-size: 13px;
    }}
    .chip .k {{ color: {cfg.COLOR_STOCK}; }}
    .placeholder {{
        border: 1px dashed #333; border-radius: 10px; padding: 30px;
        text-align: center; color: {cfg.COLOR_MUTED}; background: #141414;
    }}
    .stat-box {{
        background: #1a1a1a; border-radius: 8px; padding: 12px 16px;
        border: 1px solid #2a2a2a;
    }}
    .stat-box .label {{ color: {cfg.COLOR_MUTED}; font-size: 13px; }}
    .stat-box .val {{ font-size: 22px; font-weight: 700; }}
    div[data-testid="stChatInput"] textarea {{ background: #1a1a1a; color: {cfg.COLOR_TEXT}; }}
</style>
""", unsafe_allow_html=True)


# ── 顶部标题 ──────────────────────────────────────────────────────────────
st.markdown(
    f'<div class="title-bar"><h1>劫财AI交易</h1>'
    f'<div class="sub">指数择时 · 主线模式 · 短线模式 · 个股模式 · AI助手 ｜ 多数据源实时更新</div></div>',
    unsafe_allow_html=True,
)

# ── 健康检查 ──────────────────────────────────────────────────────────────
_health = data.health()
_kstatus = knowledge.status()
_status_chips = []
_status_chips.append(f'<span class="chip">数据源(akshare): <span class="k">{"在线" if _health["akshare"] else "离线"}</span></span>')
_status_chips.append(f'<span class="chip">千问API: <span class="k">{"已配置" if _health["qwen_key"] else "未配置"}</span></span>')
_status_chips.append(f'<span class="chip">同花顺API: <span class="k">{"在线" if _health.get("ths_api") else "离线"}</span></span>')
_status_chips.append(f'<span class="chip">知识库: <span class="k">{_kstatus["files"]} 文件</span></span>')
st.markdown("".join(_status_chips), unsafe_allow_html=True)

_FEATURES = ["指数择时", "主线模式", "短线模式", "个股模式", "AI助手"]
_feature = st.radio("功能", _FEATURES, horizontal=True, label_visibility="collapsed")


# ── 通用：彩色 HTML 表格 ───────────────────────────────────────────────────
def colored_table(df: pd.DataFrame, stock_cols=("名称",), pct_cols=("涨跌幅", "ret_5d", "ret_30d")) -> str:
    if df.empty:
        return '<div class="muted">（无数据）</div>'
    rows = []
    head = "".join(f"<th>{c}</th>" for c in df.columns)
    for _, r in df.iterrows():
        cells = []
        for c in df.columns:
            v = r[c]
            cls = ""
            if c in stock_cols:
                cls = "stk"
            elif c in pct_cols and pd.notna(v):
                cls = "up" if float(v) > 0 else ("down" if float(v) < 0 else "")
            elif c == "is_100d_new_high" and bool(v):
                cls = "up"; v = "是"
            elif c == "is_100d_new_high":
                v = "否"
            if isinstance(v, float):
                v = f"{v:.2f}"
            cells.append(f'<td class="{cls}">{v if pd.notna(v) else "-"}</td>')
        rows.append("".join(cells))
    body = "".join(f"<tr>{row}</tr>" for row in rows)
    return f'<table class="stock-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def sortable_table(df: pd.DataFrame, stock_cols=("名称",), pct_cols=("涨跌幅", "ret_5d", "ret_30d")):
    """可排序彩色表格：st.dataframe + Styler（点击表头排序，涨红跌绿，个股黄字）。"""
    if df.empty:
        st.markdown('<div class="muted">（无数据）</div>', unsafe_allow_html=True)
        return
    show = df.copy()
    if "is_100d_new_high" in show.columns:
        show["is_100d_new_high"] = show["is_100d_new_high"].map(lambda v: "是" if bool(v) else "否")

    def _pct_color(v):
        if pd.isna(v):
            return ""
        v = float(v)
        if v > 0:
            return f"color: {cfg.COLOR_UP}; font-weight: 600"
        if v < 0:
            return f"color: {cfg.COLOR_DOWN}; font-weight: 600"
        return ""

    sty = show.style
    num_cols = [c for c in show.columns if pd.api.types.is_float_dtype(show[c])]
    if num_cols:
        sty = sty.format("{:.2f}", subset=num_cols, na_rep="-")
    _pcts = [c for c in pct_cols if c in show.columns]
    if _pcts:
        sty = sty.map(_pct_color, subset=_pcts) if hasattr(sty, "map") else sty.applymap(_pct_color, subset=_pcts)
    _stks = [c for c in stock_cols if c in show.columns]
    if _stks:
        _stk_css = f"color: {cfg.COLOR_STOCK}; font-weight: 600"
        sty = sty.map(lambda _: _stk_css, subset=_stks) if hasattr(sty, "map") else sty.applymap(lambda _: _stk_css, subset=_stks)
    st.dataframe(sty, use_container_width=True, hide_index=True,
                 height=min(38 * (len(show) + 1) + 4, 600))


def sortable_big_table(df: pd.DataFrame, pct_cols=("涨跌幅", "涨速"), stock_cols=("名称",)):
    """筛选结果专用大字表：18px、涨红跌绿、个股黄字、点击表头排序（JS）。"""
    import html as _html
    import streamlit.components.v1 as components
    if df.empty:
        st.markdown('<div class="muted">（无数据）</div>', unsafe_allow_html=True)
        return
    ths = "".join(
        f'<th data-i="{i}">{_html.escape(str(c))} <span class="arrow"></span></th>'
        for i, c in enumerate(df.columns))
    trs = []
    for _, r in df.iterrows():
        tds = []
        for c in df.columns:
            v = r[c]
            cls, raw = "", ""
            if c in stock_cols:
                cls = "stk"
            elif c in pct_cols and pd.notna(v):
                cls = "up" if float(v) > 0 else ("down" if float(v) < 0 else "")
            if pd.isna(v):
                txt = "-"
            elif isinstance(v, float):
                raw = str(v)
                txt = f"{v:,.2f}" if c != "竞价量" else f"{v:,.0f}"
            else:
                raw = str(v)
                txt = str(v)
            tds.append(f'<td class="{cls}" data-v="{_html.escape(raw)}">{_html.escape(txt)}</td>')
        trs.append("<tr>" + "".join(tds) + "</tr>")
    table = (f'<table id="t"><thead><tr>{ths}</tr></thead>'
             f'<tbody>{"".join(trs)}</tbody></table>')
    page = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    body{{margin:0;background:{cfg.COLOR_BG};font-family:{cfg.FONT_FAMILY};}}
    table{{width:100%;border-collapse:collapse;font-size:18px;color:{cfg.COLOR_TEXT};}}
    th{{background:#242424;padding:9px 10px;text-align:left;white-space:nowrap;
        cursor:pointer;user-select:none;position:sticky;top:0;border-bottom:2px solid #333;}}
    th:hover{{color:{cfg.COLOR_STOCK};}}
    td{{padding:8px 10px;border-bottom:1px solid #222;white-space:nowrap;}}
    tr:hover td{{background:#1f1f1f;}}
    .stk{{color:{cfg.COLOR_STOCK};font-weight:600;}}
    .up{{color:{cfg.COLOR_UP};font-weight:600;}}
    .down{{color:{cfg.COLOR_DOWN};font-weight:600;}}
    .arrow{{font-size:12px;color:#888;}}
    </style></head><body>{table}<script>
    const tb=document.querySelector('#t tbody');
    document.querySelectorAll('#t th').forEach((th,i)=>{{
      th.addEventListener('click',()=>{{
        const asc=th.dataset.asc!=='1';
        document.querySelectorAll('#t th').forEach(h=>{{h.dataset.asc='';h.querySelector('.arrow').textContent='';}});
        th.dataset.asc=asc?'1':'0';
        th.querySelector('.arrow').textContent=asc?'\\u25B2':'\\u25BC';
        const rows=[...tb.rows];
        rows.sort((a,b)=>{{
          const x=a.cells[i].dataset.v,y=b.cells[i].dataset.v;
          if(x===''&&y==='')return 0; if(x==='')return 1; if(y==='')return -1;
          const nx=parseFloat(x),ny=parseFloat(y);
          let c=(!isNaN(nx)&&!isNaN(ny))?nx-ny:String(x).localeCompare(String(y),'zh');
          return asc?c:-c;
        }});
        rows.forEach(r=>tb.appendChild(r));
      }});
    }});
    </script></body></html>"""
    components.html(page, height=min(46 * (len(df) + 1) + 24, 640), scrolling=True)


def conds_chips(conds: list) -> str:
    if not conds:
        return '<span class="muted">（无条件）</span>'
    labels = []
    for c in conds:
        f = c["field"]
        if f == "close_gt_ma":
            if int(c.get("days", 1) or 1) > 1:
                labels.append(f"连续{c['days']}日收盘>{c['ma']}日均线")
            else:
                labels.append(f"收盘价>{c['ma']}日均线")
        elif f == "return_ndays":
            labels.append(f"今日涨幅>{c['min_pct']}%" if int(c['days']) == 1
                          else f"{c['days']}日涨幅>{c['min_pct']}%")
        elif f == "new_high":
            labels.append(f"{c['days']}日新高")
        elif f == "free_float_cap":
            labels.append(f"流通市值>{c['min_yi']}亿")
        elif f == "sector":
            labels.append(f"属于{c['name']}板块")
        elif f == "board":
            labels.append(f"{'非' if c.get('exclude') else ''}{c['name']}")
    return "".join(f'<span class="chip"><span class="k">●</span> {l}</span>' for l in labels)


def _cond_labels(conds: list) -> list:
    out = []
    for c in conds:
        f = c.get("field")
        if f == "close_gt_ma":
            if int(c.get("days", 1) or 1) > 1:
                out.append(f"连续{c.get('days')}日收盘>{c.get('ma')}日均线")
            else:
                out.append(f"收盘>{c.get('ma')}日均线")
        elif f == "return_ndays":
            out.append(f"今日涨幅>{c.get('min_pct')}%" if int(c.get('days', 0)) == 1
                       else f"{c.get('days')}日涨幅>{c.get('min_pct')}%")
        elif f == "new_high":
            out.append(f"{c.get('days')}日新高")
        elif f == "free_float_cap":
            out.append(f"流通市值>{c.get('min_yi')}亿")
        elif f == "sector":
            out.append(f"{c.get('name')}板块")
        elif f == "board":
            out.append(f"{'非' if c.get('exclude') else ''}{c.get('name')}")
    return out


# ── 占位页 ────────────────────────────────────────────────────────────────
def placeholder(title: str, spec_file: str, desc: str):
    st.markdown(
        f'<div class="placeholder"><h3>{title}</h3>'
        f'<p style="font-size:18px;">🚧 功能开发中，敬请期待</p>'
        f'<p>{desc}</p></div>', unsafe_allow_html=True)
    spec = cfg.DOCS_DIR / spec_file
    if spec.exists():
        with st.expander(f"📖 相关规范：{spec_file}"):
            st.markdown(spec.read_text(encoding="utf-8"))


if _feature == "指数择时":
    import plotly.graph_objects as go
    from src import index_timing as it

    def _kline_fig(df: pd.DataFrame, title: str, marks: dict | None = None,
                   height: int = 620,
                   ma_lines: tuple[tuple[int, str, str], ...] | None = None) -> "go.Figure":
        """日K + 可定制均线；marks={日期:{signal,source}} 多标在K线下方、空/转标在上方。"""
        fig = go.Figure()
        pct = (df["收盘"] / df["收盘"].shift(1) - 1) * 100
        _hover = [
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
            text=_hover, hoverinfo="text"))
        _ma_default = ((5, "MA5", "#f7d774"), (10, "MA10", "#4dd0e1"),
                       (30, "MA30", "#ba68c8"), (60, "MA60", "#90a4ae"))
        for n, label, color in (ma_lines or _ma_default):
            fig.add_trace(go.Scatter(
                x=df["日期"], y=df["收盘"].rolling(n).mean().round(2),
                name=label, line=dict(width=1.5, color=color), hoverinfo="skip"))
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
                        hovertext=hover, hoverinfo="text"))
        fig.update_xaxes(type="category", nticks=10, showgrid=False,
                         tickfont=dict(size=12, color=cfg.COLOR_TEXT),
                         tickangle=-45, rangeslider_visible=False)
        fig.update_yaxes(gridcolor="#222", showticklabels=True,
                         tickfont=dict(size=12, color=cfg.COLOR_TEXT),
                         side="left")
        fig.update_layout(
            title=dict(text=title, font=dict(size=18, color=cfg.COLOR_TEXT)),
            height=height, paper_bgcolor=cfg.COLOR_BG, plot_bgcolor="#141414",
            font=dict(color=cfg.COLOR_TEXT, size=13, family=cfg.FONT_FAMILY),
            margin=dict(l=55, r=10, t=48, b=60), hovermode="x unified",
            dragmode="pan",
            legend=dict(orientation="h", y=1.06, x=0, bgcolor="rgba(0,0,0,0)"))
        return fig

    _PLOTLY_CFG = {"scrollZoom": True, "displaylogo": False,
                   "modeBarButtonsToAdd": ["zoom2d", "resetScale2d"]}

    # ── 今日预判面板 ──────────────────────────────────────────────────────
    if it.should_auto_judge():
        with st.spinner(f"已到尾盘 {it.JUDGE_TIME}，AI 自动判断今日多空转…"):
            it.ai_judge()
    _today = time.strftime("%Y-%m-%d")
    _rec = it.load_ai_predictions().get(_today)
    _hist = it.load_history_signals()
    _latest_review = _hist.iloc[-1] if not _hist.empty else None

    c1, c2, c3, c4, c5 = st.columns([1, 1, 2, 1.4, 1])
    _sig = (_rec or {}).get("signal", "")
    _sig_color = it.SIGNAL_COLORS.get(_sig, cfg.COLOR_MUTED)
    with c1:
        st.markdown(f'<div class="stat-box"><div class="label">今日预判（{_today}）</div>'
                    f'<div class="val" style="color:{_sig_color};">{_sig or "未判断"}</div></div>',
                    unsafe_allow_html=True)
    with c2:
        st.markdown(f'<div class="stat-box"><div class="label">中级周期</div>'
                    f'<div class="val">{(_rec or {}).get("mid_cycle", "-")}</div></div>',
                    unsafe_allow_html=True)
    with c3:
        st.markdown(f'<div class="stat-box"><div class="label">仓位建议</div>'
                    f'<div class="val" style="font-size:17px;">{(_rec or {}).get("position", "-")}</div></div>',
                    unsafe_allow_html=True)
    with c4:
        _to = it.market_turnover_wanyi()
        _to_color = cfg.COLOR_UP if (_to or 0) >= 2.5 else cfg.COLOR_TEXT
        st.markdown(f'<div class="stat-box"><div class="label">全市场成交额</div>'
                    f'<div class="val" style="color:{_to_color};">'
                    f'{f"{_to} 万亿" if _to is not None else "-"}</div></div>',
                    unsafe_allow_html=True)
    with c5:
        if st.button("🤖 立即判断", use_container_width=True):
            with st.spinner("AI 判断中…"):
                r = it.ai_judge(force=True)
            if r.get("error"):
                st.error(f"判断失败：{r['error']}")
            else:
                st.rerun()

    if _rec and _rec.get("reason"):
        st.markdown(f'<span class="chip">AI 依据: <span class="k">{_rec["reason"]}</span>'
                    f'（{_rec.get("time", "")}）</span>', unsafe_allow_html=True)
    _hint = it.pattern_hint(it.all_signals())
    if _hint:
        st.markdown(f'<span class="chip">📐 组合规则: <span class="k">{_hint}</span></span>',
                    unsafe_allow_html=True)
    if _latest_review is not None:
        st.markdown(
            f'<span class="chip">📋 复盘表最新（{_latest_review["日期"]}）: '
            f'指数择时 <span class="k">{_latest_review["信号"]}</span> · '
            f'中级周期 <span class="k">{_latest_review["中级周期"]}</span> · '
            f'情绪 {_latest_review["情绪周期"]} · {_latest_review["指数"]}</span>',
            unsafe_allow_html=True)

    # ── 指数日K（上证带多空转标记，其余可切换；容量1000天，60秒实时刷新）──
    _c_idx, _c_rf = st.columns([5, 1])
    with _c_idx:
        _idx_name = st.radio("指数", list(data.INDEX_KLINE_SYMBOLS.keys()),
                             horizontal=True, label_visibility="collapsed")
    with _c_rf:
        if st.button("🔄 刷新行情", use_container_width=True):
            data.clear_cache("get_index_daily")
            st.rerun()
    _sym = data.INDEX_KLINE_SYMBOLS[_idx_name]
    _idf = data.get_index_daily(_sym, days=1060)
    if _idf.empty:
        st.error("指数日K数据获取失败，请稍后刷新重试。")
    else:
        _idf = _idf.tail(1000).reset_index(drop=True)
        st.plotly_chart(_kline_fig(_idf,
                                   f"{_idx_name} 日K（{len(_idf)}天 · MA5/10/30/60）"),
                        use_container_width=True, theme=None, config=_PLOTLY_CFG)

    # ── 平均股价日K（多空线 = MA10，多空转标记在此）──────────────────────────
    _avg = it.avg_price_kline(days=360)
    if _avg.empty:
        st.warning("平均股价需先构建 390 日全市场数据缓存（约1-2分钟）。")
        if st.button("🔄 构建全市场数据缓存"):
            prog = st.progress(0.0, text="构建全市场指标缓存…")

            def _bcb(done, total):
                prog.progress(done / total, text=f"拉取历史 {done}/{total}")

            data.build_metrics_cache(progress_cb=_bcb)
            prog.empty()
            st.rerun()
    else:
        _marks = it.all_signals()
        st.plotly_chart(_kline_fig(_avg,
                                   f"全市场平均股价 日K（{len(_avg)}天 · 多空线=MA10"
                                   f"{' · 多空转标记' if _marks else ''}）",
                                   height=520, marks=_marks,
                                   ma_lines=((10, "多空线", "#4dd0e1"),)),
                        use_container_width=True, theme=None, config=_PLOTLY_CFG)

    with st.expander("📖 中级周期规范（node_spec.md）"):
        _spec = cfg.DOCS_DIR / "node_spec.md"
        if _spec.exists():
            st.markdown(_spec.read_text(encoding="utf-8"))

elif _feature == "主线模式":
    placeholder("主线模式", "theme_spec.md",
                "在趋势A/C周期中识别主线板块，跟踪趋势核心/补涨/主线ETF。")

elif _feature == "短线模式":
    placeholder("短线模式", "node_spec.md",
                "短线周期判断与情绪博弈（连板空间/分歧转一致/弱转强等），规则待补充。")

elif _feature == "个股模式":
    placeholder("个股模式", "theme_stock_spec.md",
                "个股买卖点与持仓管理（后续补充K线与个股详情）。")


# ── AI 助手 ───────────────────────────────────────────────────────────────
if _feature == "AI助手":
    st.markdown("### 🤖 AI 助手")
    _ai_mode = st.radio("AI助手模式", ["💬 智能问答", "🔬 个股分组筛选"],
                        horizontal=True, label_visibility="collapsed")

    # ── 智能问答 ──────────────────────────────────────────────────────────
    if _ai_mode.startswith("💬"):
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
                with st.spinner("思考中…"):
                    ans = ai.chat(q, history=st.session_state.messages)
                st.markdown(ans)
            st.session_state.messages.append({"role": "assistant", "content": ans})

    # ── 个股分组筛选 ──────────────────────────────────────────────────────
    else:
        st.markdown("用自然语言描述筛选条件，AI 解析后实时筛股并保存为可更新的分组。")
        st.caption("示例：收盘价大于5日均线，30天涨幅大于40%，5日涨幅大于10%，百日新高，自由流通市值大于30亿，属于半导体板块")

        # ── 数据缓存：筛选秒出的关键 ──
        cs = data.metrics_cache_status()
        if cs["exists"]:
            badge = "已就绪" if cs["fresh"] else "已过期"
            st.markdown(
                f'<span class="chip">数据缓存: <span class="k">{badge}</span> · '
                f'{cs["rows"]}只 · 截至 {cs["mtime"]} · {cs["age_min"]}分钟前</span>',
                unsafe_allow_html=True)
        else:
            st.markdown(
                '<span class="chip">数据缓存: <span class="k">未构建</span>'
                '（首次筛选自动构建，约15秒，之后毫秒级）</span>', unsafe_allow_html=True)
        _cb1, _cb2 = st.columns([4, 1])
        with _cb2:
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
                        sf.save_group(gname.strip(), conds or [], res.to_dict("records"))
                        st.success(f"已保存分组「{gname.strip()}」。")

        # ── 已存分组 ──────────────────────────────────────────────────────
        st.markdown("---")
        st.markdown("#### 已保存分组")
        groups = sf.list_groups()
        if not groups:
            st.markdown('<span class="muted">暂无分组，筛选后可保存。</span>', unsafe_allow_html=True)
        for g in groups:
            with st.container():
                gc1, gc2, gc3, gc4 = st.columns([3, 1, 1, 1])
                with gc1:
                    chips = "".join(
                        '<span class="chip">' + l + "</span>" for l in _cond_labels(g["conditions"])
                    )
                    st.markdown(
                        f'<div class="stat-box"><span style="font-size:18px;font-weight:700;'
                        f'color:{cfg.COLOR_STOCK}">{g["name"]}</span>'
                        f'<br><span class="muted">更新于 {g.get("updated_at","")}</span>'
                        f'<br>{chips}'
                        f'<br><span class="muted">含 {len(g.get("stocks",[]))} 只个股</span></div>',
                        unsafe_allow_html=True)
                with gc2:
                    if st.button("👁 查看", key=f"view_{g['name']}"):
                        st.session_state["view_group"] = g["name"]
                with gc3:
                    if st.button("🔄 更新", key=f"upd_{g['name']}"):
                        prog = st.progress(0.0, text="更新中…")
                        upd = sf.update_group(g["name"], progress_cb=lambda d, t: prog.progress(d / t))
                        prog.empty()
                        if upd:
                            st.success(f"已刷新「{g['name']}」，现有 {len(upd['stocks'])} 只。")
                        else:
                            st.error("更新失败。")
                with gc4:
                    if st.button("🗑 删除", key=f"del_{g['name']}"):
                        sf.delete_group(g["name"])
                        st.success(f"已删除「{g['name']}」。")
                        st.rerun()
                if st.session_state.get("view_group") == g["name"]:
                    gdf = pd.DataFrame(g.get("stocks", []))
                    show_cols = [c for c in ["代码", "名称", "最新价", "涨跌幅", "涨速", "竞价量",
                                             "涨停封单额", "自由流通市值(亿)", "成交额(亿)", "概念板块",
                                             "成交额", "流通市值_亿"] if c in gdf.columns]
                    sortable_big_table(gdf[show_cols] if show_cols else gdf)
