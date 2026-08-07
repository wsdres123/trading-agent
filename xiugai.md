# 修改建议：与主流软件的差距（本轮优化后）

已落地：双阶段复核、LLM 网关、数据质量看门狗、熔断降级、时点股票池、WS 广播、三层评测、多用户登录、React 前端大版本（两版迭代）。以下为剩余差距。（2026-08-06 五次更新：按"只留必要修复"原则精简；分时图实时化经确认为必要，回补至第四节）

## 一、工程基建（最优先）
1. **无依赖锁定**：无 requirements.txt / pyproject.toml。akshare/东财等上游接口月月在变，版本不锁定一次升级即全线崩。立即 `pip freeze` 落盘并区分运行/开发依赖。
2. **单机单点**：Redis 单实例无持久化配置，重启丢会话与负缓存；.data/ts 无备份。先开 Redis AOF + crontab 每日备份 .data/ts。

## 二、数据：与主流行情软件的核心差距
3. **实时性**：免费源 HTTP 30s 轮询 vs 主流亚秒推送。ws_source.py 的 L2 Client 仍是占位，凭证接入是下一阶段头号里程碑。
4. **Tick/盘口/逐笔缺失**：打板判断（炸板、排队、撤单）的核心数据，通达信 L2/淘股吧标配。可先只订阅自选+连板池几十只。
5. **资金流缺失**：无北向资金、主力净流入、涨停股资金流——同花顺/东财核心页面，AI 证据里完全没有资金面。
6. **基本面与事件**：无财报/公告/事件日历（解禁、股东大会、龙虎榜明细），事件驱动是主流软件标配。
7. **历史深度**：日K仅约 1060 天，分钟线仅盘后补拉部分标的；主流是 10 年全量 + 全市场分钟线，盘中回测无基础。

## 三、交易闭环（最大功能差距）
8. **无模拟盘**：预测只写 predictions.jsonl，没有持仓/仓位/盈亏账本。增加 paper trading 模块，按 position_cap 落账、按收盘回填盈亏。
9. **无风控硬门控**：个股止损/止盈/移动止损、总仓位上限、单日亏损熔断均无实现；主流软件风控是程序级硬约束（与 D 周期 cap=0 同模式推广）。
10. **无信号推送**：起变/择时跳变/止损触发不会主动通知；主流是微信/飞书推送。monitor.py 已有飞书 webhook，复用成本极低。
11. **无券商下单抽象**：QMT/Ptrade/easytrader 暂不接也要留接口层，否则永远是"看盘软件"不是"交易软件"。
12. **无自选股/持仓页**：stock_filter 分组是筛选分组，缺每人自选 + 持仓盈亏视图。

## 四、前端体验 — React 必要修复（上轮 A/B 问题已全部修复并实测确认）

13. **React 版无"首次创建管理员"入口**：Streamlit 版有 bootstrap_form（ui.py:115-130），React Login 只有用户名/密码。全新部署无账号时走不通。加 bootstrap 端点 + 条件 UI，或明文规定走 Streamlit/CLI 建号。
14. **token 仍在 localStorage + Bearer 头**（上轮安全项唯一遗留）：公网前必须改 HttpOnly Cookie + SameSite，apiFetch 去掉手工 Authorization 拼装、auth.ts 去掉 localStorage。XSS 即窃取 token 的风险仍在。
15. **useQuoteWS 卸载时重连泄漏**（ws.ts:82-87，潜伏 bug）：cleanup 的 close() 触发 onclose 又排新定时器（该定时器不会被 clearTimeout）。16 依赖该 hook，启用前必修。
16. **分时图非实时**（必要）：IntradayPanel 用 getQuote 一次性拉取，盘中打开后数字静止，个股页最核心的盘中体验缺失。实现：分钟序列仍走 getQuote 打底，挂 useQuoteWS(code) 把推送的最新价实时 append/更新到序列尾部（IntradayChart 已支持增量数据重绘）。
17. **明日推演仍是占位页**：tomorrow.py 后端逻辑完整，七大功能中唯一未迁移项。加 /api/tomorrow + 页面渲染。
18. **长任务无进度**：筛选缓存构建（15s-2min）与 /api/eval/run 是阻塞请求，无进度反馈且有请求超时风险。迁 /api/task/* 任务流 + SSE 进度推送。

完成 13/17/18 即可触发 Streamlit 8601 退役（下线 ui.py/pages/ 与 streamlit 依赖），双轨运行结束。

## 五、AI 与评测运营
19. **标注数据只覆盖 timing/emotion/filter/rag**：主线、短线、个股无 ground truth，模型迭代盲目。先人工积累各 100 条。
20. **预测回填未常态化**：update_future_outcomes 需每日盘后跑，接入 cron，评测报告页展示命中率随时间趋势曲线。
21. **memory.py 未落地**：jiagou.md 引用了该文件但实际不存在，MTM/LTM 设计（jishu.md 第四节）未实现，AI 助手仍每次冷启动。按原设计补写约 100 行。
22. **成本无熔断**：llm_gateway 有 token 统计但无每日预算上限与按用户配额，恶意连续 rejudge 可烧穿额度（/api/theme/ai 已加鉴权但无限频）。
23. **无 prompt 灰度**：PROMPT_VERSION 只记录不对比。加影子模式：rejudge 时新旧 prompt 双跑、只记录不生效，积累一周再切换。

## 六、部署运维
24. **容器化缺失**：无 Dockerfile/compose，三进程靠 start.sh 手工管理；改 Docker Compose + restart=always + systemd 托管。
25. **日志无治理**：排查靠 nohup.out；统一 logging 配置、按天滚动、ERROR 级接飞书。

## 七、执行顺序建议
- **P0（本周）**：依赖锁定、Redis AOF + .data 备份、LLM 每日预算熔断、React 收尾三件（14 Cookie 化 token / 13 管理员入口 / 17 明日推演页）
- **P1（两周内）**：信号飞书推送、风控硬门控、模拟盘账本（交易闭环最小集）、16 分时图实时化（含 15 修 useQuoteWS）、18 长任务进度
- **P2（一月内）**：资金流接入、板块热力图、L2 凭证落地、Streamlit 退役
- **P3（持续）**：memory.py、标注数据集扩充、prompt 影子模式
