"""回测指标纯函数 + 净值曲线 单测。

覆盖 ``src/backtest/metrics.py`` 的全部纯函数（基准收益/净值、最优策略挑选、
结论摘要、低交易次数警告），以及 ``EquityCurveAnalyzer`` 在端到端回测中
确实产出逐 bar 净值序列（integration）。
"""
import pytest

from src.backtest.metrics import (
    compute_buyhold_equity,
    compute_buyhold_return,
    pick_best_strategy,
    build_conclusion,
    low_trade_warnings,
)


# ── compute_buyhold_equity ────────────────────────────────────────
class TestBuyHoldEquity:
    @pytest.mark.unit
    def test_first_value_equals_initial_cash(self):
        eq = compute_buyhold_equity([1, 2, 3], [10.0, 12.0, 15.0], 1_000_000)
        assert eq["values"][0] == pytest.approx(1_000_000)

    @pytest.mark.unit
    def test_scales_by_close_ratio(self):
        # 首日 10、末日 15 → 末日净值 = 1e6 * 15/10 = 1.5e6
        eq = compute_buyhold_equity([1, 2, 3], [10.0, 12.0, 15.0], 1_000_000)
        assert eq["values"][-1] == pytest.approx(1_500_000)
        assert eq["values"][1] == pytest.approx(1_200_000)

    @pytest.mark.unit
    def test_dates_passthrough(self):
        eq = compute_buyhold_equity(["a", "b"], [10.0, 11.0], 100)
        assert eq["dates"] == ["a", "b"]

    @pytest.mark.unit
    def test_empty_prices(self):
        assert compute_buyhold_equity([], [], 1_000_000) == {"dates": [], "values": []}

    @pytest.mark.unit
    def test_zero_first_price_fallback(self):
        # 首日收盘为 0 → 无法归一，回退为常量净值
        eq = compute_buyhold_equity([1, 2], [0.0, 5.0], 1_000_000)
        assert eq["values"] == [1_000_000, 1_000_000]


# ── compute_buyhold_return ────────────────────────────────────────
class TestBuyHoldReturn:
    @pytest.mark.unit
    def test_total_return_matches_manual(self):
        closes = [10.0, 12.0, 15.0]  # +50%
        total, _ = compute_buyhold_return(closes, days=365)
        assert total == pytest.approx(50.0, abs=0.01)

    @pytest.mark.unit
    def test_annual_return_one_year_equals_total(self):
        # 恰好 365 天 → 年化 == 总收益（与 engine 年化口径一致）
        closes = [10.0, 12.0]
        total, annual = compute_buyhold_return(closes, days=365)
        assert annual == pytest.approx(total, abs=0.01)

    @pytest.mark.unit
    def test_short_series_returns_zero(self):
        assert compute_buyhold_return([10.0], days=10) == (0.0, 0.0)
        assert compute_buyhold_return([], days=10) == (0.0, 0.0)

    @pytest.mark.unit
    def test_negative_return(self):
        closes = [10.0, 8.0]  # -20%
        total, _ = compute_buyhold_return(closes, days=365)
        assert total == pytest.approx(-20.0, abs=0.01)


# ── pick_best_strategy ────────────────────────────────────────────
class TestPickBestStrategy:
    @pytest.mark.unit
    def test_picks_highest_sharpe(self):
        rows = [
            {"strategy_name": "A", "sharpe": 0.5, "total_return": 10, "trades": 8},
            {"strategy_name": "B", "sharpe": 1.2, "total_return": 5, "trades": 8},
            {"strategy_name": "C", "sharpe": 0.8, "total_return": 12, "trades": 8},
        ]
        assert pick_best_strategy(rows) == "B"

    @pytest.mark.unit
    def test_ignores_error_rows(self):
        rows = [
            {"strategy_name": "A", "sharpe": 0.9, "trades": 8},
            {"strategy_name": "B", "error": "boom"},
            {"strategy_name": "C", "sharpe": 0.1, "trades": 8},
        ]
        assert pick_best_strategy(rows) == "A"

    @pytest.mark.unit
    def test_ignores_zero_trade_rows(self):
        rows = [
            {"strategy_name": "A", "sharpe": 9.9, "trades": 0},
            {"strategy_name": "B", "sharpe": 1.0, "trades": 8},
        ]
        assert pick_best_strategy(rows) == "B"

    @pytest.mark.unit
    def test_falls_back_to_total_return_when_sharpe_missing(self):
        rows = [
            {"strategy_name": "A", "total_return": 10, "trades": 8},
            {"strategy_name": "B", "total_return": 25, "trades": 8},
        ]
        assert pick_best_strategy(rows) == "B"

    @pytest.mark.unit
    def test_all_invalid_returns_none(self):
        rows = [
            {"strategy_name": "A", "error": "x"},
            {"strategy_name": "B", "sharpe": 1.0, "trades": 0},
        ]
        assert pick_best_strategy(rows) is None


