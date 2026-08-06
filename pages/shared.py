"""UI 公共组件：彩色表格 / 筛选条件 chips / 占位页。

从 ui.py 拆出，供 pages/* 各页面复用。
"""
from __future__ import annotations

import html as _html

import pandas as pd
import streamlit as st

from config import settings as cfg


def e(s) -> str:
    """HTML escape helper：所有写入 unsafe_allow_html 的动态字符串先转义。"""
    return _html.escape(str(s), quote=True)


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
    return "".join(f'<span class="chip"><span class="k">●</span> {e(l)}</span>'
                   for l in cond_labels(conds))


def rng_label(name: str, lo, hi, unit: str) -> str:
    if lo is not None and hi is not None:
        return f"{name}{lo}-{hi}{unit}"
    if lo is not None:
        return f"{name}>{lo}{unit}"
    return f"{name}<{hi}{unit}"


def cond_labels(conds: list) -> list:
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
            out.append(rng_label(name, c.get("min_pct"), c.get("max_pct"), "%"))
        elif f == "new_high":
            out.append(f"{c.get('days')}日新高")
        elif f == "new_low":
            out.append(f"{c.get('days')}日新低")
        elif f == "free_float_cap":
            out.append(rng_label("流通市值", c.get("min_yi"), c.get("max_yi"), "亿"))
        elif f == "total_cap":
            out.append(rng_label("总市值", c.get("min_yi"), c.get("max_yi"), "亿"))
        elif f == "amount":
            out.append(rng_label("今日成交额", c.get("min_yi"), c.get("max_yi"), "亿"))
        elif f == "turnover_rate":
            out.append(rng_label("换手率", c.get("min_pct"), c.get("max_pct"), "%"))
        elif f == "price":
            out.append(rng_label("股价", c.get("min"), c.get("max"), "元"))
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
            out.append(rng_label(f"距{c.get('days', 100)}日高点回撤",
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


