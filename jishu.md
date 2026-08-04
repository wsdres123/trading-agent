主流APP的个股筛选器为什么这么快？
主流财经 App（如东方财富、同花顺、雪球、万得等）的个股筛选，本质上是一个**"在海量数据里，按多个条件瞬间找出符合条件的股票"**的问题。它们能做到毫秒级响应，靠的是一套"空间换时间"的组合技。

---

## 一、筛选个股用的是什么方法？

你可以把 A 股全市场 5000 多只股票想象成一张巨大的 Excel 表格，每只股票有几十列属性：价格、涨跌幅、市盈率、市值、所属行业、资金流向……

用户筛选时，可能同时勾选好几个条件：
> "市盈率 < 20，且市值 > 100 亿，且行业是新能源，且今日主力资金净流入 > 5000 万"

如果让数据库**逐行扫描**这 5000 多行，计算每一行是否符合条件，那至少要几十到几百毫秒。用户多了还会卡死。所以它们用的是下面这套技术组合拳：

### 1. 内存数据库 —— 数据放在"大脑"里，而不是"书本"里

普通数据库把数据存在硬盘上，查的时候像翻书一样，一页一页找，很慢。

财经 App 会把**热点数据**（比如当日的行情、财务指标、资金流向）全部加载到**内存**（RAM）里。内存的读取速度比硬盘快 1 万倍以上。

> 就像考试前把重点内容背下来，考试时直接"想"出来，而不是去翻书。

万得等金融数据服务商甚至专门研发了**基于内存的倒排索引系统**，在 1 亿条数据量下，多字段组合查询平均响应时间控制在 **10 毫秒以下**。

### 2. 倒排索引 —— 像字典的"索引页"

传统查字典是按页翻，倒排索引是先看"索引页"：哪个字在第几页。

应用到股票筛选上：
- 把所有"市盈率 < 20"的股票 ID 提前存成一个列表
- 把所有"新能源行业"的股票 ID 提前存成一个列表
- 把所有"市值 > 100 亿"的股票 ID 提前存成一个列表

用户筛选时，系统不需要看每一只股票，而是**直接把这几个列表取交集**，瞬间得到结果。

### 3. 位图索引（Bitmap Index）—— 用 0 和 1 做"开关"

这是处理多条件筛选的利器。

假设有 5000 只股票，系统给每个筛选条件（比如"市盈率<20"）维护一个长度为 5000 的位图：

```
股票列表: [茅台, 宁德, 比亚迪, 中芯, ...]
市盈率<20: [  0,    1,     1,    0, ...]  ← 1表示符合，0表示不符合
新能源行业: [  0,    1,     1,    0, ...]
市值>100亿: [  1,    1,     1,    1, ...]
```

用户勾选三个条件时，系统只需要做**位运算（AND）**：
```
  0 1 1 0 ...
& 0 1 1 0 ...
& 1 1 1 1 ...
= 0 1 1 0 ...  ← 第2、3只股票符合
```

CPU 做位运算极快，一次能算 64 位，5000 只股票只要几十个 CPU 周期，**纳秒级**就能完成交集计算。

### 4. B+ 树索引 —— 范围查询的"快速通道"

对于"价格在 10~20 元之间"这种范围查询，系统用 B+ 树索引。它像一本有层级目录的书，能快速定位到某个区间，不需要逐行扫描。

实测中，B+ 树在百万级数据下主键查找平均只需 **2 毫秒**。

### 5. 预计算 + 缓存 —— 把常见答案提前准备好

80% 的用户筛选条件其实就那几种组合（比如"低市盈率+高股息"、"涨停股"等）。系统会**提前把热门组合的结果算好，存在缓存里**。

下次有人点同样的筛选条件，直接读缓存，连计算都省了。

---

## 二、为什么能做到毫秒级？—— 四层"作弊"叠加

| 层级 | 手段 | 效果 |
|------|------|------|
| **数据层** | 热点数据全放内存，冷数据放 SSD | 读取速度从毫秒级降到微秒级 |
| **索引层** | 倒排索引 + 位图索引 + B+ 树 | 从"逐行扫描"变成"直接定位"，复杂度从 O(n) 降到 O(1) 或 O(log n) |
| **计算层** | 位运算、SIMD 向量化、CPU 缓存优化 | 一次处理 64/128/256 位数据，榨干 CPU 性能 |
| **架构层** | 分布式分片、结果缓存、CDN 加速 | 把压力分散到多台机器，热门查询直接返回缓存 |

