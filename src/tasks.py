"""异步任务队列：Celery + Redis Broker。

Worker 启动方式（需同时消费 default 与 heavy 队列）：
  cd /home/lixiang/langchain/bigA/trading-agent_new
  celery -A src.tasks worker --loglevel=info --concurrency=2 -Q default,heavy

任务查询：
  GET /api/task/{task_id}  → {"status": "SUCCESS"/"PENDING"/"FAILURE", "result": ...}
"""
from __future__ import annotations

import logging
from urllib.parse import quote_plus

from config import settings as cfg

logger = logging.getLogger("tasks")

# ── Celery App ─────────────────────────────────────────────────────────────
# Redis 启用 requirepass 时，broker URL 必须带密码
_PW = quote_plus(cfg.REDIS_PASSWORD) if cfg.REDIS_PASSWORD else ""
_AUTH = f":{_PW}@" if _PW else ""
_BROKER = f"redis://{_AUTH}{cfg.REDIS_HOST}:{cfg.REDIS_PORT}/{cfg.REDIS_DB}"
_BACKEND = _BROKER

try:
    from celery import Celery

    celery_app = Celery(
        "jc_tasks",
        broker=_BROKER,
        backend=_BACKEND,
    )
    celery_app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="Asia/Shanghai",
        enable_utc=False,
        task_acks_late=True,           # 任务执行完才确认，崩溃后重新投递
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,  # 每个 worker 一次只取 1 个任务，避免积压
        result_expires=3600,           # 结果保留 1 小时
        task_routes={
            "src.tasks.pull_stock_history": {"queue": "heavy"},
            "src.tasks.generate_eval_report": {"queue": "heavy"},
            "src.tasks.run_backtest": {"queue": "heavy"},
            "src.tasks.retry_datasource_pull": {"queue": "default"},
        },
    )
    _CELERY_OK = True
except Exception as e:
    logger.warning("Celery unavailable: %s — async tasks disabled", e)
    celery_app = None
    _CELERY_OK = False


# ── 任务定义 ────────────────────────────────────────────────────────────────

def _task(*args, **kwargs):
    """仅在 Celery 可用时注册为 task，否则返回普通函数。"""
    def deco(fn):
        if _CELERY_OK:
            return celery_app.task(*args, **kwargs)(fn)
        return fn
    return deco


@_task(
    name="src.tasks.pull_stock_history",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    time_limit=120,
)
def pull_stock_history(self, symbols: list[str], days: int = 365):
    """批量拉取个股历史K线，写入 ts_store。盘后定时任务（15:30）触发。"""
    from src.monitor import metrics
    try:
        metrics.task_enqueued("pull_stock_history")
        from src import data
        results = {}
        for sym in symbols:
            try:
                df = data.get_stock_hist(sym, days=days)
                results[sym] = len(df)
            except Exception as e:
                logger.warning("pull_stock_history %s failed: %s", sym, e)
                results[sym] = -1
        metrics.task_done("pull_stock_history")
        return results
    except Exception as exc:
        metrics.task_failed("pull_stock_history")
        raise self.retry(exc=exc)


@_task(
    name="src.tasks.generate_eval_report",
    bind=True,
    max_retries=2,
    default_retry_delay=60,
    time_limit=300,
)
def generate_eval_report(self, save_path: str | None = None):
    """生成 AI 评测报告，写入 eval/results/。手动触发或每周日定时。"""
    from src.monitor import metrics
    try:
        metrics.task_enqueued("generate_eval_report")
        import subprocess, sys
        result = subprocess.run(
            [sys.executable, "-m", "eval.run_all"],
            capture_output=True, text=True, timeout=270,
            cwd=str(cfg.PROJECT_ROOT),
        )
        metrics.task_done("generate_eval_report")
        return {
            "returncode": result.returncode,
            "stdout": result.stdout[-2000:],
            "stderr": result.stderr[-500:],
        }
    except Exception as exc:
        metrics.task_failed("generate_eval_report")
        raise self.retry(exc=exc)


@_task(
    name="src.tasks.run_backtest",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    time_limit=120,
)
def run_backtest(self, strategy: str, start_date: str, end_date: str):
    """策略回测（预留接口，逻辑由 strategy 参数指定）。用户触发。"""
    from src.monitor import metrics
    try:
        metrics.task_enqueued("run_backtest")
        # 占位：后续接入具体回测引擎
        logger.info("run_backtest: strategy=%s %s~%s", strategy, start_date, end_date)
        metrics.task_done("run_backtest")
        return {"strategy": strategy, "start": start_date, "end": end_date, "status": "ok"}
    except Exception as exc:
        metrics.task_failed("run_backtest")
        raise self.retry(exc=exc)


@_task(
    name="src.tasks.retry_datasource_pull",
    bind=True,
    max_retries=5,
    default_retry_delay=30,
    time_limit=60,
)
def retry_datasource_pull(self, fn_name: str, kwargs: dict | None = None):
    """数据源临时不可用时的重试拉取，由预热任务失败回调触发。"""
    from src.monitor import metrics
    try:
        metrics.task_enqueued("retry_datasource_pull")
        from src import data as _data
        fn = getattr(_data, fn_name, None)
        if fn is None:
            raise ValueError(f"Unknown data function: {fn_name}")
        result = fn(**(kwargs or {}))
        metrics.task_done("retry_datasource_pull")
        return {"fn": fn_name, "rows": len(result) if hasattr(result, "__len__") else "ok"}
    except Exception as exc:
        metrics.task_failed("retry_datasource_pull")
        raise self.retry(exc=exc)


# ── 任务提交辅助 ────────────────────────────────────────────────────────────

def submit(task_fn, *args, **kwargs) -> str | None:
    """提交任务，返回 task_id。Celery 不可用时不执行（避免与 bind=True 语义冲突），返回 None。"""
    if not _CELERY_OK:
        logger.warning("Celery unavailable, task %s dropped", getattr(task_fn, "name", task_fn))
        return None
    result = task_fn.delay(*args, **kwargs)
    return result.id


def get_task_status(task_id: str) -> dict:
    """查询任务状态，供 /api/task/{id} 端点调用。"""
    if not _CELERY_OK:
        return {"status": "DISABLED", "result": None}
    try:
        from celery.result import AsyncResult
        res = AsyncResult(task_id, app=celery_app)
        return {
            "task_id": task_id,
            "status": res.status,           # PENDING/STARTED/SUCCESS/FAILURE/RETRY
            # 仅返回结果，不向调用方暴露服务端 traceback
            "result": res.result if res.successful() else None,
        }
    except Exception as e:
        return {"status": "ERROR", "error": str(e)}
