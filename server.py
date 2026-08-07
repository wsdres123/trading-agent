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
    # 预热 LLM 连接（复用 httpx 连接池）
    try:
        from src.llm_gateway import _get_client
        _get_client()
    except Exception:
        pass
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


def _auth_user(authorization: str | None = Header(default=None)) -> str | None:
    """从 Authorization: Bearer <token> 提取用户名。"""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    from src import auth
    token = authorization[7:]
    user = auth.get_session_user(token)
    if user:
        auth.touch_session(token)
    return user


def _require_auth(authorization: str | None = Header(default=None)) -> str:
    """要求登录，返回用户名。"""
    user = _auth_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话过期")
    return user


@app.get("/api/spot")
async def api_spot(user: str = Depends(_require_auth)):
    df = await async_fetch.get_stock_spot_fast_async(_http_client)
    return df.to_dict("records")


@app.get("/api/index")
async def api_index(user: str = Depends(_require_auth)):
    df = await async_fetch.sina_index_spot_async(_http_client)
    return df.to_dict("records")


@app.get("/api/hot")
async def api_hot(user: str = Depends(_require_auth)):
    spot_key = _cache_key("get_stock_spot", (), {})
    spot_df = redis_get(spot_key)
    df = await async_fetch.get_hot_stocks_async(
        _http_client, spot_df=spot_df, top=10)
    return df.to_dict("records")


@app.get("/api/quote/{code}")
async def api_quote(code: str, user: str = Depends(_require_auth)):
    return await async_fetch.get_stock_quote_fast_async(_http_client, code)


@app.get("/api/avg_price")
async def api_avg_price(user: str = Depends(_require_auth)):
    return await async_fetch.get_realtime_avg_price_async(_http_client)


@app.get("/api/trade-calendar")
async def api_trade_calendar(user: str = Depends(_require_auth)):
    """A股交易日历。"""
    dates = data.get_trade_calendar()
    return {"count": len(dates), "first": dates[0] if dates else None,
            "last": dates[-1] if dates else None, "dates": dates}


@app.get("/api/securities")
async def api_securities(date: str | None = None, user: str = Depends(_require_auth)):
    """证券主数据快照（代码/名称/ST状态）。date 为空返回当日，否则返回时点股票池。"""
    if date is None:
        df = data.get_securities_snapshot()
    else:
        df = data.get_point_in_time_pool(date)
    st_count = int((df["st_status"] == "ST").sum()) if "st_status" in df.columns else 0
    return {"date": date or "today", "count": len(df), "st_count": st_count,
            "stocks": df.to_dict("records")}


@app.get("/api/index_daily/{symbol}")
async def api_index_daily(symbol: str, days: int = 380, user: str = Depends(_require_auth)):
    # 同花顺指数（88xxxx）走已实现的同花顺指数接口
    if symbol.startswith("88"):
        def _ths_fetch():
            from src import data
            df = data.get_ths_index_daily(symbol, days=days)
            return df
        df = await asyncio.to_thread(_ths_fetch)
    else:
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


# ── Auth endpoints (React frontend) ─────────────────────────────────────────


@app.post("/api/login")
async def api_login(body: dict = Body(...)):
    from src import auth
    username = body.get("username", "")
    password = body.get("password", "")
    if auth.is_locked(username):
        raise HTTPException(
            status_code=429,
            detail="账户已锁定，请15分钟后重试",
            headers={"Retry-After": str(auth.LOCK_SECONDS)},
        )
    user = auth.verify(username, password)
    if not user:
        if auth.is_locked(username):
            raise HTTPException(
                status_code=429,
                detail="连续失败过多，账户已锁定15分钟",
                headers={"Retry-After": str(auth.LOCK_SECONDS)},
            )
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = auth.create_session(user)
    return {"token": token, "user": user}


@app.post("/api/logout")
async def api_logout(authorization: str | None = Header(default=None)):
    from src import auth
    token = (authorization or "").replace("Bearer ", "")
    auth.delete_session(token)
    return {"status": "ok"}


@app.get("/api/me")
async def api_me(user: str = Depends(_require_auth)):
    from src import auth
    users = auth.list_users()
    role = next((u["role"] for u in users if u["username"] == user), "user")
    return {"user": user, "role": role}