---

## 三、通俗比喻

想象你在一个超级图书馆找书：

- **没有优化时**：你一本一本翻，看每本书是否符合条件（逐行扫描）→ 慢到崩溃。
- **内存数据库**：把热门书架搬到你眼前，不用去仓库取（数据在内存）。
- **倒排索引**：图书馆有一本"分类索引册"，告诉你"科幻小说在第 3、7、12 号书架"（先查索引）。
- **位图索引**：每个书架有个红绿灯，绿灯表示符合你的某个条件，你只需要看同时亮绿灯的书架（位运算交集）。
- **缓存**：图书管理员发现很多人问同一本书，直接放在柜台，伸手就能拿。

---

## 一句话总结

> **主流财经 App 用"内存存储 + 倒排/位图/B+树索引 + 预计算缓存"的组合拳，把"逐行翻 Excel"变成了"查字典 + 按开关"，所以能在毫秒级完成多条件个股筛选。**



模型性能优化
1. Prompt Caching（收益最大，改动最小）

  当前每次 LLM 调用都重复发送完整的 system prompt + RAG 知识片段（可能 2000-4000 tokens）。DashScope 兼容 OpenAI 的 extra_body 透传方式支持 prompt caching：

  # ai_assistant.py chat() 中
  resp = _client.chat.completions.create(
      model=cfg.QWEN_CHAT_MODEL,
      messages=messages,
      tools=market_tools.TOOL_SCHEMAS,
      tool_choice="auto",
      temperature=0.4,
      extra_body={"enable_thinking": False},  # 不需要思考的任务关掉
  )

  建议：
  - 把 SYSTEM_PROMPT 的固定部分（角色定义 + 规则）和动态部分（RAG context）分开，固定部分走缓存
  - DashScope 的 qwen-plus/max 支持 上下文缓存，首次写入后同一前缀后续调用延迟降 50%+、费用降 50%
  - knowledge.search() 返回的 chunks 如果相似度高，可以复用同一份缓存前缀

  2. 模型降级路由（省钱省时间）

  当前 parse_filter_conditions() 用 qwen3.7-max（最贵的模型）做结构化 JSON 提取，这个任务完全可以用 qwen-turbo 甚至更小的模型完成：

  # 当前
  model=cfg.QWEN_CHAT_MODEL  # qwen3.7-max

  # 建议
  model="qwen-turbo"  # 结构化提取不需要 max

  分级建议：

  ┌──────────────┬─────────────┬────────────────────┬──────────────────────┐
  │     任务     │  当前模型   │      建议模型      │         理由         │
  ├──────────────┼─────────────┼────────────────────┼──────────────────────┤
  │ 筛选条件解析 │ qwen3.7-max │ qwen-turbo         │ 纯结构化提取         │
  ├──────────────┼─────────────┼────────────────────┼──────────────────────┤
  │ 情绪节点判断 │ qwen-plus   │ qwen-plus（保持）  │ 需要推理能力         │
  ├──────────────┼─────────────┼────────────────────┼──────────────────────┤
  │ 个股分时点评 │ qwen-turbo  │ qwen-turbo（保持） │ 已是最优             │
  ├──────────────┼─────────────┼────────────────────┼──────────────────────┤
  │ AI 助手对话  │ qwen3.7-max │ qwen-plus          │ 大部分问题不需要 max │
  ├──────────────┼─────────────┼────────────────────┼──────────────────────┤
  │ 明日推演     │ qwen-plus   │ qwen-plus（保持）  │ 需要综合推理         │
  └──────────────┴─────────────┴────────────────────┴──────────────────────┘

  3. 流式输出（感知延迟优化）

  当前 chat() 是同步等待完整响应。改为 streaming 可以让用户 1-2 秒内看到第一个字：

  stream = _client.chat.completions.create(
      model=cfg.QWEN_CHAT_MODEL,
      messages=messages,
      tools=market_tools.TOOL_SCHEMAS,
      stream=True,
  )
  for chunk in stream:
      delta = chunk.choices[0].delta
      if delta.content:
          yield delta.content

  Streamlit 的 st.write_stream() 原生支持生成器，改动很小。

  4. RAG 检索优化

  当前：400 字分块 + 60 字重叠 + 纯向量检索。

  建议：
  - 分块调大：改为 800 字 + 200 字重叠。交易知识段落通常较长，400 字容易截断语义
  - 加 BM25 混合检索：纯向量对精确术语（"打板大面""连板梯队"）检索不准，加 BM25 做 RRFRank 融合
  - 缓存嵌入 batch：当前 _embed_cache.json 逐条嵌入，初始化时应该 batch 调用（DashScope 支持一次最多 25 条）

  5. 工具调用轮次压缩

  MAX_TOOL_ROUNDS = 5，但绝大多数问题 0-1 轮就够了。建议：
  - 加一个提前终止：如果连续 2 轮工具返回的数据模型没用到，提前结束
  - 或者把 MAX 降到 3，超限直接返回"请简化问题"
  
  
  
  
  api性能优化
  1. WebSocket 替代轮询（最大改进点）

  当前 Streamlit 用 st_autorefresh 每 N 秒整页刷新，而 WebSocket 已经写好但没人消费。

  建议：用 streamlit-js-eval 或 streamlit-component 消费 WebSocket，避免整页重渲染。这能：
  - 消除 st_autorefresh 导致的 Plotly 图表重建（每次刷新 Plotly 重新渲染是大头开销）
  - 数据新鲜度从"刷新间隔"变成"推送即更新"

  如果不想改 Streamlit，至少把 st_autorefresh 的间隔从固定值改成盘中 10s + 盘外 300s，减少非交易时段无意义请求。

  2. 序列化从 pickle 换 msgpack/orjson

  当前 Redis 用 pickle 序列化 DataFrame，每次 redis_get 都要 pickle.loads。

  建议：
  - DataFrame 用 parquet 序列化到内存（df.to_parquet() → bytes），比 pickle 快 3-5x
  - 简单 dict/list 用 msgpack 替代 pickle，速度快且跨语言安全
  - pickle 还有安全风险（架构文档已知限制 #4）

  import msgpack

  def _serialize(val):
      if isinstance(val, pd.DataFrame):
          return b'__df__' + val.to_parquet(compression=None)
      return msgpack.dumps(val, use_bin_type=True)

  def _deserialize(data):
      if data.startswith(b'__df__'):
          return pd.read_parquet(io.BytesIO(data[6:]))
      return msgpack.loads(data, raw=False)

  3. Redis 连接池复用

  当前 redis_get / redis_set 每次都 Redis(connection_pool=_redis_pool) 创建新 client 实例。虽然连接池是复用的，但 client 对象创建本身有开销。

  建议：保持一个全局 r = Redis(connection_pool=...) 实例，直接复用：

  _redis_client = None

  def _get_client():
      global _redis_client
      if _redis_client is None:
          _redis_client = redis.Redis(connection_pool=_redis_pool)
      return _redis_client

  4. 后台预热自适应频率

  当前 60s 固定间隔。但：
  - 9:25-9:30 竞价期间数据变化极快，应该 10-15s
  - 11:30-13:00 午休不需要预热
  - 14:50-15:00 尾盘应该加密到 15s

  def _warmup_interval() -> int:
      now = datetime.now()
      m = now.hour * 60 + now.minute
      if 565 <= m <= 570:   return 10   # 竞价
      if 690 <= m <= 780:  return 60   # 上午盘
      if 780 <= m <= 840:  return 0    # 午休，跳过
      if 840 <= m <= 900:  return 60   # 下午盘
      if 900 <= m <= 905:  return 15   # 尾盘
      return 300                         # 非交易时段

  5. httpx 连接池优化

  当前 max_connections=100 是全局上限，但没有设置 per-host 限制。腾讯/新浪/东财各占多少连接不可控。

  建议：
  limits = httpx.Limits(
      max_connections=100,
      max_keepalive_connections=20,
      keepalive_expiry=20,
  )   
  # 对腾讯这种大批量请求，单独设 per-host
  # 通过 mounts 实现不同域名不同配置

  6. 压缩 Redis 大值

  全市场快照 get_stock_spot() 返回 5533 只股票的 DataFrame，pickle 后可能几 MB。

  建议：加一层 gzip/zstd 压缩：
  import gzip
  def redis_set(key, val, ttl):
      data = gzip.compress(_serialize(val))  # 压缩后通常缩小 5-10x
      r.setex(key, int(ttl), data)
      
      
