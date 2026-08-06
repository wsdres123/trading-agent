"""情绪节点：大模型学习 emotional node.md + 屠龙表-竞价表.csv 历史节点，判断当日情绪节点。

- load_auction_table()：竞价表历史节点（日期/指数/节点/亏效/一字/断板）
- market_stats()：当日盘面统计（大面数/跌停/涨停/高度个股等，供模型判断）
- ai_judge()：结合规范+历史+当日统计输出今日情绪节点（每日缓存，初步判断版）
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

import numpy as np
import pandas as pd

from config import settings as cfg
from src import data
from src.schema_validator import load_spec_text

logger = logging.getLogger("emotion_node")

AUCTION_CSV = cfg.KNOWLEDGE_DIR / "屠龙表 - 竞价表.csv"
EMO_SPEC = cfg.DOCS_DIR / "emotional node.md"
EMO_STORE = cfg.DATA_DIR / "emotion_ai.json"
JUDGE_TIME = "14:57"

# 规范中的节点全集（模型输出约束，与竞价表CSV命名对齐）
NODE_NAMES = ["混沌", "混沌分歧",
              "主升--确认", "主升--加速", "主升--高潮", "主升--分歧",
              "主升--延续", "日内分歧转一致", "日内分歧未转一致",
              "退潮", "退潮加速", "退潮转衰竭", "退潮中继",
              "冰点", "冰点转折",
              "修复--弱", "修复--中等", "修复--强",
              "修复--加速", "修复--高潮", "修复延续",
              "加速", "加速转衰竭",
              "龙头确认", "短线情绪确认"]


def load_auction_table() -> pd.DataFrame:
    """竞价表历史：日期/指数/节点/小票亏效/大票亏效/一字/断板（仅保留有节点标注的行）。"""
    if not AUCTION_CSV.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(AUCTION_CSV, encoding="utf-8-sig")
    except Exception as e:
        logger.warning("竞价表读取失败：%s", e)
        return pd.DataFrame()
    df.columns = [str(c).replace("\n", "").strip() for c in df.columns]
    if "日期" not in df.columns:
        df = df.rename(columns={df.columns[0]: "日期"})
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce").dt.strftime("%Y-%m-%d")
    df = df.dropna(subset=["日期"])
    if "节点" in df.columns:
        df = df[df["节点"].notna() & df["节点"].astype(str).str.strip().ne("")]
    keep = [c for c in ["日期", "指数", "节点", "小票亏效", "大票亏效", "一字", "断板"]
            if c in df.columns]
    return df[keep].fillna("").reset_index(drop=True)


def _limit_pct(code: str, name: str = "") -> float:
    """涨跌停幅度：创业板/科创板20%，北交所30%，ST/SST 5%，其余主板10%。"""
    if code.startswith(("30", "68")):
        return 20.0
    if code.startswith(("8", "4", "92")):
        return 30.0
    if "ST" in str(name).upper():
        return 5.0
    return 10.0


def daban_damian_count() -> int | None:
    """打板大面数：今日曾封板（涨停）后炸板，主板非ST，当前涨幅<5% 的个股数。
    数据源：akshare stock_zt_pool_zbgc_em（东方财富炸板股池）。
    """
    try:
        import akshare as ak
        today = datetime.now().strftime("%Y%m%d")
        zb = ak.stock_zt_pool_zbgc_em(date=today)
        if zb.empty:
            return 0
        codes = zb["代码"].astype(str)
        names = zb["名称"].astype(str)
        pct = pd.to_numeric(zb["涨跌幅"], errors="coerce")
        is_main = ~codes.str.startswith(("30", "68", "8", "4", "92"))
        is_st = names.str.upper().str.contains("ST")
        return int((is_main & ~is_st & (pct < 5.0)).sum())
    except Exception:
        return None


def hot_stock_stats() -> dict:
    """同花顺热榜前10热门股表现：热门个股指数(平均涨跌幅) + 明细。"""
    out: dict = {"hot_list": []}
    try:
        hot = data.get_hot_stocks(top=10)
        if hot.empty:
            return out
        pct = pd.to_numeric(hot.get("涨跌幅"), errors="coerce")
        if pct.notna().any():
            out["热门个股指数"] = round(float(pct.mean()), 2)
            out["热门股大跌数"] = int((pct <= -7).sum())
            lim = hot["代码"].astype(str).map(_limit_pct)
            out["热门股跌停数"] = int((pct <= -(lim - 0.2)).sum())
            out["热门股涨停数"] = int((pct >= lim - 0.2).sum())
        out["hot_list"] = hot.to_dict("records")
    except Exception as e:
        logger.warning("热门股统计失败：%s", e)
    return out


def market_stats() -> dict:
    """当日盘面统计：大面数(跌>7%)、跌停/涨停数、涨>7%数、高度个股(10日涨幅>40%)。"""
    out: dict = {}
    try:
        spot = data.get_stock_spot()
        pct = pd.to_numeric(spot["涨跌幅"], errors="coerce")
        lim = spot["代码"].astype(str).map(_limit_pct)
        out["大面数"] = int((pct <= -7).sum())
        out["涨超7数"] = int((pct >= 7).sum())
        out["跌停数"] = int((pct <= -(lim - 0.2)).sum())
        out["涨停数"] = int((pct >= lim - 0.2).sum())
        # 打板大面：用东方财富炸板池（曾封板已炸）+ 主板非ST + 涨幅<5%
        try:
            import akshare as ak
            _today = datetime.now().strftime("%Y%m%d")
            _zb = ak.stock_zt_pool_zbgc_em(date=_today)
            if not _zb.empty:
                _codes = _zb["代码"].astype(str)
                _names = _zb["名称"].astype(str)
                _pct = pd.to_numeric(_zb["涨跌幅"], errors="coerce")
                _is_main = ~_codes.str.startswith(("30", "68", "8", "4", "92"))
                _is_st = _names.str.upper().str.contains("ST")
                out["打板大面数"] = int((_is_main & ~_is_st & (_pct < 5.0)).sum())
            else:
                out["打板大面数"] = 0
        except Exception:
            pass
    except Exception as e:
        logger.warning("盘面统计失败：%s", e)
    try:
        cache = data.load_metrics_cache()
        if cache is None:
            cache = data.load_metrics_cache(allow_stale=True)
        if cache is not None and "closes" in cache.columns:
            cnt = 0
            for arr in cache["closes"]:
                a = np.asarray(arr, dtype=float)
                if a.size > 10 and a[-11] and not np.isnan(a[-11]) \
                        and (a[-1] / a[-11] - 1) * 100 > 40:
                    cnt += 1
            out["高度个股数"] = cnt
    except Exception as e:
        logger.warning("高度个股统计失败：%s", e)
    try:
        idx = data.get_index_daily("sh000001", days=3)
        if len(idx) >= 2:
            out["上证涨跌幅"] = round(
                float(idx["收盘"].iloc[-1] / idx["收盘"].iloc[-2] - 1) * 100, 2)
    except Exception:
        pass
    try:
        idx_spot = data.get_index_spot()
        if not idx_spot.empty:
            sh_row = idx_spot[idx_spot["代码"].str.contains("000001", na=False)]
            if not sh_row.empty:
                amt = float(sh_row.iloc[0].get("成交额", 0) or 0)
                if amt > 0:
                    out["上证成交额(亿)"] = round(amt / 1e8, 1)
    except Exception:
        pass
    try:
        from src import index_timing as it
        rec = it.load_ai_predictions().get(datetime.now().strftime("%Y-%m-%d"), {})
        if rec.get("signal"):
            out["平均股价信号"] = rec["signal"]
        if rec.get("mid_cycle"):
            out["中级周期"] = rec["mid_cycle"]
    except Exception:
        pass
    return out


# ── 历史盘面统计（从 metrics_cache 回算）─────────────────────────────────

_hist_cache: dict | None = None


def _load_hist_arrays() -> dict | None:
    """加载 metrics_cache 并构建交易日→数组索引映射（单次加载，内存复用）。"""
    global _hist_cache
    if _hist_cache is not None:
        return _hist_cache
    cache = data.load_metrics_cache(allow_stale=True)
    if cache is None or "closes" not in cache.columns:
        return None
    idx = data.get_index_daily("sh000001", days=400)
    if idx.empty:
        return None
    trading_dates = idx["日期"].astype(str).str.strip().tolist()
    date_to_pos = {d: i for i, d in enumerate(trading_dates)}
    _hist_cache = {
        "df": cache,
        "trading_dates": trading_dates,
        "date_to_pos": date_to_pos,
    }
    return _hist_cache


def historical_market_stats(date_str: str, pool_codes: set[str] | None = None) -> dict:
    """从 metrics_cache 回算某日的盘面统计（大面/涨停/跌停/打板大面/涨超7）。

    date_str: YYYY-MM-DD
    pool_codes: 该日时点股票池代码集合，非 None 时只统计池内股票（消除幸存者偏差）
    """
    hc = _load_hist_arrays()
    if hc is None:
        return {}
    date_str = date_str.strip()
    if date_str not in hc["date_to_pos"]:
        return {}
    target_pos = hc["date_to_pos"][date_str]
    df = hc["df"]
    codes = df["代码"].astype(str).values
    lim = np.array([_limit_pct(c) for c in codes])
    is_main = np.array([not c.startswith(("30", "68", "8", "4", "92")) for c in codes])

    last_dates = df["last_date"].astype(str).values
    closes_col = df["closes"].values
    highs_col = df["highs"].values

    pct_all, high_all, prev_close_all, valid = [], [], [], []
    for j in range(len(df)):
        arr_len = len(closes_col[j])
        last_pos = hc["date_to_pos"].get(str(last_dates[j]), -1)
        if last_pos < 0:
            continue
        offset = target_pos - last_pos
        arr_idx = arr_len - 1 + offset
        if arr_idx < 1 or arr_idx >= arr_len:
            continue
        c = np.asarray(closes_col[j], dtype=float)
        h = np.asarray(highs_col[j], dtype=float)
        if np.isnan(c[arr_idx]) or np.isnan(c[arr_idx - 1]) or c[arr_idx - 1] == 0:
            continue
        pct_all.append((c[arr_idx] / c[arr_idx - 1] - 1) * 100)
        high_all.append(h[arr_idx])
        prev_close_all.append(c[arr_idx - 1])
        valid.append(j)

    if pool_codes is not None:
        keep = [i for i, j in enumerate(valid) if codes[j] in pool_codes]
        pct_all = [pct_all[i] for i in keep]
        high_all = [high_all[i] for i in keep]
        prev_close_all = [prev_close_all[i] for i in keep]
        valid = [valid[i] for i in keep]

    if not pct_all:
        return {}
    pct = np.array(pct_all)
    highs = np.array(high_all)
    prev_closes = np.array(prev_close_all)
    lim_v = lim[valid]
    main_v = is_main[valid]
    not_st_v = (lim_v > 5.0)  # ST涨停5%，lim_v==5时排除

    lim_price = (prev_closes * (1 + lim_v / 100)).round(2)
    touched = (highs >= lim_price - 0.01)

    out = {
        "大面数": int(np.sum(pct <= -7)),
        "涨超7数": int(np.sum(pct >= 7)),
        "跌停数": int(np.sum(pct <= -(lim_v - 0.2))),
        "涨停数": int(np.sum(pct >= lim_v - 0.2)),
        "打板大面数": int(np.sum(touched & (pct < lim_v - 1.5) & main_v & not_st_v)),
    }
    return out


_last_rebuild_attempt: float = 0.0


def _maybe_rebuild_metrics_cache() -> None:
    """若 metrics_cache 超过12小时未更新，在后台线程触发一次重建（每小时最多一次）。"""
    global _last_rebuild_attempt
    import time
    now = time.time()
    if now - _last_rebuild_attempt < 3600:
        return
    try:
        from src import data as _d
        mtime = _d.METRICS_CACHE.stat().st_mtime if _d.METRICS_CACHE.exists() else 0
        if now - mtime > _d.METRICS_CACHE_TTL:
            _last_rebuild_attempt = now
            import threading
            threading.Thread(target=_d.build_metrics_cache, daemon=True).start()
            logger.info("metrics_cache 已过期，已触发后台重建")
    except Exception as e:
        logger.debug("触发重建检查失败: %s", e)


def stats_history(days: int = 10) -> pd.DataFrame:
    """近N个交易日盘面统计序列（大面/涨停/跌停），供模型做相对比较。"""
    _maybe_rebuild_metrics_cache()
    try:
        from src import theme_mode
        m = theme_mode._matrices()
    except Exception:
        m = None
    if m is None:
        return pd.DataFrame()
    C, dates, codes = m["C"], m["dates"], m["codes"]
    H = m.get("H")
    lim = np.array([_limit_pct(c) for c in codes])
    is_main = np.array([not c.startswith(("30", "68", "8", "4", "92")) for c in codes])
    not_st = (lim > 5.0)  # ST涨停5%，lim==5时排除
    today = datetime.now().strftime("%Y-%m-%d")
    rows = []
    for i in range(max(1, C.shape[1] - days), C.shape[1]):
        if dates[i] >= today:   # 当天数据以实时快照为准
            continue
        with np.errstate(invalid="ignore"):
            pct = (C[:, i] / C[:, i - 1] - 1) * 100
        row = {"日期": dates[i],
               "大面数": int(np.nansum(pct <= -7)),
               "涨超7数": int(np.nansum(pct >= 7)),
               "跌停数": int(np.nansum(pct <= -(lim - 0.2))),
               "涨停数": int(np.nansum(pct >= lim - 0.2))}
        if H is not None and H.size and i > 0:
            with np.errstate(invalid="ignore"):
                hi_pct = (H[:, i] / C[:, i - 1] - 1) * 100
            row["打板大面数"] = int(np.nansum(
                (hi_pct >= lim - 0.2) & (pct < lim - 1.5) & is_main & not_st))
        rows.append(row)
    return pd.DataFrame(rows)


def load_predictions() -> dict:
    if EMO_STORE.exists():
        try:
            return json.loads(EMO_STORE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_predictions(d: dict) -> None:
    EMO_STORE.parent.mkdir(parents=True, exist_ok=True)
    EMO_STORE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


JUDGE_PROMPT = """A股情绪节点判断。只输出JSON：
{{"node":"节点名","reason":"依据40字内","advice":"操作提示一句","confidence":0.8,"abstain":false,"evidence":[{{"feature":"特征名","value":"特征值","direction":"bullish/bearish/neutral"}}]}}
节点选：{nodes}

