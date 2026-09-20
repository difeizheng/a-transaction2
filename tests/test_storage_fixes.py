"""P0 数据层修复单测 —— daily_bars upsert-update（修复 qfq 复权漂移）+
account peak_value（回撤 high-water mark 持久化）。

这两个是 P0 最易静默回归的点：
- upsert 若退回 IGNORE，除权后历史复权价永远刷新不了 → 回测失真；
- peak_value 若不持久化/被覆盖，回撤熔断形同虚设。
"""
import pytest
import pandas as pd


# ── daily_bars upsert-update（复权漂移修复）──────────────────────
class TestDailyBarsUpsert:
    @pytest.mark.integration
    def test_upsert_updates_existing_row(self, storage):
        # 同一 (code, trade_date) 二次写入应**更新**而非 IGNORE。
        # 模拟除权后 qfq 历史价重算：首写 close=10.2，重写 close=9.0。
        df1 = pd.DataFrame([{
            "code": "000001", "trade_date": "2024-01-01",
            "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
            "volume": 1000, "amount": 10200, "pct_chg": 2.0,
        }])
        storage.upsert_daily_bars(df1)

        df2 = pd.DataFrame([{
            "code": "000001", "trade_date": "2024-01-01",
            "open": 9.0, "high": 9.5, "low": 8.8, "close": 9.0,
            "volume": 1000, "amount": 9000, "pct_chg": -2.0,
        }])
        storage.upsert_daily_bars(df2)

        bars = storage.get_daily_bars("000001")
        assert len(bars) == 1               # 不产生重复行
        assert bars.iloc[0]["close"] == 9.0  # 已被刷新（旧 IGNORE 实现会是 10.2）

    @pytest.mark.integration
    def test_upsert_preserves_distinct_dates(self, storage):
        # 不同 trade_date 互不影响；重复写两次不产生重复
        df = pd.DataFrame([
            {"code": "000001", "trade_date": "2024-01-01", "open": 10.0,
             "high": 10.0, "low": 10.0, "close": 10.0,
             "volume": 0, "amount": 0, "pct_chg": 0.0},
            {"code": "000001", "trade_date": "2024-01-02", "open": 11.0,
             "high": 11.0, "low": 11.0, "close": 11.0,
             "volume": 0, "amount": 0, "pct_chg": 10.0},
        ])
        storage.upsert_daily_bars(df)
        storage.upsert_daily_bars(df)  # 重复写两次
        bars = storage.get_daily_bars("000001")
        assert len(bars) == 2

    @pytest.mark.integration
    def test_adj_factor_column_present_and_writable(self, storage):
        # adj_factor 列存在（迁移），df 含该列时可写入/读取（P1 raw+factor 重构预留）
        df = pd.DataFrame([{
            "code": "000001", "trade_date": "2024-01-01",
            "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
            "volume": 1000, "amount": 10000, "pct_chg": 0.0,
            "adj_factor": 1.23,
        }])
        storage.upsert_daily_bars(df)
        bars = storage.get_daily_bars("000001")
        assert "adj_factor" in bars.columns
        assert bars.iloc[0]["adj_factor"] == pytest.approx(1.23)

    @pytest.mark.integration
    def test_missing_adj_factor_leaves_null(self, storage):
        # akshare 无 adj_factor：写入不含该列 → NULL，不影响其它列
        df = pd.DataFrame([{
            "code": "000002", "trade_date": "2024-01-01",
            "open": 5.0, "high": 5.0, "low": 5.0, "close": 5.0,
            "volume": 100, "amount": 500, "pct_chg": 0.0,
        }])
        storage.upsert_daily_bars(df)
        bars = storage.get_daily_bars("000002")
        assert len(bars) == 1
        assert bars.iloc[0]["close"] == 5.0
        # adj_factor 缺失 → NULL（pandas 读为 NaN）
        assert pd.isna(bars.iloc[0]["adj_factor"])


# ── account peak_value（回撤 high-water mark 持久化）─────────────
class TestAccountPeakValue:
    @pytest.mark.integration
    def test_initial_peak_equals_initial_cash(self, storage):
        # 首次 upsert_account：peak_value = initial_cash（回撤起点峰值）
        storage.upsert_account(1_000_000.0, 1_000_000.0)
        acc = storage.get_account()
        assert acc["peak_value"] == 1_000_000.0

    @pytest.mark.integration
    def test_update_peak_value_persists(self, storage):
        storage.upsert_account(900_000.0, 1_000_000.0)
        storage.update_peak_value(1_200_000.0)
        acc = storage.get_account()
        assert acc["peak_value"] == 1_200_000.0

    @pytest.mark.integration
    def test_upsert_account_does_not_clobber_peak(self, storage):
        # 【关键】后续 upsert_account（如 _save_cash 刷资金）不得覆盖 peak_value。
        # 否则回撤 high-water mark 会被日常资金更新清掉。
        storage.upsert_account(900_000.0, 1_000_000.0)   # peak=1M
        storage.update_peak_value(1_200_000.0)           # peak=1.2M
        storage.upsert_account(850_000.0, 1_000_000.0)   # 刷资金，不应动 peak
        acc = storage.get_account()
        assert acc["cash"] == 850_000.0
        assert acc["peak_value"] == 1_200_000.0          # high-water mark 保留


