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


# ── Qwen API（阿里千问，OpenAI 兼容端点）─────────────────────────────────
# Key 保护：明文只允许出现在进程内存；磁盘上为机器绑定的密文（.data/.keystore）。
from config import secret_store as _ks

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
SPOT_TTL = 60          # 实时快照 1 分钟
BOARD_TTL = 86400      # 板块成分股 1 天
HIST_TTL = 300         # 历史K线 5 分钟


def as_project_path(rel: str) -> Path:
    """把相对路径转成基于项目根的绝对路径。"""
    return PROJECT_ROOT / rel