# ── Timing / Emotion / Market data (React frontend) ────────────────────────
@app.get("/api/timing")
async def api_timing(user: str = Depends(_require_auth)):
    """今日指数择时判断。"""
    import asyncio, time as _time
    from src import index_timing as it

    today = _time.strftime("%Y-%m-%d")
    rec = await asyncio.to_thread(it.load_ai_predictions)
    today_rec = rec.get(today, {}) if rec else {}
    turnover = await asyncio.to_thread(it.market_turnover_wanyi)
    signals = await asyncio.to_thread(it.all_signals)
    hint = it.pattern_hint(signals) if signals else ""
    hist = await asyncio.to_thread(it.load_history_signals)
    latest = None
    if hist is not None and not hist.empty:
        row = hist.iloc[-1]
        latest = {"日期": str(row.get("日期", "")), "信号": str(row.get("信号", "")),
                   "中级周期": str(row.get("中级周期", "")), "情绪周期": str(row.get("情绪周期", "")),
                   "指数": str(row.get("指数", ""))}

    if it.should_auto_judge() and not today_rec:
        await asyncio.to_thread(it.ai_judge)
        rec = await asyncio.to_thread(it.load_ai_predictions)
        today_rec = rec.get(today, {}) if rec else {}

    return {
        "signal": today_rec.get("signal", ""),
        "mid_cycle": today_rec.get("mid_cycle", "-"),
        "position": today_rec.get("position", "-"),
        "reason": today_rec.get("reason", ""),
        "time": today_rec.get("time", ""),
        "turnover": turnover,
        "pattern_hint": hint,
        "latest_review": latest,
    }


@app.post("/api/timing/judge")
async def api_timing_judge(user: str = Depends(_require_auth)):
    import asyncio
    from src import index_timing as it
    result = await asyncio.to_thread(it.ai_judge, True)
    if result.get("error"):
        raise HTTPException(status_code=500, detail=result["error"])
    return {"status": "ok", "result": result}


@app.get("/api/timing/signals")
async def api_timing_signals(user: str = Depends(_require_auth)):
    import asyncio
    from src import index_timing as it
    signals = await asyncio.to_thread(it.all_signals)
    return {"signals": signals}


@app.get("/api/emotion")
async def api_emotion(user: str = Depends(_require_auth)):
    import asyncio, time as _time
    from src import emotion_node as en

    today = _time.strftime("%Y-%m-%d")
    recs = await asyncio.to_thread(en.load_predictions)
    today_rec = recs.get(today, {}) if recs else {}
    daban = await asyncio.to_thread(en.daban_damian_count)

    if en.should_auto_judge() and not today_rec:
        await asyncio.to_thread(en.ai_judge)
        recs = await asyncio.to_thread(en.load_predictions)
        today_rec = recs.get(today, {}) if recs else {}

    return {
        "node": today_rec.get("node", ""),
        "stats": today_rec.get("stats", {}),
        "prev_stats": today_rec.get("prev_stats", {}),
        "reason": today_rec.get("reason", ""),
        "advice": today_rec.get("advice", ""),
        "time": today_rec.get("time", ""),
        "daban_damian": daban,
    }


@app.post("/api/emotion/judge")
async def api_emotion_judge(user: str = Depends(_require_auth)):
    import asyncio
    from src import emotion_node as en
    result = await asyncio.to_thread(en.ai_judge, True)
    if result.get("error"):
        raise HTTPException(status_code=500, detail=result["error"])
    return {"status": "ok", "result": result}


@app.get("/api/avg_price_kline")
async def api_avg_price_kline(days: int = 360, user: str = Depends(_require_auth)):
    import asyncio
    from src import index_timing as it
    df = await asyncio.to_thread(it.avg_price_kline, days)
    if df is None or df.empty:
        return {"data": []}
    df = df.fillna(0)
    cols = {c: c for c in df.columns}
    records = df.rename(columns=cols).to_dict("records")
    for r in records:
        for k, v in r.items():
            if hasattr(v, "item"):
                r[k] = v.item()
            elif isinstance(v, float) and (v != v):
                r[k] = None
    return {"data": records}


@app.get("/api/market_stats")
async def api_market_stats(user: str = Depends(_require_auth)):
    import asyncio, time as _time
    from src import emotion_node as en
    daban = await asyncio.to_thread(en.daban_damian_count)
    today = _time.strftime("%Y-%m-%d")
    recs = await asyncio.to_thread(en.load_predictions)
    today_rec = recs.get(today, {}) if recs else {}
    stats = today_rec.get("stats", {})
    return {"打板大面数": daban, **stats}


@app.get("/api/market_turnover")
async def api_market_turnover(user: str = Depends(_require_auth)):
    import asyncio
    from src import index_timing as it
    to = await asyncio.to_thread(it.market_turnover_wanyi)
    return {"turnover": to}


