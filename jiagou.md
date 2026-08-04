# 劫财AI交易 — 工程架构文档

## 一、整体架构

```
┌─────────────────────────────────────────────────────────┐
│                    用户浏览器                             │
│              http://localhost:8601                         │
└──────────────┬──────────────────────────┬────────────────┘
               │                          │
               ▼                          ▼
┌──────────────────────┐   ┌──────────────────────────────┐
│  Streamlit UI (8601) │   │  FastAPI 后端 (8602)          │
│  ui.py               │   │  server.py                    │
│                      │   │                               │
│  7 大功能页面:        │   │  后台预热任务 (自适应频率):    │
│  · 指数择时           │   │  · 全市场快照 → Redis          │
│  · 主线模式           │   │  · 指数行情 → Redis            │
│  · 短线模式           │   │  · 热门股 → Redis              │
│  · 个股模式           │   │  · 指数日K → Redis             │
│  · 明日推演           │   │  · 同花顺指数日K → Redis      │
│  · AI助手             │   │  · 健康检查 → Redis            │
│  · 评测报告           │   │  · 每日快照 → ts_store         │
│                      │   │                               │
│                      │   │  REST API: /api/freshness     │
│  数据获取:            │   │  /api/spot /api/index ...     │
│  data.py @ttl_cache  │◄──│                               │
│  L1→L2 Redis→ts_store│   │  WebSocket:                    │
│                      │   │  /ws/market /ws/quotes/{code} │
│  图表: Plotly         │   │                               │
│  自动刷新: 自适应频率 │   │                               │
└──────┬───────────────┘   └──────┬───────────────────────┘
       │                          │
       │   ┌──────────────────────┘
       ▼   ▼
┌──────────────────────────────────────────────────────────┐
│  缓存层 (三级)                                            │
│                                                          │
│  L0 进程内存 (_L1_CACHE)   spot=10s / K线=15s  微秒级    │
│  L2 Redis (6379) — TTL     spot=60s / hist=300s 毫秒级   │
│  L3 DuckDB (.data/ts/) — 历史K线+每日快照 永久持久化     │
│                                                          │
│  键格式: jc:{函数名}:{sha256}                             │
│  序列化: parquet + msgpack + gzip                        │
│  兜底: Redis不可用时→_MEM_CACHE内存字典                   │
│  持久化: K线→ts_store parquet / 收盘→daily snapshot      │
└──────────────────────────────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────────────────────┐
│  数据源 (src/data.py 同步 / src/async_fetch.py 异步)      │
│                                                          │
│  腾讯财经 (qt.gtimg.cn)     → 实时行情, 分时, 涨跌停     │
│  新浪财经 (hq.sinajs.cn)    → 指数行情, 日K线            │
│  东方财富 (push2.eastmoney) → 板块, 热门股               │
│  同花顺   (fuyao.aicubes)   → 热榜, 连板, 概念           │
│  akshare                    → A股列表, 历史K线(兜底)     │
└──────────────────────────────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────────────────────┐
│  LLM 层 — 通义千问 Qwen (OpenAI 兼容 API)                 │
│                                                          │
│  模型路由（按任务分级）:                                   │
│  · qwen-turbo     → 筛选条件解析, 个股分时AI点评         │
│  · qwen-plus      → AI助手对话, 情绪节点, 指数择时,      │
│                     主线分析, 明日推演                    │
│  · qwen3.7-max    → 仅作为复杂任务兜底                   │
│  · text-embedding-v3 → RAG 混合检索 (1024维)             │
│                                                          │
│  性能优化:                                                │
│  · Prompt Caching: 固定 system prefix 走 DashScope 缓存  │
│  · 流式输出: chat_stream() + st.write_stream()           │
│  · enable_thinking=False: 关闭思考链节省 token            │
│  · 工具轮次压缩: MAX_TOOL_ROUNDS=3                       │
│                                                          │
│  调用模式:                                                │
│  · 规范学习: docs/*.md + knowledge/*.csv                 │
│  · 函数调用: market_tools.py 提供5个行情查询工具          │
│  · RAG混合检索: BM25+向量余弦 RRF融合, 800字分块         │
│  · 每日缓存: .data/*_ai.json, 每日更新一次               │
└──────────────────────────────────────────────────────────┘
```

