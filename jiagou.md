# 劫财AI交易 — 工程架构文档

## 一、整体架构

```
┌─────────────────────────────────────────────────────────┐
│                    用户浏览器                             │
│        http://localhost:5173 (dev) 或 :8602 (prod)       │
└──────────────┬──────────────────────────┬────────────────┘
               │                          │
               ▼                          ▼
┌──────────────────────┐   ┌──────────────────────────────┐
│  React 前端 (5173)   │   │  FastAPI 后端 (8602)          │
│  frontend/           │   │  server.py                    │
│  Vite+React18+TS     │   │                               │
│  Tailwind CSS        │   │  后台预热任务 (自适应频率):    │
│  ECharts K线/分时    │   │  · 全市场快照/指数/热门股     │
│                      │   │  · 指数日K/同花顺指数日K     │
│  7 大功能页面:        │   │  · 健康检查/每日快照         │
│  · 指数择时           │   │                               │
│  · 主线模式           │   │  REST: /api/* 全量端点        │
│  · 短线模式           │   │  WS: /ws/market /ws/quotes    │
│  · 个股模式           │   │  静态挂载: frontend/dist      │
│  · 明日推演           │   │                               │
│  · AI助手             │   │                               │
│  · 评测报告           │   │                               │
│                      │   │                               │
│  数据获取:            │   │                               │
│  fetch→/api/*         │◄──│                               │
│  WS→/ws/market增量   │   │                               │
│  状态: TanStack Query │   │                               │
│  + Zustand            │   │                               │
└──────┬───────────────┘   └──────┬───────────────────────┘
       │                          │
       │   ┌──────────────────────┘
       ▼   ▼
┌──────────────────────────────────────────────────────────┐
│  缓存层 (三级)                                            │
│  L0 进程内存 (_L1_CACHE)   spot=10s / K线=15s            │
│  L2 Redis (6379) — TTL     spot=60s / hist=300s          │
│  L3 Parquet (.data/ts/) — 历史K线+每日快照 永久           │
│  键格式: jc:{函数名}:{sha256}  序列化: parquet+msgpack+gz │
└──────────────────────────────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────────────────────┐
│  数据源 (src/data.py / src/async_fetch.py)                │
│  腾讯 → 实时行情/涨跌停  |  新浪 → 指数行情/日K线         │
│  东财 → 板块/热门股      |  同花顺 → 连板/概念/指数       │
│  akshare → A股列表/历史K线(兜底)                          │
└──────────────────────────────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────────────────────┐
│  LLM 层 — 通义千问 Qwen (OpenAI 兼容 API)                 │
│  模型路由: turbo(筛选/点评) → plus(分析/对话) → max(兜底) │
│  优化: Prompt Caching / 流式输出 / enable_thinking=False  │
│  RAG: BM25+向量余弦 RRF融合, 800字分块                    │
│  记忆: STM(会话) → MTM(jsonl) → LTM(向量库)              │
└──────────────────────────────────────────────────────────┘
```

## 二、技术栈

| 层级 | 技术 | 用途 |
|------|------|------|
| 前端 | React 18 + TypeScript + Vite + Tailwind CSS | Web 界面 + ECharts K线/分时图表 |
| 后端 | FastAPI + Uvicorn | 异步 REST + WebSocket + 静态服务 |
| 缓存 | Redis 7 + 进程内存 | L2 TTL + L0 热数据 |
| 存储 | Parquet (ts_store) | 历史K线 + 每日快照 |
| 数据 | pandas / httpx / requests | 清洗 + 异步/同步 HTTP |
| 认证 | SQLite + bcrypt + Redis 会话 | 多用户登录（详见 7.3） |
| LLM | Qwen (OpenAI SDK) | 对话/分析/RAG嵌入 |
| 环境 | Python 3.11 (Anaconda) | LD_LIBRARY_PATH 解决 GLIBCXX |

## 三、进程架构

| 进程 | 端口 | 职责 |
|------|------|------|
| Redis | 6379 | TTL 缓存 |
| FastAPI | 8602 | 7个后台预热 + REST + WebSocket + 静态服务(prod) |
| React dev server | 5173 | Vite 开发服务器 (dev only, proxy→8602) |

启动: `run.sh`(前台) / `start.sh`(后台) / `stop.sh`(停止)
前端开发: `cd frontend && npm run dev` → http://localhost:5173
前端构建: `cd frontend && npm run build` → FastAPI 同域 http://localhost:8602 提供

## 四、核心模块

### 4.1 数据层