# ── Chat SSE ────────────────────────────────────────────────────────────────
from fastapi.responses import StreamingResponse


@app.post("/api/chat/stream")
async def api_chat_stream(body: dict = Body(...), user: str = Depends(_require_auth)):
    from src import ai_assistant as ai
    question = body.get("question", "")
    history = body.get("history", [])

    async def gen():
        loop = asyncio.get_event_loop()
        import queue, threading
        q: queue.Queue = queue.Queue()
        done = object()
        def _worker():
            try:
                for chunk in ai.chat_stream(question, history=history):
                    q.put(chunk)
            except Exception as e:
                q.put(e)
            finally:
                q.put(done)
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        while True:
            item = await loop.run_in_executor(None, q.get)
            if item is done:
                break
            if isinstance(item, Exception):
                yield f"data: [ERROR: {item}]\n"
                break
            yield f"data: {item}\n"

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


# ── Groups (React frontend) ─────────────────────────────────────────────────
@app.get("/api/groups")
async def api_groups(user: str = Depends(_require_auth)):
    from src import stock_filter as sf
    groups = sf.list_groups(user)
    return groups


@app.post("/api/groups")
async def api_groups_action(body: dict = Body(...), user: str = Depends(_require_auth)):
    from src import stock_filter as sf
    action = body.get("action", "")
    name = body.get("name", "")
    if action == "save":
        conditions = body.get("conditions", [])
        stocks = body.get("stocks", [])
        sf.save_group(name, conditions, stocks, username=user)
        return {"status": "ok"}
    elif action == "update":
        result = sf.update_group(name, username=user)
        if not result:
            raise HTTPException(status_code=500, detail="更新失败")
        return {"status": "ok", "group": result}
    elif action == "delete":
        sf.delete_group(name, username=user)
        return {"status": "ok"}
    raise HTTPException(status_code=400, detail="unknown action")


# ── Filter (React frontend) ────────────────────────────────────────────────
@app.post("/api/filter")
async def api_filter(body: dict = Body(...), user: str = Depends(_require_auth)):
    import asyncio
    from src import data, stock_filter as sf, ai_assistant as ai
    action = body.get("action", "")

    if action == "cache_status":
        return await asyncio.to_thread(data.metrics_cache_status)
    elif action == "parse":
        desc = body.get("desc", "")
        parsed = await asyncio.to_thread(ai.parse_filter_conditions, desc)
        # Add labels
        from pages.shared import cond_labels
        for c in parsed.get("conditions", []):
            c["label"] = None
        labels = cond_labels(parsed.get("conditions", []))
        for c, l in zip(parsed.get("conditions", []), labels):
            c["label"] = l
        return parsed
    elif action == "exec":
        conditions = body.get("conditions", [])
        if data.load_metrics_cache(allow_stale=True) is None:
            await asyncio.to_thread(data.build_metrics_cache)
        res = await asyncio.to_thread(sf.run_filter, conditions)
        if not res.empty:
            res = await asyncio.to_thread(sf.finalize_results, res)
        res = res.fillna(0)
        records = res.to_dict("records")
        for r in records:
            for k, v in r.items():
                if hasattr(v, "item"):
                    r[k] = v.item()
                elif isinstance(v, float) and (v != v):
                    r[k] = None
        return {"stocks": records}
    raise HTTPException(status_code=400, detail="unknown action")


# ── Short term / Theme / Single stock (React frontend) ─────────────────────
@app.post("/api/short_term")
async def api_short_term(body: dict = Body(...), user: str = Depends(_require_auth)):
    import asyncio
    from src import short_term as stm
    date_str = body.get("date", "")
    result = await asyncio.to_thread(stm.detect, date_str)
    return _serialize_result(result)


@app.post("/api/theme")
async def api_theme(body: dict = Body(...), user: str = Depends(_require_auth)):
    import asyncio
    from src import theme_mode as tm
    start = body.get("start", "")
    end = body.get("end", "")
    result = await asyncio.to_thread(tm.detect, start, end)
    return _serialize_result(result)


@app.post("/api/theme/ai")
async def api_theme_ai(body: dict = Body(...), user: str = Depends(_require_auth)):
    import asyncio
    from src import theme_mode as tm
    data = body.get("data", {})
    text = await asyncio.to_thread(tm.ai_analyze, data)
    return {"text": text}


