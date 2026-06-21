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