| 模块 | 职责 |
|------|------|
| `src/data.py` | 同步行情，多数据源(腾讯→Fuyao→akshare)，分时竞价量/涨速，个股日K入parquet，1分钟K线，所有函数 `@ttl_cache` |
| `src/async_fetch.py` | 异步行情，httpx + asyncio.gather |
| `src/redis_cache.py` | L1+L2 分层缓存，parquet/msgpack/gzip 序列化 |
| `src/ts_store.py` | Parquet 时序存储：指数日K/同花顺指数/每日快照/个股日K/分钟K线/交易日历/证券主数据快照，`_atomic_save` 原子写+fcntl锁 |
| `src/ths_data.py` | 同花顺 API 封装（snapshot 实时快照降级源、trade_calendar 交易日历） |
| `src/ws_source.py` | L2 WebSocket 抽象层（东方财富/同花顺 Level2，未配置降级轮询） |
| `src/ws_hub.py` | WebSocket 广播中枢：Redis Pub/Sub fan-out，客户端共享单点采集（痛点3） |
| `src/data_quality.py` | 数据质量校验器：`validate_spot`（价格为0/NaN、涨跌幅超限丢弃）+ `validate_ohlc`（OHLC约束校验） |
| `src/source_health.py` | 数据源熔断器：连续失败3次→黑名单300s，`is_available`/`record`/`status` |

`ttl_cache` 查询链: `L0 进程内存(10-15s) → L2 Redis(60s) → ts_store parquet → HTTP`

### 4.2 分析层

| 模块 | 职责 |
|------|------|
| `src/index_timing.py` | 指数择时：中级周期(A/B/C/D) + 每日信号，双阶段复核 |
| `src/emotion_node.py` | 情绪节点：25种节点(7个一级分类)，双阶段复核 |
| `src/theme_mode.py` | 主线模式：趋势A/C周期主线检测 + 有效性评分 |
| `src/short_term.py` | 短线模式：三层决策(硬门控→评分→LLM)，883958信号(起变/延续) |
| `src/single_stock.py` | 个股模式：分时AI点评(qwen-turbo) |
| `src/tomorrow.py` | 明日推演：汇总所有模块判断 |

### 4.3 AI 层

| 模块 | 职责 |
|------|------|
| `src/knowledge.py` | RAG混合检索：BM25+向量余弦 RRF融合 |
| `src/market_tools.py` | LLM函数调用：6个行情查询工具 |
| `src/ai_assistant.py` | AI助手：chat_stream()流式 + 筛选条件解析 |
| `src/schema_validator.py` | Schema校验 + 双阶段复核编排 |
| `src/memory.py` | 分层记忆 STM/MTM/LTM 接口 |
| `src/feature_provider.py` | 统一特征构建器（线上/回测同构） |

### 4.4 其他

| 模块 | 职责 |
|------|------|
| `src/stock_filter.py` | 选股筛选：粗筛→精筛，分组按用户隔离 |
| `src/auth.py` | 多用户登录：SQLite+bcrypt+Redis 会话+失败锁定+CLI |
| `src/realtime_widget.py` | [旧] 实时行情 bar JS（Streamlit 版，React 用 ws.ts hook 替代） |
| `server.py` | FastAPI：预热 + REST（含前端 API）+ WebSocket 广播 + 静态服务 |
| `ui.py` | [旧] Streamlit UI（保留兼容，待退役） |
| `config/settings.py` | 全局配置 |

## 五、数据流

```
行情数据流:
  FastAPI 后台(自适应间隔) → async_fetch → 腾讯/新浪/东财/同花顺
    → Redis(TTL) + ts_store(parquet)
    → ws_hub 发布 Redis Pub/Sub(jc:quotes) → /ws/market 广播给所有前端
  React 页面 → fetch /api/* → TanStack Query 缓存 → ECharts 渲染
  React MarketBar → 直连 /ws/market → WS hook 增量更新(无整页刷新)

LLM 分析流:
  docs/*.md + knowledge/*.csv → RAG(BM25+embedding→RRF)
    → 盘面统计 + LLM(qwen-plus) → JSON → .data/*_ai.json → /api/* → React

AI 助手:
  用户输入 → /api/chat/stream (SSE) → RAG+记忆检索 → chat_stream(qwen-plus)
    → 需要数据？→ market_tools(最多3轮) → SSE 流式推送给 React
```

## 六、文件清单

