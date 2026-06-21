"""P1/P2 数据层补充单测 —— ExecutionReport 落库（可审计）+ 退市股 plumbing。

- execution_reports 表：save → get 往返，关键字段 + report_json 可反序列化；
- auto_trader.run() 末尾落库（用真实临时 Storage，验证端到端可审计）；
- tushare get_stock_list 的 include_delisted 参数路由（mock，零网络）。
"""
import pandas as pd
import pytest

from src.trading.auto_trader import AutoTrader, ExecutionReport, RiskParams


# ── execution_reports 存储往返 ──────────────────────────────────
class TestExecutionReportStorage:
    @pytest.mark.integration
    def test_save_and_get_roundtrip(self, storage):
        report = {
            "timestamp": "2026-06-20T10:00:00",
            "mode": "signal",
            "strategy_keys": ["ma_cross", "small_cap"],
            "drawdown_pct": 12.3,
            "deescalation_tier": 1,
            "paused_reason": "",
            "buy_suggestions": [{"code": "000001", "quantity": 100}],
            "sell_suggestions": [{"code": "000002", "reason": "止损"}],
            "executed_orders": [],
            "stop_loss_sells": [],
            "take_profit_sells": [],
            "deescalation_sells": [],
            "risk_blocked": [{"code": "000003", "reason": "总仓位"}],
            "errors": [],
            "strategy_candidates": [{"code": "000001"}],
        }
        rid = storage.save_execution_report(report)
        assert rid > 0

        rows = storage.get_execution_reports(limit=10)
        assert len(rows) == 1
        row = rows[0]
        assert row["mode"] == "signal"
        assert row["n_buy_suggestions"] == 1
        assert row["n_sell_suggestions"] == 1
        assert row["n_blocked"] == 1
        assert row["n_candidates"] == 1
        # report_json 反序列化回完整 dict
        assert row["report"]["buy_suggestions"][0]["code"] == "000001"

    @pytest.mark.integration
    def test_multiple_reports_ordered_newest_first(self, storage):
        for i in range(3):
            storage.save_execution_report({"timestamp": f"2026-06-2{i}", "mode": "signal"})
        rows = storage.get_execution_reports(limit=10)
        assert len(rows) == 3
        # id DESC → 最后插入的最前
        assert rows[0]["id"] > rows[-1]["id"]


# ── auto_trader.run() 落库端到端 ─────────────────────────────────
class TestAutoTraderPersists:
    @pytest.mark.unit
    def test_run_persists_report(self, tmp_path):
        # 用真实临时 Storage + 真实 Portfolio/Simulator 走端到端，避免 mock storage
        from src.data.storage import Storage
        from src.trading.portfolio import Portfolio
        from src.trading.simulator import TradingSimulator
        from unittest.mock import MagicMock

        storage = Storage(str(tmp_path / "t.db"))
        portfolio = Portfolio(storage, initial_cash=1_000_000.0)

        dm = MagicMock()
        dm.storage = storage
        dm.get_daily_bars.return_value = pd.DataFrame()
        dm.get_financial_data.return_value = pd.DataFrame()
        # account 已由 Portfolio 初始化写入
        advisor = MagicMock()

        simulator = TradingSimulator(dm)  # 持有上面的 portfolio？见下
        simulator.portfolio = portfolio   # 注入真实 portfolio
        trader = AutoTrader(dm, simulator, advisor)  # signal 模式
        report = trader.run([], ["ma_cross"])  # 空池 → 早早返回，但仍应落库

        rows = storage.get_execution_reports(limit=5)
        assert len(rows) == 1
        assert rows[0]["mode"] == "signal"
        assert rows[0]["n_candidates"] == 0


# ── 退市股 include_delisted 参数路由（mock，零网络）──────────────
class TestDelistedPlumbing:
    @pytest.mark.unit
    def test_tushare_includes_delisted_statuses(self):
        from src.data.tushare_fetcher import TushareFetcher
        import types

        # 构造一个不连真实 tushare 的 fetcher（绕过 __init__ 的 token 检查）
        fetcher = TushareFetcher.__new__(TushareFetcher)

        calls = []

        class _FakePro:
            def stock_basic(self, exchange="", list_status="", fields=""):
                calls.append(list_status)
                # 模拟：L 返回在市，D 返回退市
                if list_status == "L":
                    return pd.DataFrame({"ts_code": ["000001.SZ"], "name": ["A"], "market": ["SZ"]})
                elif list_status == "D":
                    return pd.DataFrame({"ts_code": ["000002.SZ"], "name": ["退市"], "market": ["SZ"]})
                return pd.DataFrame()  # P 暂空

        fetcher._pro = _FakePro()
        # include_delisted=True → 应请求 L + D + P 三个状态
        df = fetcher.get_stock_list(include_delisted=True)
        statuses_requested = set(calls)
        assert {"L", "D", "P"} == statuses_requested
        # 结果应含退市股
        assert "000002" in df["code"].values

    @pytest.mark.unit
    def test_tushare_default_excludes_delisted(self):
        from src.data.tushare_fetcher import TushareFetcher

        fetcher = TushareFetcher.__new__(TushareFetcher)
        calls = []

        class _FakePro:
            def stock_basic(self, exchange="", list_status="", fields=""):
                calls.append(list_status)
                return pd.DataFrame({"ts_code": ["000001.SZ"], "name": ["A"], "market": ["SZ"]})

        fetcher._pro = _FakePro()
        fetcher.get_stock_list()  # 默认 include_delisted=False
        assert calls == ["L"]  # 只请求在市
