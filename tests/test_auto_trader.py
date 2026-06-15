"""AutoTrader 风控单测 —— 4 阶段风控逻辑回归保护。

覆盖：止损/止盈触发、T+1 跳过、单日交易次数上限、总仓位上限、单股仓位上限、最大回撤暂停。
依赖：mock DataManager、TradingSimulator、Advisor（不依赖真实网络/LLM）。
"""
import pytest
from unittest.mock import MagicMock, patch

from src.trading.auto_trader import AutoTrader, ExecutionReport, RiskParams


# ── Fixtures ────────────────────────────────────────────────────
@pytest.fixture
def mock_dm():
    """Mock DataManager，返回空数据。"""
    dm = MagicMock()
    dm.get_daily_bars.return_value = __import__("pandas").DataFrame()
    dm.get_financial_data.return_value = __import__("pandas").DataFrame()
    return dm


@pytest.fixture
def mock_simulator():
    """Mock TradingSimulator，返回空持仓和固定价格。"""
    sim = MagicMock()
    sim.portfolio.positions = {}
    sim.get_portfolio_summary.return_value = {
        "total_value": 1_000_000.0,
        "market_value": 0.0,
        "cash": 1_000_000.0,
    }
    sim.get_current_price.return_value = 10.0
    sim.place_buy.return_value = {"success": True, "order": {"quantity": 100, "price": 10.0}}
    sim.place_sell.return_value = {"success": True, "order": {"quantity": 100, "price": 10.0}}
    return sim


@pytest.fixture
def mock_advisor():
    """Mock Advisor，返回高置信度买入建议。"""
    advisor = MagicMock()
    advisor.analyze_stock.return_value = {
        "buy_suggestion": "建议买入",
        "confidence": 80.0,
    }
    return advisor


# ── Phase 0: 止盈止损 ───────────────────────────────────────────
class TestStopLossTakeProfit:
    @pytest.mark.unit
    def test_stop_loss_triggers_sell(self, mock_dm, mock_simulator, mock_advisor):
        # 持仓亏损 10%（超过止损线 8%）
        mock_simulator.portfolio.positions = {
            "000001": {
                "code": "000001", "name": "X",
                "cost_price": 10.0, "current_price": 9.0,
                "available": 100,  # T+1 可卖
            }
        }
        risk = RiskParams(stop_loss_pct=8.0)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk)
        report = ExecutionReport()

        trader._check_stop_loss_take_profit(report)

        assert len(report.stop_loss_sells) == 1
        assert "止损" in report.stop_loss_sells[0]["reason"]
        mock_simulator.place_sell.assert_called_once_with("000001", 100)

    @pytest.mark.unit
    def test_take_profit_triggers_sell(self, mock_dm, mock_simulator, mock_advisor):
        # 持仓盈利 25%（超过止盈线 20%）
        mock_simulator.portfolio.positions = {
            "000002": {
                "code": "000002", "name": "Y",
                "cost_price": 10.0, "current_price": 12.5,
                "available": 100,
            }
        }
        risk = RiskParams(take_profit_pct=20.0)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk)
        report = ExecutionReport()

        trader._check_stop_loss_take_profit(report)

        assert len(report.take_profit_sells) == 1
        assert "止盈" in report.take_profit_sells[0]["reason"]

    @pytest.mark.unit
    def test_t1_unavailable_skipped(self, mock_dm, mock_simulator, mock_advisor):
        # 当日买入，available=0 → 不触发止盈止损（T+1 限制）
        mock_simulator.portfolio.positions = {
            "000003": {
                "code": "000003", "name": "Z",
                "cost_price": 10.0, "current_price": 8.0,  # 亏损 20%
                "available": 0,  # T+1 不可卖
            }
        }
        risk = RiskParams(stop_loss_pct=8.0)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk)
        report = ExecutionReport()

        trader._check_stop_loss_take_profit(report)

        assert len(report.stop_loss_sells) == 0
        mock_simulator.place_sell.assert_not_called()

    @pytest.mark.unit
    def test_within_threshold_no_action(self, mock_dm, mock_simulator, mock_advisor):
        # 持仓亏损 5%（未达止损线 8%）
        mock_simulator.portfolio.positions = {
            "000004": {
                "code": "000004", "name": "W",
                "cost_price": 10.0, "current_price": 9.5,
                "available": 100,
            }
        }
        risk = RiskParams(stop_loss_pct=8.0, take_profit_pct=20.0)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk)
        report = ExecutionReport()

        trader._check_stop_loss_take_profit(report)

        assert len(report.stop_loss_sells) == 0
        assert len(report.take_profit_sells) == 0