# ── financial_data ann_date（财务 PIT，堵前视偏差）──────────────
class TestFinancialAnnDate:
    @pytest.mark.integration
    def test_ann_date_column_present(self, storage):
        # 列存在（迁移）
        df = pd.DataFrame([{
            "code": "000001", "report_date": "2024-03-31",
            "ann_date": "2024-04-29", "pe_ttm": 8.0, "roe": 12.0,
        }])
        storage.upsert_financial_data(df)
        fin = storage.get_financial_data("000001")
        assert "ann_date" in fin.columns
        assert fin.iloc[0]["ann_date"] == "2024-04-29"

    @pytest.mark.integration
    def test_ann_date_missing_leaves_null(self, storage):
        # 无 ann_date 写入 → NULL，不影响其它列
        df = pd.DataFrame([{
            "code": "000002", "report_date": "2024-03-31",
            "pe_ttm": 5.0, "roe": 10.0,
        }])
        storage.upsert_financial_data(df)
        fin = storage.get_financial_data("000002")
        assert pd.isna(fin.iloc[0]["ann_date"])
        assert fin.iloc[0]["pe_ttm"] == 5.0

    @pytest.mark.integration
    def test_ann_date_backfilled_on_repull(self, storage):
        # 首写无 ann_date，重写带 ann_date → update-existing 刷新（IGNORE 实现会留 NULL）
        df1 = pd.DataFrame([{"code": "000003", "report_date": "2024-03-31", "pe_ttm": 5.0}])
        storage.upsert_financial_data(df1)
        df2 = pd.DataFrame([{"code": "000003", "report_date": "2024-03-31",
                             "ann_date": "2024-04-29", "pe_ttm": 5.0}])
        storage.upsert_financial_data(df2)
        fin = storage.get_financial_data("000003")
        assert fin.iloc[0]["ann_date"] == "2024-04-29"


# ── equity_snapshots（净值曲线数据底座）────────────────────────
class TestEquitySnapshots:
    @pytest.mark.integration
    def test_upsert_and_read(self, storage):
        storage.upsert_equity_snapshot({
            "date": "2026-07-23", "total_value": 500000.0,
            "cash": 400000.0, "market_value": 100000.0,
        })
        df = storage.get_equity_snapshots()
        assert len(df) == 1
        assert df.iloc[0]["total_value"] == 500000.0

    @pytest.mark.integration
    def test_same_day_upsert_overwrites(self, storage):
        for v in (500000.0, 510000.0):
            storage.upsert_equity_snapshot({
                "date": "2026-07-23", "total_value": v,
                "cash": 400000.0, "market_value": v - 400000.0,
            })
        df = storage.get_equity_snapshots()
        assert len(df) == 1 and df.iloc[0]["total_value"] == 510000.0

    @pytest.mark.integration
    def test_start_date_filter(self, storage):
        for d in ("2026-07-01", "2026-07-20"):
            storage.upsert_equity_snapshot({
                "date": d, "total_value": 1.0, "cash": 1.0, "market_value": 0.0})
        df = storage.get_equity_snapshots(start_date="2026-07-15")
        assert list(df["date"]) == ["2026-07-20"]


# ── watchlist tags（分组/筛选）─────────────────────────────────
class TestWatchlistTags:
    @pytest.mark.integration
    def test_tags_column_and_update(self, storage):
        storage.add_to_watchlist({
            "code": "000001", "name": "平安银行", "score": 0.8,
            "signals": "", "reason": "", "source": "manual",
        })
        storage.update_watchlist_tags("000001", "白马,观察")
        items = storage.get_watchlist()
        assert items[0]["tags"] == "白马,观察"


# ── llm_call_log token 用量统计（QA 成本展示）──────────────────
class TestLlmTokenUsage:
    @pytest.mark.integration
    def test_sum_since(self, storage):
        storage.save_llm_call_log({"created_at": "2026-07-20T10:00:00",
                                   "input_tokens": 100, "output_tokens": 50})
        storage.save_llm_call_log({"created_at": "2026-07-24T10:00:00",
                                   "input_tokens": 200, "output_tokens": 80})
        usage = storage.get_llm_token_usage("2026-07-24T00:00:00")
        assert usage == {"calls": 1, "input_tokens": 200, "output_tokens": 80}
        usage_all = storage.get_llm_token_usage("2020-01-01")
        assert usage_all["calls"] == 2 and usage_all["input_tokens"] == 300