## 二、技术栈

| 层级 | 技术 | 用途 |
|------|------|------|
| 前端 UI | Streamlit 1.30 | Web 界面，7 大功能页面 |
| 前端图表 | Plotly | K线图、分时图、子图(价格+量能) |
| 前端刷新 | streamlit-autorefresh | 自适应盘中刷新(竞价10s/盘中30s/尾盘15s) |
| 后端 API | FastAPI 0.122 | 异步 REST + WebSocket 服务 |
| ASGI 服务器 | Uvicorn | 运行 FastAPI |
| 缓存层 | Redis 7 (源码编译) | L2 TTL 缓存，parquet/msgpack/gzip 序列化 |
| 缓存层 L1 | 进程内存 `_L1_CACHE` | 微秒级热数据缓存，跳过 Redis 网络 IO |
| 时序存储 | DuckDB 1.5 + Parquet | 历史K线持久化 + 每日快照 + SQL 分析查询 |
| 异步 HTTP | httpx.AsyncClient | asyncio.gather 并发 |
| 同步 HTTP | requests | THS API、部分兜底请求 |
| 数据处理 | pandas / numpy | 行情数据清洗、矩阵计算 |
| A股数据 | akshare | A股列表、历史K线(兜底) |
| LLM | Qwen (OpenAI SDK) | 对话、分析、RAG嵌入 |
| RAG | text-embedding-v3 + rank_bm25 | BM25+向量 RRF 混合检索 |
| 密钥保护 | cryptography (Fernet) | API Key 机器绑定加密 |
| 运行环境 | Python 3.11 (Anaconda) | LD_LIBRARY_PATH 解决 GLIBCXX |

## 三、进程架构

| 进程 | 端口 | 启动方式 | 职责 |
|------|------|----------|------|
| Redis | 6379 | `redis-server --daemonize yes` | TTL 缓存 |
| FastAPI | 8602 | `uvicorn server:app` | 7个后台预热 + REST + WebSocket |
| Streamlit | 8601 | `streamlit run ui.py` | 用户界面 |

| 脚本 | 用途 |
|------|------|
| `run.sh` | 前台启动 |
| `start.sh` | 全后台启动 |
| `stop.sh` | 停止服务 |

环境变量: `export LD_LIBRARY_PATH="/home/lixiang/anaconda3/lib:${LD_LIBRARY_PATH:-}"`

## 四、核心模块说明

### 4.1 数据层

| 模块 | 行数 | 职责 |
|------|------|------|
| `src/data.py` | 929 | 同步行情数据，多数据源(腾讯→新浪→akshare)，所有函数 `@ttl_cache` |
| `src/async_fetch.py` | 474 | 异步行情拉取，httpx + asyncio.gather |
| `src/redis_cache.py` | 180 | L1+L2 分层缓存：进程内存(10-15s)→Redis TTL→内存兜底，parquet/msgpack/gzip 序列化 |
| `src/ts_store.py` | 140 | DuckDB 时序存储：指数日K/同花顺指数/每日快照 parquet 持久化 |
| `src/ths_data.py` | 173 | 同花顺 API 封装 |

`data.py` 核心函数: `get_stock_spot()` / `get_stock_hist()` / `get_index_daily()` / `get_hot_stocks()` / `health()`