字段说明：
- node: 从节点选中选一个
- confidence: 0.0~1.0，对判断的置信度
- abstain: true表示不确定，建议选混沌
- evidence: 支撑判断的关键特征，feature必须是输入中出现的统计指标名

核心规则（按优先级）：
0. 打板大面硬约束（不可被大面减幅覆盖）：≥10禁修复--强（上限中等修复）；≥10禁主升--确认；≥8禁主升--确认（新规范：打板大面<8才可判主升--确认）；≥5禁修复--强（强修复需打板大面<5）。
1. 先看昨日是否大面多(≥30)。若是，今日大面骤减→修复类：减幅<50%→修复--弱，50-70%→修复--中等，70-90%→修复--中等（需指数放量才可升强），减幅≥90%→可能修复--强。不得超出规则0的上限。不能判为主升。
2. 强修复硬约束（独立于规则1）：即使大面减幅达90%以上，也必须同时满足以下条件才能判修复--强，否则上限为修复--中等：
   - 上证涨跌幅 ≥ 0.4%（指数放量大阳）
   - 上证成交额比昨日增加≥1300亿
   - 打板大面 < 5（强修复质量要求）
   - 大面绝对值 < 10
   如果上证涨跌幅<0.4%，直接判修复--中等，不得判修复--强。
