# a-transaction2_claude（A股智能交易/研究系统）

> 架构细节（6 层流水线、SQLite schema、数据源路由机制）见 `CLAUDE.md`，本文只记差分信息：命令、约定、坑。

## 命令

- 启动 UI：`streamlit run src/ui/app.py`（Windows 用 `run_streamlit.bat`，**固定端口 8602**，勿随意改）。必须从项目根运行。
- 测试：`pytest`（项目根直接跑，`pytest.ini` 已设 `pythonpath = .`，无需手动设 PYTHONPATH）。其他入口（scripts、REPL）则需项目根在 PYTHONPATH 上。
- 按 marker 跑：`pytest -m unit` / `pytest -m integration`。`--strict-markers` 已开启，新 marker 必须先在 `pytest.ini` 注册，否则报错。
- 测试依赖单独装：`pip install -r requirements-dev.txt`（不混入运行时 `requirements.txt`）。
- **没有配置任何 linter / type-checker**，不要假设有格式化门禁。

## 约定

- 密钥注入优先级：真实 `os.environ` > 项目根 `.env` > `config/config.yaml` 占位符。`src/config.py` 自带零依赖 `.env` 加载器，**不要引入 python-dotenv**。新密钥走环境变量并在 `.env.example` 登记变量名。
- UI 页面契约：在 `src/ui/page_modules/` 放模块（大页可拆为同名 package，`__init__.py` 再导出 `render`/`render_sidebar`），并在 `app.py` 的 `PAGES` 字典注册。共享服务一律走 `src/ui/core.py` 的 `get_dm`/`get_simulator`/`get_advisor` 单例，**不要再在页面里各自 `@st.cache_resource` 造 DataManager**（2026-07 重构已收敛，此前 7 页 7 个 engine）。详见下方"坑"。
- 新增策略：继承 `BaseStrategy` 实现 `screen()`，可选覆写 `evaluate_stock()`（`supports_evaluate()` 自动变 True，AutoTrader 才会用它），然后在 `src/strategy/screener.py` 的 `STRATEGY_REGISTRY` 注册——UI 无需改动。
- 新增数据源：继承 `BaseFetcher`，不支持的 data_type **必须 raise `NotImplementedError`**（路由层靠它静默跳过该源，不算失败）；在 `DataManager.__init__` 注册并加 `data_source_routes` 行。
- 测试只覆盖纯逻辑与 SQLite 集成（tmp_path 临时库）；**刻意不写**依赖真实网络（akshare/tushare/cninfo）、LLM API、Streamlit UI 的测试——新增测试也请保持这条边界。

## 禁区与坑

- **`src/ui/page_modules/` 绝不能改名回 `pages/`**：Streamlit 原生多页应用会扫描入口脚本同级的 `pages/` 目录，自动生成一套英文坏导航（`render()` 不会被调用），与 `app.py` 的手动中文导航叠加。
- 不可提交/不可手改：`.env`、`config/config.yaml`（提交的是 `config/config.example.yaml`）、`data/*.db`（已在 .gitignore）。
- A 股规则已内建且测试覆盖，改动前先读 `tests/test_trading_rules.py` / `test_portfolio.py`：T+1（当日买入 `available=0`，`end_of_day()` 解锁）、板块涨跌停（主板 ±10% / 创业板科创板 ±20% / 北交所 ±30%）、100 股整手、佣金印花税。
- 数据源故障切换是**会话级**的（`_session_overrides`，不持久化）：本次跑挂到备用源，下次任务自动重试主源——不要在代码里加跨任务的持久降级逻辑。
- SQLite schema 迁移是内联 `ALTER TABLE ... ADD COLUMN` 包 try/except 的容错写法（见 `Storage._init_tables()`），加列沿用此模式，不要引入迁移框架。
