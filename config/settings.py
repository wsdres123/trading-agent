"""集中配置：路径、配色、Qwen API、常量。读 .env，不依赖第三方 dotenv。"""
from __future__ import annotations

import os
from pathlib import Path

# ── 项目根目录 ────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_env(env_path: Path) -> None:
    """手动解析 .env，写入 os.environ（已存在的环境变量优先）。"""
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip("'\"")
        os.environ.setdefault(key, val)


_load_env(PROJECT_ROOT / ".env")


# ── 路径 ──────────────────────────────────────────────────────────────────
KNOWLEDGE_DIR = PROJECT_ROOT / os.environ.get("KNOWLEDGE_DIR", "knowledge").rstrip("/")
DOCS_DIR = PROJECT_ROOT / "docs"
INDEX_CACHE_DIR = PROJECT_ROOT / os.environ.get("INDEX_CACHE_DIR", ".knowledge_index").rstrip("/")
DATA_DIR = PROJECT_ROOT / ".data"
GROUPS_FILE = DATA_DIR / "groups.json"
SECTOR_CACHE = DATA_DIR / "sector.parquet"

for _d in (INDEX_CACHE_DIR, DATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ── 管理接口安全 ───────────────────────────────────────────────────────────
# 允许的 UI 来源（CORS 白名单，同时用于 WebSocket Origin 校验）
CORS_ORIGINS = os.environ.get(
    "CORS_ORIGINS",
    "http://localhost:8601,http://localhost:5173,http://127.0.0.1:8601,http://127.0.0.1:5173,http://127.0.0.1:8602,https://trade.jiecaiai.com",
).split(",")


# ── Qwen API（阿里千问，OpenAI 兼容端点）─────────────────────────────────
# Key 保护：明文只允许出现在进程内存；磁盘上为机器绑定的密文（.data/.keystore）。
from config import secret_store as _ks

# 高成本/敏感 API（如 /api/rejudge）需要 X-API-Key 头；空值时仅在 127.0.0.1 暴露。
# Key 保护与 Qwen Key 相同：明文只在进程内存，磁盘为机器绑定密文。
_ADMIN_KEYSTORE = DATA_DIR / ".keystore.admin"
_admin_raw = os.environ.get("ADMIN_API_KEY", "")
if _admin_raw and _admin_raw != _ks.PLACEHOLDER:
    if _ks.save_key(_ADMIN_KEYSTORE, _admin_raw):
        _ks.scrub_env_file(PROJECT_ROOT / ".env", ("ADMIN_API_KEY",), _admin_raw)
    ADMIN_API_KEY = _admin_raw
else:
    ADMIN_API_KEY = _ks.load_key(_ADMIN_KEYSTORE)
os.environ["ADMIN_API_KEY"] = _ks.PLACEHOLDER if ADMIN_API_KEY else ""

# Redis 密码同样加密入库；启动脚本通过 `python -m config.settings --redis-password` 取值。
_REDIS_PW_KEYSTORE = DATA_DIR / ".keystore.redis"
_redis_pw_raw = os.environ.get("REDIS_PASSWORD", "")
if _redis_pw_raw and _redis_pw_raw != _ks.PLACEHOLDER:
    if _ks.save_key(_REDIS_PW_KEYSTORE, _redis_pw_raw):
        _ks.scrub_env_file(PROJECT_ROOT / ".env", ("REDIS_PASSWORD",), _redis_pw_raw)
    REDIS_PASSWORD = _redis_pw_raw
else:
    REDIS_PASSWORD = _ks.load_key(_REDIS_PW_KEYSTORE)
os.environ["REDIS_PASSWORD"] = _ks.PLACEHOLDER if REDIS_PASSWORD else ""

_KEY_NAMES = ("DASHSCOPE_API_KEY", "QWEN_API_KEY")
_KEYSTORE = DATA_DIR / ".keystore"


def _resolve_api_key() -> str:
    raw = os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("QWEN_API_KEY", "")
    if raw and raw != _ks.PLACEHOLDER:
        # 首次发现明文：加密入库（回读校验通过后）抹除 .env 中的明文
        if _ks.save_key(_KEYSTORE, raw):
            _ks.scrub_env_file(PROJECT_ROOT / ".env", _KEY_NAMES, raw)
        return raw
    return _ks.load_key(_KEYSTORE)


QWEN_API_KEY = _resolve_api_key()
QWEN_API_KEY_MASKED = _ks.mask(QWEN_API_KEY)
# 环境变量中不保留明文，防止子进程/异常栈间接泄露
for _k in _KEY_NAMES:
    if os.environ.get(_k):
        os.environ[_k] = _ks.PLACEHOLDER

# ── 同花顺金融数据 API（fuyao.aicubes.cn）─────────────────────────────────
_THS_KEY_NAMES = ("THS_API_KEY",)
_THS_KEYSTORE = DATA_DIR / ".keystore.ths"


def _resolve_ths_api_key() -> str:
    raw = os.environ.get("THS_API_KEY", "")
    if raw and raw != _ks.PLACEHOLDER:
        if _ks.save_key(_THS_KEYSTORE, raw):
            _ks.scrub_env_file(PROJECT_ROOT / ".env", _THS_KEY_NAMES, raw)
        return raw
    return _ks.load_key(_THS_KEYSTORE)


THS_API_KEY = _resolve_ths_api_key()
THS_API_KEY_MASKED = _ks.mask(THS_API_KEY)
if os.environ.get("THS_API_KEY"):
    os.environ["THS_API_KEY"] = _ks.PLACEHOLDER
THS_BASE_URL = os.environ.get("THS_BASE_URL", "https://fuyao.aicubes.cn/api")

QWEN_BASE_URL = os.environ.get("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
QWEN_CHAT_MODEL = os.environ.get("QWEN_CHAT_MODEL", "qwen3.7-max")
QWEN_PLUS_MODEL = os.environ.get("QWEN_PLUS_MODEL", "qwen-plus")
QWEN_TURBO_MODEL = os.environ.get("QWEN_TURBO_MODEL", "qwen-turbo")
QWEN_EMBEDDING_MODEL = os.environ.get("QWEN_EMBEDDING_MODEL", "text-embedding-v3")


# ── 配色（黑底 / 个股黄字 / 涨红跌绿 / 其他白字）─────────────────────────
COLOR_BG = "#0e0e0e"
COLOR_PANEL = "#1a1a1a"
COLOR_TEXT = "#ffffff"
COLOR_STOCK = "#ffd500"      # 个股字体黄色
COLOR_UP = "#ff3b3b"         # 涨 红
COLOR_DOWN = "#22c55e"       # 跌 绿
COLOR_MUTED = "#9ca3af"
COLOR_ACCENT = "#1a56db"

# 字体偏大，避免看不清
FONT_SIZE = 18
FONT_FAMILY = "'Microsoft YaHei','PingFang SC','Noto Sans CJK SC',sans-serif"


# ── 数据缓存 TTL（秒）──────────────────────────────────────────────────────
SPOT_TTL = 20          # 实时快照 20 秒（配合 5s 预热频率，轮询延迟 ≤5s）
BOARD_TTL = 86400      # 板块成分股 1 天
HIST_TTL = 300         # 历史K线 5 分钟
CALENDAR_TTL = 86400   # 交易日历 1 天

# ── L1 内存缓存（进程级，跳过 Redis 网络 IO）──────────────────────────────
L1_TTL_SPOT = 5        # spot/热数据 5 秒
L1_TTL_KLINE = 15      # K线数据 15 秒

# ── 个股增量推送（方案A：轮询优化替代 L2 WS，见 ws_source.py 模块说明）─────
SPOT_REFRESH_CHG_PCT = float(os.environ.get("SPOT_REFRESH_CHG_PCT", "0.2"))   # 涨跌幅变动阈值(百分点)
SPOT_REFRESH_PRICE_PCT = float(os.environ.get("SPOT_REFRESH_PRICE_PCT", "0.3"))  # 最新价变动阈值(%)
SPOT_REFRESH_TOP_N = int(os.environ.get("SPOT_REFRESH_TOP_N", "20"))          # 每轮最多推送变动条数

# ── DuckDB 时序存储 ───────────────────────────────────────────────────────
TS_DIR = DATA_DIR / "ts"
TS_DIR.mkdir(parents=True, exist_ok=True)
for _sd in ("index_daily", "ths_index", "snapshots", "stocks", "minutes", "calendar", "securities"):
    (TS_DIR / _sd).mkdir(exist_ok=True)
SNAPSHOT_HOUR = 15
SNAPSHOT_MINUTE = 5
MINUTE_PULL_HOUR = 15
MINUTE_PULL_MINUTE = 10

# ── Redis 缓存 ────────────────────────────────────────────────────────────
REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_DB = int(os.environ.get("REDIS_DB", "0"))
# REDIS_PASSWORD 已在上方"管理接口安全"段解析（加密存 .keystore.redis），此处不重复读取
REDIS_KEY_PREFIX = os.environ.get("REDIS_KEY_PREFIX", "jc")
REDIS_ENABLED = os.environ.get("REDIS_ENABLED", "true").lower() in ("true", "1", "yes")

# ── FastAPI 异步服务 ──────────────────────────────────────────────────────
FASTAPI_HOST = os.environ.get("FASTAPI_HOST", "127.0.0.1")
FASTAPI_PORT = int(os.environ.get("FASTAPI_PORT", "8602"))

# ── 异步拉取参数 ──────────────────────────────────────────────────────────
ASYNC_MAX_CONNECTIONS = int(os.environ.get("ASYNC_MAX_CONNECTIONS", "100"))
ASYNC_MAX_KEEPALIVE = int(os.environ.get("ASYNC_MAX_KEEPALIVE", "20"))
ASYNC_TIMEOUT = float(os.environ.get("ASYNC_TIMEOUT", "8.0"))

# ── L2 WebSocket 行情数据源（付费源，未配置时降级到 HTTP 轮询）──────────────
L2_SOURCE = os.environ.get("L2_SOURCE", "none")  # none / eastmoney / ths
L2_WS_URL = os.environ.get("L2_WS_URL", "")
L2_WS_TOKEN = os.environ.get("L2_WS_TOKEN", "")
L2_SUBSCRIBE_CODES = os.environ.get("L2_SUBSCRIBE_CODES", "")  # 逗号分隔订阅代码


def is_trading_hours() -> bool:
    """判断当前是否在A股交易时段（交易日 09:25–15:05）。"""
    from datetime import datetime
    now = datetime.now()
    try:
        from src import data as _data
        if not _data.is_trading_day():
            return False
    except Exception:
        if now.weekday() >= 5:
            return False
    m = now.hour * 60 + now.minute
    return 565 <= m <= 905  # 09:25=565, 15:05=905


def as_project_path(rel: str) -> Path:
    """把相对路径转成基于项目根的绝对路径。"""
    return PROJECT_ROOT / rel


if __name__ == "__main__":
    import sys
    # 供启动脚本取 Redis 密码（含 keystore 解密），不打印其他任何内容
    if len(sys.argv) > 1 and sys.argv[1] == "--redis-password":
        sys.stdout.write(REDIS_PASSWORD)