```
trading-agent_new/
├── frontend/                  # React 18 前端工程
│   ├── package.json
│   ├── vite.config.ts          # proxy /api /ws → 8602
│   ├── tsconfig.json
│   ├── tailwind.config.js      # 复刻 COLOR_* 主题
│   ├── index.html
│   └── src/
│       ├── main.tsx            # React 入口
│       ├── App.tsx             # 路由 + 认证守卫 + 页面切换
│       ├── index.css           # Tailwind + 全局黑底样式
│       ├── store/
│       │   ├── auth.ts         # Zustand: token/user
│       │   └── nav.ts          # 当前页面选择
│       ├── api/
│       │   ├── client.ts       # fetch 封装 + token 注入 + SSE
│       │   ├── ws.ts           # WebSocket hooks (useMarketWS/useQuoteWS)
│       │   └── endpoints.ts    # 所有 API 端点定义
│       ├── components/
│       │   ├── Layout.tsx      # 顶栏 + 功能导航 + 状态chip
│       │   ├── MarketBar.tsx   # WS 实时行情条
│       │   ├── StockTable.tsx  # 涨红跌绿黄字排序表(虚拟滚动)
│       │   ├── KLineChart.tsx  # ECharts 蜡烛图 (MA + 信号标记)
│       │   ├── IntradayChart.tsx # 分时图
│       │   ├── Chip.tsx        # 标签芯片
│       │   ├── StatBox.tsx     # 统计卡片
│       │   └── Login.tsx       # 登录页
│       └── pages/
│           ├── Timing.tsx       # 指数择时
│           ├── Theme.tsx       # 主线模式
│           ├── ShortTerm.tsx   # 短线模式
│           ├── SingleStock.tsx # 个股模式
│           ├── Tomorrow.tsx    # 明日推演
│           ├── AIAssistant.tsx # AI助手
│           └── EvalReport.tsx  # 评测报告
├── server.py                  # FastAPI 后端 (REST + WS + 静态服务)
├── ui.py                      # [旧] Streamlit UI (保留兼容，待退役)
├── pages/                     # [旧] Streamlit 页面 (保留兼容)
├── run.sh / start.sh / stop.sh
├── config/
│   ├── settings.py            # 全局配置
│   └── secret_store.py        # 密钥加密
├── src/
│   ├── data.py                # 同步行情 (ttl_cache + 实时补充)
│   ├── async_fetch.py         # 异步行情
│   ├── redis_cache.py         # L1+L2 分层缓存
│   ├── ts_store.py            # Parquet 时序存储
│   ├── schema_validator.py    # Schema校验 + 双阶段复核
│   ├── feature_provider.py    # 统一特征构建器
│   ├── knowledge.py           # RAG检索
│   ├── market_tools.py        # LLM工具
│   ├── ai_assistant.py        # AI助手
│   ├── stock_filter.py        # 选股筛选
│   ├── ths_data.py            # 同花顺API (snapshot降级源)
│   ├── ws_source.py           # L2 WebSocket抽象层
│   ├── ws_hub.py              # WS广播中枢 (Redis Pub/Sub)
│   ├── data_quality.py       # 数据质量校验 (spot/ohlc)
│   ├── source_health.py      # 数据源熔断器 (连续失败→黑名单)
│   ├── realtime_widget.py     # 前端实时行情bar (JS增量更新)
│   ├── auth.py                # 多用户登录 (SQLite+bcrypt)
│   ├── index_timing.py        # 指数择时
│   ├── emotion_node.py        # 情绪节点
│   ├── theme_mode.py          # 主线模式
│   ├── short_term.py          # 短线模式
│   ├── single_stock.py        # 个股模式
│   ├── tomorrow.py            # 明日推演
│   └── memory.py              # 分层记忆
├── docs/                      # 交易规范文档
├── knowledge/                 # 屠龙表交易系统 (CSV)
├── eval/                      # 评测模块 (8维度)
├── .data/                     # 运行时缓存
│   ├── short_term_signal.json # 短线起变信号去重状态
│   └── ts/                    # Parquet 时序存储
│       ├── index_daily/       # 指数日K (上证/深证/创业板/科创)
│       ├── ths_index/         # 同花顺指数 (883958/883902/883418)
│       ├── stocks/            # 个股日K (按代码分文件)
│       ├── minutes/           # 1分钟K线 (按代码/日期分区)
│       └── snapshots/         # 每日收盘快照
└── .knowledge_index/          # RAG向量索引
```

## 七、缓存策略

| 层级 | 位置 | TTL | 用途 |
|------|------|-----|------|
| L0 | `_L1_CACHE` | 10-15s | 热数据微秒级，跳过 Redis IO |
| L2 | Redis | 60-300s | 行情/指数/热榜 (parquet/msgpack+gzip) |
| L2 兜底 | `_MEM_CACHE` | — | Redis 不可用时降级 |
| L3 | `.data/ts/*.parquet` | 永久 | 指数日K / 同花顺指数 / 每日快照 / 个股日K / 分钟K线 / 交易日历 / 证券主数据快照 |
| L4 | `.data/*.json` | 每日 | AI 判断结果 |