3. 昨日大面少且今日涨停大增/高度板加速→主升类。主升--确认需同时满足：打板大面<8，热门股无跌停，热股指数涨超2%。打板大面≥8禁止判主升--确认。
3a. 主升--高潮升级规则（优先级高于主升--确认）：若满足主升--确认全部条件，且同时满足：上证涨跌幅>1%、上证成交额比昨日放量2000亿以上、平均股价信号为"转"或"多"（涨幅较好）、大面<15（若连板空间数据有，需>6板），则应判主升--高潮而非主升--确认。主升--高潮是主升浪顶部情绪，特征是核心板块指数跳空大涨、补涨批量涨停、连板高度加速。注：平均股价涨超2%是高潮的强信号，但信号为"转"时若指数放量和涨幅足够，也可能是高潮。
4. 退潮绝对门槛：退潮要求大面>50且热门股跌停≥3且热股指数跌超1%且龙头断板；退潮加速要求总跌停数>10且热门股跌停≥5；冰点要求大面>200且跌停>20且热股指数跌超3%。不满足门槛不得判对应退潮类。
5. 节点流转约束：修复类次日不可能直接跳到退潮加速，中间至少经历混沌或退潮。冰点后的修复如果失败，优先判混沌/混沌分歧而非退潮加速。
6. 前日大面低基数(≤10)时，相对增幅无意义，但若今日同时满足主升--确认硬条件（大面<20、跌停<10、涨停数≥50或涨超7数≥100、高度个股≥10、打板大面<8、热门股无跌停、热股指数涨超2%、指数上涨/平盘），应判主升--确认（或若满足规则3a则判主升--高潮），不强制混沌。否则按绝对值判断：大面<100且跌停<10→混沌/混沌分歧。
7. 混沌分歧条件：大面30-50个，跌停<5，热门股涨跌幅<-1%，打板大面>5。热门股批量大跌/跌停(≥3个)→退潮；热门股涨停加速→主升--加速。
8. 拿不准选保守(混沌/修复)，设abstain=true或confidence<0.5，不要强行输出伪确定结论。

