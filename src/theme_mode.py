"""主线模式：基于 theme_spec.md 的主线识别（适用于趋势A周期/C周期）。

- 逐日筛选：近30日涨幅>50% 且 当日成交额>30亿 的候选票
- 候选票找共同板块（东财概念，带本地缓存）→ 板块连续强≥5天 判定为主线
- 主线唯一：最强板块为唯一主线，成员重叠板块为关联板块；B/D周期日不计入连续强度
- 趋势核心：主线候选中成交额≥100亿（每波1-3个）；其余为趋势补涨
- detect(start, end)：支持任意日期区间回测；ai_analyze()：大模型学习规范后给出最终主线判断
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

from config import settings as cfg
from src import data

logger = logging.getLogger("theme_mode")

THEME_SPEC = cfg.DOCS_DIR / "theme_spec.md"

MIN_RET30_PCT = 50.0     # 近30日涨幅阈值
MIN_AMOUNT_YI = 30.0     # 当日成交额阈值（亿）
CORE_AMOUNT_YI = 100.0   # 趋势核心成交额阈值（亿）
MIN_BOARD_STOCKS = 3     # 单日同板块候选票数 ≥3 视为板块强
MIN_STREAK = 5           # 板块连续强天数 ≥5 判定主线

# 过于宽泛、无题材意义的板块不参与主线统计（指数成分/风格/市值/股东类）
_GENERIC_BOARDS = {"融资融券", "机构重仓", "深股通", "沪股通", "标准普尔", "富时罗素",
                   "MSCI中国", "证金持股", "百元股", "创业板综", "转债标的",
                   "同花顺漂亮100", "昨日涨停", "昨日连板", "昨日触板", "次新股",
                   "ST板块", "国企改革", "央企改革", "央国企改革", "专精特新",
                   "预盈预增", "预亏预减", "股权激励", "基金重仓", "社保重仓",
                   "QFII重仓", "增持回购", "破净股", "中盘股", "大盘股", "小盘股",
                   "微盘股", "中字头", "AH股", "AB股", "GDR", "低价股", "高价股",
                   "破发股", "举牌", "股票回购", "茅指数", "宁组合", "养老金",
                   "周期股", "股权集中", "西部大开发", "东数西算", "独角兽",
                   "创业成份", "科创板做市股", "注册制次新股", "再融资", "壳资源",
                   "重组概念",
                   "最近多板", "东方财富热股", "趋势股", "行业龙头", "高成长股",
                   "股权分散", "券商金股", "深圳特区", "自贸区",
                   "参股银行", "参股保险", "参股券商", "参股期货", "参股新三板",
                   "长江三角", "珠三角", "成渝特区", "滨海新区", "京津冀", "雄安新区"}
_INDEX_BOARD_RE = re.compile(r"\d{3}")   # 中证500/深成500/上证380/HS300_ 等指数类
_REGION_BOARD_RE = re.compile(r".板块$")  # 福建板块/上海板块 等地域类
_NOISE_RE = re.compile(r"昨日|振幅|市净率|市盈率|风格|成长$|价值$|股息|重仓|持股|回购")

# 泛泛的行业大类不能当选唯一主线名（主线应是具体题材，如半导体/机器人/光通信）
_BROAD_BOARDS = {"电子", "计算机", "通信", "传媒", "医药", "医药商业", "军工", "国防军工",
                 "机械设备", "通用设备", "专用设备", "电力设备", "电气设备", "基础化工",
                 "化工", "有色金属", "钢铁", "煤炭", "汽车", "汽车零部件", "食品饮料",
                 "建筑材料", "建筑装饰", "轻工制造", "纺织服装", "商贸零售", "交通运输",
                 "公用事业", "环保", "银行", "证券", "保险", "房地产", "农林牧渔",
                 "人工智能", "新能源", "新材料", "科技", "大科技", "互联网"}


def _is_theme_board(name: str) -> bool:
    return bool(name) and name not in _GENERIC_BOARDS \
        and not _INDEX_BOARD_RE.search(name) and not _REGION_BOARD_RE.search(name) \
        and not _NOISE_RE.search(name)


# ── 全市场矩阵（收盘/成交额，右对齐 + 交易日轴）─────────────────────────
_MAT_MEMO: dict = {"key": None, "val": None}


def _matrices() -> dict | None:
    try:
        key = data.METRICS_CACHE.stat().st_mtime
    except Exception:
        key = None
    if key is not None and _MAT_MEMO["key"] == key:
        return _MAT_MEMO["val"]
    cache = data.load_metrics_cache()
    if cache is None:
        cache = data.load_metrics_cache(allow_stale=True)
    if cache is None or not {"closes", "volumes"}.issubset(cache.columns):
        return None

    def mat(col: str) -> np.ndarray:
        arrs = [a if a is not None and len(a) else [] for a in cache[col].tolist()]
        L = max((len(a) for a in arrs), default=0)
        M = np.full((len(arrs), L), np.nan)
        for i, a in enumerate(arrs):
            M[i, L - len(a):] = a
        return M

    C, V = mat("closes"), mat("volumes")
    if C.size == 0 or C.shape[1] < 40:
        return None
    Op, Hi, Lo = mat("opens"), mat("highs"), mat("lows")
    A = C * V  # 成交额估算：收盘价×成交量(股)

    # 日期轴：与上证交易日对齐，末列 = 缓存 last_date
    L = C.shape[1]
    idx = data.get_index_daily("sh000001", days=L + 30)
    all_dates = idx["日期"].tolist() if not idx.empty else []
    last_date = None
    if "last_date" in cache.columns:
        try:
            last_date = str(cache["last_date"].dropna().mode().iloc[0])[:10]
        except Exception:
            pass
    if all_dates and last_date in all_dates:
        end = all_dates.index(last_date) + 1
        seg = all_dates[max(0, end - L):end]
    else:
        seg = all_dates[-L:]
    if len(seg) < L:  # 交易日不足则截断矩阵左侧
        sl = slice(-len(seg), None)
        C, A = C[:, sl], A[:, sl]
        if Op.size:
            Op = Op[:, sl]
        if Hi.size:
            Hi = Hi[:, sl]
        if Lo.size:
            Lo = Lo[:, sl]
    out = {"codes": cache["代码"].astype(str).tolist(),
           "names": cache["名称"].astype(str).tolist(),
           "dates": seg, "C": C, "A": A, "O": Op, "H": Hi, "L": Lo}
    _MAT_MEMO["key"], _MAT_MEMO["val"] = key, out
    return out


# ── 概念板块（东财全量拉取 → 题材过滤，专用缓存）─────────────────────────
THEME_CONCEPT_CACHE = cfg.DATA_DIR / "theme_concepts.json"


def _fetch_boards(session, code: str) -> list[str] | None:
    """东财：个股全部所属板块 → 过滤后保留前15个题材板块。"""
    try:
        r = session.get("https://push2.eastmoney.com/api/qt/slist/get",
                        params={"spt": "3", "pi": "0", "pz": "50", "po": "1", "fid": "f3",
                                "fltt": "2", "invt": "2", "np": "1",
                                "secid": data._em_secid(code), "fields": "f12,f14"},
                        headers=data._EM_HEADERS, timeout=6)
        diff = (r.json().get("data") or {}).get("diff") or []
        names = [d["f14"] for d in diff
                 if str(d.get("f12", "")).startswith("BK") and _is_theme_board(d.get("f14"))]
        return names[:15]
    except Exception as e:
        logger.debug("板块 %s 失败：%s", code, e)
        return None


def _concepts_for(codes: list[str], max_workers: int = 16) -> dict[str, list[str]]:
    try:
        cached = json.loads(THEME_CONCEPT_CACHE.read_text(encoding="utf-8"))
    except Exception:
        cached = {}
    missing = [c for c in codes if c not in cached]
    if missing:
        session = data._sina_session()
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(_fetch_boards, session, c): c for c in missing}
            for fut in as_completed(futs):
                res = fut.result()
                if res is not None:
                    cached[futs[fut]] = res
        try:
            THEME_CONCEPT_CACHE.write_text(json.dumps(cached, ensure_ascii=False),
                                           encoding="utf-8")
        except Exception as e:
            logger.warning("写题材板块缓存失败：%s", e)
    return {c: [b for b in cached.get(c, []) if _is_theme_board(b)] for c in codes}


# ── 主线识别 ──────────────────────────────────────────────────────────────
def detect(start: str, end: str, min_ret30: float = MIN_RET30_PCT,
           min_amount_yi: float = MIN_AMOUNT_YI, min_board_stocks: int = MIN_BOARD_STOCKS,
           min_streak: int = MIN_STREAK) -> dict:
    """识别 [start, end] 区间内的主线板块与核心/补涨个股。日期格式 YYYY-MM-DD。"""
    m = _matrices()
    if m is None:
        return {"error": "need_cache"}
    dates, C, A = m["dates"], m["C"], m["A"]
    day_idx = [i for i, d in enumerate(dates) if start <= d <= end and i >= 30]
    if not day_idx:
        return {"error": "no_days"}

    # 逐日中级周期（复盘表）：B/D 周期不可能有主线，当日不计入板块连续强天数
    mid_cycle: dict[str, str] = {}
    try:
        from src import index_timing
        sig = index_timing.load_history_signals()
        if sig is not None and not sig.empty:
            mid_cycle = dict(zip(sig["日期"].astype(str), sig["中级周期"].astype(str)))
    except Exception as e:
        logger.warning("读取复盘表中级周期失败：%s", e)

    # 成交前10指数（同花顺 883902 真实指数，K线 + 趋势参考，不作硬性切断）
    ths_idx = data.get_ths_index_daily("883902", days=220)
    ths_map: dict[str, int] = {}
    ths_close = np.array([])
    ths_ma5 = np.array([])
    ths_up = np.array([], dtype=bool)
    if ths_idx is not None and not ths_idx.empty:
        ths_idx = ths_idx.sort_values("日期").reset_index(drop=True)
        ths_close = ths_idx["收盘"].to_numpy(dtype=float)
        _n_t = len(ths_close)
        ths_ma5 = np.full(_n_t, np.nan)
        for _i in range(4, _n_t):
            ths_ma5[_i] = float(np.nanmean(ths_close[_i - 4:_i + 1]))
        ths_up = ~(np.isnan(ths_close) | np.isnan(ths_ma5)) & (ths_close > ths_ma5)
        ths_map = {str(ths_idx.at[i, "日期"]): i for i in range(_n_t)}

    # 逐日候选：30日涨幅 & 成交额
    daily: list[dict] = []
    cand_days: dict[int, set[int]] = {}  # 股票行号 → 入选日集合
    for i in day_idx:
        with np.errstate(invalid="ignore"):
            ret30 = (C[:, i] / C[:, i - 30] - 1) * 100
            mask = (ret30 >= min_ret30) & (A[:, i] >= min_amount_yi * 1e8)
        rows = np.flatnonzero(mask)
        for r in rows:
            cand_days.setdefault(int(r), set()).add(i)
        cyc = mid_cycle.get(dates[i], "").strip()
        _ti = ths_map.get(dates[i])
        daily.append({"i": i, "日期": dates[i], "rows": rows.tolist(),
                      "周期": cyc, "ok_cycle": cyc not in ("B", "D"), "ti": _ti,
                      "ok_uptrend": bool(ths_up[_ti]) if _ti is not None and _ti < len(ths_up) else False,
                      "tidx": (float(ths_close[_ti]) if _ti is not None and _ti < len(ths_close)
                               and not np.isnan(ths_close[_ti]) else None)})

    all_rows = sorted(cand_days)
    codes = [m["codes"][r] for r in all_rows]
    concepts = _concepts_for(codes) if codes else {}
    row_boards = {r: concepts.get(m["codes"][r], []) for r in all_rows}

    # 逐日板块计数 → 强板块序列
    for d in daily:
        cnt: dict[str, int] = {}
        for r in d["rows"]:
            for b in row_boards.get(r, []):
                cnt[b] = cnt.get(b, 0) + 1
        d["boards"] = cnt
        d["strong"] = {b for b, n in cnt.items() if n >= min_board_stocks}

    # 板块连续强 ≥ min_streak 天 → 主线（取最长连续段；B/D 周期日切断连续）
    mainlines = []
    all_boards = {b for d in daily for b in d["strong"]}
    for b in all_boards:
        flags = [b in d["strong"] and d["ok_cycle"] for d in daily]
        best, cur, cur_s, best_rng = 0, 0, 0, (0, 0)
        for k, f in enumerate(flags):
            if f:
                cur = cur + 1 if cur else 1
                if cur == 1:
                    cur_s = k
                if cur > best:
                    best, best_rng = cur, (cur_s, k)
            else:
                cur = 0
        if best < min_streak:
            continue
        s_k, e_k = best_rng
        seg = daily[s_k:e_k + 1]
        seg_rows = sorted({r for d in seg for r in d["rows"] if b in row_boards.get(r, [])})
        i0, i1 = seg[0]["i"], seg[-1]["i"]
        stocks = []
        for r in seg_rows:
            amt_max = float(np.nanmax(A[r, i0:i1 + 1])) / 1e8
            base = C[r, max(0, i0 - 1)]
            rng_pct = (C[r, i1] / base - 1) * 100 if base and not np.isnan(base) else np.nan
            first = next(d["日期"] for d in seg if r in d["rows"]
                         and b in row_boards.get(r, []))
            stocks.append({"代码": m["codes"][r], "名称": m["names"][r],
                           "最大成交额_亿": round(amt_max, 1),
                           "区间涨幅": round(float(rng_pct), 2) if pd.notna(rng_pct) else None,
                           "首次入选": first,
                           "概念": "、".join(row_boards.get(r, [])[:3])})
        stocks.sort(key=lambda s: -s["最大成交额_亿"])
        core = [s for s in stocks if s["最大成交额_亿"] >= CORE_AMOUNT_YI][:3]
        core_codes = {s["代码"] for s in core}
        follow = [s for s in stocks if s["代码"] not in core_codes]
        _seg_up = [d["ok_uptrend"] for d in seg]
        _gate_open = bool(_seg_up and sum(_seg_up) >= len(_seg_up) / 2)
        mainlines.append({
            "board": b, "start": seg[0]["日期"], "end": seg[-1]["日期"], "days": best,
            "max_count": max(d["boards"].get(b, 0) for d in seg),
            "ongoing": seg[-1]["日期"] == daily[-1]["日期"],
            "gate_open": _gate_open,
            "core": core, "follow": follow,
        })
    mainlines.sort(key=lambda x: (-x["days"], -x["max_count"]))

    # 主线唯一：最强的"具体板块"为唯一主线（泛泛大类如电子/人工智能不当选）；
    # 成员重叠的板块记为"关联板块"，其余为"次强板块"
    if mainlines:
        specific = [ml for ml in mainlines if ml["board"] not in _BROAD_BOARDS]
        primary = specific[0] if specific else mainlines[0]
        p_codes = {s["代码"] for s in primary["core"] + primary["follow"]}
        related, secondary = [], []
        for ml in mainlines:
            if ml is primary:
                continue
            codes_set = {s["代码"] for s in ml["core"] + ml["follow"]}
            inter = len(codes_set & p_codes)
            if inter and inter / min(len(codes_set), len(p_codes)) >= 0.5:
                related.append(ml["board"])
            else:
                secondary.append(ml["board"])
        primary["related"] = related
        primary["secondary"] = secondary
        mainlines = [primary]

    daily_df = pd.DataFrame([{
        "日期": d["日期"], "周期": d["周期"] or "-", "候选数": len(d["rows"]),
        "强板块": "、".join(f"{b}({n})" for b, n in
                          sorted(d["boards"].items(), key=lambda kv: -kv[1])[:5]
                          if n >= min_board_stocks),
    } for d in daily])
    gate_open = sum(1 for d in daily if d["ok_uptrend"])

    def _tcol(col, ti):
        if ti is None or ths_idx is None or ti >= len(ths_idx):
            return None
        v = ths_idx.at[ti, col]
        return float(v) if pd.notna(v) else None

    def _tma5(ti):
        return (float(ths_ma5[ti]) if ti is not None and ti < len(ths_ma5)
                and not np.isnan(ths_ma5[ti]) else None)

    idx_df = pd.DataFrame({
        "日期": [d["日期"] for d in daily],
        "开盘": [_tcol("开盘", d["ti"]) for d in daily],
        "最高": [_tcol("最高", d["ti"]) for d in daily],
        "最低": [_tcol("最低", d["ti"]) for d in daily],
        "收盘": [d["tidx"] for d in daily],
        "MA5": [_tma5(d["ti"]) for d in daily],
        "上升趋势": [d["ok_uptrend"] for d in daily],
    })
    return {"has_mainline": bool(mainlines), "mainlines": mainlines,
            "daily": daily_df, "start": daily[0]["日期"], "end": daily[-1]["日期"],
            "gate_open_days": gate_open, "gate_total_days": len(daily),
            "turnover_idx": idx_df}


# ── AI 主线判断（大模型为最终判断者）──────────────────────────────────────
AI_PROMPT = """你是A股主线题材研究员。请先学习【主线筛选规范】（theme_spec.md），
再结合【规则初筛证据】，对 {start} ~ {end} 的主线情况给出最终判断，250字内，中文口语化。

