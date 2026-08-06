"""同花顺热榜（热门个股）。"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from config import settings as cfg
from src.redis_cache import ttl_cache
from src.data_spot import get_stock_spot
from src.data_enrich import _em_secid, _EM_HEADERS

logger = logging.getLogger("data")

# ── 同花顺热榜（热门个股）──────────────────────────────────────────────────
@ttl_cache(cfg.SPOT_TTL, l1_ttl=cfg.L1_TTL_SPOT)
def get_hot_stocks(top: int = 10) -> pd.DataFrame:
    """同花顺热榜前N热门股：排名/代码/名称/热度/最新价/涨跌幅。

    数据源：同花顺金融数据API（fuyao MCP 热榜）优先，同花顺公开热榜兜底。
    """
    df = pd.DataFrame()
    if cfg.THS_API_KEY:
        try:
            from src import ths_data
            df = ths_data.hot_stock_list("hour")
        except Exception as e:
            logger.warning("同花顺API热榜失败：%s", e)
    if df.empty:
        try:
            import requests
            r = requests.get(
                "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/"
                "v1/stock?stock_type=a&type=hour&list_type=normal",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            items = (r.json().get("data") or {}).get("stock_list") or []
            df = pd.DataFrame([{
                "排名": it.get("order"), "代码": str(it.get("code", "")),
                "名称": it.get("name", ""),
                "热度": pd.to_numeric(it.get("rate"), errors="coerce"),
                "排名变化": it.get("hot_rank_chg"),
            } for it in items])
        except Exception as e:
            logger.warning("同花顺公开热榜失败：%s", e)
    if df.empty:
        return df
    df = df.head(top).reset_index(drop=True)
    try:
        spot = get_stock_spot()[["代码", "最新价", "涨跌幅", "成交额"]]
        df = df.merge(spot, on="代码", how="left")
        df["成交额(亿)"] = (pd.to_numeric(df["成交额"], errors="coerce") / 1e8).round(1)
        df = df.drop(columns=["成交额"])
    except Exception as e:
        logger.debug("热榜合并快照失败：%s", e)
    try:
        import json
        import requests

        def _theme_boards(session, code: str) -> str | None:
            """个股所属板块（拉全量概念，过滤指数/风格类，取前3个题材板块）。"""
            try:
                r = session.get("https://push2.eastmoney.com/api/qt/slist/get",
                                params={"spt": "3", "pi": "0", "pz": "80", "po": "1",
                                        "fid": "f3", "fltt": "2", "invt": "2", "np": "1",
                                        "secid": _em_secid(code), "fields": "f12,f14"},
                                headers=_EM_HEADERS, timeout=6)
                diff = (r.json().get("data") or {}).get("diff") or []
                names = [d["f14"] for d in diff
                         if str(d.get("f12", "")).startswith("BK")
                         and not str(d["f14"]).endswith("_")]
                try:
                    from src.theme_mode import _is_theme_board
                    names = [n for n in names if _is_theme_board(n)]
                except Exception:
                    pass
                return "、".join(names[:3]) if names else None
            except Exception as e:
                logger.debug("热榜板块 %s 失败：%s", code, e)
                return None

        cache_file = cfg.DATA_DIR / "hot_boards.json"
        try:
            boards = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            boards = {}
        s = requests.Session()
        missing = [c for c in df["代码"] if c not in boards]
        if missing:
            with ThreadPoolExecutor(max_workers=8) as ex:
                for code, b in zip(missing, ex.map(lambda c: _theme_boards(s, c), missing)):
                    if b is not None:
                        boards[code] = b
            cache_file.write_text(json.dumps(boards, ensure_ascii=False), encoding="utf-8")
        df["板块"] = df["代码"].map(lambda c: boards.get(c, "-"))
    except Exception as e:
        logger.debug("热榜板块获取失败：%s", e)
    return df