输出前自检：
- 准备输出"主升--高潮"：确认上证涨跌幅>1%、上证放量>2000亿（比昨日）、平均股价信号为"多"或涨超2%、大面<15。否则降为主升--确认。
- 准备输出"修复--强"：确认大面减幅≥90%、上证涨跌幅≥0.4%、打板大面<5。否则改为修复--中等。
- 准备输出"主升--确认"：确认打板大面<8、热门股无跌停、热股指数涨超2%。若同时满足高潮条件，升级为主升--高潮。
- 准备输出"退潮"：确认大面>50、热门股跌停≥3、热股指数跌超1%。否则降为混沌分歧。
- 准备输出"冰点"：确认大面>200、跌停>20、热股指数跌超3%。否则降为退潮加速。

策略规范（单一事实来源）：
{spec}

竞价表历史：
{history}

近日盘面（旧→新）：
{recent}

今日{today}（{daban_constraint}）
{stats}
{compare}

热门股：
{hot}
"""

REVIEW_PROMPT_EMOTION = """A股情绪节点复核员。请根据以下**同一份盘面数据**独立判断情绪节点。
这是对抗式复核——你不知道第一模型的结论，需要独立给出判断。

请重点回答：1)是否违反硬规则？2)哪条证据不足？3)哪些反例可推翻结论？4)是否应弃权？5)需要什么条件才能升级？

