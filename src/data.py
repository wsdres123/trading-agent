"""多源金融数据获取 — 统一门面（facade）。

2026-08-05 重构：原 1233 行巨型文件按功能拆为 data_* 子模块，
本文件只保留对外接口再导出，所有调用方（server/ui/src/*）无需改动。

子模块划分：
    data_base      akshare 可用性 / retry / _empty 等公共底座
    data_quotes    新浪基础设施：_sina_fetch/_sina_symbol/_sina_session/指数实时
    data_spot      全 A 实时快照（腾讯→Fuyao→akshare→stale）+ 平均股价
    data_calendar  股票列表 / 交易日历 / 证券主数据快照
    data_history   个股日K/分钟K/关键指标
    data_enrich    筛选结果增强字段（涨速/封单额/自由流通/概念板块）
    data_index     指数日K / 同花顺指数日K / get_index_spot
    data_hot       同花顺热榜
    data_boards    板块/概念成分股
    data_metrics   全市场指标缓存（筛选秒出）

注意：ttl_cache 以函数名为缓存 key，函数名/默认参数即接口契约，勿改。
"""
from src.redis_cache import ttl_cache, clear_cache  # noqa: F401  (short_term 等用作装饰器)

from src.data_base import retry, _empty, _need_akshare, _AKSHARE_OK  # noqa: F401
from config import settings as cfg
from src.data_quotes import (  # noqa: F401
    INDEX_COLS, _SINA_INDICES, _SINA_KLINE_URL, _sina_fetch, _sina_index_spot,
    _sina_symbol, _sina_session, get_stock_quote_fast)
from src.data_spot import (  # noqa: F401
    SPOT_COLS, _TENCENT_BATCH, _tencent_spot_batch, get_stock_spot_fast,
    get_stock_spot, get_realtime_avg_price)
from src.data_calendar import (  # noqa: F401
    get_stock_list, get_trade_calendar, is_trading_day,
    get_securities_snapshot, get_point_in_time_pool)
from src.data_history import (  # noqa: F401
    HIST_COLS, get_stock_hist, get_stock_minute, get_stock_metrics)
from src.data_enrich import (  # noqa: F401
    CONCEPT_CACHE, FREE_FLOAT_CACHE, FREE_FLOAT_TTL, _FREE_KW, _EM_HEADERS,
    _em_secid, _load_concept_cache, _load_free_float_cache, enrich_stocks)
from src.data_index import (  # noqa: F401
    INDEX_KLINE_SYMBOLS, _get_index_daily_full, get_index_daily,
    get_ths_index_daily, get_index_spot)
from src.data_hot import get_hot_stocks  # noqa: F401
from src.data_boards import (  # noqa: F401
    BOARD_COLS, _board_list, get_industry_boards, get_board_constituents,
    sector_to_codes)
from src.data_metrics import (  # noqa: F401
    METRICS_CACHE, METRICS_CACHE_TTL, build_metrics_cache, load_metrics_cache,
    metrics_cache_status)


# ── 自检 ──────────────────────────────────────────────────────────────────
@ttl_cache(300)  # 每次页面交互都会重跑，健康检查含网络请求，必须缓存
def health() -> dict:
    from src import source_health, clock
    _ths_ok = False
    try:
        from src import ths_data
        _ths_ok = ths_data.health()
    except Exception:
        pass
    return {"akshare": _AKSHARE_OK, "qwen_key": bool(cfg.QWEN_API_KEY),
            "ths_key": bool(cfg.THS_API_KEY), "ths_api": _ths_ok,
            "source_health": source_health.status(),
            "clock": clock.status()}