`redis_cache.py` 特性:
- `ttl_cache(ttl, l1_ttl=None)` 装饰器：L1 进程内存 → L2 Redis → _MEM_CACHE 兜底 → 调用原函数 → 多层回写
- L1 热数据: spot/index/hot 等高频函数配置 `l1_ttl=10s`，K线配置 `l1_ttl=15s`
- 键格式: `jc:{fn_name}:{sha256(args+kwargs)[:16]}`
- DataFrame → parquet，dict/list → msgpack，>10KB 自动 gzip
- 向后兼容旧 pickle 格式

`ts_store.py` 特性:
- `TimeSeriesStore` 单例，DuckDB 连接懒加载，不可用时优雅降级
- 指数日K (`index_daily/*.parquet`)：保存/加载，自动检测数据新鲜度
- **向量化计算**：`calc_ma()` / `calc_returns()` / `calc_volatility()` / `get_index_stats()` — DuckDB SQL 直接在 parquet 上计算均线/涨幅/波动率/百日高低，无需加载到 pandas
- 同花顺指数 (`ths_index/*.parquet`)：连板指数/昨日成交前十
- 每日快照 (`snapshots/YYYY-MM-DD.parquet`)：收盘后自动保存全市场数据，供盘后回测
- `query(sql)` 支持 DuckDB SQL 直接查询 parquet 文件

### 4.2 分析层

| 模块 | 行数 | 职责 |
|------|------|------|
| `src/index_timing.py` | 316 | 指数择时：中级周期(A/B/C/D) + 每日多/空/转信号 |
| `src/emotion_node.py` | 363 | 情绪节点：16种节点，market_stats() + ai_judge() |
| `src/theme_mode.py` | 412 | 主线模式：趋势A/C周期主线检测 |
| `src/short_term.py` | 630 | 短线模式：连板梯队、4种模式 |
| `src/single_stock.py` | 448 | 个股模式：分时AI点评(qwen-turbo) |
| `src/tomorrow.py` | 166 | 明日推演：汇总所有模块判断 |

### 4.3 AI 层

| 模块 | 行数 | 职责 |
|------|------|------|
| `src/knowledge.py` | 221 | RAG混合检索：BM25+向量余弦 RRF融合，800字分块，BATCH=10 |
| `src/market_tools.py` | 280 | LLM函数调用：6个行情查询工具（含指数K线DuckDB分析） |
| `src/ai_assistant.py` | 446 | AI助手：chat_stream()流式 + parse_filter_conditions() |

`ai_assistant.py` 模型路由:
- `chat()` / `chat_stream()` → qwen-plus, enable_thinking=False
- `parse_filter_conditions()` → qwen-turbo
- SYSTEM_PROMPT 固定前缀与动态 RAG 上下文分离，命中 DashScope 缓存

### 4.4 其他模块

| 模块 | 行数 | 职责 |
|------|------|------|
| `src/stock_filter.py` | 583 | 选股筛选：粗筛→精筛，分组管理 |
| `server.py` | 304 | FastAPI：7个后台预热(含ts_store持久化+每日快照) + REST + WebSocket |
| `ui.py` | 1715 | Streamlit UI：7大功能页面 + 评测报告 |
| `config/settings.py` | 141 | 全局配置：路径/API密钥/模型/TTL/颜色 |
| `config/secret_store.py` | 76 | 机器绑定 Fernet 加密 |

`server.py` 预热间隔: 竞价(9:25-9:30) 10s / 盘中 30s / 午休跳过 / 尾盘(14:50-15:05) 15s / 非交易 300s

## 五、数据流

### 行情数据流

```
FastAPI 后台任务 (自适应间隔)
  → async_fetch.py (httpx + asyncio.gather)
  → 腾讯/新浪/东财/同花顺 API
  → redis_set(parquet/msgpack + gzip, TTL)  +  ts_store.save(parquet)
  → Redis (L2)                              +  DuckDB/Parquet (L3)

Streamlit 页面刷新 (自适应频率)
  → data.py @ttl_cache
  → L1 进程内存命中(10-15s) → 直接返回 (微秒级)
  → L1 miss → L2 Redis → 反序列化 → 回写 L1
  → L2 miss → ts_store 本地 parquet 兜底 (K线数据)
  → 全部 miss → HTTP 原始请求 → 回写所有层
  → DataFrame → Plotly 图表
```

