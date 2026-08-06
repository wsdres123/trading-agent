"""前端实时行情组件（痛点3：WS 增量更新）。

在 Streamlit 页面嵌入一段自持的 HTML/JS：浏览器直连 FastAPI /ws/market，
收到推送后只更新"实时平均股价 + 指数"这一条 bar 的 DOM，
K 线大图仍按原有节奏（定时/手动）刷新——即"数字跳动不整页闪"。

Streamlit 1.30 无官方 JS↔Python 回传通道，本组件纯前端渲染，不回传 Python。
"""
from __future__ import annotations

from config import settings as cfg

_MARKET_BAR_JS = """
<div class="jc-bar">
  <div class="jc-item">
    <span class="jc-label">实时平均股价</span>
    <span class="jc-val" id="jc_avg">--</span>
    <span class="jc-sub" id="jc_cnt"></span>
  </div>
  <div class="jc-idx" id="jc_idx"></div>
  <span class="jc-ts" id="jc_ts"></span>
</div>
<style>
.jc-bar {{ display:flex; align-items:center; gap:14px; flex-wrap:wrap;
  background:#161616; border:1px solid #2a2a2a; border-radius:8px;
  padding:6px 14px; margin:2px 0 6px; }}
.jc-bar .jc-label {{ color:{muted}; font-size:13px; }}
.jc-bar .jc-val {{ color:{stock}; font-size:22px; font-weight:700; margin-left:6px;
  transition:color .2s; }}
.jc-bar .jc-sub {{ color:{muted}; font-size:12px; margin-left:4px; }}
.jc-bar .jc-idx {{ display:flex; gap:8px; flex-wrap:wrap; }}
.jc-bar .jc-chip {{ background:#1f1f1f; border:1px solid #333; border-radius:10px;
  padding:1px 9px; font-size:14px; color:{text}; white-space:nowrap; }}
.jc-bar .jc-up {{ color:{up}; }} .jc-bar .jc-down {{ color:{down}; }}
.jc-bar .jc-ts {{ color:{muted}; font-size:12px; margin-left:auto; }}
</style>
<script>
(function() {{
  var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
  var url = proto + location.hostname + ':{port}/ws/market';
  var backoff = 1000;
  function cls(v) {{ return v > 0 ? 'jc-up' : (v < 0 ? 'jc-down' : ''); }}
  function esc(s) {{ return String(s==null?'':s).replace(/[&<>"']/g, function(c) {{
    return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]; }}); }}
  function connect() {{
    var ws;
    try {{ ws = new WebSocket(url); }} catch (e) {{ return; }}
    ws.onmessage = function(ev) {{
      backoff = 1000;
      var d; try {{ d = JSON.parse(ev.data); }} catch (e) {{ return; }}
      if (d.type !== 'market') return;
      var avg = document.getElementById('jc_avg');
      if (avg && d.avg_price != null) {{
        var prev = parseFloat(avg.dataset.v || '0');
        avg.textContent = Number(d.avg_price).toFixed(3);
        avg.dataset.v = d.avg_price;
        // 数字变化瞬间变色：涨红跌绿，直观感受推送
        if (prev && d.avg_price > prev) avg.style.color = '{up}';
        else if (prev && d.avg_price < prev) avg.style.color = '{down}';
      }}
      var cnt = document.getElementById('jc_cnt');
      if (cnt && d.stock_count) cnt.textContent = d.stock_count + '只';
      var idx = document.getElementById('jc_idx');
      if (idx && d.indices && d.indices.length) {{
        idx.innerHTML = d.indices.map(function(r) {{
          var v = r['涨跌幅'];
          var name = esc(String(r['名称'] || r['代码'] || '').replace(/指数$/, ''));
          var p = r['最新价'] != null ? Number(r['最新价']).toFixed(2) : '--';
          var s = v != null ? (v > 0 ? '+' : '') + Number(v).toFixed(2) + '%' : '';
          return '<span class="jc-chip">' + name + ' <b class="' + cls(v) + '">' + p +
                 '</b> <b class="' + cls(v) + '">' + s + '</b></span>';
        }}).join('');
      }}
      var ts = document.getElementById('jc_ts');
      if (ts && d.timestamp) ts.textContent = d.timestamp + ' 推送';
    }};
    ws.onclose = function() {{
      setTimeout(connect, Math.min(backoff = backoff * 2, 15000));
    }};
    ws.onerror = function() {{ try {{ ws.close(); }} catch (e) {{}} }};
  }}
  connect();
}})();
</script>
"""


def market_bar_html() -> str:
    """返回实时行情 bar 的 HTML（供 st.markdown(unsafe_allow_html=True)）。"""
    return _MARKET_BAR_JS.format(
        port=cfg.FASTAPI_PORT,
        muted=cfg.COLOR_MUTED, stock=cfg.COLOR_STOCK, text=cfg.COLOR_TEXT,
        up=cfg.COLOR_UP, down=cfg.COLOR_DOWN,
    )