同花顺指数日K: v6接口(历史) + v4实时接口(当日补充) — 解决收盘后日K更新延迟问题

### 7.1 数据正确性工程

基于 `jishu.md` 数据正确性改造落地，核心目标：**剔除伪造数据、消除未来函数、统一复权口径、限流防雪崩**。

| # | 问题 | 位置 | 修改 |
|---|------|------|------|
| 1 | 拉取失败股票用"昨价拉平"伪造 390 日 OHLCV，污染平均股价/MA/百日新高/情绪统计 | `src/data.py:build_metrics_cache` | 删除伪造逻辑，失败样本剔除 |
| 2 | 确定性未来函数：历史平均股价取缓存末端 n 天，非目标日之前 | `src/feature_provider.py` | 按 `dates<=target` 截断，矩阵列索引映射到目标日期 |
| 3 | 指数 Parquet 覆盖写：短 days 请求可把 1060 天历史截成 3 天 | `src/ts_store.py:save_index_daily` | 按日期 merge + 临时文件原子写 + `fcntl` 文件锁 |
| 4 | 缓存预热 key 与业务读不一致（days=1060 vs 380），预热白做 | `server.py` / `src/data.py:get_index_daily` | 抽象 `_get_index_daily_full(days=1060)` 全量缓存，`get_index_daily` 本地切片；预热 key 对齐 |
| 5 | Redis 锁无 token，过期后可误删他人锁 | `src/redis_cache.py:ttl_cache` | UUID token + Lua compare-and-delete |
| 6 | 失败结果也缓存 60s：一次源故障→全市场空数据 | `src/redis_cache.py:ttl_cache` | 失败结果仅 5s 负缓存（空 DataFrame / error dict） |
| 7 | 复权口径混用：AkShare 前复权 vs 同花顺不复权，不标注 | `src/data.py:get_stock_hist` | akshare 输出 `adjust=qfq` / `source=akshare`；同花顺 fallback 输出 `adjust=none` / `source=ths`，显式标注 |
| 8 | 异步无限流：全市场 ~65 请求一次 gather，无 429/退避 | `src/async_fetch.py` | 每源 `asyncio.Semaphore` + `_AsyncRateLimiter` 最小间隔 + 按源隔离 |

### 7.2 策略逻辑工程

基于 `jishu.md` 策略逻辑改造落地，核心目标：**硬规则优先于模型、规范文档作为单一事实来源、LLM 调用可观测可限流**。

| # | 问题 | 位置 | 修改 |
|---|------|------|------|
| 9 | 起变门控漏"高度板非一字板"（规范明确要求） | `src/short_term.py:hard_gate()` | `is_signal` 加入 `not high_is_one_line` |
| 10 | D 周期空仓可绕过："D+多"时仓位可达 1.0 | `src/index_timing.py:_position_rule()` | D 周期程序级强制 `position_cap=0`，无论 signal |
| 11 | 个股 D 周期禁令只在 Prompt，代码仍执行 | `src/single_stock.py:run()` | 程序级门控直接返回空结果 |
| 12 | `bool("false")==True` 反向触发弃权；JSON 贪婪正则提取 | `src/schema_validator.py` | 新增 `parse_bool()` 兼容中英文；`_strip_markdown_fences()` + 非贪婪 JSON 正则 |
| 13 | 规范文档声明了但运行时未读入，规则硬编码 Prompt | `src/index_timing.py` / `emotion_node.py` / `short_term.py` / `single_stock.py` | 运行时通过 `load_spec_text()` 读 `docs/*.md` 注入 prompt，文档即代码 |
| 14 | 延续信号 `premium_pct` 未返回，文案永远"0%溢价" | `src/short_term.py:hard_gate()` | gate 返回 `premium_pct`，`_assemble_result()` 正确渲染 |
| 15 | 所有 LLM 调用无 timeout，盘中可能永久阻塞 | 各策略模块 chat 调用 | 统一 `src/llm_gateway.py`：超时 / 重试 / 按 tier 限流 / token 统计 / 模型降级 |
| 16 | 指数择时 signal 允许 `观望`，且双阶段复核调用 `qwen3.7-max` 频繁超时（~90s） | `src/schema_validator.py` / `src/index_timing.py` | `VALID_SIGNALS` 收紧为 `多/空/转`；弃权/分歧时 signal 回退到 `转`；复核阶段改用 `qwen-plus` + 15s 超时 |
| 17 | 情绪节点低基数规则过强：前日大面≤10时绝对值判断直接判混沌，忽略主升确认硬条件 | `src/emotion_node.py` | 低基数下若同时满足主升确认硬条件（大面<20、跌停<10、涨停≥50或涨超7≥100、高度个股≥10、打板大面<10、指数上涨/平盘），允许判 `主升--确认`，不强制混沌 |