### LLM 分析流

```
docs/*.md + knowledge/*.csv
  → knowledge.py RAG (BM25 + embedding → RRF)
  → 盘面统计 + LLM (qwen-plus为主)
  → JSON → .data/*_ai.json (每日缓存)
  → Streamlit 展示
```

### AI 助手对话流

```
用户输入 → RAG检索 → chat_stream() (qwen-plus)
  → 需要数据？→ market_tools (最多3轮) → Redis
  → st.write_stream() 流式输出
```

## 六、文件清单

```
trading-agent_new/
├── ui.py                          # Streamlit UI (1715行, 7大功能)
├── server.py                      # FastAPI 后端 (276行)
├── run.sh / start.sh / stop.sh    # 启动/停止脚本
├── .env                           # 环境变量
├── .streamlit/config.toml         # Streamlit 主题
│
├── config/
│   ├── settings.py                # 全局配置 (141行)
│   └── secret_store.py           # 密钥加密 (76行)
│
├── src/
│   ├── data.py                    # 同步行情 (929行)
│   ├── async_fetch.py             # 异步行情 (474行)
│   ├── redis_cache.py             # L1+L2 分层缓存 (180行)
│   ├── ts_store.py                # DuckDB 时序存储 (140行)
│   ├── knowledge.py               # RAG检索 (221行)
│   ├── market_tools.py            # LLM工具 (280行, 6个工具)
│   ├── ai_assistant.py            # AI助手 (446行)
│   ├── stock_filter.py            # 选股筛选 (583行)
│   ├── ths_data.py                # 同花顺API (173行)
│   ├── index_timing.py            # 指数择时 (316行)
│   ├── emotion_node.py            # 情绪节点 (363行)
│   ├── theme_mode.py              # 主线模式 (412行)
│   ├── short_term.py              # 短线模式 (630行)
│   ├── single_stock.py            # 个股模式 (448行)
│   └── tomorrow.py                # 明日推演 (166行)
│
├── docs/                          # 交易规范文档
│   ├── node_spec.md               # 中级周期规范
│   ├── theme_spec.md              # 主线题材规范
│   ├── Short-term.md              # 短线模式规范
│   ├── emotional node.md          # 情绪节点分类
│   └── single stock.md            # 个股模式规范
│
├── knowledge/                     # 屠龙表交易系统 (CSV)
│
├── eval/                          # 评测模块 (8维度)
│   ├── common.py                  # 公共工具
│   ├── datasets/                  # 数据集 (565条)
│   ├── test_*.py                  # 8个评测维度
│   ├── run_all.py                 # 汇总报告
│   └── results/                   # 结果快照
│
├── .data/                         # 运行时缓存
│   ├── .keystore                  # 加密API密钥
│   └── ts/                        # DuckDB 时序存储
│       ├── ts.duckdb              # DuckDB 数据库文件
│       ├── index_daily/           # 指数日K parquet
│       │   ├── sh000001.parquet   # 上证指数 (1060天)
│       │   ├── sz399001.parquet   # 深证成指
│       │   ├── sz399006.parquet   # 创业板指
│       │   └── sh000688.parquet   # 科创50
│       ├── ths_index/             # 同花顺指数 parquet
│       │   ├── 883958.parquet     # 连板指数
│       │   └── 883902.parquet     # 昨日成交前十
│       └── snapshots/             # 每日收盘快照
│           └── YYYY-MM-DD.parquet # 全市场收盘数据
│
└── .knowledge_index/              # RAG向量索引
```

## 七、缓存策略

