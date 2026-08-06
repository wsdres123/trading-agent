"""FastAPI async server: background cache warming + REST + WebSocket.

Runs alongside Streamlit on port 8602.
Background tasks pre-populate Redis during trading hours
→ data.py's @ttl_cache gets Redis hits → Streamlit fast path.
预热频率自适应：竞价10s / 盘中60s / 午休跳过 / 尾盘15s / 非交易300s。
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
import pandas as pd
from fastapi import Body, Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from config import settings as cfg
from src.redis_cache import _cache_key, redis_get, redis_set, _redis_available
from src import async_fetch
from src import data
from src.ts_store import get_store as _get_ts_store
from src.monitor import metrics
from src import ws_hub
from src import clock
from src.tasks import get_task_status, submit, pull_stock_history, generate_eval_report

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("server")

_http_client: httpx.AsyncClient | None = None
_last_spot_update: str | None = None


def _warmup_interval() -> int:
    """根据交易时段返回预热间隔（秒）。午休返回 0 表示跳过。

    方案A（轮询优化，替代 L2 WS）：盘中 5s 高频批量拉取全市场快照，
    端到端延迟 ≤5s；请求经 async_fetch 限流（8 并发/批），源站压力可控。
    """
    now = datetime.now()
    if not data.is_trading_day():
        return 300
    m = now.hour * 60 + now.minute
    if 565 <= m <= 570:   return 5     # 9:25-9:30 竞价
    if 570 <= m <= 690:   return 5     # 9:30-11:30 上午盘
    if 690 <= m <= 780:   return 0     # 11:30-13:00 午休，跳过
    if 780 <= m <= 890:   return 5     # 13:00-14:50 下午盘
    if 890 <= m <= 905:   return 5     # 14:50-15:05 尾盘
    return 300                          # 非交易时段


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client
    _http_client = httpx.AsyncClient(
        limits=httpx.Limits(
            max_connections=cfg.ASYNC_MAX_CONNECTIONS,
            max_keepalive_connections=cfg.ASYNC_MAX_KEEPALIVE,
            keepalive_expiry=20,
        ),
        timeout=httpx.Timeout(cfg.ASYNC_TIMEOUT),
        headers={"User-Agent": "Mozilla/5.0"},
    )
    # 启动即校准本地时钟（行情源 Date 头），信号时间戳统一走 clock.now()
    await clock.sync(_http_client)
    tasks = [
        asyncio.create_task(_bg_refresh_spot()),
        asyncio.create_task(_bg_refresh_index()),
        asyncio.create_task(_bg_refresh_hot()),
        asyncio.create_task(_bg_refresh_index_daily()),
        asyncio.create_task(_bg_refresh_ths_index_daily()),
        asyncio.create_task(_bg_refresh_health()),
        asyncio.create_task(_bg_save_daily_snapshot()),
        asyncio.create_task(_bg_save_securities_snapshot()),
        asyncio.create_task(_bg_monitor_redis()),
        asyncio.create_task(_bg_refresh_metrics_cache()),
        asyncio.create_task(_bg_l2_ws()),
        asyncio.create_task(_bg_pull_minute_kline()),
    ]
    logger.info("FastAPI server started, %d background tasks", len(tasks))
    yield
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await _http_client.aclose()
    _http_client = None


# 生产环境关闭 Swagger/OpenAPI，避免接口枚举
app = FastAPI(
    title="劫财AI交易 Backend", lifespan=lifespan,
    docs_url=None, redoc_url=None, openapi_url=None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.CORS_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ── L2 WebSocket 行情后台任务 ──────────────────────────────────────────────
_l2_active = False


async def _bg_l2_ws():
    """L2 行情推送主循环。

    ThsFuyaoClient：connect() 是永驻轮询循环，_l2_active=True 期间
      _bg_refresh_spot 跳过腾讯轮询，由 Fuyao REST 快照替代。
    占位客户端（EastmoneyL2Client）：connect() raise NotImplementedError，
      自动降级到 HTTP 轮询，300s 后重试。
    """
    global _l2_active
    from src.ws_source import get_l2_source
    while True:
        source = get_l2_source()
        if source is None:
            await asyncio.sleep(300)
            continue
        try:
            # 合并回调：Redis 缓存更新 + ws_hub 广播，一个回调避免互相覆盖
            async def _l2_on_tick(tick: dict):
                if not isinstance(tick, dict):
                    return
                t = tick.get("type")
                if t == "quote":
                    code = tick.get("code", "")
                    if code:
                        redis_set(_cache_key("spot_quote", (code,), {}),
                                  tick.get("data", {}), cfg.SPOT_TTL)
                    await ws_hub.publish_quote(tick)
                elif t == "market":
                    await ws_hub.publish_quote(tick)

            source.on_tick(_l2_on_tick)

            # subscribe 可在 connect 前调用（ThsFuyaoClient 支持；占位客户端会 raise）
            codes = [c.strip() for c in cfg.L2_SUBSCRIBE_CODES.split(",") if c.strip()]
            if codes:
                await source.subscribe(codes)

            _l2_active = True
            logger.info("L2 源启动，订阅 %d 只", len(codes))
            # connect() 永驻（ThsFuyaoClient）或抛异常（占位/凭证未配置）
            await source.connect()
            # 正常退出（不应到达此处）
            _l2_active = False

        except NotImplementedError as e:
            logger.warning("L2 未实现：%s，降级 HTTP 轮询，300s 后重试", e)
            _l2_active = False
            await asyncio.sleep(300)
        except ConnectionError as e:
            logger.warning("L2 连接失败：%s，60s 后重试", e)
            _l2_active = False
            await asyncio.sleep(60)
        except Exception as e:
            logger.warning("L2 异常：%s，5s 后重试", e)
            _l2_active = False
            await asyncio.sleep(5)
        finally:
            _l2_active = False
            try:
                await source.close()
            except Exception:
                pass


# ── 盘后分钟线补拉 ──────────────────────────────────────────────────────────
async def _bg_pull_minute_kline():
    """盘后补拉连板池1分钟K线到 ts_store（akshare stock_zh_a_hist_min_em）。"""
    from src import data
    while True:
        now = datetime.now()
        if not data.is_trading_day():
            await asyncio.sleep(3600)
            continue
        m = now.hour * 60 + now.minute
        if m < cfg.MINUTE_PULL_HOUR * 60 + cfg.MINUTE_PULL_MINUTE:
            await asyncio.sleep(600)
            continue
        marker = cfg.DATA_DIR / f".minute_pulled_{now.strftime('%Y-%m-%d')}"
        if marker.exists():
            await asyncio.sleep(3600)
            continue
        codes: set[str] = set()
        try:
            from src import short_term
            try:
                zt = short_term.get_zt_pool_ths()
                if not zt.empty and "代码" in zt.columns:
                    codes.update(zt["代码"].astype(str).str.zfill(6).tolist())
            except Exception as e:
                logger.debug("get_zt_pool_ths 失败：%s", e)
            if not codes:
                zt = short_term.get_zt_pool(now.strftime("%Y%m%d"))
                if not zt.empty and "代码" in zt.columns:
                    codes.update(zt["代码"].astype(str).str.zfill(6).tolist())
        except Exception as e:
            logger.debug("连板池获取失败：%s", e)
        if not codes:
            await asyncio.sleep(3600)
            continue
        logger.info("盘后补拉分钟线 %d 只", len(codes))
        date_str = now.strftime("%Y-%m-%d")
        for code in sorted(codes):
            try:
                await asyncio.to_thread(data.get_stock_minute, code, date_str)
            except Exception as e:
                logger.debug("分钟线 %s 失败：%s", code, e)
            await asyncio.sleep(1)
        marker.touch()
        logger.info("盘后分钟线补拉完成")
        await asyncio.sleep(3600)


# ── Background tasks: pre-populate Redis during trading hours ──────────────
_prev_spot: pd.DataFrame | None = None   # 上一轮快照，用于个股增量推送


def _spot_changes(df: pd.DataFrame, prev: pd.DataFrame | None) -> list[dict]:
    """对比两轮快照，返回变动超过阈值的个股 quote 列表（最多 TOP_N 条）。"""
    if prev is None or df.empty or prev.empty:
        return []
    cols = ["代码", "最新价", "涨跌幅", "成交额"]
    cur = df[cols].set_index("代码", drop=False)
    old = prev[cols].set_index("代码", drop=False)
    cur_p = pd.to_numeric(cur["涨跌幅"], errors="coerce")
    old_p = pd.to_numeric(old["涨跌幅"], errors="coerce").reindex(cur.index)
    cur_v = pd.to_numeric(cur["最新价"], errors="coerce")
    old_v = pd.to_numeric(old["最新价"], errors="coerce").reindex(cur.index)
    moved = ((cur_p - old_p).abs() >= cfg.SPOT_REFRESH_CHG_PCT) | (
        (cur_v - old_v).abs() / old_v.abs().replace(0, float("nan")) * 100
        >= cfg.SPOT_REFRESH_PRICE_PCT)
    if not moved.any():
        return []
    out = cur.loc[moved.fillna(True)].sort_values(
        "涨跌幅", key=pd.to_numeric, ascending=False).head(cfg.SPOT_REFRESH_TOP_N)
    return [{"code": str(r["代码"]),
             "data": {"最新价": float(r["最新价"]) if pd.notna(r["最新价"]) else None,
                      "涨跌幅": float(r["涨跌幅"]) if pd.notna(r["涨跌幅"]) else None}}
            for _, r in out.iterrows()]


async def _bg_refresh_spot():
    global _last_spot_update, _prev_spot
    from src import source_health
    while True:
        interval = _warmup_interval()
        if interval > 0 and _http_client:
            if _l2_active:
                # L2 推送写 Redis，跳过腾讯轮询
                await asyncio.sleep(max(interval, 10))
                continue
            if not source_health.is_available("tencent"):
                logger.debug("tencent blacklisted, skip spot refresh")
                await asyncio.sleep(max(interval, 30))
                continue
            try:
                # 超时保护：慢请求不再阻塞 5s 节奏，本轮跳过、下轮重试
                df = await asyncio.wait_for(
                    async_fetch.get_stock_spot_fast_async(_http_client),
                    timeout=cfg.ASYNC_TIMEOUT)
                if len(df) > 1000:
                    source_health.record("tencent", True)
                    key = _cache_key("get_stock_spot", (), {})
                    redis_set(key, df, cfg.SPOT_TTL)
                    _last_spot_update = datetime.now().isoformat(timespec="seconds")
                    logger.debug("Spot cache warmed: %d rows", len(df))
                    # 个股增量推送：涨跌幅/价格变动超阈值的股票逐条广播
                    try:
                        for q in _spot_changes(df, _prev_spot):
                            await ws_hub.publish_quote({
                                "type": "quote", **q,
                                "timestamp": time.strftime("%H:%M:%S")})
                    except Exception as qe:
                        logger.debug("quote broadcast failed: %s", qe)
                    _prev_spot = df[["代码", "最新价", "涨跌幅", "成交额"]]
                    # 单点采集 → Pub/Sub 广播：所有 WS 客户端共享这一份上游请求
                    try:
                        idx = await async_fetch.sina_index_spot_async(_http_client)
                        prices = pd.to_numeric(df["最新价"], errors="coerce")
                        await ws_hub.publish_quote({
                            "type": "market",
                            "avg_price": round(float(prices.mean()), 3) if len(prices) else None,
                            "stock_count": int(len(prices)),
                            "indices": idx.to_dict("records") if not idx.empty else [],
                            "timestamp": time.strftime("%H:%M:%S"),
                        })
                    except Exception as be:
                        logger.debug("market broadcast failed: %s", be)
                else:
                    source_health.record("tencent", False)
            except asyncio.TimeoutError:
                logger.warning("BG spot refresh timeout (>%ss), skip this round", cfg.ASYNC_TIMEOUT)
            except Exception as e:
                source_health.record("tencent", False)
                logger.warning("BG spot refresh failed: %s", e)
        await asyncio.sleep(max(interval, 10))


async def _bg_refresh_index():
    while True:
        interval = _warmup_interval()
        if interval > 0 and _http_client:
            try:
                df = await async_fetch.sina_index_spot_async(_http_client)
                if not df.empty:
                    key = _cache_key("get_index_spot", (), {})
                    redis_set(key, df, cfg.SPOT_TTL)
            except Exception as e:
                logger.warning("BG index refresh failed: %s", e)
        await asyncio.sleep(max(interval, 10))


async def _bg_refresh_hot():
    while True:
        interval = _warmup_interval()
        if interval > 0 and _http_client:
            try:
                spot_key = _cache_key("get_stock_spot", (), {})
                spot_df = redis_get(spot_key)
                df = await async_fetch.get_hot_stocks_async(
                    _http_client, spot_df=spot_df, top=10)
                if not df.empty:
                    key = _cache_key("get_hot_stocks", (), {"top": 10})
                    redis_set(key, df, cfg.SPOT_TTL)
            except Exception as e:
                logger.warning("BG hot refresh failed: %s", e)
        await asyncio.sleep(max(interval, 10))


async def _bg_refresh_index_daily():
    ts = _get_ts_store()
    while True:
        interval = _warmup_interval()
        if interval > 0 and _http_client:
            for sym in ("sh000001", "sz399001", "sz399006", "sh000688"):
                try:
                    # 预热写全量 1060 天，与 data.py _get_index_daily_full 的缓存 key 一致
                    df = await async_fetch.get_index_daily_async(
                        _http_client, sym, 1060)
                    key = _cache_key("_get_index_daily_full", (sym,), {"days": 1060})
                    redis_set(key, df, cfg.SPOT_TTL)
                    ts.save_index_daily(sym, df)
                except Exception as e:
                    logger.warning("BG index_daily %s failed: %s", sym, e)
        await asyncio.sleep(max(interval, 30))


async def _bg_refresh_ths_index_daily():
    ts = _get_ts_store()
    while True:
        interval = _warmup_interval()
        if interval > 0 and _http_client:
            for code, days in [("883958", 120), ("883902", 220)]:
                try:
                    df = await async_fetch.get_ths_index_daily_async(
                        _http_client, code, days)
                    key = _cache_key("get_ths_index_daily", (code,), {"days": days})
                    redis_set(key, df, cfg.SPOT_TTL)
                    ts.save_ths_index(code, df)
                except Exception as e:
                    logger.warning("BG ths_index %s failed: %s", code, e)
        await asyncio.sleep(max(interval, 30))


async def _bg_refresh_health():
    while True:
        try:
            # 每轮顺带重校准时钟（行情源 Date 头），偏差 >3s 会告警
            if _http_client:
                await clock.sync(_http_client)
            h = data.health()
            h["clock"] = clock.status()
            key = _cache_key("health", (), {})
            redis_set(key, h, 300)
        except Exception as e:
            logger.warning("BG health failed: %s", e)
        await asyncio.sleep(300)


async def _bg_save_daily_snapshot():
    """每日收盘后保存全市场快照到 .data/ts/snapshots/，供盘后回测。"""
    ts = _get_ts_store()
    while True:
        now = datetime.now()
        m = now.hour * 60 + now.minute
        today = now.strftime("%Y-%m-%d")
        if (data.is_trading_day()
                and cfg.SNAPSHOT_HOUR * 60 + cfg.SNAPSHOT_MINUTE <= m
                and m <= cfg.SNAPSHOT_HOUR * 60 + cfg.SNAPSHOT_MINUTE + 10
                and not ts.has_daily_snapshot(today)):
            try:
                spot_key = _cache_key("get_stock_spot", (), {})
                spot_df = redis_get(spot_key)
                if spot_df is not None and not spot_df.empty:
                    ts.save_daily_snapshot(today, spot_df)
            except Exception as e:
                logger.warning("BG daily snapshot failed: %s", e)
        await asyncio.sleep(60)


async def _bg_save_securities_snapshot():
    """每日收盘后保存证券主数据快照（代码/名称/ST状态），消除回测幸存者偏差。"""
    ts = _get_ts_store()
    while True:
        now = datetime.now()
        m = now.hour * 60 + now.minute
        today = now.strftime("%Y-%m-%d")
        if (data.is_trading_day()
                and cfg.SNAPSHOT_HOUR * 60 + cfg.SNAPSHOT_MINUTE <= m
                and m <= cfg.SNAPSHOT_HOUR * 60 + cfg.SNAPSHOT_MINUTE + 10
                and not ts.has_securities_snapshot(today)):
            try:
                df = data.get_securities_snapshot()
                if not df.empty:
                    ts.save_securities_snapshot(today, df)
            except Exception as e:
                logger.warning("BG securities snapshot failed: %s", e)
        await asyncio.sleep(60)


async def _bg_monitor_redis():
    """每 60s 检测 Redis 可用性并上报 Prometheus 指标。"""
    while True:
        available = _redis_available()
        metrics.redis_status(available)
        await asyncio.sleep(60)


async def _bg_refresh_metrics_cache():
    """每 13 小时在后台线程重建 metrics_cache，保持近日统计数据新鲜。"""
    await asyncio.sleep(60)  # 启动 1min 后执行首次检查
    while True:
        try:
            if _warmup_interval() > 0:  # 仅交易时段重建
                logger.info("BG metrics_cache rebuild starting...")
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, data.build_metrics_cache)
                logger.info("BG metrics_cache rebuild done")
        except Exception as e:
            logger.warning("BG metrics_cache rebuild failed: %s", e)
        await asyncio.sleep(13 * 3600)


# ── REST endpoints ─────────────────────────────────────────────────────────

def _require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    """高成本/敏感接口鉴权。未配置 ADMIN_API_KEY 时默认放行（仍受 127.0.0.1 绑定保护）。"""
    if not cfg.ADMIN_API_KEY:
        return
    if not x_api_key or not hmac.compare_digest(x_api_key, cfg.ADMIN_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


@app.post("/api/rebuild-cache")
async def api_rebuild_cache(_=Depends(_require_api_key)):
    """手动触发 metrics_cache 重建（后台线程执行）。"""
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, data.build_metrics_cache)
    return {"status": "rebuild_started"}


@app.get("/api/health")
async def api_health():
    ts = _get_ts_store()
    from src import source_health
    return {
        "status": "ok",
        "redis": _redis_available(),
        "trading_hours": cfg.is_trading_hours(),
        "time": clock.now().strftime("%H:%M:%S"),
        "polling_interval": _warmup_interval(),
        "data_freshness": {
            "spot_last": _last_spot_update,
            "index_sh000001": ts.index_daily_freshness("sh000001"),
            "index_sz399001": ts.index_daily_freshness("sz399001"),
        },
        "source_health": source_health.status(),
        "clock": clock.status(),
    }


@app.get("/api/test-830000")
async def api_test_830000(_=Depends(_require_api_key)):
    """测试获取830000平均股价指数。"""
    results = {}
    for code in ["830000", "883958"]:
        try:
            df = data.get_ths_index_daily(code, days=10)
            if df.empty:
                results[code] = {"status": "empty", "rows": 0, "columns": []}
            else:
                results[code] = {
                    "status": "ok",
                    "rows": len(df),
                    "columns": list(df.columns),
                    "last_row": df.iloc[-1].to_dict() if not df.empty else None,
                }
        except Exception as e:
            results[code] = {"status": "error", "error": str(e)}
    return results


@app.get("/api/test-avg-price")
async def api_test_avg_price(_=Depends(_require_api_key)):
    """测试平均股价K线数据。"""
    from src import index_timing as it
    try:
        df = it.avg_price_kline(days=10)
        if df.empty:
            return {"status": "empty", "rows": 0}
        return {
            "status": "ok",
            "rows": len(df),
            "columns": list(df.columns),
            "last_row": df.iloc[-1].to_dict() if not df.empty else None,
            "first_row": df.iloc[0].to_dict() if not df.empty else None,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/api/test-signals")
async def api_test_signals(_=Depends(_require_api_key)):
    """测试多空转信号检测。"""
    from src import index_timing as it
    import pandas as pd

    # Clear caches to get fresh data
    data.clear_cache("avg_price_kline")
    data.clear_cache("all_signals")

    try:
        signals = it.all_signals()
        sorted_dates = sorted(signals.keys())[-5:]
        recent = {d: signals[d] for d in sorted_dates}

        # Check MA10 crossing for today (with real-time data)
        avg = it.avg_price_kline(days=20)
        ma10_detail = None
        if not avg.empty and len(avg) >= 11:
            avg = avg.reset_index(drop=True)
            # Append today's real-time avg price
            import time as _time
            today_str = _time.strftime("%Y-%m-%d")
            if str(avg["日期"].iloc[-1]) != today_str:
                rt = data.get_realtime_avg_price()
                rt_val = rt.get("avg_price")
                if rt_val:
                    prev_close = float(avg["收盘"].iloc[-1])
                    today_row = pd.DataFrame([{
                        "日期": today_str,
                        "开盘": round(prev_close, 2),
                        "最高": round(max(prev_close, rt_val), 2),
                        "最低": round(min(prev_close, rt_val), 2),
                        "收盘": round(rt_val, 2),
                    }])
                    avg = pd.concat([avg, today_row], ignore_index=True)

            ma10 = avg["收盘"].rolling(10, min_periods=10).mean()
            last = avg.iloc[-1]
            prev = avg.iloc[-2]
            ma10_detail = {
                "today_date": str(last["日期"]),
                "today_close": float(last["收盘"]),
                "today_ma10": float(ma10.iloc[-1]) if pd.notna(ma10.iloc[-1]) else None,
                "yesterday_close": float(prev["收盘"]),
                "yesterday_ma10": float(ma10.iloc[-2]) if pd.notna(ma10.iloc[-2]) else None,
                "crossed_above": bool(
                    prev["收盘"] <= ma10.iloc[-2] and last["收盘"] > ma10.iloc[-1]
                ) if (pd.notna(ma10.iloc[-1]) and pd.notna(ma10.iloc[-2])) else None,
            }

        return {
            "status": "ok",
            "total_signals": len(signals),
            "recent_5": recent,
            "ma10_detail": ma10_detail,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/api/test-avg-status")
async def api_test_avg_status(_=Depends(_require_api_key)):
    """测试平均股价状态（AI judge 使用的信息）。"""
    from src import index_timing as it
    try:
        status = it._avg_price_status()
        return {"status": "ok", "avg_price_status": status}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/rejudge")
async def api_rejudge(_=Depends(_require_api_key)):
    """强制重新运行 AI 指数择时判断（使用最新平均股价数据）。"""
    from src import index_timing as it
    try:
        result = it.ai_judge(force=True)
        return {"status": "ok", "result": result}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/rejudge-emotion")
async def api_rejudge_emotion(_=Depends(_require_api_key)):
    """强制重新运行情绪节点 AI 判断（使用最新规则）。"""
    from src import emotion_node as en
    try:
        result = en.ai_judge(force=True)
        return {"status": "ok", "result": result}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/api/freshness")
async def api_freshness():
    """数据新鲜度详情：各指数parquet最新日期 + spot最后更新时间。"""
    ts = _get_ts_store()
    return {
        "spot_last": _last_spot_update,
        "indices": {
            sym: ts.index_daily_freshness(sym)
            for sym in ("sh000001", "sz399001", "sz399006", "sh000688")
        },
        "ths_indices": {
            code: ts.index_daily_freshness(code)
            for code in ("883958", "883902")
        },
    }


@app.get("/api/spot")
async def api_spot():
    df = await async_fetch.get_stock_spot_fast_async(_http_client)
    return df.to_dict("records")


@app.get("/api/index")
async def api_index():
    df = await async_fetch.sina_index_spot_async(_http_client)
    return df.to_dict("records")


@app.get("/api/hot")
async def api_hot():
    spot_key = _cache_key("get_stock_spot", (), {})
    spot_df = redis_get(spot_key)
    df = await async_fetch.get_hot_stocks_async(
        _http_client, spot_df=spot_df, top=10)
    return df.to_dict("records")


@app.get("/api/quote/{code}")
async def api_quote(code: str):
    return await async_fetch.get_stock_quote_fast_async(_http_client, code)


@app.get("/api/avg_price")
async def api_avg_price():
    return await async_fetch.get_realtime_avg_price_async(_http_client)


@app.get("/api/trade-calendar")
async def api_trade_calendar():
    """A股交易日历。"""
    dates = data.get_trade_calendar()
    return {"count": len(dates), "first": dates[0] if dates else None,
            "last": dates[-1] if dates else None, "dates": dates}


@app.get("/api/securities")
async def api_securities(date: str | None = None):
    """证券主数据快照（代码/名称/ST状态）。date 为空返回当日，否则返回时点股票池。"""
    if date is None:
        df = data.get_securities_snapshot()
    else:
        df = data.get_point_in_time_pool(date)
    st_count = int((df["st_status"] == "ST").sum()) if "st_status" in df.columns else 0
    return {"date": date or "today", "count": len(df), "st_count": st_count,
            "stocks": df.to_dict("records")}


@app.get("/api/index_daily/{symbol}")
async def api_index_daily(symbol: str, days: int = 380):
    df = await async_fetch.get_index_daily_async(_http_client, symbol, days)
    return df.to_dict("records")


@app.get("/metrics")
async def api_prometheus_metrics(_=Depends(_require_api_key)):
    """Prometheus 指标端点，供 Grafana/Prometheus 采集。"""
    from fastapi.responses import PlainTextResponse
    try:
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
        return PlainTextResponse(
            generate_latest().decode("utf-8"),
            media_type=CONTENT_TYPE_LATEST,
        )
    except Exception:
        return PlainTextResponse("# prometheus_client not available\n")


@app.get("/api/task/{task_id}")
async def api_task_status(task_id: str, _=Depends(_require_api_key)):
    """查询 Celery 异步任务状态。"""
    return get_task_status(task_id)


@app.post("/api/task/pull_history")
async def api_pull_history(
    symbols: list[str] = Body(..., max_length=200),
    days: int = Body(365, le=1000),
    _=Depends(_require_api_key),
):
    """手动触发批量K线拉取任务（symbols≤200, days≤1000）。"""
    task_id = submit(pull_stock_history, symbols, days)
    return {"task_id": task_id, "status": "submitted" if task_id else "celery_disabled"}


@app.post("/api/task/eval_report")
async def api_eval_report(_=Depends(_require_api_key)):
    """手动触发评测报告生成任务。"""
    task_id = submit(generate_eval_report)
    return {"task_id": task_id, "status": "submitted" if task_id else "celery_disabled"}


# ── WebSocket endpoints ────────────────────────────────────────────────────
def _ws_origin_allowed(websocket: WebSocket) -> bool:
    """浏览器 WS 不受 CORS 约束，必须手动校验 Origin，防止跨站页面连本机。"""
    origin = websocket.headers.get("origin", "")
    if not origin:
        return True  # 非浏览器客户端（无 Origin 头），受 127.0.0.1 绑定保护
    return origin.rstrip("/") in {o.rstrip("/") for o in cfg.CORS_ORIGINS}


async def _ws_reject(websocket: WebSocket) -> None:
    await websocket.close(code=4403, reason="Origin not allowed")


@app.websocket("/ws/market")
async def ws_market(websocket: WebSocket):
    if not _ws_origin_allowed(websocket):
        return await _ws_reject(websocket)
    await websocket.accept()
    try:
        # 首帧：立即从 Redis 缓存推一帧，前端不用等下一个广播周期
        try:
            spot_key = _cache_key("get_stock_spot", (), {})
            spot_df = redis_get(spot_key)
            if spot_df is not None and len(spot_df) > 1000:
                prices = pd.to_numeric(spot_df["最新价"], errors="coerce")
                idx_df = await async_fetch.sina_index_spot_async(_http_client) \
                    if _http_client else pd.DataFrame()
                await websocket.send_json({
                    "type": "market",
                    "avg_price": round(float(prices.mean()), 3) if len(prices) else None,
                    "stock_count": int(len(prices)),
                    "indices": idx_df.to_dict("records") if not idx_df.empty else [],
                    "timestamp": time.strftime("%H:%M:%S"),
                })
        except Exception as e:
            logger.debug("WS market first frame failed: %s", e)
        # 之后只消费广播频道——客户端数量不再放大上游请求
        async for msg in ws_hub.iter_quotes():
            if msg.get("type") == "market":
                await websocket.send_json(msg)
    except WebSocketDisconnect:
        logger.debug("WS market client disconnected")


@app.websocket("/ws/quotes/{code}")
async def ws_quotes(websocket: WebSocket, code: str):
    if not _ws_origin_allowed(websocket):
        return await _ws_reject(websocket)
    await websocket.accept()
    try:
        # 首帧
        try:
            q = await async_fetch.get_stock_quote_fast_async(_http_client, code) \
                if _http_client else {}
            if q:
                await websocket.send_json({"type": "quote", "code": code, "data": q,
                                           "timestamp": time.strftime("%H:%M:%S")})
        except Exception as e:
            logger.debug("WS quotes first frame failed: %s", e)
        async for msg in ws_hub.iter_quotes():
            if msg.get("type") == "quote" and msg.get("code") == code:
                await websocket.send_json(msg)
    except WebSocketDisconnect:
        logger.debug("WS quotes client disconnected")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=cfg.FASTAPI_HOST, port=cfg.FASTAPI_PORT)
