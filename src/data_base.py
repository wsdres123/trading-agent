"""data 子包公共底座：akshare 可用性、重试装饰器、通用小工具。

原 src/data.py 巨型文件按功能拆分为 data_* 子模块后的公共层，
集中放置被多个子模块依赖的基础件，避免循环导入。
"""
from __future__ import annotations

import time
import logging
from functools import wraps

import pandas as pd

logger = logging.getLogger("data")

try:
    import akshare as ak
    _AKSHARE_OK = True
    _AKSHARE_ERR = None
except Exception as e:  # 环境问题（如 libstdc++ 缺失）时给出清晰提示
    ak = None
    _AKSHARE_OK = False
    _AKSHARE_ERR = e
    logger.error("akshare 导入失败：%s。请用 run.sh 启动（设 LD_LIBRARY_PATH）。", e)


# ── 重试装饰器（应对东方财富偶发断连）────────────────────────────────────
def retry(retries: int = 4, base: float = 1.5):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            last = None
            for i in range(retries):
                try:
                    return fn(*args, **kwargs)
                except Exception as e:
                    last = e
                    time.sleep(base * (i + 1))
            logger.warning("%s 重试 %d 次仍失败：%s", fn.__name__, retries, last)
            raise last
        return wrapper
    return deco


def _need_akshare():
    if not _AKSHARE_OK:
        raise RuntimeError(
            "akshare 不可用，无法获取实时行情。"
            "请使用 run.sh 启动，或设置 LD_LIBRARY_PATH=/home/lixiang/anaconda3/lib。"
            f" 原始错误：{_AKSHARE_ERR}"
        )


def _empty(cols: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=cols)