| 层级 | 位置 | TTL | 用途 |
|------|------|-----|------|
| L0 | `_L1_CACHE` (进程内存) | 10-15s | 热数据微秒级访问，跳过 Redis 网络 IO |
| L1 | `st.cache_data` | 会话级 | UI 渲染 |
| L2 | Redis | 60s~86400s | 行情/指数/热榜 (parquet/msgpack+gzip) |
| L2 兜底 | `_MEM_CACHE` | 同 Redis | Redis 不可用时降级 |
| L3 | `.data/ts/*.parquet` | 永久 (DuckDB) | 指数日K / 同花顺指数 / 每日收盘快照 |
| L4 | `.data/*.json` | 每日 | AI 判断结果 |
| L5 | `.data/*.parquet` | 12h | 指标矩阵 |
| L6 | `.knowledge_index/` | 手动重建 | RAG 嵌入向量 |

**缓存查询链**（以 `get_index_daily` 为例）:
```
L0 进程内存 (15s) → L2 Redis (60s) → L3 ts_store 本地 parquet → HTTP 原始请求
```

**L1 缓存配置**:
| 函数 | L2 TTL | L1 TTL | 说明 |
|------|--------|--------|------|
| `get_stock_spot()` | 60s | 10s | 全市场实时快照 |
| `get_stock_quote_fast()` | 60s | 10s | 个股快照 |
| `get_index_spot()` | 60s | 10s | 指数行情 |
| `get_hot_stocks()` | 60s | 10s | 热门股 |
| `get_index_daily()` | 60s | 15s | 指数日K (含ts_store) |
| `get_ths_index_daily()` | 60s | 15s | 同花顺指数 (含ts_store) |
| `get_stock_hist()` | 300s | — | 个股K线 (5min足够长，无需L1) |

**DuckDB 向量化计算**（ts_store）：
- `calc_ma(symbol, period)` — N日均线，DuckDB SQL 直接聚合 parquet
- `calc_returns(symbol, days)` — N日涨跌幅，窗口函数计算
- `calc_volatility(symbol, days)` — N日年化波动率，STDDEV_SAMP × √252
- `get_index_stats(symbol)` — 综合统计（收盘+MA5/10/20/60+涨幅+百日高低），单条SQL完成
- 比 pandas 加载全量 DataFrame 再计算快 3-5x，用于 LLM 工具调用场景

## 八、评测体系 (eval/)

| 维度 | 需标准答案 | 来源 | 数据量 |
|------|-----------|------|--------|
| 数据准确性 | 否 | 规则即标准 | 自动断言 |
| 情绪节点 | 是 | 复盘表.csv | 424 条 |
| 指数择时 | 是 | 复盘表.csv | 86 条 |
| 筛选NLP | 是 | 手写 | 35 条 |
| RAG检索 | 是 | 手写 | 20 条 |
| LLM质量 | 否 | LLM-as-Judge | 3 题 |
| 工具路由 | 半自动 | 问题→工具映射 | 5 条 |
| 性能基准 | 否 | 自动测量 | 7 项 |

```bash
python -m eval.run_all              # 无费用
python -m eval.run_all --all        # 含 LLM 调用
python -m eval.run_all --regression # 回归对比
```

评测结果可在 Streamlit "评测报告" 页面查看，包含数据完整性检查、各维度通过率、失败详情及历史报告对比。

## 九、已知限制

1. **WebSocket 未被 Streamlit 消费** — 已就绪但 UI 用自适应轮询
2. **上游数据源无公开 WebSocket** — 腾讯/新浪/东财/同花顺均为 HTTP 轮询，盘中轮询间隔已优化至 30s
3. **明日推演 UI 未接入** — 后端逻辑完整，前端未展示
4. **无 requirements.txt** — 依赖未锁定版本
5. **LLM 单次调用** — 无多轮验证/对抗确认
6. **GLIBCXX 依赖** — 必须设置 `LD_LIBRARY_PATH`
7. **data.py 单文件 929 行** — 数据源/解析/统计混合