@app.get("/api/theme/capacity_limit")
async def api_theme_capacity_limit(user: str = Depends(_require_auth)):
    """昨日容量涨停次日观察：昨日涨停 + 成交额>30亿 + 流通市值>200亿，今日表现。"""
    def _run():
        try:
            import pandas as pd
            from datetime import datetime, timedelta
            from src import ths_data
            from src.short_term import get_zt_pool_ths

            # 昨日（跳周末）
            today = datetime.now()
            offset = 1
            while True:
                yd_dt = today - timedelta(days=offset)
                if yd_dt.weekday() < 5:
                    break
                offset += 1
            yd = yd_dt.strftime("%Y-%m-%d")

            # 用同花顺涨停池（当日数据，今天调用取今天，需要昨日的历史数据）
            # 同花顺 limit-up-pool 只有当日，用 akshare 作为昨日数据源
            try:
                import akshare as ak
                df = ak.stock_zt_pool_em(date=yd.replace("-", ""))
            except Exception as e:
                return {"date": yd, "stocks": [], "error": f"昨日涨停池获取失败: {e}"}
            if df is None or df.empty:
                return {"date": yd, "stocks": [], "error": "昨日涨停池为空"}

            # 列名归一
            col_map = {}
            for c in df.columns:
                cl = c.strip()
                if "代码" in cl: col_map[c] = "代码"
                elif "名称" in cl: col_map[c] = "名称"
                elif "成交额" in cl: col_map[c] = "成交额"
                elif "流通市值" in cl: col_map[c] = "流通市值"
                elif "连板" in cl: col_map[c] = "连板数"
                elif "所属行业" in cl or "行业" in cl: col_map[c] = "所属行业"
            df = df.rename(columns=col_map)
            for col in ("成交额", "流通市值"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

            # 筛选：成交额>30亿 + 流通市值>200亿
            mask = pd.Series([True] * len(df), index=df.index)
            if "成交额" in df.columns:
                mask &= df["成交额"] > 30e8
            if "流通市值" in df.columns:
                mask &= df["流通市值"] > 200e8
            df = df[mask].copy()
            if df.empty:
                return {"date": yd, "stocks": [], "avg_up": None, "avg_down": None,
                        "up_count": 0, "down_count": 0, "total": 0}

            codes = [str(r).zfill(6) for r in df["代码"].tolist()]

            # 用同花顺 snapshot_batch 批量取今日行情
            snap = ths_data.snapshot_batch(codes)
            snap_map: dict = {}
            if not snap.empty:
                for _, sr in snap.iterrows():
                    snap_map[str(sr.get("代码", "")).zfill(6)] = sr

            rows = []
            for _, r in df.iterrows():
                code = str(r.get("代码", "")).zfill(6)
                sr = snap_map.get(code, {})
                pct = float(sr["涨跌幅"]) if hasattr(sr, "__getitem__") and sr.get("涨跌幅") is not None else None
                rows.append({
                    "代码": code,
                    "名称": str(r.get("名称", "")),
                    "昨成交额(亿)": round(float(r["成交额"]) / 1e8, 1) if pd.notna(r.get("成交额")) else None,
                    "流通市值(亿)": round(float(r["流通市值"]) / 1e8, 0) if pd.notna(r.get("流通市值")) else None,
                    "连板数": int(r["连板数"]) if "连板数" in r.index and pd.notna(r.get("连板数")) else 1,
                    "所属行业": str(r.get("所属行业", "")),
                    "今日涨跌幅": pct,
                    "今日最新价": float(sr["最新价"]) if hasattr(sr, "__getitem__") and sr.get("最新价") is not None else None,
                })
            pcts = [x["今日涨跌幅"] for x in rows if x["今日涨跌幅"] is not None]
            up_count = sum(1 for p in pcts if p > 0)
            down_count = sum(1 for p in pcts if p <= 0)
            avg_up = round(sum(p for p in pcts if p > 0) / up_count, 2) if up_count else None
            avg_down = round(sum(p for p in pcts if p <= 0) / down_count, 2) if down_count else None
            return {
                "date": yd,
                "stocks": sorted(rows, key=lambda x: -(x["今日涨跌幅"] or -999)),
                "avg_up": avg_up, "avg_down": avg_down,
                "up_count": up_count, "down_count": down_count, "total": len(rows),
            }
        except Exception as e:
            return {"date": "", "stocks": [], "error": str(e)}

    return await asyncio.to_thread(_run)



@app.post("/api/single_stock")
async def api_single_stock(body: dict = Body(...), user: str = Depends(_require_auth)):
    import asyncio
    from src import single_stock as ss
    date_str = body.get("date", "")
    result = await asyncio.to_thread(ss.run, date_str)
    return _serialize_result(result)


def _serialize_result(obj):
    """递归序列化含 DataFrame/numpy 的结果为 JSON 安全的 dict。"""
    import pandas as pd
    import numpy as np
    if obj is None:
        return None
    if isinstance(obj, dict):
        return {k: _serialize_result(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize_result(v) for v in obj]
    if isinstance(obj, pd.DataFrame):
        obj = obj.fillna(0)
        return obj.to_dict("records")
    if isinstance(obj, pd.Series):
        return obj.to_dict()
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, float) and obj != obj:
        return None
    return obj


# ── Eval run (React frontend) ──────────────────────────────────────────────
@app.get("/api/data_status")
async def api_data_status(user: str = Depends(_require_auth)):
    """检查评测数据文件是否存在及行数。"""
    from pathlib import Path
    base = Path(__file__).parent / "eval" / "datasets"
    files = {
        "情绪节点": "emotion_cases.jsonl",
        "指数择时": "timing_cases.jsonl",
        "筛选NLP": "filter_nlp.jsonl",
        "RAG检索": "rag_queries.jsonl",
    }
    result = {}
    for name, fname in files.items():
        p = base / fname
        if p.exists():
            with open(p, encoding="utf-8") as f:
                result[name] = sum(1 for _ in f)
        else:
            result[name] = 0
    return result


@app.post("/api/eval/run")
async def api_eval_run(body: dict = Body(...), user: str = Depends(_require_auth)):
    import asyncio, importlib as il
    mode = body.get("mode", "no_llm")
    modules_map = {
        "no_llm": [
            ("数据准确性", "eval.test_data_accuracy"),
            ("情绪节点", "eval.test_emotion"),
            ("指数择时", "eval.test_timing"),
            ("输出可靠性", "eval.test_output_reliability"),
            ("真实交易质量", "eval.test_trading_quality"),
            ("筛选NLP解析", "eval.test_filter_nlp"),
            ("RAG检索质量", "eval.test_rag_recall"),
            ("性能基准", "eval.test_benchmark"),
        ],
        "all": [
            ("LLM输出质量", "eval.test_llm_quality"),
            ("工具路由", "eval.test_tool_routing"),
        ],
        "filter": [("筛选NLP解析", "eval.test_filter_nlp")],
    }
    if mode == "filter":
        modules = modules_map["filter"]
    elif mode == "all":
        modules = modules_map["no_llm"] + modules_map["all"]
    else:
        modules = modules_map["no_llm"]

    results = {}
    for name, mod_path in modules:
        try:
            mod = il.import_module(mod_path)
            r = await asyncio.to_thread(mod.run)
            results[name] = r
        except Exception as e:
            results[name] = {"error": str(e), "pass": 0, "fail": 0}
    return {"results": _serialize_result(results)}


# ── WebSocket endpoints ────────────────────────────────────────────────────
def _ws_origin_allowed(websocket: WebSocket) -> bool:
    """浏览器 WS 不受 CORS 约束，必须手动校验 Origin，防止跨站页面连本机。"""
    origin = websocket.headers.get("origin", "")
    if not origin:
        return False
    return origin.rstrip("/") in {o.rstrip("/") for o in cfg.CORS_ORIGINS}


async def _ws_reject(websocket: WebSocket) -> None:
    await websocket.close(code=4403, reason="Origin not allowed")


def _ws_check_auth(websocket: WebSocket) -> bool:
    """Validate auth token from query param before WS accept."""
    from src import auth
    token = websocket.query_params.get("token", "")
    if not token:
        return False
    user = auth.get_session_user(token)
    if user:
        auth.touch_session(token)
        return True
    return False


@app.websocket("/ws/market")
async def ws_market(websocket: WebSocket):
    if not _ws_origin_allowed(websocket) or not _ws_check_auth(websocket):
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
    if not _ws_origin_allowed(websocket) or not _ws_check_auth(websocket):
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


# ── React SPA serving (production) ─────────────────────────────────────────
from pathlib import Path as _Path
_frontend_dist = _Path(__file__).parent / "frontend" / "dist"
if _frontend_dist.exists():
    from fastapi.responses import FileResponse as _FileResponse
    _index_html = _frontend_dist / "index.html"

    @app.get("/{full_path:path}")
    async def _spa_fallback(full_path: str):
        candidate = _frontend_dist / full_path
        if full_path and candidate.is_file():
            return _FileResponse(str(candidate))
        return _FileResponse(str(_index_html))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=cfg.FASTAPI_HOST, port=cfg.FASTAPI_PORT)
