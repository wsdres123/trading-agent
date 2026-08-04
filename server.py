"""FastAPI async server: background cache warming + REST + WebSocket.

Runs alongside Streamlit on port 8602.
Background tasks pre-populate Redis during trading hours
→ data.py's @ttl_cache gets Redis hits → Streamlit fast path.
预热频率自适应：竞价10s / 盘中60s / 午休跳过 / 尾盘15s / 非交易300s。
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
import pandas as pd
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from config import settings as cfg
from src.redis_cache import _cache_key, redis_get, redis_set, _redis_available
from src import async_fetch
from src import data
from src.ts_store import get_store as _get_ts_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("server")

_http_client: httpx.AsyncClient | None = None
_last_spot_update: str | None = None


def _warmup_interval() -> int:
    """根据交易时段返回预热间隔（秒）。午休返回 0 表示跳过。"""
    now = datetime.now()
    if now.weekday() >= 5:
        return 300
    m = now.hour * 60 + now.minute
    if 565 <= m <= 570:   return 10    # 9:25-9:30 竞价
    if 570 <= m <= 690:   return 30    # 9:30-11:30 上午盘
    if 690 <= m <= 780:   return 0     # 11:30-13:00 午休，跳过
    if 780 <= m <= 890:   return 30    # 13:00-14:50 下午盘
    if 890 <= m <= 905:   return 15    # 14:50-15:05 尾盘
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
    tasks = [
        asyncio.create_task(_bg_refresh_spot()),
        asyncio.create_task(_bg_refresh_index()),
        asyncio.create_task(_bg_refresh_hot()),
        asyncio.create_task(_bg_refresh_index_daily()),
        asyncio.create_task(_bg_refresh_ths_index_daily()),
        asyncio.create_task(_bg_refresh_health()),
        asyncio.create_task(_bg_save_daily_snapshot()),
    ]
    logger.info("FastAPI server started, %d background tasks", len(tasks))
    yield
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await _http_client.aclose()
    _http_client = None


app = FastAPI(title="劫财AI交易 Backend", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Background tasks: pre-populate Redis during trading hours ──────────────
async def _bg_refresh_spot():
    global _last_spot_update
    while True:
        interval = _warmup_interval()
        if interval > 0 and _http_client:
            try:
                df = await async_fetch.get_stock_spot_fast_async(_http_client)
                if len(df) > 1000:
                    key = _cache_key("get_stock_spot", (), {})
                    redis_set(key, df, cfg.SPOT_TTL)
                    _last_spot_update = datetime.now().isoformat(timespec="seconds")
                    logger.debug("Spot cache warmed: %d rows", len(df))
            except Exception as e:
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
                    df = await async_fetch.get_index_daily_async(
                        _http_client, sym, 1060)
                    key = _cache_key("get_index_daily", (sym,), {"days": 1060})
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
            h = data.health()
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
        if (now.weekday() < 5
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


# ── REST endpoints ─────────────────────────────────────────────────────────
@app.get("/api/health")
async def api_health():
    ts = _get_ts_store()
    return {
        "status": "ok",
        "redis": _redis_available(),
        "trading_hours": cfg.is_trading_hours(),
        "time": time.strftime("%H:%M:%S"),
        "polling_interval": _warmup_interval(),
        "data_freshness": {
            "spot_last": _last_spot_update,
            "index_sh000001": ts.index_daily_freshness("sh000001"),
            "index_sz399001": ts.index_daily_freshness("sz399001"),
        },
    }


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


@app.get("/api/index_daily/{symbol}")
async def api_index_daily(symbol: str, days: int = 380):
    df = await async_fetch.get_index_daily_async(_http_client, symbol, days)
    return df.to_dict("records")


# ── WebSocket endpoints ────────────────────────────────────────────────────
@app.websocket("/ws/market")
async def ws_market(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            interval = _warmup_interval()
            if interval > 0 and _http_client:
                try:
                    spot, idx = await asyncio.gather(
                        async_fetch.get_stock_spot_fast_async(_http_client),
                        async_fetch.sina_index_spot_async(_http_client),
                    )
                    prices = pd.to_numeric(
                        spot["最新价"], errors="coerce") if not spot.empty \
                        else pd.Series([], dtype=float)
                    await websocket.send_json({
                        "type": "market",
                        "avg_price": round(float(prices.mean()), 3) if len(prices) else None,
                        "stock_count": int(len(prices)),
                        "indices": idx.to_dict("records") if not idx.empty else [],
                        "timestamp": time.strftime("%H:%M:%S"),
                    })
                except Exception as e:
                    logger.warning("WS market push failed: %s", e)
            await asyncio.sleep(5 if 0 < interval <= 60 else 30)
    except WebSocketDisconnect:
        logger.info("WS market client disconnected")


@app.websocket("/ws/quotes/{code}")
async def ws_quotes(websocket: WebSocket, code: str):
    await websocket.accept()
    try:
        while True:
            interval = _warmup_interval()
            if interval > 0 and _http_client:
                try:
                    q = await async_fetch.get_stock_quote_fast_async(
                        _http_client, code)
                    if q:
                        await websocket.send_json({
                            "type": "quote",
                            "code": code,
                            "data": q,
                            "timestamp": time.strftime("%H:%M:%S"),
                        })
                except Exception as e:
                    logger.warning("WS quote push failed: %s", e)
            await asyncio.sleep(3 if 0 < interval <= 60 else 30)
    except WebSocketDisconnect:
        logger.info("WS quotes client disconnected")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=cfg.FASTAPI_HOST, port=cfg.FASTAPI_PORT)
