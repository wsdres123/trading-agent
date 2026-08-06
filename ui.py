"""劫财AI交易 — UI 入口（登录 + 导航，页面实现见 pages/）。

启动：bash run.sh  （自动设置 LD_LIBRARY_PATH 后 streamlit run ui.py）
七大功能（顶部 tab）：指数择时 / 主线模式 / 短线模式 / 个股模式 / 明日推演 / AI助手 / 评测报告
配色：黑底 / 个股黄字 / 其他白字 / 涨红跌绿 / 字体偏大。
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))

from config import settings as cfg
from src import data, knowledge
from pages import shared, timing, theme, short_term, single_stock, ai_assistant, eval_report
from pages.shared import e as _e

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


# ── 登录（未登录时拦截整个页面）──────────────────────────────────────────
# Streamlit 1.30 无 cookie API，会话 token 走 URL query param（?token=xxx），
# 服务端校验 Redis 会话；token 为 32 字节随机串，仅限 Tailscale 内网使用。
from src import auth as _auth

_token = st.query_params.get("token")
_current_user = _auth.get_session_user(_token)

if _current_user is None:
    _col_l, _col_m, _col_r = st.columns([1, 2, 1])
    with _col_m:
        st.markdown(
            '<div style="text-align:center; margin-top:80px;">'
            f'<span style="font-size:40px; font-weight:800; color:{cfg.COLOR_STOCK};">📊 劫财AI交易</span>'
            '</div>', unsafe_allow_html=True)
        with st.form("login_form", clear_on_submit=False):
            _lu = st.text_input("用户名", placeholder="用户名")
            _lp = st.text_input("密码", type="password", placeholder="密码")
            _submit = st.form_submit_button("登 录", use_container_width=True)
        if _submit:
            if _lu.strip() and _lp:
                _user = _auth.verify(_lu, _lp)
                if _user:
                    st.query_params["token"] = _auth.create_session(_user)
                    st.rerun()
                else:
                    st.error("用户名或密码错误（连续失败 5 次将锁定 15 分钟）")
            else:
                st.warning("请输入用户名和密码")
        if _auth.user_count() == 0:
            st.info("首次使用：请先创建管理员账号")
            with st.form("bootstrap_form"):
                _bu = st.text_input("管理员用户名", value="admin")
                _bp = st.text_input("设置密码（至少6位）", type="password")
                _bp2 = st.text_input("再输入一次", type="password")
                if st.form_submit_button("创建管理员并登录", use_container_width=True):
                    if _bp != _bp2:
                        st.error("两次密码不一致")
                    else:
                        try:
                            _auth.register_user(_bu.strip(), _bp)
                            st.query_params["token"] = _auth.create_session(_bu.strip())
                            st.rerun()
                        except ValueError as _ve:
                            st.error(str(_ve))
    st.stop()  # ← 未登录到此为止，后面的功能全部不渲染

# 已登录：会话滑动续期
_auth.touch_session(_token)
if st.session_state.pop("_jc_logout", False):
    _auth.delete_session(_token)
    st.query_params.clear()
    st.rerun()


# ── 顶部标题 + 状态（单行紧凑，滚动置顶）──────────────────────────────────
_health = data.health()
_kstatus = knowledge.status()
_status_chips = []
_status_chips.append(f'<span class="chip">数据源(akshare): <span class="k">{"在线" if _health["akshare"] else "离线"}</span></span>')
_status_chips.append(f'<span class="chip">千问API: <span class="k">{"已配置" if _health["qwen_key"] else "未配置"}</span></span>')
_status_chips.append(f'<span class="chip">同花顺API: <span class="k">{"在线" if _health.get("ths_api") else "离线"}</span></span>')
_status_chips.append(f'<span class="chip">知识库: <span class="k">{_kstatus["files"]} 文件</span></span>')
_status_chips.append(f'<span class="chip">用户: <span class="k">{_e(_current_user)}</span></span>')
_status_chips.append(f'<span class="chip">交易日: <span class="k">{"是" if data.is_trading_day() else "否"}</span></span>')
_sh = _health.get("source_health", {})
if _sh:
    _src_parts = []
    for _src in ("tencent", "fuyao", "akshare"):
        _info = _sh.get(_src, {})
        _ok = _info.get("available", True)
        _src_parts.append(f'{_src}:{"✓" if _ok else "✗(熔断)"}')
    _status_chips.append(f'<span class="chip">数据源健康: <span class="k">{" ".join(_src_parts)}</span></span>')
_clock_st = _health.get("clock", {})
if _clock_st.get("samples"):
    _off = _clock_st.get("offset_sec", 0.0)
    _status_chips.append(f'<span class="chip">时钟: <span class="k">偏差{_off:+.1f}s{" ⚠" if _clock_st.get("drift_warning") else ""}</span></span>')
_tb_l, _tb_r = st.columns([9, 1])
with _tb_l:
    st.markdown(
        '<div class="title-bar"><span class="app-title">劫财AI交易</span>'
        + "".join(_status_chips) + '</div>',
        unsafe_allow_html=True,
    )
with _tb_r:
    if st.button("退出登录", key="jc_logout_btn"):
        st.session_state["_jc_logout"] = True
        st.rerun()

# ── 功能导航 ───────────────────────────────────────────────────────────────
_FEATURES = ["指数择时", "主线模式", "短线模式", "个股模式", "明日推演", "AI助手", "评测报告"]
_feature = st.radio("功能", _FEATURES, horizontal=True, label_visibility="collapsed")

if _feature == "指数择时":
    timing.render(_current_user)
elif _feature == "主线模式":
    theme.render()
elif _feature == "短线模式":
    short_term.render()
elif _feature == "个股模式":
    single_stock.render()
elif _feature == "明日推演":
    shared.placeholder("明日推演", "deduction_spec.md",
                       "汇总指数择时、情绪节点、主线模式与市场统计，AI 推演次日多空、情绪、主线与操作计划。")
elif _feature == "AI助手":
    ai_assistant.render(_current_user)
elif _feature == "评测报告":
    eval_report.render()