统一 LLM 网关 (`src/llm_gateway.py`) 要点：

- **超时**：默认 30s，避免网络抖动导致页面卡死；
- **重试**：同一模型失败后可重试，重试次数外部配置；
- **限流**：按 `qwen-turbo / qwen-plus / qwen-max` 滑动窗口限流，防止 429/退避；
- **Token 统计**：累计 input/output tokens 与调用次数，便于成本审计；
- **模型降级链**：支持首选模型 + fallback 模型，与生产 `ai_judge` 行为一致；
- **接入范围**：指数择时、情绪节点、短线模式、个股模式、主线模式、明日推演、回测均走网关。

### 7.3 安全工程

基于 `jishu.md` 安全改造落地，核心目标：**默认不暴露公网、敏感接口需鉴权、用户/AI 输入不直接进 HTML、共享文件写操作原子化**。

| # | 问题 | 位置 | 修改 |
|---|------|------|------|
| 16 | 8601/8602 绑 `0.0.0.0` 无登录无限流，任何人可触发 rejudge 烧 API Key | `run.sh` / `server.py` / `.streamlit/config.toml` | 监听 `127.0.0.1`；`ADMIN_API_KEY` + `X-API-Key` 鉴权 `/api/rejudge*`、`/api/rebuild-cache`；生产需配合反向代理 |
| 17 | CORS `["*"]` + Streamlit 关闭 XSRF | `server.py:83-88` / `.streamlit/config.toml` | CORS 白名单 `CORS_ORIGINS`；开启 `enableXsrfProtection` |
| 18 | 分组写共享 JSON 无锁；UI 多处 unsafe HTML 拼用户/AI 输入 | `src/stock_filter.py:545` / `ui.py` | `fcntl` 文件锁 + 临时文件原子写；`ui.py` 新增 `_e()` helper，所有 `unsafe_allow_html=True` 区块内的动态字符串（择时/情绪/主线/短线/个股/AI助手/评测）均先 `html.escape` |

### 7.3.1 多用户登录

| 项 | 实现 |
|---|---|
| 账号 | `src/auth.py`：SQLite（`.data/users.db`）+ bcrypt(12轮) 哈希；首个注册用户为 admin |
| 会话 | 32 字节随机 token 存 Redis（`jc:auth:session:*`），7 天有效 + 滑动续期；Redis 不可用降级进程内存 |
| 防爆破 | 同一用户名连续失败 5 次锁定 15 分钟（Redis 计数） |
| 前端 | `ui.py` 登录页拦截（未登录 `st.stop()`），状态栏显示用户 + 退出按钮；Streamlit 1.30 无 cookie API，token 走 URL query param（仅限 Tailscale 内网） |
| 数据隔离 | 分组按用户隔离（`stock_filter` 各接口带 `username` 参数，`scope` 字段命名空间） |
| 管理 | `python -m src.auth register/list/passwd/rm`；远程访问见 `docs/远程访问.md`（Tailscale） |

### 7.4 数据层演进

基于 `jishu.md` 数据层优化建议落地，不引入新时序库，扩展现有 DuckDB+Parquet：

