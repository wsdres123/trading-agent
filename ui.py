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
    header[data-testid="stHeader"] {{ display: none; }}
    div.block-container {{ padding-top: 0.4rem; padding-bottom: 0.6rem; }}
    div[data-testid="stVerticalBlock"] {{ gap: 0.45rem; }}
    /* 置顶：标题行 + 功能选择整体作为一个 sticky 容器，避免互相遮挡 */
    div[data-testid="stVerticalBlock"] > div:has(> div[data-testid="stVerticalBlock"] .title-bar) {{
        position: sticky; top: 0; z-index: 120; background: {cfg.COLOR_BG};
        padding-bottom: 2px; border-bottom: 1px solid #222;
    }}
    .title-bar {{
        background: linear-gradient(90deg, #1a1a1a, #2a2210);
        border-left: 5px solid {cfg.COLOR_STOCK};
        padding: 4px 14px; border-radius: 6px; margin-bottom: 2px;
        display: flex; align-items: center; flex-wrap: nowrap; overflow: hidden;
        white-space: nowrap;
    }}
    .title-bar .app-title {{
        font-size: 20px; font-weight: 700; margin-right: 8px; white-space: nowrap;
    }}
    .title-bar .chip {{ flex-shrink: 0; }}
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
        border: 1px solid #333; border-radius: 11px; padding: 2px 10px;
        margin: 1px 3px 1px 0; font-size: 14px;
    }}
    .chip .k {{ color: {cfg.COLOR_STOCK}; }}
    div[data-testid="stDataFrame"] {{ zoom: 1.15; }}
    .placeholder {{
        border: 1px dashed #333; border-radius: 10px; padding: 30px;
        text-align: center; color: {cfg.COLOR_MUTED}; background: #141414;
    }}
    .stat-box {{
        background: #1a1a1a; border-radius: 6px; padding: 6px 12px;
        border: 1px solid #2a2a2a;
    }}
    .stat-box .label {{ color: {cfg.COLOR_MUTED}; font-size: 13px; }}
    .stat-box .val {{ font-size: 20px; font-weight: 700; }}
    div[data-testid="stChatInput"] textarea {{ background: #1a1a1a; color: {cfg.COLOR_TEXT}; }}
</style>
""", unsafe_allow_html=True)


# ── 顶部标题 + 状态（单行紧凑，滚动置顶）──────────────────────────────────
_health = data.health()
_kstatus = knowledge.status()
_status_chips = []
_status_chips.append(f'<span class="chip">数据源(akshare): <span class="k">{"在线" if _health["akshare"] else "离线"}</span></span>')
_status_chips.append(f'<span class="chip">千问API: <span class="k">{"已配置" if _health["qwen_key"] else "未配置"}</span></span>')
_status_chips.append(f'<span class="chip">同花顺API: <span class="k">{"在线" if _health.get("ths_api") else "离线"}</span></span>')
_status_chips.append(f'<span class="chip">知识库: <span class="k">{_kstatus["files"]} 文件</span></span>')
with st.container():
    st.markdown(
        f'<div class="title-bar"><span class="app-title">劫财AI交易</span>'
        + "".join(_status_chips) + '</div>',
        unsafe_allow_html=True,
    )
    _FEATURES = ["指数择时", "主线模式", "短线模式", "个股模式", "明日推演", "AI助手"]
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


def sortable_table(df: pd.DataFrame, stock_cols=("名称",), pct_cols=("涨跌幅", "ret_5d", "ret_30d"),
                   height: int | None = None):
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
                 height=height or min(38 * (len(show) + 1) + 4, 600))


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
    return "".join(f'<span class="chip"><span class="k">●</span> {l}</span>'
                   for l in _cond_labels(conds))


def _rng_label(name: str, lo, hi, unit: str) -> str:
    if lo is not None and hi is not None:
        return f"{name}{lo}-{hi}{unit}"
    if lo is not None:
        return f"{name}>{lo}{unit}"
    return f"{name}<{hi}{unit}"


def _cond_labels(conds: list) -> list:
    out = []
    for c in conds:
        f = c.get("field")
        if f in ("close_gt_ma", "close_lt_ma"):
            op = ">" if f == "close_gt_ma" else "<"
            if int(c.get("days", 1) or 1) > 1:
                out.append(f"连续{c.get('days')}日收盘{op}{c.get('ma')}日均线")
            else:
                out.append(f"收盘{op}{c.get('ma')}日均线")
        elif f == "return_ndays":
            name = "今日涨幅" if int(c.get("days", 0)) == 1 else f"{c.get('days')}日涨幅"
            out.append(_rng_label(name, c.get("min_pct"), c.get("max_pct"), "%"))
        elif f == "new_high":
            out.append(f"{c.get('days')}日新高")
        elif f == "new_low":
            out.append(f"{c.get('days')}日新低")
        elif f == "free_float_cap":
            out.append(_rng_label("流通市值", c.get("min_yi"), c.get("max_yi"), "亿"))
        elif f == "total_cap":
            out.append(_rng_label("总市值", c.get("min_yi"), c.get("max_yi"), "亿"))
        elif f == "amount":
            out.append(_rng_label("今日成交额", c.get("min_yi"), c.get("max_yi"), "亿"))
        elif f == "turnover_rate":
            out.append(_rng_label("换手率", c.get("min_pct"), c.get("max_pct"), "%"))
        elif f == "price":
            out.append(_rng_label("股价", c.get("min"), c.get("max"), "元"))
        elif f == "volume_surge":
            out.append(f"量比≥{c.get('ratio')}（对比5日均量）")
        elif f == "consecutive_up":
            out.append(f"连涨{c.get('days')}天")
        elif f == "consecutive_down":
            out.append(f"连跌{c.get('days')}天")
        elif f == "ma_bullish":
            out.append("均线多头排列(5>10>20)")
        elif f == "ma_bearish":
            out.append("均线空头排列(5<10<20)")
        elif f == "drawdown_from_high":
            out.append(_rng_label(f"距{c.get('days', 100)}日高点回撤",
                                  c.get("min_pct"), c.get("max_pct"), "%"))
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

    # ── 情绪节点（AI 学习竞价表 + emotional node.md，初步判断）──────────────
    from src import emotion_node as en

    if en.should_auto_judge():
        with st.spinner(f"已到尾盘 {en.JUDGE_TIME}，AI 自动判断今日情绪节点…"):
            en.ai_judge()
    _erec = en.load_predictions().get(_today)
    _en1, _en2, _en3 = st.columns([1, 3.4, 1])
    with _en1:
        st.markdown(f'<div class="stat-box"><div class="label">今日情绪节点</div>'
                    f'<div class="val" style="color:{cfg.COLOR_STOCK};">'
                    f'{(_erec or {}).get("node") or "未判断"}</div></div>',
                    unsafe_allow_html=True)
    with _en2:
        _est = (_erec or {}).get("stats", {})
        _epv = (_erec or {}).get("prev_stats", {})
        if _est:
            _pm = _epv.get("大面数")
            _cm = _est.get("大面数")
            _cmp = ""
            if _pm and _cm is not None:
                _chg = (_cm - _pm) / _pm * 100
                _cmp = (f' （昨 {_pm} → 今 {_cm}，'
                        f'{"减少" if _chg < 0 else "增加"}{abs(_chg):.0f}%）')
            st.markdown(
                f'<span class="chip">盘面: '
                f'大面(跌>7%) <span class="k">{_cm if _cm is not None else "-"}</span>{_cmp} · '
                f'跌停 <span style="color:{cfg.COLOR_DOWN};">{_est.get("跌停数", "-")}</span> · '
                f'涨停 <span style="color:{cfg.COLOR_UP};">{_est.get("涨停数", "-")}</span> · '
                f'涨超7% <span class="k">{_est.get("涨超7数", "-")}</span> · '
                f'高度股(10日>40%) <span class="k">{_est.get("高度个股数", "-")}</span></span>',
                unsafe_allow_html=True)
        if _erec and _erec.get("reason"):
            st.markdown(f'<span class="chip">🎭 AI 依据: <span class="k">{_erec["reason"]}</span>'
                        f' ｜ {_erec.get("advice", "")}（{_erec.get("time", "")}）</span>',
                        unsafe_allow_html=True)
    with _en3:
        if st.button("🎭 判断情绪节点", use_container_width=True):
            with st.spinner("AI 学习竞价表历史 + 统计当日盘面…"):
                _er = en.ai_judge(force=True)
            if _er.get("error"):
                st.error(f"判断失败：{_er['error']}")
            else:
                st.rerun()
    _auc = en.load_auction_table()
    if not _auc.empty:
        _al = _auc.iloc[-1]
        st.markdown(
            f'<span class="chip">📒 竞价表最新（{_al["日期"]}）: '
            f'节点 <span class="k">{_al["节点"]}</span> · 指数 {_al.get("指数", "")} · '
            f'小票亏效 {_al.get("小票亏效", "") or "-"} · '
            f'大票亏效 {_al.get("大票亏效", "") or "-"} · '
            f'断板 {_al.get("断板", "") or "-"}</span>', unsafe_allow_html=True)

    # ── 同花顺热榜小窗 ＋ 旁边放AI助手筛选分组（自选1-2个）──────────────────
    _hot = data.get_hot_stocks(top=10)
    if not _hot.empty:
        _hpct = pd.to_numeric(_hot.get("涨跌幅"), errors="coerce")
        if _hpct is not None and _hpct.notna().any():
            _hidx = float(_hpct.mean())
            _hc = cfg.COLOR_UP if _hidx > 0 else (cfg.COLOR_DOWN if _hidx < 0 else cfg.COLOR_TEXT)
            st.markdown(
                f'<span class="chip">🔥 同花顺热门个股指数（前10平均涨跌幅）: '
                f'<span style="color:{_hc};font-weight:700;">{_hidx:+.2f}%</span> · '
                f'大跌(≤-7%) <span style="color:{cfg.COLOR_DOWN};">{int((_hpct <= -7).sum())}</span> · '
                f'翻红 <span style="color:{cfg.COLOR_UP};">{int((_hpct > 0).sum())}</span> · '
                f'<span class="k">前10热门股（小窗可上下左右滑动）↓</span></span>',
                unsafe_allow_html=True)
    _hw, _gw = st.columns([2.2, 2.8])
    with _hw:
        if st.button("🔄", key="idx_hot_rf", help="刷新热门股"):
            data.clear_cache("get_hot_stocks")
            data.clear_cache("get_stock_spot")
            st.rerun()
        if _hot.empty:
            st.markdown('<span class="chip">🔥 同花顺热榜: <span class="k">暂不可用</span></span>',
                        unsafe_allow_html=True)
        else:
            _hcols = [c for c in ("排名", "代码", "名称", "最新价", "涨跌幅", "成交额(亿)", "板块")
                      if c in _hot.columns]
            sortable_table(_hot[_hcols], pct_cols=("涨跌幅",), stock_cols=("名称",),
                           height=220)
    with _gw:
        _groups = sf.list_groups()
        if not _groups:
            st.markdown('<div class="muted">（暂无分组——到「AI助手」用一句话筛选并保存分组，'
                        '这里可自选展示 1-2 个分组）</div>', unsafe_allow_html=True)
        else:
            _gnames = [g["name"] for g in _groups]
            _gsel = st.multiselect("展示分组", _gnames, default=_gnames[:2],
                                   max_selections=2, label_visibility="collapsed",
                                   placeholder="自选要展示的分组（最多2个）",
                                   key="idx_show_groups")
            if _gsel:
                for _gc, _gn in zip(st.columns(len(_gsel)), _gsel):
                    _g = next(x for x in _groups if x["name"] == _gn)
                    with _gc:
                        st.markdown(
                            f'<span class="chip">📁 <span class="k">{_gn}</span> · '
                            f'{len(_g.get("stocks", []))}只 · 更新 {_g.get("updated_at", "-")}</span>',
                            unsafe_allow_html=True)
                        if st.button("🔄", key=f"idx_gupd_{_gn}", help="更新分组"):
                            with st.spinner(f"更新「{_gn}」…"):
                                _upd = sf.update_group(_gn)
                            if _upd:
                                st.rerun()
                            else:
                                st.error("更新失败。")
                        _gdf = pd.DataFrame(_g.get("stocks", []))
                        if _gdf.empty:
                            st.markdown('<div class="muted">（空分组，可到AI助手更新）</div>',
                                        unsafe_allow_html=True)
                        else:
                            _gshow = [c for c in ("名称", "最新价", "涨跌幅", "涨速",
                                                  "成交额(亿)", "概念板块", "竞价量",
                                                  "自由流通市值(亿)", "ret_5d", "ret_30d")
                                      if c in _gdf.columns]
                            sortable_table(_gdf[_gshow], stock_cols=("名称",),
                                           pct_cols=("涨跌幅", "涨速", "ret_5d", "ret_30d"),
                                           height=178)

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

    with st.expander("📖 情绪节点规范（emotional node.md）"):
        _espec = cfg.DOCS_DIR / "emotional node.md"
        if _espec.exists():
            st.markdown(_espec.read_text(encoding="utf-8"))

elif _feature == "主线模式":
    from datetime import date, timedelta

    import plotly.graph_objects as go

    from src import theme_mode as tm

    st.markdown("### 🧭 主线模式（趋势A/C周期 · 主线板块与核心/补涨个股）")

    # 同花顺热股指数（当前参考，无历史序列，仅辅助当前判断）
    try:
        _hot = data.get_hot_stocks(top=10)
        if not _hot.empty:
            _hpct = pd.to_numeric(_hot.get("涨跌幅"), errors="coerce").dropna()
            if len(_hpct):
                _hidx = float(_hpct.mean())
                _hc = cfg.COLOR_UP if _hidx > 0 else (cfg.COLOR_DOWN if _hidx < 0 else cfg.COLOR_TEXT)
                st.markdown(
                    f'<span class="chip">🔥 同花顺热股指数(当前参考): '
                    f'<span style="color:{_hc};font-weight:700;">{_hidx:+.2f}%</span> · '
                    f'前10平均涨跌幅（无历史，仅当前判断参考）</span>', unsafe_allow_html=True)
    except Exception:
        pass

    # 表单内改日期不触发页面重跑（否则日历一选就整页刷新，感觉卡死）
    with st.form("tm_form", border=False):
        _tc1, _tc2, _tc3, _tc4 = st.columns([1.4, 1.4, 1.2, 4])
        with _tc1:
            _t_start = st.date_input("开始日期", value=date.today() - timedelta(days=60),
                                     max_value=date.today())
        with _tc2:
            _t_end = st.date_input("结束日期", value=date.today(),
                                   max_value=date.today())
        with _tc3:
            st.markdown('<div style="height:28px;"></div>', unsafe_allow_html=True)
            _t_go = st.form_submit_button("🔍 识别主线", use_container_width=True,
                                          type="primary")

    # 仅点按钮或首次进入才计算，改日期不自动重算（避免界面卡顿）
    if _t_go or "tm_res" not in st.session_state:
        with st.spinner("识别主线：逐日筛选候选票 → 共同板块 → 连续强度…"):
            st.session_state["tm_res"] = tm.detect(str(_t_start), str(_t_end))
            st.session_state.pop("tm_ai", None)
    _tr = st.session_state.get("tm_res", {})

    if _tr.get("error") == "need_cache":
        st.warning("主线识别需先构建全市场数据缓存（含成交量序列，约1-2分钟）。"
                   "旧版缓存缺成交量，也需重建。")
        if st.button("🔄 构建全市场数据缓存", key="tm_build"):
            _prog = st.progress(0.0, text="构建全市场指标缓存…")

            def _tm_bcb(done, total):
                _prog.progress(done / total, text=f"拉取历史 {done}/{total}")

            data.build_metrics_cache(progress_cb=_tm_bcb)
            _prog.empty()
            st.session_state.pop("tm_res", None)
            st.rerun()
    elif _tr.get("error") == "no_days":
        st.error("所选区间内没有可用交易日（缓存序列约390天，请缩小/调整区间）。")
    elif _tr:
        _gid = _tr.get("gate_open_days", 0)
        _gtot = _tr.get("gate_total_days", 0)
        _gcolor = cfg.COLOR_UP if _gid > 0 else cfg.COLOR_MUTED
        _jc, _kc = st.columns([1.35, 1])
        with _jc:
            # ── 主线判断结论（主线唯一；B/D周期日不计入连续强度）────────────
            if _tr["has_mainline"]:
                _ml0 = _tr["mainlines"][0]
                st.markdown(
                    f'<div class="stat-box"><div class="label">主线判断'
                    f'（{_tr["start"]} ~ {_tr["end"]} · 主线唯一 · B/D周期无主线）</div>'
                    f'<div class="val" style="color:{cfg.COLOR_UP};">✅ 唯一主线：'
                    f'{_ml0["board"]}{"（进行中）" if _ml0["ongoing"] else "（已结束）"}'
                    f'</div></div>', unsafe_allow_html=True)
                _rel, _sec = _ml0.get("related", []), _ml0.get("secondary", [])
                if _rel:
                    st.markdown(f'<span class="chip">🔗 关联板块（成员重叠，共{len(_rel)}个）: '
                                f'<span class="k">{"、".join(_rel[:8])}'
                                f'{"等" if len(_rel) > 8 else ""}</span></span>',
                                unsafe_allow_html=True)
                if _sec:
                    st.markdown(f'<span class="chip">📎 次强板块（非主线）: '
                                f'<span class="k">{"、".join(_sec[:8])}</span></span>',
                                unsafe_allow_html=True)
            else:
                _gate_note = ("；且成交前10指数全程下行（开启参考偏弱）"
                              if _gid == 0 else "")
                st.markdown(
                    f'<div class="stat-box"><div class="label">主线判断'
                    f'（{_tr["start"]} ~ {_tr["end"]}）</div>'
                    f'<div class="val" style="color:{cfg.COLOR_MUTED};">❌ 无主线：'
                    f'区间内无板块满足「30日涨幅>50% + 成交额>30亿 的票 ≥3只 且连续强≥5天」'
                    f'（B/D周期日不计入）{_gate_note}</div></div>', unsafe_allow_html=True)
        with _kc:
            # ── 成交前10指数 K线（同花顺883902，主线开启参考，缩小置右）────
            st.markdown(
                f'<span class="chip">📊 成交前10指数(同花顺883902): '
                f'<span style="color:{_gcolor};font-weight:700;">上升趋势 {_gid}/{_gtot} 日</span></span>',
                unsafe_allow_html=True)
            _idf = _tr.get("turnover_idx")
            if _idf is not None and not _idf.empty:
                _idf = _idf.dropna(subset=["收盘"])
                if not _idf.empty:
                    _ifig = go.Figure(go.Candlestick(
                        x=_idf["日期"], open=_idf["开盘"], high=_idf["最高"],
                        low=_idf["最低"], close=_idf["收盘"],
                        increasing=dict(line=dict(color=cfg.COLOR_UP), fillcolor=cfg.COLOR_UP),
                        decreasing=dict(line=dict(color=cfg.COLOR_DOWN), fillcolor=cfg.COLOR_DOWN),
                        name="成交前10指数"))
                    if _idf["MA5"].notna().any():
                        _ifig.add_trace(go.Scatter(x=_idf["日期"], y=_idf["MA5"],
                                                   mode="lines", name="MA5",
                                                   line=dict(color=cfg.COLOR_TEXT, width=1.2, dash="dot")))
                    _ifig.update_xaxes(type="category", nticks=8, tickangle=-45, showgrid=False,
                                       rangeslider_visible=False,
                                       tickfont=dict(size=9, color=cfg.COLOR_TEXT))
                    _ifig.update_yaxes(gridcolor="#222", tickfont=dict(size=9, color=cfg.COLOR_TEXT))
                    _ifig.update_layout(
                        title=dict(text="成交前10指数(同花顺883902)·主线开启参考",
                                   font=dict(size=12, color=cfg.COLOR_TEXT)),
                        height=250, paper_bgcolor=cfg.COLOR_BG, plot_bgcolor="#141414",
                        font=dict(color=cfg.COLOR_TEXT, size=10, family=cfg.FONT_FAMILY),
                        margin=dict(l=36, r=8, t=34, b=38),
                        showlegend=False)
                    st.plotly_chart(_ifig, use_container_width=True, theme=None,
                                    config={"displaylogo": False})
        # ── AI 最终判断（大模型学习 theme_spec.md 后给出结论）───────────
        if "tm_ai" not in st.session_state:
            with st.spinner("🤖 AI 学习 theme_spec.md 并判断主线中…"):
                st.session_state["tm_ai"] = tm.ai_analyze(_tr)
        if st.session_state.get("tm_ai"):
            st.markdown(f'<span class="chip">🤖 AI 主线判断: '
                        f'<span class="k">{st.session_state["tm_ai"]}</span></span>',
                        unsafe_allow_html=True)

        # ── 各主线板块明细 ──────────────────────────────────────────────
        for _ml in _tr.get("mainlines", []):
            st.markdown(
                f'<span class="chip">🔥 主线 <span class="k">{_ml["board"]}</span> · '
                f'{_ml["start"]} ~ {_ml["end"]} 连续强 {_ml["days"]} 天'
                f'{"（进行中）" if _ml["ongoing"] else "（已结束）"} · '
                f'单日峰值 {_ml["max_count"]} 只 · '
                f'门槛{"✅开启" if _ml.get("gate_open") else "⚠未开启"}</span>',
                unsafe_allow_html=True)
            _cc1, _cc2 = st.columns(2)
            with _cc1:
                st.markdown(f'**⚔️ 趋势核心（阵眼，成交额≥{tm.CORE_AMOUNT_YI:.0f}亿）'
                            '· 卖点：尾盘破5日线/退潮/跌停/主升2次分歧**')
                sortable_table(pd.DataFrame(_ml["core"]), pct_cols=("区间涨幅",))
            with _cc2:
                st.markdown('**🚀 趋势补涨 · 卖点：尾盘破3日线/退潮**')
                sortable_table(pd.DataFrame(_ml["follow"]), pct_cols=("区间涨幅",))

        # ── 每日候选强度时间轴 ──────────────────────────────────────────
        _dd = _tr.get("daily")
        if _dd is not None and not _dd.empty:
            _tfig = go.Figure(go.Bar(
                x=_dd["日期"], y=_dd["候选数"],
                marker_color=cfg.COLOR_STOCK,
                hovertext=[f"{r['日期']}<br>候选 {r['候选数']} 只<br>{r['强板块'] or '无强板块'}"
                           for _, r in _dd.iterrows()],
                hoverinfo="text"))
            _tfig.update_xaxes(type="category", nticks=12, tickangle=-45, showgrid=False,
                               tickfont=dict(size=12, color=cfg.COLOR_TEXT))
            _tfig.update_yaxes(gridcolor="#222",
                               tickfont=dict(size=12, color=cfg.COLOR_TEXT))
            _tfig.update_layout(
                title=dict(text="每日主线候选强度（30日涨幅>50% 且 成交额>30亿 的票数）",
                           font=dict(size=15, color=cfg.COLOR_TEXT)),
                height=300, paper_bgcolor=cfg.COLOR_BG, plot_bgcolor="#141414",
                font=dict(color=cfg.COLOR_TEXT, size=13, family=cfg.FONT_FAMILY),
                margin=dict(l=45, r=10, t=42, b=55))
            st.plotly_chart(_tfig, use_container_width=True, theme=None,
                            config={"displaylogo": False})
            with st.expander("📋 每日强板块明细"):
                sortable_table(_dd, stock_cols=(), pct_cols=())

    with st.expander("📖 主线筛选规范（theme_spec.md）"):
        _tspec = cfg.DOCS_DIR / "theme_spec.md"
        if _tspec.exists():
            st.markdown(_tspec.read_text(encoding="utf-8"))


elif _feature == "短线模式":
    from datetime import date as _st_date_cls

    import plotly.graph_objects as go

    from src import short_term as stm

    st.markdown("### ⚡ 短线模式（连板梯队 · 起变信号 · 情绪博弈）")

    with st.form("st_form", border=False):
        _sc1, _sc2 = st.columns([1.4, 4])
        with _sc1:
            _st_date = st.date_input("分析日期", value=_st_date_cls.today(),
                                     max_value=_st_date_cls.today(), key="st_date_input")
        with _sc2:
            st.markdown('<div style="height:28px;"></div>', unsafe_allow_html=True)
            _st_go = st.form_submit_button("🔍 分析起变信号", use_container_width=True,
                                           type="primary")

    if _st_go or "st_res" not in st.session_state:
        with st.spinner("收集连板证据 → AI 判断起变信号…"):
            st.session_state["st_res"] = stm.detect(str(_st_date))
            st.session_state["st_date_key"] = str(_st_date)
    _sr = st.session_state.get("st_res", {})

    if _sr:
        _sev = _sr.get("evidence", {})
        _sai = _sr.get("ai_result", {})
        _sld = _sr.get("ladder_df")
        _smarks = _sr.get("scan_marks", [])

        _kl, _lr = st.columns([1.2, 1])
        with _kl:
            st.markdown('<span class="chip">📊 883958 连板指数 · '
                        '起变候选（橙色▲）</span>', unsafe_allow_html=True)
            _sk = data.get_ths_index_daily("883958", days=120)
            if _sk is not None and not _sk.empty:
                _sk = _sk.sort_values("日期").reset_index(drop=True)
                _sfig = go.Figure(go.Candlestick(
                    x=_sk["日期"], open=_sk["开盘"], high=_sk["最高"],
                    low=_sk["最低"], close=_sk["收盘"],
                    increasing=dict(line=dict(color=cfg.COLOR_UP), fillcolor=cfg.COLOR_UP),
                    decreasing=dict(line=dict(color=cfg.COLOR_DOWN), fillcolor=cfg.COLOR_DOWN),
                    name="883958"))
                _cand_dates = {m["date"] for m in _smarks}
                if _cand_dates:
                    _cand_rows = _sk[_sk["日期"].isin(_cand_dates)]
                    if not _cand_rows.empty:
                        _sfig.add_trace(go.Scatter(
                            x=_cand_rows["日期"], y=_cand_rows["最低"] * 0.995,
                            mode="markers", name="起变候选",
                            marker=dict(symbol="triangle-up", size=10, color="orange"),
                            hovertext=[f"起变候选 {d}" for d in _cand_rows["日期"]],
                            hoverinfo="text"))
                _st_ds = str(_st_date)
                if _st_ds in _sk["日期"].values:
                    _tr = _sk[_sk["日期"] == _st_ds].iloc[0]
                    _sfig.add_trace(go.Scatter(
                        x=[_st_ds], y=[float(_tr["最高"]) * 1.01],
                        mode="markers", name="当日",
                        marker=dict(symbol="arrow-down", size=14, color="red"),
                        hovertext=f"当日 {_st_ds}", hoverinfo="text"))
                _sfig.update_xaxes(
                    type="date", nticks=8, tickangle=-45, showgrid=False,
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
                _sfig.update_yaxes(gridcolor="#222", tickfont=dict(size=9, color=cfg.COLOR_TEXT))
                _sfig.update_layout(
                    height=320, paper_bgcolor=cfg.COLOR_BG, plot_bgcolor="#141414",
                    font=dict(color=cfg.COLOR_TEXT, size=10, family=cfg.FONT_FAMILY),
                    margin=dict(l=36, r=8, t=6, b=8), showlegend=False,
                    dragmode="zoom")
                st.plotly_chart(_sfig, use_container_width=True, theme=None,
                                config={"displaylogo": False, "scrollZoom": True})
            # ── AI 起变信号判断（紧贴K线下方）──────────────────────
            _is_sig = _sai.get("is_signal", False)
            _sig_color = cfg.COLOR_UP if _is_sig else cfg.COLOR_MUTED
            _sig_icon = "✅" if _is_sig else "❌"
            st.markdown(
                f'<div class="stat-box"><div class="label">AI 起变信号</div>'
                f'<div class="val" style="color:{_sig_color};">{_sig_icon} '
                f'{"起变信号出现" if _is_sig else "暂无起变信号"} · '
                f'{_sai.get("cycle_type", "未知")}</div></div>',
                unsafe_allow_html=True)
            if _sai.get("signal_reason"):
                st.markdown(f'<span class="chip">📝 {_sai["signal_reason"]}</span>',
                            unsafe_allow_html=True)
        with _lr:
            st.markdown(
                f'<span class="chip">🪜 连板天梯 · {_sev.get("ladder_count", 0)}只 · '
                f'空间{_sev.get("space", 0)}板</span>', unsafe_allow_html=True)
            if _sld is not None and not _sld.empty:
                sortable_table(_sld, stock_cols=("名称",), pct_cols=("涨跌幅",))
            else:
                st.markdown(
                    '<div class="muted">⚠️ 无涨停池数据。东方财富涨停池接口仅保留近20个交易日数据，'
                    '更早的日期无法获取。883958/883418指数K线仍有历史数据可分析。</div>',
                    unsafe_allow_html=True)

        # ── 触发模式卡片 ─────────────────────────────────────────
        for _sm in _sai.get("modes", []):
            if not _sm.get("triggered"):
                continue
            st.markdown(
                f'<span class="chip">🔥 <span class="k">{_sm.get("mode", "")}</span> · '
                f'买点: {_sm.get("buy_point", "-")} · '
                f'卖点: {_sm.get("sell_point", "-")} · '
                f'仓位: {_sm.get("position", "-")}</span>',
                unsafe_allow_html=True)
            _cands = _sm.get("candidates", [])
            if _cands:
                _cdf = pd.DataFrame(_cands)
                _show = [c for c in ("code", "name", "reason") if c in _cdf.columns]
                if _show:
                    st.dataframe(_cdf[_show], use_container_width=True, hide_index=True,
                                 height=min(38 * (len(_cdf) + 1) + 4, 200))

        if _sai.get("summary"):
            st.markdown(f'<span class="chip">📋 总结: '
                        f'<span class="k">{_sai["summary"]}</span></span>',
                        unsafe_allow_html=True)

        # ── 证据明细 ──────────────────────────────────────────────
        with st.expander("📋 当日证据明细"):
            _hb = _sev.get("high_board")
            _lb = _sev.get("lianban_883958", {})
            _wp = _sev.get("weipan_883418", {})
            st.markdown(f"**日期**: {_sev.get('date', '-')}")
            st.markdown(f"**连板空间**: {_sev.get('space', 0)}板")
            if _hb:
                st.markdown(
                    f"**高度板**: {_hb.get('名称', '-')}({_hb.get('代码', '-')}) "
                    f"{_hb.get('连板数', 0)}板 · 行业={_hb.get('所属行业', '-')} · "
                    f"首封={_hb.get('首次封板时间', '-')} · "
                    f"炸板={_hb.get('炸板次数', '-')} · 换手={_hb.get('换手率', '-')}")
                st.markdown(
                    f"**高度板是否一字板**: "
                    f"{'是' if _sev.get('high_board_is_one_line') else '否'}")
            st.markdown(
                f"**883958连板指数**: 连阴{_lb.get('prev_green_red_days', 0)}天 · "
                f"跳空高开={'是' if _lb.get('gap_up') else '否'} · "
                f"拉红={'是' if _lb.get('turn_red') else '否'} · "
                f"信号触发={'是' if _lb.get('triggered') else '否'}")
            st.markdown(
                f"**883418微盘股**: 涨跌幅={_wp.get('change_pct', 0)}% · "
                f"安全={'是' if _wp.get('safe') else '否'}")
            st.markdown(f"**涨停池总数**: {_sev.get('ladder_count', 0)}只")
            _klines = _lb.get("klines", [])
            if _klines:
                st.markdown("**883958 近期K线:**")
                st.dataframe(pd.DataFrame(_klines), use_container_width=True,
                             hide_index=True)

    with st.expander("📖 短线模式规范（Short-term.md）"):
        _sspec = cfg.DOCS_DIR / "Short-term.md"
        if _sspec.exists():
            st.markdown(_sspec.read_text(encoding="utf-8"))

elif _feature == "个股模式":
    placeholder("个股模式", "theme_stock_spec.md",
                "个股买卖点与持仓管理（后续补充K线与个股详情）。")

elif _feature == "明日推演":
    placeholder("明日推演", "deduction_spec.md",
                "汇总指数择时、情绪节点、主线模式与市场统计，AI 推演次日多空、情绪、主线与操作计划。")


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
        st.caption("示例：今日成交额大于30亿 · 换手率大于5% · 流通市值30到200亿 · 总市值大于100亿 · "
                   "股价5到50元 · 收盘价大于5日均线 · 跌破20日线 · 30天涨幅大于40% · 5日跌幅大于5% · "
                   "百日新高 · 60日新低 · 连涨3天 · 均线多头排列 · 量比大于2 · 距高点回撤10%以内 · "
                   "属于半导体板块 · 剔除北交所")

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