只输出JSON：
{{"node":"节点名","reason":"独立依据40字内","advice":"操作提示一句","confidence":0.8,"abstain":false,"evidence":[{{"feature":"特征名","value":"特征值","direction":"bullish/bearish/neutral"}}]}}
节点选：{nodes}

核心规则（按优先级）：
0. 打板大面≥10禁修复--强；打板大面≥8禁主升--确认（新规范：主升--确认需打板大面<8）；打板大面≥5禁修复--强（强修复需<5）。
1. 昨日大面多(≥30)今日骤减→修复类（减幅<50%弱，50-70%中等，70-90%中等（需指数确认），≥90%可能强，受规则0限制）。
2. 修复--强需上证涨跌幅≥0.4%且成交额放大≥1300亿且打板大面<5且大面绝对值<10。
3. 退潮门槛：大面>50且热门股跌停≥3且热股指数跌超1%；冰点：大面>200且跌停>20且热股指数跌超3%；退潮加速：总跌停数>10且热门股跌停≥5。
4. 主升--确认硬条件：大面<20、跌停<10、热门股无跌停、热股指数涨超2%、打板大面<8；前日大面低基数(≤10)时满足以上条件应判主升--确认，不强制混沌。
4a. 主升--高潮升级（优先于确认）：满足确认全部条件且上证涨跌幅>1%、上证放量比昨日+2000亿以上、平均股价信号"转"或"多"、大面<15→判主升--高潮。平均股价涨超2%是强信号但非必须，指数放量和涨幅才是核心门槛。
5. 拿不准选混沌，设abstain=true，不要强行输出伪确定结论。