| 优化项 | 状态 | 实现 |
|--------|------|------|
| L0内存 + L2 Redis 双层缓存 | 已落地 | `src/redis_cache.py:ttl_cache` — L1快路径(`l1_ttl`) → L2 Redis → `_MEM_CACHE` 兜底；SETNX+UUID token+Lua compare-and-delete 击穿防护；TTL `±10%` 抖动防雪崩；失败结果 5s 负缓存 |
| 个股日K入 Parquet/DuckDB | 已落地 | `src/ts_store.py` 新增 `save_stock_daily`/`load_stock_daily`/`stock_daily_freshness`/`get_stock_stats`（复用 `_atomic_save` 原子写+fcntl 锁、`get_index_stats` 的 DuckDB SQL 模式）；`src/data.py:get_stock_hist` 查询链改为 L1→Redis→parquet→HTTP，与 `_get_index_daily_full` 一致；顺带修复 `save_ths_index` 非原子覆盖写 |
| 当日分时数据 | 已落地 | `src/data.py:_tencent_minute` 腾讯分时接口 → 竞价量(09:30首笔)/涨速(最近1分钟)，用于选股筛选增强；`src/single_stock.py` 个股分时AI点评 |
| 历史分钟线（1分钟 OHLCV） | 已落地 | `src/ts_store.py:save_stock_minute`/`load_stock_minute`（按日 parquet，原子写）；`src/data.py:get_stock_minute`（akshare `stock_zh_a_hist_min_em`，缓存1天）；`server.py:_bg_pull_minute_kline` 盘后 15:10 补拉连板池+自选股，`sleep(1)` 限流 |
| 分级数据架构（Fuyao 降级源） | 已落地 | `src/data.py:get_stock_spot` 降级链 腾讯→Fuyao `snapshot_batch`→akshare；`get_stock_spot_fast` 失败时 Fuyao L2 降级（`ths_data.snapshot_batch`） |
| WebSocket L2 直连 | 部分落地（待凭证） | `src/ws_source.py` L2 抽象层（`L2Source` 基类 + `EastmoneyL2Client`/`ThsL2Client` 占位 + `get_l2_source` 工厂）；`server.py:_bg_l2_ws` 维持连接 + tick 写 Redis + `_l2_active` 标志，失败无缝降级轮询；凭证到位后仅需填充 Client 方法体 |
| 交易日历（痛点2） | 已落地 | `src/ths_data.py:trade_calendar`（Fuyao `/a-share/calendar/trading-days`，近一年）+ akshare `tool_trade_date_hist_sina` 兜底（全量历史 1990-2026）；`src/ts_store.py:save/load_trade_calendar`（parquet 持久化）；`src/data.py:get_trade_calendar`/`is_trading_day`/`get_prev_trade_day`（查询接口，缓存1天）；`config/settings.py:is_trading_hours` + `server.py:_warmup_interval` + `ui.py` 自动刷新均已接入日历，替换硬编码 `weekday()<5`；新增 `GET /api/trade-calendar` 端点 |
| 证券主数据时点化（痛点2） | 已落地 | `src/data.py:get_securities_snapshot`（`stock_info_a_code_name` + `stock_zh_a_st_em` → 代码/名称/st_status）；`src/ts_store.py:save/load/has_securities_snapshot` + `get_point_in_time_pool`（按日 parquet，时点股票池扫描取最近快照）；`server.py:_bg_save_securities_snapshot` 盘后每日落盘；新增 `GET /api/securities` 端点；`src/emotion_node.py:historical_market_stats` 加 `pool_codes` 参数按池过滤；`eval/backtest.py:backtest_emotion` 三处调用传入时点池，消除幸存者偏差；UI 状态栏显示交易日 chip |
| 真 WS 推送 + 前端增量更新（痛点3） | 已落地 | `src/ws_hub.py`：Redis Pub/Sub 广播中枢（频道 `jc:quotes`），上游**单点采集**一次 → 所有客户端共享推送，客户端数量不再放大上游请求；`server.py` 预热协程每轮发布 market 帧（平均股价/指数/股票数），`/ws/market` 与 `/ws/quotes/{code}` 改为订阅广播（首帧从 Redis 快照立即下发，订阅连接不设 socket_timeout 防空闲断开）；L2 tick（`_bg_l2_ws`）同样直进广播频道，凭证接入后毫秒级触达；`src/realtime_widget.py` 前端 JS 组件：浏览器直连 `/ws/market`，收到推送**只更新行情 bar DOM**（实时均价涨红跌绿闪变 + 指数 chip），断线自动重连退避；`ui.py` 指数择时页嵌入该 bar，整页 autorefresh 从竞价10s/盘中30s 降为统一 **60s**（仅重算当日K线/信号，不再承担实时性） |
| 数据质量看门狗（痛点4） | 已落地 | **校验器** `src/data_quality.py:validate_spot`（丢弃价格为0/NaN + 涨跌幅超法定涨跌停+2%的异常行）+ `validate_ohlc`（high≥max(open,close)、low≤min(open,close) 约束校验）；**熔断器** `src/source_health.py:SourceHealth`（连续失败3次→黑名单300s，到期自动恢复，内存状态无需 Redis）；**集成** `src/data.py:get_stock_spot` 每源调用前 `is_available` 检查 + 调用后 `record` 记录 + `validate_spot` 清洗，`get_stock_hist` 加 `validate_ohlc`；**降级返回** `_stale_spot()` 全源失败时从 Redis 取最近缓存标记 `stale=True`/`data_source=cache`，不返回空；**Prometheus 接入** `monitor.datasource_timer`/`datasource_error` 在每源调用点接入（此前零调用）；`server.py:/api/health` 新增 `source_health` 字段，`_bg_refresh_spot` 被熔断源跳过；`ui.py` 状态栏新增 `数据源健康: tencent✓ fuyao✓ akshare✓` chip |