硬性规则：
- 主线是唯一的：最多只能认定一个主线板块（可附带关联板块）。
- 主线必须是某一个具体板块（如半导体、机器人、光通信），不能是泛泛的大类
  （如电子、人工智能、新能源、科技这类宽泛名称）。
- B周期、D周期不可能有主线；初筛证据中标注了每日中级周期，B/D日的板块强度不能作为主线依据。

输出必须包含：
1) 有无主线及唯一主线板块名（含关联板块说明）；2) 趋势核心（阵眼）点评；
3) 趋势补涨机会点评；4) 按规范给出当前操作建议（含卖点提示）。
若判断无主线，说明原因并给出观察建议。

【主线筛选规范】
{spec}

【规则初筛证据】
{result}
"""


def ai_analyze(result: dict) -> str:
    if not cfg.QWEN_API_KEY:
        return "未配置 QWEN_API_KEY"
    spec = THEME_SPEC.read_text(encoding="utf-8") if THEME_SPEC.exists() else ""
    lines = []
    if result.get("has_mainline"):
        for ml in result["mainlines"][:5]:
            core = "、".join(f"{s['名称']}({s['最大成交额_亿']}亿)" for s in ml["core"]) or "无"
            follow = "、".join(s["名称"] for s in ml["follow"][:10]) or "无"
            lines.append(f"唯一主线[{ml['board']}] {ml['start']}~{ml['end']} 连续{ml['days']}天"
                         f"{'（进行中）' if ml['ongoing'] else '（已结束）'}，"
                         f"峰值{ml['max_count']}只；趋势核心：{core}；趋势补涨：{follow}")
            if ml.get("related"):
                _rel = ml["related"]
                lines.append(f"关联板块（与主线成员重叠，共{len(_rel)}个）："
                             f"{'、'.join(_rel[:8])}{'等' if len(_rel) > 8 else ''}")
            if ml.get("secondary"):
                lines.append(f"次强板块（非主线）：{'、'.join(ml['secondary'])}")
    else:
        lines.append("区间内未识别出满足条件的主线板块（注意：B/D周期不可能有主线）。")
    lines.append(f"成交前10指数(同花顺883902)上升趋势：{result.get('gate_open_days', 0)}/"
                 f"{result.get('gate_total_days', 0)} 日上升。该指数为开启参考："
                 "上升趋势更支持主线有效，下行时主线质量打折，但不直接否决主线。")
    for ml in result.get("mainlines", [])[:5]:
        lines.append(f"主线[{ml['board']}] 区间成交前10指数门槛："
                     f"{'开启(上升趋势)' if ml.get('gate_open') else '未开启(下行)'}")
    tail = result.get("daily")
    if tail is not None and not tail.empty:
        lines.append("最近10日强板块（含中级周期，B/D日不能作为主线依据）：")
        for _, r in tail.tail(10).iterrows():
            lines.append(f"{r['日期']} 周期[{r.get('周期', '-')}] "
                         f"候选{r['候选数']}只 {r['强板块'] or '-'}")
    try:
        from openai import OpenAI
        client = OpenAI(api_key=cfg.QWEN_API_KEY, base_url=cfg.QWEN_BASE_URL)
        resp = client.chat.completions.create(
            model=cfg.QWEN_CHAT_MODEL,
            messages=[{"role": "user", "content": AI_PROMPT.format(
                start=result.get("start", ""), end=result.get("end", ""),
                spec=spec, result="\n".join(lines))}],
            temperature=0.2)
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.error("AI 主线判读失败：%s", e)
        return f"AI 判读失败：{e}"
