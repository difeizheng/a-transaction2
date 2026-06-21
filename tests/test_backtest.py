"""Backtest 单测 —— 手续费模型与回测引擎。

覆盖：AShareCommissionInfo 手续费计算（买入无印花税/卖出有印花税/最低佣金）、
BacktestEngine 合成数据端到端（验证返回结构、无数据报错）。
"""
import pytest
import pandas as pd
import backtrader as bt

from src.backtest.engine import AShareCommissionInfo, BacktestEngine


# ── AShareCommissionInfo 手续费（佣金+过户费双边 + 卖出印花税）─────
class TestAShareCommissionInfo:
    @pytest.mark.unit
    def test_buy_commission_no_stamp_duty(self):
        # 买入 1000 元：佣金 1000×0.0003=0.3 + 过户费 1000×0.0001=0.1 = 0.4（买入无印花税）
        # 注意：_getcommission 返回原始计算值，最低 5 元由 backtrader 的 getcommission() 应用
        comm = AShareCommissionInfo(commission=0.0003, stamp_duty=0.001)
        fee = comm._getcommission(size=100, price=10.0)  # 买入 100 股 × 10 元
        assert fee == pytest.approx(0.4, abs=0.01)  # 佣金 0.3 + 过户费 0.1

    @pytest.mark.unit
    def test_sell_commission_includes_stamp_duty(self):
        # 卖出 1000 元：佣金 0.3 + 过户费 0.1 + 印花税 1000×0.001=1 → 总 1.4
        comm = AShareCommissionInfo(commission=0.0003, stamp_duty=0.001)
        fee = comm._getcommission(size=-100, price=10.0)  # 卖出 100 股 × 10 元
        assert fee == pytest.approx(1.4, abs=0.01)  # 佣金 0.3 + 过户 0.1 + 印花税 1

    @pytest.mark.unit
    def test_large_amount_commission_respects_rate(self):
        # 买入 10 万元：佣金 30 + 过户费 10 = 40（买入无印花税）
        comm = AShareCommissionInfo(commission=0.0003, stamp_duty=0.001)
        fee = comm._getcommission(size=10000, price=10.0)  # 10000 股 × 10 元
        assert fee == pytest.approx(40.0, abs=0.01)

    @pytest.mark.unit
    def test_large_sell_commission_includes_both(self):
        # 卖出 10 万元：佣金 30 + 过户费 10 + 印花税 100 → 总 140
        comm = AShareCommissionInfo(commission=0.0003, stamp_duty=0.001)
        fee = comm._getcommission(size=-10000, price=10.0)
        assert fee == pytest.approx(140.0, abs=0.01)

    @pytest.mark.unit
    def test_custom_rates(self):
        # 自定义费率：佣金 0.001，印花税 0.002（过户费仍用默认 0.0001）
        comm = AShareCommissionInfo(commission=0.001, stamp_duty=0.002)
        # 买入 1 万元：佣金 10 + 过户费 1 = 11
        fee_buy = comm._getcommission(size=1000, price=10.0)
        assert fee_buy == pytest.approx(11.0, abs=0.01)
        # 卖出 1 万元：佣金 10 + 过户费 1 + 印花税 20 → 总 31
        fee_sell = comm._getcommission(size=-1000, price=10.0)
        assert fee_sell == pytest.approx(31.0, abs=0.01)

    @pytest.mark.unit
    def test_transfer_fee_is_bilateral(self):
        # 过户费双边：买入也收（与 trading.rules.calc_commission 口径一致）
        comm = AShareCommissionInfo(commission=0.0003, stamp_duty=0.0005)
        # 买入 10 万：佣金 30 + 过户 10 = 40（无印花税）
        assert comm._getcommission(size=10000, price=10.0) == pytest.approx(40.0, abs=0.01)

    @pytest.mark.unit
    def test_default_stamp_duty_is_new_rate(self):
        # 防回归：默认印花税必须是 0.0005（2023.8.28 起），不是旧 0.001
        comm = AShareCommissionInfo()
        assert comm.p.stamp_duty == 0.0005
        assert comm.p.transfer_fee == 0.0001