策略规范（单一事实来源）：
{spec}

竞价表历史：
{history}

近日盘面（旧→新）：
{recent}

今日{today}（{daban_constraint}）
{stats}
{compare}

热门股：
{hot}
"""


def _daman_compare(hist_stats: pd.DataFrame, today_stats: dict) -> str:
    """前日大面→今日大面对比，输出提示行。"""
    if hist_stats.empty or not today_stats:
        return ""
    prev = hist_stats.iloc[-1]
    prev_dm = int(prev["大面数"]) if pd.notna(prev["大面数"]) else 0
    today_dm = int(today_stats.get("大面数", 0)) if today_stats.get("大面数") is not None else 0
    today_dt = int(today_stats.get("跌停数", 0)) if today_stats.get("跌停数") is not None else 0
    # 低基数场景：前日大面≤10时，相对增幅无意义，按绝对值+跌停数判断
    if prev_dm <= 10:
        if today_dm <= 20:
            tag = "混沌"
        elif today_dm <= 100:
            tag = "混沌分歧" if today_dt < 10 else "退潮"
        elif today_dm <= 200:
            tag = "退潮" if today_dt >= 10 else "混沌分歧"
        else:
            tag = "冰点"
        return (f"前日大面{prev_dm}（低基数）→今日大面{today_dm}，跌停{today_dt}，"
                f"按绝对值判断→{tag}")
    chg = (today_dm - prev_dm) / prev_dm * 100
    if chg < 0:
        reduction = abs(chg)
        if reduction < 50:
            tag = "修复--弱"
        elif reduction < 70:
            tag = "修复--中等"
        elif reduction < 90:
            tag = "修复--中等（70-90%，需指数放量确认才能升强）"
        else:
            tag = "修复--强（需验证指数放量+上证涨幅≥0.4%）"
        return f"前日大面{prev_dm}→今日大面{today_dm}，减少{reduction:.0f}%→{tag}"
    if chg > 0:
        if today_dm <= 100 and today_dt < 10:
            return (f"前日大面{prev_dm}→今日大面{today_dm}，增加{chg:.0f}%，"
                    f"但绝对值不高且跌停{today_dt}<10→混沌分歧")
        return f"前日大面{prev_dm}→今日大面{today_dm}，增加{chg:.0f}%→退潮"
    return f"前日大面{prev_dm}→今日大面{today_dm}，持平"


def ai_judge(force: bool = False) -> dict:
    """判断今日情绪节点。每日缓存；force=True 重新判断。

    使用结构化校验器：validate_emotion_decision() 对 LLM 输出进行
    枚举校验、置信度截断、evidence 过滤、硬约束覆盖（打板大面/跌停门槛）。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    store = load_predictions()
    if not force and today in store:
        return store[today]

    if not cfg.QWEN_API_KEY:
        return {"error": "未配置 QWEN_API_KEY"}

    from src.llm_gateway import call_llm

    # 若 metrics_cache 已过期，同步重建一次（保证历史统计含昨日数据）
    try:
        import time as _t
        if not data.METRICS_CACHE.exists() or \
                _t.time() - data.METRICS_CACHE.stat().st_mtime > data.METRICS_CACHE_TTL:
            logger.info("ai_judge: metrics_cache 过期，同步重建中...")
            data.build_metrics_cache()
    except Exception as _e:
        logger.warning("ai_judge: metrics_cache 重建失败: %s", _e)

    hist = load_auction_table()
    hist_lines = "\n".join(
        f"{r['日期']} {r['节点']} {r.get('指数', '')}"
        for _, r in hist.tail(15).iterrows()) or "（无历史标注）"
    stats = market_stats()
    stats_lines = "\n".join(f"{k}：{v}" for k, v in stats.items()) or "（盘面数据不可用）"
    _db = stats.get("打板大面数")
    if _db is not None and _db >= 10:
        _db_constr = f"打板大面{_db}个，禁强修复（上限中等修复）且禁主升--确认"
    elif _db is not None and _db >= 8:
        _db_constr = f"打板大面{_db}个，禁主升--确认（需<8），可判中等修复"
    elif _db is not None and _db >= 5:
        _db_constr = f"打板大面{_db}个，禁强修复（强修复需<5），可判主升--确认"
    elif _db is not None:
        _db_constr = f"打板大面{_db}个，无限制"
    else:
        _db_constr = "打板大面数据不可用"
    hist_stats = stats_history(10)
    _db_col = "打板大面数" in hist_stats.columns
    # 检查历史数据新鲜度：若最后一行日期不是昨日，追加警告
    _stale_warning = ""
    if not hist_stats.empty:
        _last_hist_date = hist_stats.iloc[-1]["日期"]
        from datetime import timedelta
        _yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        if _last_hist_date < _yesterday:
            _stale_warning = (f"\n【注意】历史统计数据最新仅到 {_last_hist_date}，"
                              f"缺少 {_last_hist_date} 之后至昨日的数据，"
                              "请勿用昨日大面推导今日修复幅度；改为参考今日绝对值判断节点。")
    recent_lines = "\n".join(
        f"{r['日期']} 大面{r['大面数']} 跌停{r['跌停数']} 涨停{r['涨停数']}"
        + (f" 打板大面{int(r['打板大面数'])}" if _db_col and pd.notna(r['打板大面数']) else "")
        for _, r in hist_stats.iterrows()) or "（无历史统计）"
    recent_lines += _stale_warning
    hot = hot_stock_stats()
    _hot_rows = []
    for h in (hot.get("hot_list") or [])[:5]:
        p = h.get("涨跌幅")
        p_str = f"{p:+.2f}%" if pd.notna(p) else "-"
        _hot_rows.append(f"{h.get('名称', '')} {p_str} [{h.get('板块', '-')}]")
    hot_lines = "\n".join(_hot_rows) or "（热榜不可用）"
    if hot.get("热门个股指数") is not None:
        hot_lines = f"热指{hot['热门个股指数']:+.2f}%\n" + hot_lines
    spec_text = load_spec_text(EMO_SPEC)
    prompt = JUDGE_PROMPT.format(nodes="、".join(NODE_NAMES),
                                 history=hist_lines, recent=recent_lines,
                                 today=today, daban_constraint=_db_constr,
                                 stats=stats_lines, hot=hot_lines,
                                 compare=_daman_compare(hist_stats, stats),
                                 spec=spec_text)
    _fmt_kwargs = dict(nodes="、".join(NODE_NAMES), history=hist_lines,
                       recent=recent_lines, today=today, daban_constraint=_db_constr,
                       stats=stats_lines, hot=hot_lines,
                       compare=_daman_compare(hist_stats, stats),
                       spec=spec_text)
    review_prompt = REVIEW_PROMPT_EMOTION.format(**_fmt_kwargs)

    from src.schema_validator import validate_emotion_decision, dual_stage_review

    def _call_llm(model: str, llm_prompt: str = "") -> str | None:
        """调用 LLM 返回原始文本，失败返回 None。"""
        return call_llm(
            prompt=llm_prompt or prompt,
            model=model,
            temperature=0.1,
            max_tokens=500,
            timeout=30.0,
            retries=0,
        )

    # 尝试主模型 + 兜底模型，每个最多重试 1 次
    validated = None
    for model in ["qwen-plus", cfg.QWEN_CHAT_MODEL]:
        for attempt in range(2):
            raw = _call_llm(model, prompt)
            if raw is None:
                continue
            result = validate_emotion_decision(raw, stats=stats)
            if not result.get("abstain") or result["node"] != "混沌":
                validated = result
                break
            if attempt == 0:
                logger.info("校验返回混沌，重试一次 (%s)", model)
        if validated and not validated.get("abstain"):
            break

    if validated is None:
        logger.error("AI 情绪节点判断全部失败")
        return {"error": "LLM 调用与校验均失败"}

    # 双阶段复核：低置信/弃权/前后跳变 → 触发 qwen3.7-max 对抗式复核
    store = load_predictions()
    sorted_dates = sorted(store.keys(), reverse=True)
    prev_result = store[sorted_dates[0]] if sorted_dates and sorted_dates[0] != today else None

    def _validate_for_review(raw: str) -> dict:
        return validate_emotion_decision(raw, stats=stats)

    validated = dual_stage_review(
        call_llm=_call_llm,
        validate_fn=_validate_for_review,
        stage1_result=validated,
        prev_result=prev_result,
        key_field="node",
        review_prompt=review_prompt,
    )

    # 组装最终结果（向后兼容 + 新增字段）
    result = {
        "node": validated["node"],
        "reason": validated["reason"],
        "advice": validated["advice"],
        "confidence": validated["confidence"],
        "abstain": validated["abstain"],
        "evidence": validated["evidence"],
        "stats": stats,
        "prev_stats": hist_stats.iloc[-1].to_dict() if not hist_stats.empty else {},
        "time": datetime.now().strftime("%H:%M"),
        "reviewed": validated.get("reviewed", False),
        "agreed": validated.get("agreed", None),
    }
    if validated.get("review_note"):
        result["review_note"] = validated["review_note"]
    if validated.get("review_reason"):
        result["review_reason"] = validated["review_reason"]
    store[today] = result
    _save_predictions(store)
    try:
        from src import predictions
        predictions.append_prediction(
            type="emotion", date=today, node=result["node"],
            confidence=result["confidence"],
            evidence_snapshot=result.get("evidence", []),
            abstain=result.get("abstain", False),
            model_id="qwen-plus",
        )
    except Exception:
        pass
    return result


def should_auto_judge() -> bool:
    """尾盘 14:57 后且今日尚无判断 → 自动触发。"""
    now = datetime.now()
    if now.strftime("%H:%M") < JUDGE_TIME or now.weekday() >= 5:
        return False
    return now.strftime("%Y-%m-%d") not in load_predictions()
