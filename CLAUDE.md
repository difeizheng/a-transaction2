# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
# Install dependencies
pip install -r requirements.txt

# Start the Streamlit UI (run from project root)
streamlit run src/ui/app.py

# The launcher pins a port (Windows convenience wrapper)
run_streamlit.bat        # serves on http://localhost:8602
```

Run from the project root (`a-transaction2_claude`). `src/ui/app.py` inserts the project root onto `sys.path`, so absolute imports like `from src.data.manager import DataManager` resolve regardless of cwd — but other entry points (scripts, REPL) still need the project root on `PYTHONPATH`.

## Tests

Tests use **pytest** (configured in `pytest.ini`; dev deps in `requirements-dev.txt`). `pytest.ini` sets `pythonpath = .` so `from src...` resolves when running from the project root — no `PYTHONPATH` hackery needed.

```bash
# One-time: install test deps
pip install -r requirements-dev.txt

# Full suite (run from project root)
pytest

# Single file / single test
pytest tests/test_trading_rules.py
pytest "tests/test_portfolio.py::TestT1Selling::test_cannot_sell_same_day"

# By marker (tests are tagged @pytest.mark.unit or @pytest.mark.integration)
pytest -m unit          # pure functions, no DB/network
pytest -m integration   # uses a per-test temp SQLite via tmp_path