## 八、评测体系

模型成功率拆为三层，对应代码分别落地在 `eval/test_output_reliability.py`、`eval/test_emotion.py` / `eval/test_timing.py`、`eval/test_trading_quality.py`。

```bash
python -m eval.run_all              # 无费用（含输出可靠性、真实交易质量）
python -m eval.run_all --all        # 含 LLM 调用
```

### 8.1 第一层：输出可靠性

目标：接近 100%。审计所有缓存预测记录（`timing_ai.json` / `emotion_ai.json`）：

- JSON / schema 合法率
- 枚举合法率（`signal` / `mid_cycle` / `node`）
- 证据可追溯率（`evidence` 引用特征是否在已知集合）
- 硬规则违规率（如 `position_cap` 是否由程序重算）
- 数据不新鲜时的弃权正确率

实现：`src/schema_validator.py` 负责校验；`eval/test_output_reliability.py` 批量审计并输出 `schema_validity_rate`。

### 8.2 第二层：标签一致性

用人工标注的复盘表作为标准答案，评估 AI 判断与标签是否一致：

| 模块 | 指标 |
|------|------|
| 指数择时 | `eval/test_timing.py` — 信号准确率、周期准确率 |
| 情绪节点 | `eval/test_emotion.py` — Macro-F1 / 混淆矩阵，关注危险类别误判（如“退潮误判为主升”） |
| 主线/个股/短线 | 待补充标注数据集 |

数据集位于 `eval/datasets/*.jsonl`。

### 8.3 第三层：真实交易质量

为每一条预测持久化到 `.data/predictions.jsonl`，schema 包含：

```text
prediction_id
as_of_timestamp
data_cutoff_timestamp
model_id
prompt_version
feature_version
signal / node / decision
confidence
evidence_snapshot
position_cap
future_outcome
```

实现：`src/predictions.py` 提供 `append_prediction()` / `load_predictions()` / `update_future_outcomes()`；`src/index_timing.py`、`src/emotion_node.py`、`src/short_term.py` 在生成预测时写入。

回填未来收益后，`eval/test_trading_quality.py` 按任务评估：

| 任务 | 指标 |
|------|------|
| 择时（timing） | 次日 / 3日 / 5日 方向命中率 |
| 情绪（emotion） | 节点后 1/3/5 日 883958 连板指数平均溢价 |
| 短线（shortterm） | 信号后 1/3/5 日连板指数收益、失败率 |

### 8.4 防止未来信息泄漏

回测日期为 T 时：

1. 只读取 T 时点可取得的数据；
2. 不读取 T 之后的 CSV 标签、题材归属或最终复盘结论；
3. 不把同日复盘标签混入输入后又拿它做标准答案；
4. 线上与回测使用同一 `FeatureProvider`、同一 prompt、同一模型版本；
5. 数据、特征、prompt、模型均记录版本（`PROMPT_VERSION` / `FEATURE_VERSION`）。

相关实现：`eval/backtest.py` 中做模型降级链、版本字段、 leakage 检查；`src/feature_provider.py` 统一特征构建。

## 九、稳定性设计

- **防缓存雪崩**: TTL 加随机抖动 `±10s`，分散过期时间
- **防缓存击穿**: Redis SETNX 分布式锁，热点 Key 单请求回源
- **数据源容错**: 多源降级(腾讯→新浪→akshare)，异常时返回旧缓存
- **同花顺数据更新**: 交易日收盘后强制走 HTTP，v6失败时 v4 实时接口补充当日

## 十、模型效率优化

### 10.1 线上与回测一致性

统一特征构建器 `FeatureProvider`：线上 `date_str=None` 读实时，回测 `date_str="YYYY-MM-DD"` 读历史。保证决策逻辑/prompt/模型版本完全一致，只数据来源不同。

### 10.2 结构化决策与 Schema 校验

每类 LLM 输出定义严格 Schema + 8步业务校验（枚举值/置信度/证据溯源/仓位重算/重试/降级），不确定时指数择时返回"转"、情绪节点返回"混沌"。

### 10.3 三层决策架构

| 层级 | 责任 | 实现 |
|------|------|------|
| 硬门控 | 程序 | `hard_gate()` — 事实性条件(space/883958/微盘)由程序判定 |
| 特征评分 | 程序 | `score_modes()` — 模式匹配数值评分，一字板自动排除 |
| LLM 裁决 | 模型 | `_llm_adjudicate()` — 仅边界案例(0.3<score<0.7)调用 |

