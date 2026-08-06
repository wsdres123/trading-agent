"""L2 行情数据源抽象层。

L2_SOURCE=ths 时激活 ThsFuyaoClient：
  用同花顺 Fuyao REST 快照接口（GET /api/a-share/prices/snapshot）做定频轮询，
  对外表现与 WebSocket 推送一致——connect() 是永驻 async 循环，
  subscribe(codes) 更新订阅列表，on_tick / on_snapshot 回调驱动 ws_hub 广播。

  轮询频率：盘中 cfg.THS_POLL_INTERVAL（默认 5s），非交易时段 60s。
  批量拉取：每批最多 cfg.THS_BATCH_SIZE（默认 100）支股票，并发拉取全部批次。
  降级：THS_API_KEY 缺失或连续 5 次失败 → raise ConnectionError，
        server._bg_l2_ws 捕获后降级到原 HTTP 轮询（_l2_active=False）。

L2_SOURCE=eastmoney 时：WS 凭证到位后填充 EastmoneyL2Client（当前占位）。
L2_SOURCE=none/off/空（默认）：不启动 L2，沿用 _bg_refresh_spot HTTP 轮询。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

from config import settings as cfg

logger = logging.getLogger("ws_source")

# ── 可调参数（优先读环境变量，无则用默认值）────────────────────────────────
import os as _os
_POLL_INTERVAL    = int(_os.environ.get("THS_POLL_INTERVAL", "5"))    # 盘中轮询秒数
_IDLE_INTERVAL    = int(_os.environ.get("THS_IDLE_INTERVAL", "60"))   # 非交易时段轮询秒数
_BATCH_SIZE       = int(_os.environ.get("THS_BATCH_SIZE",    "100"))  # 每批快照个数
_MAX_CONSEC_FAIL  = int(_os.environ.get("THS_MAX_FAIL",      "5"))    # 最大连续失败次数


class L2Source:
    """L2 行情数据源抽象基类。

    生命周期：get_l2_source() 工厂构造
        → server._bg_l2_ws 注册 on_tick/on_snapshot
        → connect()（子类实现，永驻直到 close()）
        → subscribe(codes) 更新订阅列表
        → on_tick 回调写 Redis / 广播
        → close()
    """

    def __init__(self):
        self._on_tick: Optional[Callable] = None
        self._on_snapshot: Optional[Callable] = None
        self._connected = False

    async def connect(self) -> None:
        """建立连接 / 启动轮询循环。子类永驻直到 _connected=False 或抛异常。"""
        raise NotImplementedError

    async def subscribe(self, codes: list[str]) -> None:
        """订阅标的行情（可在 connect 后动态调用）。"""
        raise NotImplementedError

    def on_tick(self, callback: Callable) -> None:
        """注册 tick 回调：callback(tick: dict) -> None（可注册多次，覆盖前值）。"""
        self._on_tick = callback

    def on_snapshot(self, callback: Callable) -> None:
        """注册快照回调：callback(snapshots: list[dict]) -> None。"""
        self._on_snapshot = callback

    async def close(self) -> None:
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected


# ── 同花顺 Fuyao REST 快照轮询客户端 ─────────────────────────────────────

class ThsFuyaoClient(L2Source):
    """同花顺 Fuyao REST 快照接口 → 模拟 L2 推送。

    接口文档：https://fuyao.aicubes.cn/docs/api-reference/prices/
    端点：GET /api/a-share/prices/snapshot
    鉴权：Header X-api-key（cfg.THS_API_KEY）
    响应信封：ApiResponse { code, message, data: { item: [...] } }
              业务错误经 code 字段表达，HTTP 状态码恒为 200。

    connect() 永驻：按轮询间隔批量拉取订阅列表快照，
      每支股票触发一次 on_tick(quote_dict)，
      每轮结束后整体触发一次 on_snapshot(list[quote_dict])。
    """

    def __init__(self):
        super().__init__()
        if not cfg.THS_API_KEY:
            raise RuntimeError("THS_API_KEY 未配置，ThsFuyaoClient 无法初始化")
        self._codes: list[str] = []       # 已订阅6位代码列表
        self._lock = asyncio.Lock()       # 保护 _codes 并发修改
        self._consec_fail = 0

    # ── 公开接口 ──────────────────────────────────────────────────────────

    async def subscribe(self, codes: list[str]) -> None:
        """更新订阅代码列表（全量替换）。可在 connect 前或 connect 后调用。"""
        async with self._lock:
            self._codes = [c.strip() for c in codes if c.strip()]
        logger.info("ThsFuyaoClient 订阅 %d 只", len(self._codes))

    async def connect(self) -> None:
        """启动轮询循环，永驻直到 close() 或连续失败超限。"""
        if not cfg.THS_API_KEY:
            raise ConnectionError("THS_API_KEY 未配置，无法启动 Fuyao 轮询")

        self._connected = True
        self._consec_fail = 0
        logger.info("ThsFuyaoClient 轮询启动（间隔 %ds，批次 %d）",
                    _POLL_INTERVAL, _BATCH_SIZE)

        while self._connected:
            interval = _POLL_INTERVAL if self._is_trading_hours() else _IDLE_INTERVAL
            try:
                async with self._lock:
                    codes = list(self._codes)

                if codes:
                    snapshots = await self._fetch_all(codes)
                    if snapshots:
                        self._consec_fail = 0
                        await self._fire(snapshots)
                    else:
                        self._consec_fail += 1
                        logger.warning("ThsFuyaoClient 快照返回空（连续 %d 次）",
                                       self._consec_fail)
                        if self._consec_fail >= _MAX_CONSEC_FAIL:
                            raise ConnectionError(
                                f"连续 {_MAX_CONSEC_FAIL} 次快照为空，放弃重连")

                await asyncio.sleep(interval)

            except ConnectionError:
                self._connected = False
                raise
            except asyncio.CancelledError:
                self._connected = False
                raise
            except Exception as e:
                self._consec_fail += 1
                logger.warning("ThsFuyaoClient 轮询异常（%d/%d）：%s",
                               self._consec_fail, _MAX_CONSEC_FAIL, e)
                if self._consec_fail >= _MAX_CONSEC_FAIL:
                    self._connected = False
                    raise ConnectionError(
                        f"连续 {_MAX_CONSEC_FAIL} 次异常，放弃重连") from e
                await asyncio.sleep(interval)

    async def close(self) -> None:
        self._connected = False
        logger.info("ThsFuyaoClient 轮询停止")

    # ── 内部实现 ──────────────────────────────────────────────────────────

    @staticmethod
    def _is_trading_hours() -> bool:
        """简单判断是否交易时段（9:15-15:05），不依赖 data 避免循环导入。"""
        try:
            from src import data as _data
            return _data.is_trading_day() and _data.cfg.is_trading_hours()
        except Exception:
            h = time.localtime().tm_hour
            return 9 <= h < 15

    async def _fetch_all(self, codes: list[str]) -> list[dict]:
        """并发批次拉取全部代码快照，返回统一格式 quote dict 列表。"""
        from src.ths_data import _to_thscode, _from_thscode, _to_float

        batches = [codes[i:i + _BATCH_SIZE] for i in range(0, len(codes), _BATCH_SIZE)]
        tasks = [self._fetch_batch(b, _to_thscode, _from_thscode, _to_float)
                 for b in batches]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        out: list[dict] = []
        for r in results:
            if isinstance(r, Exception):
                logger.debug("批次快照失败：%s", r)
            elif r:
                out.extend(r)
        return out

    async def _fetch_batch(self, codes: list[str],
                           to_thscode, from_thscode, to_float) -> list[dict]:
        """异步 HTTP GET 一批快照，解析为 quote dict 列表。"""
        import httpx
        thscodes = ",".join(to_thscode(c) for c in codes)
        url = cfg.THS_BASE_URL.rstrip("/") + "/a-share/prices/snapshot"
        headers = {
            "X-api-key": cfg.THS_API_KEY,
            "Accept": "application/json",
        }
        now_ms = int(time.time() * 1000)

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params={"thscodes": thscodes},
                                    headers=headers)
        resp.raise_for_status()
        j = resp.json()

        # ApiResponse 信封：code != 0 为业务错误，HTTP 状态码恒 200
        api_code = j.get("code", -1)
        if api_code == 2001:
            raise ConnectionError("Fuyao API 鉴权失败（code=2001），检查 THS_API_KEY")
        if api_code != 0:
            logger.warning("Fuyao API 业务错误 code=%s: %s",
                           api_code, j.get("message", ""))
            return []

        items = (j.get("data") or {}).get("item") or []
        out = []
        for it in items:
            code6 = from_thscode(it.get("thscode", ""))
            last_price = to_float(it.get("last_price"))
            prev_price = to_float(it.get("prev_price"))
            pct = None
            if last_price and prev_price and prev_price != 0:
                pct = round((last_price / prev_price - 1) * 100, 4)
            quote = {
                # on_tick 格式与 server._bg_refresh_spot 保持一致
                "type":    "quote",
                "code":    code6,
                "data": {
                    "代码":    code6,
                    "名称":    it.get("ticker", ""),
                    "最新价":  last_price,
                    "涨跌幅":  pct if pct is not None else to_float(
                        it.get("price_change_ratio_pct")),
                    "涨跌额":  to_float(it.get("price_change")),
                    "今开":    to_float(it.get("open_price")),
                    "最高":    to_float(it.get("high_price")),
                    "最低":    to_float(it.get("low_price")),
                    "昨收":    to_float(it.get("prev_price")),
                    "成交量":  to_float(it.get("volume")),
                    "成交额":  to_float(it.get("turnover")),
                },
                "timestamp": now_ms,
            }
            out.append(quote)
        return out

    async def _fire(self, snapshots: list[dict]) -> None:
        """逐个触发 on_tick，批次结束后触发 on_snapshot。"""
        cb_tick = self._on_tick
        cb_snap = self._on_snapshot

        if cb_tick:
            for q in snapshots:
                try:
                    result = cb_tick(q)
                    # 支持异步回调（server 侧 _l2_broadcast 是 async def）
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    logger.debug("on_tick 回调异常：%s", e)

        if cb_snap:
            try:
                result = cb_snap(snapshots)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.debug("on_snapshot 回调异常：%s", e)


# ── 东方财富 L2 WS（凭证到位后实现）─────────────────────────────────────

class EastmoneyL2Client(L2Source):
    """东方财富 Level2 WebSocket 客户端（占位）。

    端点/鉴权从 cfg.L2_WS_URL / cfg.L2_WS_TOKEN 读取。
    凭证到位后实现 connect/subscribe/_parse，其余不变。
    """

    def __init__(self):
        super().__init__()
        self._url = cfg.L2_WS_URL
        self._token = cfg.L2_WS_TOKEN

    async def connect(self) -> None:
        logger.warning("EastmoneyL2Client 未实现（凭证待提供），降级到 HTTP 轮询")
        raise NotImplementedError("Eastmoney L2 WS 接入待凭证提供后实现")

    async def subscribe(self, codes: list[str]) -> None:
        raise NotImplementedError


# ── 注册表 + 工厂 ─────────────────────────────────────────────────────────

_L2_REGISTRY: dict[str, type[L2Source]] = {
    "ths":        ThsFuyaoClient,
    "eastmoney":  EastmoneyL2Client,
}


def get_l2_source() -> Optional[L2Source]:
    """L2 数据源工厂。

    L2_SOURCE=ths      → ThsFuyaoClient（Fuyao REST 轮询，需 THS_API_KEY）
    L2_SOURCE=eastmoney → EastmoneyL2Client（占位，凭证到位后实现）
    L2_SOURCE=none/off/空 → None（沿用 _bg_refresh_spot HTTP 轮询）
    """
    name = cfg.L2_SOURCE.lower()
    if name in ("", "none", "off"):
        return None
    cls = _L2_REGISTRY.get(name)
    if cls is None:
        logger.warning("未知 L2_SOURCE=%s，降级到 HTTP 轮询", name)
        return None
    try:
        return cls()
    except RuntimeError as e:
        logger.warning("L2 源 %s 初始化失败：%s，降级到 HTTP 轮询", name, e)
        return None
    except Exception as e:
        logger.warning("L2 源 %s 构造异常：%s，降级到 HTTP 轮询", name, e)
        return None