# ── low_trade_warnings ────────────────────────────────────────────
class TestLowTradeWarnings:
    @pytest.mark.unit
    def test_flags_below_threshold(self):
        rows = [{"strategy_name": "A", "trades": 3}]
        assert "「A」(3 笔)" in low_trade_warnings(rows)

    @pytest.mark.unit
    def test_does_not_flag_at_threshold(self):
        rows = [{"strategy_name": "A", "trades": 5}]
        assert low_trade_warnings(rows) == []

    @pytest.mark.unit
    def test_ignores_zero_trades(self):
        rows = [{"strategy_name": "A", "trades": 0}]
        assert low_trade_warnings(rows) == []

    @pytest.mark.unit
    def test_ignores_error_rows(self):
        rows = [{"strategy_name": "A", "error": "x", "trades": 1}]
        assert low_trade_warnings(rows) == []


# ── build_conclusion ──────────────────────────────────────────────
class TestBuildConclusion:
    ROWS_WIN = [{"strategy_name": "A", "sharpe": 1.0, "total_return": 20,
                 "annual_return": 18, "max_drawdown": 9, "win_rate": 60, "trades": 10}]

    @pytest.mark.unit
    def test_beats_benchmark(self):
        benchmark = {"code": "000300", "name": "沪深300", "annual_return": 5.0}
        text = build_conclusion(self.ROWS_WIN, benchmark, "2022-01-01", "2024-12-31")
        assert "最优策略「A」" in text
        assert "跑赢基准" in text

    @pytest.mark.unit
    def test_loses_to_benchmark(self):
        rows = [{"strategy_name": "A", "sharpe": 0.2, "total_return": 5,
                 "annual_return": 4, "max_drawdown": 15, "win_rate": 40, "trades": 10}]
        benchmark = {"code": "000300", "name": "沪深300", "annual_return": 12.0}
        text = build_conclusion(rows, benchmark, "2022-01-01", "2024-12-31")
        assert "跑输基准" in text

    @pytest.mark.unit
    def test_no_benchmark_omits_reference(self):
        text = build_conclusion(self.ROWS_WIN, None, "2022-01-01", "2024-12-31")
        assert "基准" not in text
        assert "最优策略「A」" in text


# ── EquityCurveAnalyzer 端到端 ────────────────────────────────────
class TestEquityCurveAnalyzer:
    @pytest.mark.integration
    def test_engine_returns_nonempty_equity_curve(self):
        import pandas as pd
        from src.backtest.engine import BacktestEngine

        config = {"backtest": {"initial_cash": 1_000_000.0, "commission": 0.0003, "stamp_duty": 0.001}}
        engine = BacktestEngine(config)
        dates = pd.date_range("2024-01-01", periods=30, freq="D")
        prices = [10.0 + i * 0.1 for i in range(30)]
        bars = {"000001": pd.DataFrame({
            "trade_date": dates, "open": prices, "high": [p + 0.2 for p in prices],
            "low": [p - 0.2 for p in prices], "close": prices, "volume": [1000] * 30,
        })}
        result = engine.run(bars, ["000001"], "2024-01-01", "2024-01-31")

        assert "equity_curve" in result
        curve = result["equity_curve"]
        assert len(curve["dates"]) == len(curve["values"])
        assert len(curve["values"]) > 0
        # 首值 ≈ 初始资金（首 bar 未建仓，组合价值=现金）
        assert curve["values"][0] == pytest.approx(1_000_000.0, rel=1e-3)
