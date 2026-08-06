"""全链路监控：Prometheus 指标注册 + 飞书告警。

使用方式：
  from src.monitor import metrics
  metrics.cache_hit(fn_name)        # 缓存命中
  metrics.cache_miss(fn_name)       # 缓存未命中
  metrics.llm_latency(seconds)      # LLM 调用耗时
  metrics.llm_tokens(n)             # Token 消耗
  metrics.datasource_error(source)  # 数据源错误
  metrics.task_enqueued(task_name)  # 任务入队
  metrics.task_done(task_name)      # 任务完成
  metrics.task_failed(task_name)    # 任务失败
"""
from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger("monitor")

# ── Prometheus 指标 ────────────────────────────────────────────────────────
try:
    from prometheus_client import Counter, Gauge, Histogram, REGISTRY

    _CACHE_HIT = Counter(
        "jc_cache_hits_total", "Cache hits", ["fn"],
        registry=REGISTRY,
    )
    _CACHE_MISS = Counter(
        "jc_cache_misses_total", "Cache misses", ["fn"],
        registry=REGISTRY,
    )
    _LLM_LATENCY = Histogram(
        "jc_llm_latency_seconds", "LLM call latency",
        buckets=[0.5, 1, 2, 5, 10, 30],
        registry=REGISTRY,
    )
    _LLM_TOKENS = Counter(
        "jc_llm_tokens_total", "LLM token consumption",
        registry=REGISTRY,
    )
    _DS_ERROR = Counter(
        "jc_datasource_errors_total", "Data source errors", ["source"],
        registry=REGISTRY,
    )
    _DS_LATENCY = Histogram(
        "jc_datasource_latency_seconds", "Data source latency", ["source"],
        buckets=[0.1, 0.5, 1, 2, 5],
        registry=REGISTRY,
    )
    _TASK_ENQUEUED = Counter(
        "jc_task_enqueued_total", "Tasks enqueued", ["task"],
        registry=REGISTRY,
    )
    _TASK_DONE = Counter(
        "jc_task_done_total", "Tasks completed", ["task"],
        registry=REGISTRY,
    )
    _TASK_FAILED = Counter(
        "jc_task_failed_total", "Tasks failed", ["task"],
        registry=REGISTRY,
    )
    _REDIS_AVAILABLE = Gauge(
        "jc_redis_available", "Redis availability (1=up, 0=down)",
        registry=REGISTRY,
    )
    _PROM_OK = True
except Exception as e:
    logger.warning("prometheus_client unavailable: %s — metrics disabled", e)
    _PROM_OK = False


# ── 告警阈值（可通过环境变量覆盖）────────────────────────────────────────
_ALERT_LLM_P99_THRESHOLD = float(os.environ.get("ALERT_LLM_P99_SEC", "10"))
_ALERT_DS_ERR_RATE = float(os.environ.get("ALERT_DS_ERR_RATE", "0.1"))   # 10%
_FEISHU_WEBHOOK = os.environ.get("FEISHU_ALERT_WEBHOOK", "")

# 告警冷却：同一类告警 5 分钟内不重复发
_alert_cooldown: dict[str, float] = {}
_ALERT_COOLDOWN_SEC = 300


def _send_feishu_alert(title: str, content: str) -> None:
    """发飞书 Webhook 告警，5 分钟冷却。"""
    if not _FEISHU_WEBHOOK:
        logger.warning("[ALERT] %s: %s", title, content)
        return
    now = time.time()
    if now - _alert_cooldown.get(title, 0) < _ALERT_COOLDOWN_SEC:
        return
    _alert_cooldown[title] = now
    try:
        import httpx
        payload = {
            "msg_type": "text",
            "content": {"text": f"【劫财AI告警】{title}\n{content}"},
        }
        httpx.post(_FEISHU_WEBHOOK, json=payload, timeout=5)
        logger.info("Alert sent: %s", title)
    except Exception as e:
        logger.warning("Failed to send feishu alert: %s", e)


# ── 指标对外接口 ────────────────────────────────────────────────────────────
class _Metrics:
    """指标上报入口，所有方法均容错（Prometheus 不可用时静默跳过）。"""

    def cache_hit(self, fn: str) -> None:
        if _PROM_OK:
            try:
                _CACHE_HIT.labels(fn=fn).inc()
            except Exception:
                pass

    def cache_miss(self, fn: str) -> None:
        if _PROM_OK:
            try:
                _CACHE_MISS.labels(fn=fn).inc()
            except Exception:
                pass

    def llm_latency(self, seconds: float, tokens: int = 0) -> None:
        if _PROM_OK:
            try:
                _LLM_LATENCY.observe(seconds)
                if tokens:
                    _LLM_TOKENS.inc(tokens)
            except Exception:
                pass
        # 超阈值告警
        if seconds > _ALERT_LLM_P99_THRESHOLD:
            _send_feishu_alert(
                "LLM 调用超时",
                f"本次 LLM 调用耗时 {seconds:.1f}s，超过阈值 {_ALERT_LLM_P99_THRESHOLD}s",
            )

    def datasource_error(self, source: str) -> None:
        if _PROM_OK:
            try:
                _DS_ERROR.labels(source=source).inc()
            except Exception:
                pass
        _send_feishu_alert(
            f"数据源错误：{source}",
            f"数据源 {source} 请求失败，请检查网络或 API 可用性",
        )

    def datasource_latency(self, source: str, seconds: float) -> None:
        if _PROM_OK:
            try:
                _DS_LATENCY.labels(source=source).observe(seconds)
            except Exception:
                pass

    def redis_status(self, available: bool) -> None:
        if _PROM_OK:
            try:
                _REDIS_AVAILABLE.set(1 if available else 0)
            except Exception:
                pass
        if not available:
            _send_feishu_alert("Redis 不可用", "Redis 连接失败，系统已降级为内存缓存")

    def task_enqueued(self, task: str) -> None:
        if _PROM_OK:
            try:
                _TASK_ENQUEUED.labels(task=task).inc()
            except Exception:
                pass

    def task_done(self, task: str) -> None:
        if _PROM_OK:
            try:
                _TASK_DONE.labels(task=task).inc()
            except Exception:
                pass

    def task_failed(self, task: str) -> None:
        if _PROM_OK:
            try:
                _TASK_FAILED.labels(task=task).inc()
            except Exception:
                pass
        _send_feishu_alert(
            f"后台任务失败：{task}",
            f"Celery 任务 {task} 执行失败，请查看 Worker 日志",
        )


metrics = _Metrics()


# ── LLM 调用计时上下文管理器 ──────────────────────────────────────────────
class llm_timer:
    """with llm_timer(): ... 自动上报 LLM 耗时。"""
    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, *_):
        metrics.llm_latency(time.perf_counter() - self._start)


# ── 数据源调用计时上下文管理器 ────────────────────────────────────────────
class datasource_timer:
    """with datasource_timer("腾讯"): ... 自动上报数据源耗时和错误。"""
    def __init__(self, source: str):
        self._source = source

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type, *_):
        elapsed = time.perf_counter() - self._start
        metrics.datasource_latency(self._source, elapsed)
        if exc_type is not None:
            metrics.datasource_error(self._source)