评测
一、评测维度总览

  ┌─────────────────────────────────────────────────────────────┐
  │                    评测体系 (eval/)                           │
  ├──────────────────┬──────────────────────────────────────────┤
  │  需要标准答案     │  · 数据准确性    · 选股筛选              │
  │  (Ground Truth)  │  · 买卖点判断    · 情绪节点分类           │
  │                  │  · 指数择时信号  · RAG 检索召回           │
  ├──────────────────┼──────────────────────────────────────────┤
  │  不需要标准答案   │  · LLM 输出质量 (LLM-as-Judge)          │
  │  (自动评估)      │  · 工具调用正确性  · 响应一致性           │
  │                  │  · 延迟/成本/吞吐  · 回归对比             │
  └──────────────────┴──────────────────────────────────────────┘

  ---
  二、各维度详细设计

  维度 1：数据准确性 — 不需要标准答案

  数据层有客观可验证的事实，可以自动断言：

  # eval/test_data_accuracy.py
  def test_spot_columns():
      """全市场快照必须包含关键字段"""
      df = get_stock_spot()
      required = {"代码", "名称", "现价", "涨跌幅", "成交额"}
      assert required.issubset(df.columns)
      assert len(df) > 5000  # A股数量

  def test_index_range():
      """指数涨跌幅不应出现离谱值"""
      idx = get_index_spot()
      assert (idx["涨跌幅"].abs() < 12).all()  # 指数单日不可能±12%

  def test_hist_continuity():
      """日K线日期应连续（无缺失交易日）"""
      df = get_stock_hist("000001", days=60)
      dates = pd.to_datetime(df["date"])
      gaps = dates.diff().dt.days.dropna()
      assert (gaps <= 3).all()  # 跨周末最多3天

  需要你提供：不需要，规则即标准。

  ---
  维度 2：情绪节点分类 — 需要标准答案

  这是你系统最核心的 AI 判断，也是最容易出错的。

  # eval/datasets/emotion_cases.jsonl
  {"date": "2025-01-15", "stats": {"大面": 12, "跌停": 8, ...}, "expected_node": "退潮加速", "source": "复盘表"}
  {"date": "2025-03-20", "stats": {...}, "expected_node": "主升一致", "source": "复盘表"}

  # eval/test_emotion.py
  def test_emotion_accuracy():
      """用历史复盘表验证情绪节点判断"""
      cases = load_jsonl("datasets/emotion_cases.jsonl")
      correct = 0
      for case in cases:
          predicted = emotion_node.ai_judge(case["stats"])
          if predicted == case["expected_node"]:
              correct += 1
      accuracy = correct / len(cases)
      assert accuracy >= 0.75  # 目标准确率

  需要你提供：是的。从 knowledge/屠龙表 - 复盘表.csv 中提取历史日期 + 对应的情绪节点标注，这就是天然的标准答案集。你已经有几百条复盘数据了。

  关键指标：
  - 准确率（整体）
  - 混淆矩阵（哪两个节点容易互相误判）
  - 边界案例通过率（大面=8 这种临界值判断对不对）

  ---
  维度 3：指数择时信号 — 需要标准答案

  同理，复盘表里有每日的多/空/转信号。

  # eval/datasets/timing_cases.jsonl
  {"date": "2025-02-10", "expected_signal": "多", "expected_cycle": "B"}

  指标：信号准确率、周期判断准确率、信号延迟天数（真实转折点后几天才检测到）。

  需要你提供：是的，同样来自复盘表。

  ---
  维度 4：选股筛选 NLP 解析 — 需要标准答案

  parse_filter_conditions() 把自然语言转 JSON，这个必须有标准答案：

  # eval/datasets/filter_nlp.jsonl
  {"input": "百日新高且成交额大于30亿的非北交所股票",
   "expected": [
     {"field": "new_high", "days": 100},
     {"field": "amount", "min_yi": 30},
     {"field": "board", "name": "北交所", "exclude": true}
   ]}

  # eval/test_filter_nlp.py
  def test_nlp_parse():
      cases = load_jsonl("datasets/filter_nlp.jsonl")
      for case in cases:
          result = parse_filter_conditions(case["input"])
          assert conditions_match(result["conditions"], case["expected"])

  指标：条件召回率（漏了几个条件）、条件精确率（多解析了几个）、字段正确率。

  需要你提供：是的。建议覆盖 30-50 个典型表达，包括：
  - 简单条件："股价大于20元"
  - 组合条件："百日新高且换手率大于5%"
  - 否定条件："不要北交所"
  - 模糊表达："放量拉升的票"

  ---
  维度 5：RAG 检索质量 — 需要标准答案

  # eval/datasets/rag_queries.jsonl
  {"query": "趋势A周期主线怎么选", "expected_keywords": ["30日涨幅", "50%", "共同板块"]}
  {"query": "打板大面怎么处理", "expected_keywords": ["空仓", "退潮", "纪律"]}

  指标：Top-K 召回率（标准答案中的关键词是否出现在返回的 chunks 中）。

  需要你提供：是的，但只需 20-30 个 query + 期望出现的关键内容片段。

  ---
  维度 6：LLM 输出质量 — 不需要标准答案（LLM-as-Judge）

  用另一个模型（或同模型不同 prompt）打分：

  # eval/test_llm_quality.py
  JUDGE_PROMPT = """你是评测员，对以下AI回答打分(1-5)：
  评分维度：
  1. 事实准确性：是否引用了正确数据
  2. 可操作性：是否给出了具体建议
  3. 简洁性：是否废话过多
  4. 风险意识：是否提示了风险

  用户问题：{question}
  AI回答：{answer}
  请输出JSON：{"factual": N, "actionable": N, "concise": N, "risk_aware": N}"""

  def test_ai_assistant_quality():
      questions = [
          "今天大盘怎么样？",
          "半导体板块还能追吗？",
          "我现在满仓怎么办？",
      ]
      for q in questions:
          answer = ai_assistant.chat(q)
          scores = judge(q, answer)
          assert scores["factual"] >= 3

  不需要你提供标准答案，但需要你定义评分维度和阈值。

  ---
  维度 7：工具调用正确性 — 不需要标准答案

  验证 AI 是否调用了正确的工具、传了正确的参数：

  # eval/test_tool_routing.py
  CASES = [
      {"question": "今天上证涨了多少", "expected_tool": "get_index_quote", "expected_args": {"symbol": "sh000001"}},
      {"question": "贵州茅台现在多少钱", "expected_tool": "get_stock_quote", "expected_args": {"code": "600519"}},
      {"question": "帮我看看半导体板块", "expected_tool": "get_board_quote", "expected_args": {"name": "半导体"}},
  ]

  def test_tool_routing():
      for case in CASES:
          # 拦截工具调用，不实际执行
          calls = capture_tool_calls(case["question"])
          assert calls[0]["name"] == case["expected_tool"]

  不需要标准答案，但需要你提供 test cases（问题 → 期望工具名）。

  ---
  维度 8：性能基准 — 不需要标准答案

  # eval/test_benchmark.py
  def test_latency():
      """关键操作的延迟上限"""
      assert benchmark(get_stock_spot) < 100     # Redis命中 < 100ms
      assert benchmark(ai_assistant.chat, "今天大盘") < 5000  # LLM < 5s

  def test_cost():
      """单次对话的token消耗上限"""
      tokens = count_tokens(ai_assistant.chat, "百日新高怎么选")
      assert tokens["total"] < 8000  # 单次不超过8k token

  def test_regression(prev_results: dict):
      """回归对比：新版本不能比旧版本差"""
      curr = run_all_evals()
      for metric in ["emotion_accuracy", "filter_parse_accuracy", "avg_latency"]:
          assert curr[metric] >= prev_results[metric] * 0.95  # 允许5%波动

  ---
  三、总结：哪些需要你提供标准答案

  ┌────────────┬────────────────┬─────────────────────┬──────────┐
  │    维度    │ 需要标准答案？ │      数据来源       │ 建议数量 │
  ├────────────┼────────────────┼─────────────────────┼──────────┤
  │ 数据准确性 │       否       │ 规则即标准          │    —     │
  ├────────────┼────────────────┼─────────────────────┼──────────┤
  │ 情绪节点   │       是       │ 复盘表.csv          │ 50-100条 │
  ├────────────┼────────────────┼─────────────────────┼──────────┤
  │ 指数择时   │       是       │ 复盘表.csv          │ 50-100条 │
  ├────────────┼────────────────┼─────────────────────┼──────────┤
  │ 筛选NLP    │       是       │ 你手写              │ 30-50条  │
  ├────────────┼────────────────┼─────────────────────┼──────────┤
  │ RAG检索    │       是       │ 你手写              │ 20-30条  │
  ├────────────┼────────────────┼─────────────────────┼──────────┤
  │ LLM质量    │       否       │ LLM-as-Judge        │    —     │
  ├────────────┼────────────────┼─────────────────────┼──────────┤
  │ 工具路由   │     半自动     │ 你定义问题→工具映射 │ 20-30条  │
  ├────────────┼────────────────┼─────────────────────┼──────────┤
  │ 性能基准   │       否       │ 自动测量            │    —     │
  └────────────┴────────────────┴─────────────────────┴──────────┘

  好消息：你最大的标准答案来源就是 knowledge/屠龙表 - 复盘表.csv，里面已经有每日的情绪节点、择时信号、复盘数据——这是天然 labeled dataset，不需要额外标注。

  ---
  四、建议的目录结构

  eval/
  ├── datasets/
  │   ├── emotion_cases.jsonl        # 从复盘表提取
  │   ├── timing_cases.jsonl         # 从复盘表提取
  │   ├── filter_nlp.jsonl           # 手写
  │   └── rag_queries.jsonl          # 手写
  ├── test_data_accuracy.py          # 自动，无需标注
  ├── test_emotion.py                # 需标注
  ├── test_timing.py                 # 需标注
  ├── test_filter_nlp.py             # 需标注
  ├── test_rag_recall.py             # 需标注
  ├── test_llm_quality.py            # LLM-as-Judge
  ├── test_tool_routing.py           # 半自动
  ├── test_benchmark.py              # 自动
  ├── run_all.py                     # 汇总报告
  └── results/
      └── 2026-08-03.json            # 每次运行的快照（供回归对比）