# ── save_backtest_result 列过滤 + trades_detail ────────────────
class TestSaveBacktestResultFiltering:
    @pytest.mark.integration
    def test_engine_style_dict_with_extra_keys(self, storage):
        # 真实 compare.py 传来的 dict 含 equity 已 pop，但仍有 initial_cash/
        # final_value/selected_stocks/trades_detail —— 此前不过滤会直接炸。
        storage.save_backtest_result({
            "strategy_name": "ma_cross", "start_date": "2024-01-01",
            "end_date": "2024-12-31", "initial_cash": 100000,
            "final_value": 110000, "selected_stocks": 5,
            "total_return": 10.0, "annual_return": 10.0, "sharpe": 1.0,
            "max_drawdown": 5.0, "win_rate": 50.0, "profit_loss_ratio": 1.5,
            "trades": 3,
            "trades_detail": '[{"code": "000001", "pnlcomm": 123.4}]',
        })
        df = storage.get_backtest_results()
        assert len(df) == 1
        assert df.iloc[0]["strategy_name"] == "ma_cross"
        assert "000001" in df.iloc[0]["trades_detail"]


# ── positions 主键重建迁移（v8）──────────────────────────────────
class TestPositionsPkMigration:
    """v8：旧库 positions 无 PRIMARY KEY → upsert_position 的 ON CONFLICT(code)
    每次抛 OperationalError，持仓现价/T+1 解锁静默不落库（2026-09 实测发现，
    真实库持仓 updated_at 停在 2026-06-15）。迁移建新表拷数据换名补 PK。"""

    def _make_legacy_db(self, db_path):
        """手工造一个 v8 之前的坏库：positions 无 PK、含重复 code。"""
        from sqlalchemy import create_engine, text
        eng = create_engine(f"sqlite:///{db_path}")
        with eng.connect() as conn:
            conn.execute(text("""
                CREATE TABLE positions (
                    code TEXT, name TEXT, quantity BIGINT, available BIGINT,
                    cost_price FLOAT, current_price FLOAT, market_value FLOAT,
                    profit_loss FLOAT, buy_date TEXT, updated_at TEXT
                )
            """))
            conn.execute(text(
                "INSERT INTO positions VALUES "
                "('000001','平安银行',1000,1000,10.0,9.5,9500,-500,'2026-01-01','2026-06-01T00:00:00'),"
                "('000001','平安银行',1000,0,10.0,9.8,9800,-200,'2026-01-01','2026-06-15T00:00:00')"
            ))
            conn.commit()
        eng.dispose()
        return db_path

    @pytest.mark.integration
    def test_v8_rebuilds_legacy_table_and_dedupes(self, tmp_path):
        from sqlalchemy import text
        from src.data.storage import Storage

        db = self._make_legacy_db(str(tmp_path / "legacy.db"))
        s = Storage(db)

        # 1) PK 已补上
        cols = s.engine.connect().execute(text("PRAGMA table_info(positions)")).fetchall()
        assert any(c[5] for c in cols), "positions 应有 PRIMARY KEY"

        # 2) 重复 code 去重，updated_at 最新行胜出
        rows = s.engine.connect().execute(
            text("SELECT code, current_price FROM positions")).fetchall()
        assert len(rows) == 1
        assert rows[0][1] == 9.8

        # 3) v8 已登记
        v = s.engine.connect().execute(
            text("SELECT version FROM schema_version WHERE version=8")).fetchall()
        assert len(v) == 1

        # 4) upsert 不再抛错且能更新
        s.upsert_position({"code": "000001", "name": "平安银行", "quantity": 1000,
                           "available": 1000, "cost_price": 10.0, "current_price": 10.5,
                           "market_value": 10500.0, "profit_loss": 500.0})
        row = s.engine.connect().execute(
            text("SELECT current_price FROM positions WHERE code='000001'")).fetchone()
        assert row[0] == 10.5

    @pytest.mark.integration
    def test_v8_idempotent_on_fresh_db(self, storage):
        """新库 CREATE 已含 PK：v8 应登记但不动表；重复构造 Storage 不炸。"""
        from sqlalchemy import text
        storage.upsert_position({"code": "000002", "name": "万科A", "quantity": 100,
                                 "available": 100, "cost_price": 8.0, "current_price": 8.5,
                                 "market_value": 850.0, "profit_loss": 50.0})
        # 二次打开同一库：v8 已在 schema_version，直接跳过
        from src.data.storage import Storage
        db_path = storage.engine.url.database
        Storage(db_path)
        rows = storage.engine.connect().execute(
            text("SELECT COUNT(*) FROM positions")).fetchone()
        assert rows[0] == 1