效果: LLM 调用频次降低，prompt 精简 ~60%，硬门控不可被 LLM 覆盖。

### 10.4 双阶段复核

```
qwen-plus 初判 → Schema 校验
  → 低置信(<0.6)/弃权/跳变？
     ├─ 否：直接输出
     └─ 是：qwen3.7-max 对抗式复核(同一份特征)
              → 一致→输出 / 分歧→观望/降仓
```

集成: 择时(signal) / 情绪(node) / 短线(modes)，主题不接入(自由文本)。

## 十一、已知限制

1. **实时行情仍受上游轮询节奏限制** — 免费源 HTTP 30s 轮询，WS 推送的"实时性"目前是 30s 粒度（React 前端无整页 rerun，增量更新丝滑）；L2 凭证接入 `ws_source.py` 后才真正毫秒级
2. **上游免费数据源无 WebSocket** — 腾讯/新浪/东财/Fuyao 均为 HTTP 轮询；已建 L2 抽象层(`src/ws_source.py`)，待付费凭证
3. **明日推演前端待完善** — 后端逻辑完整，React 页面为占位，待接入展示
4. **memory.py 分层记忆待实现** — 接口已完成，写入逻辑待接入
5. **GLIBCXX 依赖** — 必须设置 `LD_LIBRARY_PATH`
6. **Streamlit 旧 UI 保留兼容** — `ui.py` + `pages/` 保留为后备，React 前端验证完毕后退役
7. **React 生产构建 token 存 localStorage** — 开发阶段用 localStorage 存 token，公网开放前应升级为 HttpOnly Cookie + SameSite（配合 Nginx 反向代理）

## 十二、前端架构（React 迁移）

### 12.1 技术选型

| 技术 | 用途 |
|------|------|
| React 18 + TypeScript | 组件化 UI，类型安全 |
| Vite | 开发服务器 + 构建工具 |
| Tailwind CSS | 原子化样式，复刻黑底/黄字/红涨绿跌主题 |
| ECharts (echarts-for-react) | K线蜡烛图 + 分时图 + 柱状图 |
| TanStack Query | 服务端状态管理 + 轮询缓存 |
| Zustand | 轻量客户端状态（auth/nav） |
| React Router DOM | 路由 + 认证守卫 |

### 12.2 API 端点全量

React 前端通过 Vite dev proxy 或 FastAPI StaticFiles 同域访问后端：

| 端点 | 方法 | 用途 |
|------|------|------|
| `/api/login` | POST | 用户名密码 → {token, user} |
| `/api/logout` | POST | 删除会话 |
| `/api/me` | GET | 当前用户信息 |
| `/api/timing` | GET | 今日择时判断 |
| `/api/timing/judge` | POST | 强制重新判断择时 |
| `/api/timing/signals` | GET | 历史多空转信号 |
| `/api/emotion` | GET | 今日情绪节点 |
| `/api/emotion/judge` | POST | 强制重新判断情绪 |
| `/api/avg_price_kline` | GET | 平均股价日K |
| `/api/market_stats` | GET | 盘面统计 |
| `/api/market_turnover` | GET | 全市场成交额 |
| `/api/spot` | GET | 全市场实时行情 |
| `/api/index` | GET | 指数实时行情 |
| `/api/hot` | GET | 热门股 |
| `/api/quote/{code}` | GET | 个股行情 |
| `/api/avg_price` | GET | 实时均价 |
| `/api/index_daily/{symbol}` | GET | 指数日K |
| `/api/chat/stream` | POST | SSE 流式聊天 |
| `/api/groups` | GET/POST | 分组 CRUD |
| `/api/filter` | POST | 自然语言筛选 |
| `/api/short_term` | POST | 短线模式分析 |
| `/api/theme` | POST | 主线识别 |
| `/api/theme/ai` | POST | AI主线判断 |
| `/api/single_stock` | POST | 个股筛选+AI判断 |
| `/api/eval/run` | POST | 运行评测 |
| `/ws/market` | WS | 全市场行情推送 |
| `/ws/quotes/{code}` | WS | 个股行情推送 |

### 12.3 部署形态

- **开发**：`cd frontend && npm run dev` → Vite dev server (5173)，proxy `/api` 和 `/ws` 到 8602
- **生产**：`cd frontend && npm run build` → `frontend/dist/` → FastAPI StaticFiles 同域挂载，访问 `http://localhost:8602` 直接提供 React 前端
- **CORS**：开发环境白名单含 `localhost:5173`；生产同域无跨域问题