# ── 最大回撤暂停 ────────────────────────────────────────────────
class TestMaxDrawdown:
    @pytest.mark.unit
    def test_drawdown_exceeded_returns_true(self, mock_dm, mock_simulator, mock_advisor):
        # 初始 100 万，当前 80 万 → 回撤 20%（超过阈值 15%）
        mock_simulator.get_portfolio_summary.return_value = {
            "total_value": 800_000.0,
        }
        risk = RiskParams(max_drawdown_pct=15.0)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk)

        with patch("src.config.get_config") as mock_cfg:
            mock_cfg.return_value = {"trading": {"initial_cash": 1_000_000.0}}
            assert trader._is_max_drawdown_exceeded() is True

    @pytest.mark.unit
    def test_drawdown_within_threshold_returns_false(self, mock_dm, mock_simulator, mock_advisor):
        # 初始 100 万，当前 90 万 → 回撤 10%（未达阈值 15%）
        mock_simulator.get_portfolio_summary.return_value = {
            "total_value": 900_000.0,
        }
        risk = RiskParams(max_drawdown_pct=15.0)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk)

        with patch("src.config.get_config") as mock_cfg:
            mock_cfg.return_value = {"trading": {"initial_cash": 1_000_000.0}}
            assert trader._is_max_drawdown_exceeded() is False


# ── Phase 3: 风控过滤 ───────────────────────────────────────────
class TestRiskFilters:
    @pytest.mark.unit
    def test_daily_trade_cap_enforced(self, mock_dm, mock_simulator, mock_advisor):
        # 单日最大交易 2 次，尝试买 3 只 → 第 3 只被拦截
        risk = RiskParams(max_daily_trades=2, max_position_pct=50.0)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk)
        report = ExecutionReport()

        signals = [
            {"code": "c1", "name": "n1", "confidence": 80, "strategies_passed": ["s1"]},
            {"code": "c2", "name": "n2", "confidence": 80, "strategies_passed": ["s1"]},
            {"code": "c3", "name": "n3", "confidence": 80, "strategies_passed": ["s1"]},
        ]

        trader._execute_buy_orders(signals, report)

        assert len(report.executed_orders) == 2
        assert len(report.risk_blocked) == 1
        assert "单日最大交易次数" in report.risk_blocked[0]["reason"]

    @pytest.mark.unit
    def test_total_position_cap_enforced(self, mock_dm, mock_simulator, mock_advisor):
        # 总仓位已达 80%（上限 80%）→ 所有买入被拦截
        mock_simulator.get_portfolio_summary.return_value = {
            "total_value": 1_000_000.0,
            "market_value": 800_000.0,
        }
        risk = RiskParams(max_total_position_pct=80.0)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk)
        report = ExecutionReport()

        signals = [{"code": "c1", "name": "n1", "confidence": 80, "strategies_passed": ["s1"]}]
        trader._execute_buy_orders(signals, report)

        assert len(report.executed_orders) == 0
        assert len(report.risk_blocked) == 1
        assert "总仓位" in report.risk_blocked[0]["reason"]

    @pytest.mark.unit
    def test_single_stock_cap_enforced(self, mock_dm, mock_simulator, mock_advisor):
        # 000001 已持仓 20%（上限 20%）→ 再次买入被拦截
        mock_simulator.portfolio.positions = {
            "000001": {
                "code": "000001", "name": "X",
                "market_value": 200_000.0,
            }
        }
        mock_simulator.get_portfolio_summary.return_value = {
            "total_value": 1_000_000.0,
            "market_value": 200_000.0,
        }
        risk = RiskParams(max_position_pct=20.0)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk)
        report = ExecutionReport()

        signals = [{"code": "000001", "name": "X", "confidence": 80, "strategies_passed": ["s1"]}]
        trader._execute_buy_orders(signals, report)

        assert len(report.executed_orders) == 0
        assert len(report.risk_blocked) == 1
        assert "该股仓位" in report.risk_blocked[0]["reason"]

    @pytest.mark.unit
    def test_price_unavailable_skips(self, mock_dm, mock_simulator, mock_advisor):
        # 无法获取价格 → 跳过，记录错误
        mock_simulator.get_current_price.return_value = None
        risk = RiskParams()
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk)
        report = ExecutionReport()

        signals = [{"code": "c1", "name": "n1", "confidence": 80, "strategies_passed": ["s1"]}]
        trader._execute_buy_orders(signals, report)

        assert len(report.executed_orders) == 0
        assert len(report.errors) == 1
        assert "无法获取" in report.errors[0]


# ── 端到端 run() ────────────────────────────────────────────────
class TestRunEndToEnd:
    @pytest.mark.unit
    def test_empty_pool_returns_early(self, mock_dm, mock_simulator, mock_advisor):
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor)
        report = trader.run([], ["ma_cross"])

        assert report.total_executed == 0
        assert "股票池为空" in report.paused_reason or not report.paused_reason

    @pytest.mark.unit
    def test_max_drawdown_pauses_before_strategies(self, mock_dm, mock_simulator, mock_advisor):
        mock_simulator.get_portfolio_summary.return_value = {"total_value": 800_000.0}
        risk = RiskParams(max_drawdown_pct=15.0)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk)

        with patch("src.config.get_config") as mock_cfg:
            mock_cfg.return_value = {"trading": {"initial_cash": 1_000_000.0}}
            report = trader.run([{"code": "c1", "name": "n1"}], ["ma_cross"])

        assert report.paused_reason
        assert "回撤" in report.paused_reason
        assert len(report.strategy_candidates) == 0