# ── BacktestEngine 端到端 ───────────────────────────────────────
class TestBacktestEngine:
    @pytest.mark.integration
    def test_empty_data_returns_error(self):
        config = {
            "backtest": {
                "initial_cash": 1_000_000.0,
                "commission": 0.0003,
                "stamp_duty": 0.001,
            }
        }
        engine = BacktestEngine(config)
        result = engine.run(
            bars_dict={},
            signal_codes=[],
            start_date="2024-01-01",
            end_date="2024-01-31",
        )
        assert "error" in result
        assert "没有有效数据" in result["error"]

    @pytest.mark.integration
    def test_insufficient_data_skipped(self):
        # 数据少于 10 条 → 跳过，最终报"没有有效数据"
        config = {
            "backtest": {
                "initial_cash": 1_000_000.0,
                "commission": 0.0003,
                "stamp_duty": 0.001,
            }
        }
        engine = BacktestEngine(config)
        bars = {
            "000001": pd.DataFrame({
                "trade_date": ["2024-01-01", "2024-01-02"],  # 仅 2 条
                "open": [10.0, 10.5],
                "high": [10.5, 11.0],
                "low": [9.5, 10.0],
                "close": [10.2, 10.8],
                "volume": [1000, 1200],
            })
        }
        result = engine.run(bars, ["000001"], "2024-01-01", "2024-01-31")
        assert "error" in result

    @pytest.mark.integration
    def test_synthetic_data_end_to_end(self):
        # 合成 30 天上涨数据，验证回测能正常运行并返回完整结果
        config = {
            "backtest": {
                "initial_cash": 1_000_000.0,
                "commission": 0.0003,
                "stamp_duty": 0.001,
            }
        }
        engine = BacktestEngine(config)

        # 生成 30 天线性上涨数据（10→13 元）
        dates = pd.date_range("2024-01-01", periods=30, freq="D")
        prices = [10.0 + i * 0.1 for i in range(30)]
        bars = {
            "000001": pd.DataFrame({
                "trade_date": dates,
                "open": prices,
                "high": [p + 0.2 for p in prices],
                "low": [p - 0.2 for p in prices],
                "close": prices,
                "volume": [1000] * 30,
            })
        }

        result = engine.run(
            bars_dict=bars,
            signal_codes=["000001"],
            start_date="2024-01-01",
            end_date="2024-01-31",
            stop_loss=0.05,
            take_profit=0.15,
            position_pct=0.1,
            strategy_name="test_strategy",
        )

        # 验证返回结构完整
        assert "strategy_name" in result
        assert result["strategy_name"] == "test_strategy"
        assert "start_date" in result
        assert "end_date" in result
        assert "initial_cash" in result
        assert "final_value" in result
        assert "total_return" in result
        assert "annual_return" in result
        assert "sharpe" in result
        assert "max_drawdown" in result
        assert "win_rate" in result
        assert "profit_loss_ratio" in result
        assert "trades" in result

        # 验证数值合理性
        assert result["initial_cash"] == 1_000_000.0
        assert result["final_value"] > 0
        assert isinstance(result["total_return"], float)
        assert isinstance(result["trades"], int)

    @pytest.mark.integration
    def test_no_signal_codes_no_trades(self):
        # 有数据但无信号 → 不交易，最终资金不变
        config = {
            "backtest": {
                "initial_cash": 1_000_000.0,
                "commission": 0.0003,
                "stamp_duty": 0.001,
            }
        }
        engine = BacktestEngine(config)

        dates = pd.date_range("2024-01-01", periods=30, freq="D")
        prices = [10.0] * 30
        bars = {
            "000001": pd.DataFrame({
                "trade_date": dates,
                "open": prices,
                "high": [p + 0.2 for p in prices],
                "low": [p - 0.2 for p in prices],
                "close": prices,
                "volume": [1000] * 30,
            })
        }

        result = engine.run(bars, signal_codes=[], start_date="2024-01-01", end_date="2024-01-31")

        assert result["trades"] == 0
        assert result["final_value"] == pytest.approx(1_000_000.0, abs=1.0)  # 无交易，资金不变