# Coverage (optional)
pytest --cov=src --cov-report=term-missing
```

There is still **no linter or type-checker configured**. Tests target pure logic and invariants most likely to regress silently: A-share trading rules (涨跌停/手续费/手数/T+1), weekend trading-day rollback, cninfo exchange-column mapping, cross-strategy score normalization, secret-injection precedence, and Portfolio buy/sell/P&L. Tests that would require live network calls (akshare/tushare/cninfo), LLM calls (anthropic/openai), or the Streamlit UI are intentionally **not** covered — keep adding unit/integration tests for pure modules, not e2e.

## Configuration

`config/config.yaml` is the single config surface, loaded by `src/config.py` (`get_config()`, cached). Secrets are injected via environment variables with precedence **real `os.environ` > `.env` file (project root) > `config.yaml` placeholder**. `config.py` ships a dependency-free `.env` loader (no `python-dotenv`). Supported vars: `CLAUDE_API_KEY`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `TUSHARE_TOKEN`.

- `llm.provider`: `claude` or `openai` — switches `LLMAnalyzer` between Anthropic and any OpenAI-compatible endpoint
- `llm.openai_base_url`: points `LLMAnalyzer` at third-party OpenAI-compatible APIs (e.g. SiliconFlow); `model_openai` is the model id passed there
- `data_sources.*`: enables per-fetcher data sources (see routing layer below); `tushare` requires a `token` (inject via `TUSHARE_TOKEN`)

🔒 **Security:** Secrets live in `.env` (gitignored), not in `config/config.yaml` — the committed template is `config/config.example.yaml`. `.gitignore` excludes `.env`, `config/config.yaml`, and `data/*.db`. Never hardcode keys into `config.yaml`; add new ones as env vars and document the name in `.env.example`.

## Architecture

6-stage pipeline: **Data → Strategy → Backtest → Analysis → Trading → UI**.

### Multi-source data layer with routing (the part most worth understanding)

```
src/data/
├── base_fetcher.py     # BaseFetcher ABC — the unified fetcher interface
├── fetcher.py          # AKShareFetcher (akshare, default primary source)
├── tushare_fetcher.py  # TushareFetcher (requires token, high-quality bars/financials)
├── tencent_fetcher.py  # TencentFetcher (free realtime quotes)
├── cninfo_fetcher.py   # CninfoFetcher (巨潮 announcements, free)
├── source_router.py    # SourceRouter: primary/backup failover per data_type
├── storage.py          # SQLAlchemy Core over SQLite (all tables)
└── manager.py          # DataManager: orchestrates router + storage, incremental updates
```

The flow is **not** "call fetcher directly". `DataManager` calls `SourceRouter.call(data_type, ...)`, which:

1. Looks up the route for `data_type` (e.g. `daily_bars`, `realtime`, `news`) in the `data_source_routes` SQLite table — primary source, backup source, enabled flag.
2. Maps `data_type` → fetcher method name (`_METHOD_MAP` in `source_router.py`) and calls it on the primary source.
3. On failure: switches to the backup source **for the rest of this session** (`_session_overrides`, not persisted across runs). `NotImplementedError` means "this source doesn't support that data_type" and is silently skipped (not counted as a failure). All other exceptions are logged via `record_source_failure` and retried on the backup.
4. Next task re-tries the primary (no cross-task state).

Routes are user-configurable from the **数据管理** UI page (`set_route` → persisted). Available sources depend on which fetchers `DataManager.__init__` managed to instantiate.

To **add a new data source**: subclass `BaseFetcher`, implement the supported methods (raise `NotImplementedError` for unsupported ones — that's how routing excludes you), register it in `DataManager.__init__` under a fetchers-dict key, and add a `data_source_routes` row.

`DataManager.get_daily_bars()` does **incremental updates**: it checks the latest stored `trade_date` for the code and only fetches the gap. `batch_update_bars_v2()` is the UI-facing batch updater with progress callback, cancel flag, and rate limiting — the older `batch_update_bars()` is kept for compatibility.

### Strategy layer

```
src/strategy/
├── base.py          # BaseStrategy ABC + ScreenResult / StockEvaluation / ConditionCheck
├── technical.py     # MA cross, MACD golden, KDJ oversold, Bollinger breakout
├── fundamental.py   # Low valuation, high growth, industry leader
├── multifactor.py   # Z-score normalized multi-factor model
└── screener.py      # Screener + STRATEGY_REGISTRY (the strategy registration point)
```

Two evaluation modes on `BaseStrategy`:
- `screen(stock_pool, data_manager, progress_callback) -> List[ScreenResult]` — batch screen over a pool (abstract, required).
- `evaluate_stock(code, name, data_manager) -> StockEvaluation` — single-stock scorecard with `indicators`, `conditions` (list of `ConditionCheck` pass/fail), and `trace_log`. **Optional**: a strategy advertises support via `supports_evaluate()`, which checks whether the subclass overrode `evaluate_stock`. `AutoTrader` only uses strategies where `supports_evaluate()` is `True`.

`Screener.run_multi_strategy()` merges multiple strategies in `union` (average score, keep all) or `intersect` (keep only stocks all strategies selected) mode.

### Backtest

`BacktestEngine` (backtrader wrapper) + `BTStrategyWrapper` adapts a `BaseStrategy` to a backtrader `Strategy`. `BacktestComparator` runs several strategies and persists results to `backtest_results`.

### Analysis

`LLMAnalyzer` is the provider abstraction (`_call_claude` / `_call_openai`, plus `_chat_*` for message-array/system-prompt calls). `Advisor` orchestrates news fetch + LLM calls into a structured result (`buy_suggestion`, `confidence`) that `AutoTrader` consumes.

### Trading

```
src/trading/
├── rules.py        # A-share rules: price-limit %, commission, stamp duty, T+1, lot rounding
├── portfolio.py    # Portfolio: buy/sell/positions/P&L, persists orders+positions to SQLite
├── simulator.py    # TradingSimulator: wraps Portfolio with realtime price lookup
└── auto_trader.py  # AutoTrader: 4-phase automated trading loop
```

`AutoTrader.run()` is a 4-phase pipeline gated by `RiskParams` (max position %, total position %, daily trade cap, stop-loss/take-profit %, max drawdown pause, min AI confidence):
1. **Stop-loss/take-profit** — scan existing positions (only `available` shares; T+1), auto-sell beyond thresholds.
2. **Max-drawdown check** — pause new entries if portfolio drawdown exceeds the limit.
3. **Strategy screening** — run each strategy's `evaluate_stock` over the pool, collect candidates.
4. **AI analysis** → `buy_suggestion == "建议买入"` and `confidence >= min_ai_confidence`.
5. **Risk filter + execute** — enforce per-stock and total position caps and the daily-trade cap, then `place_buy`.

Everything is recorded in the `ExecutionReport` dataclass. A-share T+1 is enforced in `Portfolio.buy()` (`available=0` until `end_of_day()` unlocks shares), and board-specific price limits (主板 ±10%, 创业板/科创板 ±20%, 北交所 ±30%) are enforced in `simulator.py`.

### UI

`src/ui/app.py` is the entry. It dynamically imports the selected page module from `src/ui/page_modules/` and calls its `render_sidebar()` (if present) then `render()`. **The page contract is `render()` (+ optional `render_sidebar()`)** — add a page by dropping a module in `src/ui/page_modules/` and adding it to the `PAGES` dict in `app.py`. `src/ui/components/` holds background workers (`data_worker.py`, `screening_worker.py`) and shared widgets (`step_logger.py`, `service_info.py`).

> ⚠️ **The page-module directory is intentionally NOT named `pages`.** Streamlit's native multipage-app (MPA) feature auto-scans a `pages/` directory **sibling to the entry script** (`src/ui/app.py`) and turns each `.py` into a sidebar nav entry. If this directory were named `pages`, Streamlit would generate a second (English, broken — `render()` never called) nav on top of the manual Chinese nav in `app.py`. Renamed to `page_modules/` to avoid the collision. If you ever migrate to native MPA, reverse this rename and rewrite each module to call `render()` at module level.

## SQLite schema (data/stock.db)

13 tables, all created in `Storage._init_tables()`. Beyond the obvious ones (`stock_list`, `daily_bars`, `financial_data`, `news`, `backtest_results`, `trade_orders`, `positions`), note the routing/state tables the UI depends on: `data_source_routes`, `data_source_status`, `update_log`, `screening_sessions`, `screening_evaluations`, `watchlist`. Schema migrations are inline `ALTER TABLE ... ADD COLUMN` guarded by try/except (see `news.url`).

## Adding a new strategy

1. Create a class in `src/strategy/technical.py` or `fundamental.py` inheriting `BaseStrategy`.
2. Implement `screen()`. For AutoTrader/scorecard support, also override `evaluate_stock()` (and `supports_evaluate()` will then return `True` automatically).
3. Register it in `STRATEGY_REGISTRY` in `src/strategy/screener.py`.
4. It appears automatically in UI dropdowns; no UI changes needed.
