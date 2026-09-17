# UI 层重构设计（2026-07-24）

> 依据：对话中的「UI 菜单逐页综合评估」（7 页缺点清单），用户确认全部认同。
> 范围：**UI 层重构 + 必要的底层数据补强**。`src/data`、`src/trading`、`src/backtest` 引擎核心不动（369 测试 + 刚完成的二次审计修复保护）。

## 一、分析结论（为什么这样改）

评估暴露的缺点可归为四类根因：

| 根因 | 症状 | 对策 |
|---|---|---|
| **缺数据底座** | 仪表盘无净值曲线、回测无交易明细、QA 无成本感知 | 补 3 处存储：equity_snapshots 表、backtest_results.trades_detail 列、（llm_call_log 已有，补查询） |
| **UI 无核心层** | 7 个 DataManager 实例、无错误边界、红绿配色各页自造 | 新增 `src/ui/core.py`：共享 services 单例 + page_guard 错误边界 + 统一格式助手 |
| **god file** | screener.py 754 行、analysis.py 606 行 | 拆为同名 package（import 路径不变，app.py 零改动） |
| **小硬伤** | 回测默认区间写死 2024、日终按钮冗余、自选股无分组、页名漂移 | 逐点修复 |

## 二、设计

### 2.1 底层数据补强（先于 UI，可测试）

1. **`equity_snapshots` 表**（新）：`date TEXT PK, total_value, cash, market_value, updated_at`
   - 写入点：`TradingSimulator.snapshot_equity()`（用当前持仓价算总资产，upsert 当日）
   - 触发：dashboard/trading 页面渲染时若当日无快照则补记；`end_of_day` 后补记
   - 回撤曲线：查询时由 `cummax(total_value)` 现算，不落库
2. **`backtest_results.trades_detail`**（inline ALTER 加列，沿用 `_run_migrations` 模式）
   - `BTStrategyWrapper.notify_trade(trade)`：`trade.isclosed` 时收集 `{code, open_date, close_date, open_price, size, pnl, pnlcomm}`
   - `engine.run()` 返回 dict 带 `trades_detail`（JSON str）；comparator 透传存库
   - 旧行该列为 NULL，UI 显示「旧记录无明细」
3. **`watchlist.tags`**（inline ALTER 加列，TEXT DEFAULT ''，逗号分隔标签）

### 2.2 UI 核心层 `src/ui/core.py`

- `get_services()`：单一 `@st.cache_resource`，返回 `(DataManager, TradingSimulator, Advisor)` —— 全应用 1 个 engine
- `page_guard(fn)`：装饰器包住页面 render，异常 → `st.error` + log，不再红框糊脸
- 格式助手：`pct_span()`（涨红跌绿）、`metric_row()` 等

### 2.3 逐页改造

| 页 | 改动 |
|---|---|
| **dashboard** | +净值曲线（equity_snapshots，含回撤副图）、+情绪温度卡（读 market_sentiment 最新快照，不调 LLM）、持仓表红绿配色、删死 import |
| **数据管理** | 不动（评估为最扎实的页；线程模式记为 known limitation） |
| **screener** | 拆包 `screener/`（sidebar/cards/history/qa/progress）；QA 区显示本会话 token 消耗（`llm_call_log` 按 session started_at 过滤求和） |
| **watchlist** | +tags 编辑/筛选/排序（评分、添加时间）；价格提醒**不做**（需守护进程，独立立项） |
| **backtest** | 默认结束日期动态化（`date.today()`）；历史记录可选行 → 交易明细下钻；选 2 条 → 并排对比 + 指标 delta |
| **analysis** | 拆包 `analysis/`（sentiment/macro/deep）；页名改「🤖 AI 与市场分析」；新闻区加「距上次刷新 X」提示 |
| **trading** | 日终按钮 → 状态文本「T+1 已自动处理」；+净值 tab（与 dashboard 共用 `components/equity.py`） |
| **app.py** | render 经 page_guard；PAGES 键不变（import 路径不变） |

### 2.4 明确不做（边界）

- 价格提醒（需后台轮询守护进程，独立工程）
- 长任务 CLI 化（UI 线程模式保留，known limitation）
- `storage.py` 拆分（god object，独立 PR）
- 一键执行交易建议（安全设计，有意保留人工断点）

## 三、实现顺序与验证

1. storage 迁移 + 方法（+测试）→ 2. simulator.snapshot_equity（+测试）→ 3. engine notify_trade（+测试）
4. ui/core.py → 5. app.py 接线 → 6~11 逐页 → 12. 全量 pytest 369+ 保持绿 → 13. 文档同步（CLAUDE.md 页数 7、AGENTS.md）

**测试边界不变**：只测纯逻辑与 SQLite 集成，不测 Streamlit 渲染、不调网络/LLM。

## 四、实现状态（2026-09-17 完成）

全部落地，378 测试全绿（369 原有 + 9 新增底层），真实浏览器逐页冒烟通过：

- **底层**：storage 迁移 v6（`backtest_results.trades_detail`）/ v7（`watchlist.tags`）+ 新表 `equity_snapshots`；`save_backtest_result` 白名单过滤（顺带修掉「回测从未成功存库」的隐藏 bug）；`TradingSimulator.snapshot_equity()`（幂等，失败仅 warning）；回测引擎 `notify_trade` 收集逐笔平仓 → `trades_detail` JSON。
- **核心层**：`src/ui/core.py`（`get_services()` 单例 + `page_guard` 错误边界 + 涨红跌绿格式助手 + `enrich_profit`）；`components/equity.py` 净值+回撤双联图组件。
- **页面**：7 页全部重写/拆包；screener（754 行）与 analysis（606 行）拆为同名 package，`__init__.py` 再导出 `render`/`render_sidebar`（`_degradation_level` 保持再导出，`test_resilience` import 路径不变）。
- **验证中发现并修复**：dashboard/trading 侧边栏误用旧版 `service_info` 返回结构（KeyError 'installed'）；`Portfolio.summary()` 无 `total_profit` 键 → `enrich_profit` 现算。两者都是 page_guard 兜底后暴露的，印证错误边界的价值。