api 2次优化

一、数据层：从“能用”到“毫秒级精准”
你用了Redis做缓存，还做了Parquet+Msgpack+gzip的序列化优化，这个思路很好。但前排软件在数据链路上追求的是确定性低延迟和数据一致性。

分层缓存策略升级（L1内存 + L2 Redis）

现状：你的data.py是“Redis优先→内存兜底”，Redis挂了才用内存。

优化建议：采用L1内存缓存 + L2 Redis缓存的双层架构。对于spot=60s这类秒级热数据，第一次查询走Redis拿到后，直接在进程内存（L1）里也存一份d，TTL设更短（如10秒）。后续请求直接命中内存，连Redis的网络IO都省了，响应时间从几毫秒降到微秒级。这在高频刷新的Streamlit页面上效果会非常明显。

参考实现：可以用cachetools或自定义CacheManager实现L1，L2仍用Redis。

引入专业时序数据库，分担Redis压力

现状：所有行情快照和日K都压在Redis里，键格式用了sha256，查询灵活性有限。

优化建议：对于历史K线、日K、板块指数这类量大且需要复杂查询（如区间统计、多周期聚合）的数据，从Redis迁出，交给专业的时序数据库（如DolphinDB、InfluxDB）或列式存储（如ClickHouse）。

好处：专业时序库支持向量化计算，做“过去20日均线”“板块资金流向”这种计算比在Python里用Pandas快得多，而且能直接对接到你的LLM工具调用里。Redis则专注于存储最新的实时快照和会话状态，各司其职。

WebSocket行情直连与本地持久化

现状：从架构图看，数据源主要是通过HTTP轮询（qt.gtimg.cn等）获取。

优化建议：前排软件都使用WebSocket长连接接收L2行情推送，而不是轮询。你的架构中虽然有/ws/market，但那是FastAPI对外提供的，建议后端服务自身也通过WebSocket与数据源（如东方财富、同花顺的L2接口）建立持久连接。这样能：

将最新TICK数据实时推送到Redis，替代轮询的spot=60s，延迟从秒级降到毫秒级。

在本地（或挂载的SSD）持久化一份行情快照，用于盘后回测和策略复盘，避免每次都去拉取历史数据。


模型性能2次优化
构建“分层记忆”系统

现状：LLM的上下文主要依靠docs/*.md和knowledge/*.csv，以及每次对话的上下文。

优化建议：参考前沿的FinMem架构，为你的AI构建分层记忆（Layered Memory）。

短期记忆：存储当前会话的对话和操作（你已有）。

中期记忆：存储过去几天的交易记录、市场复盘结果，存入向量数据库，供策略反思时检索。

长期记忆：存储成功/失败的交易案例、市场规律，用于Agent的“经验”积累，让它随着使用越来越“懂”你的交易风格。








