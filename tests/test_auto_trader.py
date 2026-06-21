"""AutoTrader 风控单测 —— 信号生成器（方案 B）逻辑回归保护。

覆盖：止盈止损触发（signal/auto 两模式）、T+1 跳过、单日交易次数上限、
总仓位上限、单股仓位上限（加仓扣已有持仓）、最大回撤暂停（high-water mark）。
依赖：mock DataManager / TradingSimulator / Advisor（不依赖真实网络/LLM）。
"""
import pytest
from unittest.mock import MagicMock

from src.trading.auto_trader import AutoTrader, ExecutionReport, RiskParams


# ── Fixtures ────────────────────────────────────────────────────
@pytest.fixture
def mock_dm():
    """Mock DataManager，返回空数据 + 账户（peak_value 未初始化→用 initial 兜底）。"""
    dm = MagicMock()
    dm.get_daily_bars.return_value = __import__("pandas").DataFrame()
    dm.get_financial_data.return_value = __import__("pandas").DataFrame()
    dm.storage.get_account.return_value = {
        "initial_cash": 1_000_000.0,
        "peak_value": None,   # 未初始化，回撤以 initial 为起点峰值
    }
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
    """Mock Advisor（信号模式下 AI 仅作注释，返回值不影响决策门）。"""
    advisor = MagicMock()
    advisor.analyze_stock.return_value = {
        "buy_suggestion": "建议买入",
        "confidence": 80.0,
    }
    return advisor


# ── Phase 0: 止盈止损 ───────────────────────────────────────────
class TestStopLossTakeProfit:
    @pytest.mark.unit
    def test_stop_loss_signal_mode_records_suggestion(self, mock_dm, mock_simulator, mock_advisor):
        # 信号模式（默认）：亏损超止损线 → 记为卖出建议，**不自动下单**
        mock_simulator.portfolio.positions = {
            "000001": {"code": "000001", "name": "X",
                       "cost_price": 10.0, "current_price": 9.0, "available": 100}
        }
        risk = RiskParams(stop_loss_pct=8.0)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk)  # auto_execute=False
        report = ExecutionReport()

        trader._check_stop_loss_take_profit(report)

        assert len(report.sell_suggestions) == 1
        assert "止损" in report.sell_suggestions[0]["reason"]
        mock_simulator.place_sell.assert_not_called()  # 信号模式不下单

    @pytest.mark.unit
    def test_stop_loss_auto_mode_executes_sell(self, mock_dm, mock_simulator, mock_advisor):
        # auto 模式：亏损超止损线 → 自动卖出
        mock_simulator.portfolio.positions = {
            "000001": {"code": "000001", "name": "X",
                       "cost_price": 10.0, "current_price": 9.0, "available": 100}
        }
        risk = RiskParams(stop_loss_pct=8.0)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk, auto_execute=True)
        report = ExecutionReport()

        trader._check_stop_loss_take_profit(report)

        assert len(report.stop_loss_sells) == 1
        assert "止损" in report.stop_loss_sells[0]["reason"]
        mock_simulator.place_sell.assert_called_once_with("000001", 100)

    @pytest.mark.unit
    def test_take_profit_records_suggestion(self, mock_dm, mock_simulator, mock_advisor):
        mock_simulator.portfolio.positions = {
            "000002": {"code": "000002", "name": "Y",
                       "cost_price": 10.0, "current_price": 12.5, "available": 100}
        }
        risk = RiskParams(take_profit_pct=20.0)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk)
        report = ExecutionReport()

        trader._check_stop_loss_take_profit(report)

        assert len(report.sell_suggestions) == 1
        assert "止盈" in report.sell_suggestions[0]["reason"]

    @pytest.mark.unit
    def test_t1_unavailable_records_note_no_sell(self, mock_dm, mock_simulator, mock_advisor):
        # 当日买入 available=0 → 记为带 T+1 note 的建议，**不自动下单**
        mock_simulator.portfolio.positions = {
            "000003": {"code": "000003", "name": "Z",
                       "cost_price": 10.0, "current_price": 8.0, "available": 0}
        }
        risk = RiskParams(stop_loss_pct=8.0)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk)
        report = ExecutionReport()

        trader._check_stop_loss_take_profit(report)

        mock_simulator.place_sell.assert_not_called()
        assert len(report.sell_suggestions) == 1
        assert "T+1" in report.sell_suggestions[0]["note"]

    @pytest.mark.unit
    def test_within_threshold_no_action(self, mock_dm, mock_simulator, mock_advisor):
        # 持仓亏损 5%（未达止损线 8%）
        mock_simulator.portfolio.positions = {
            "000004": {"code": "000004", "name": "W",
                       "cost_price": 10.0, "current_price": 9.5, "available": 100}
        }
        risk = RiskParams(stop_loss_pct=8.0, take_profit_pct=20.0)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk)
        report = ExecutionReport()

        trader._check_stop_loss_take_profit(report)

        assert len(report.sell_suggestions) == 0
        mock_simulator.place_sell.assert_not_called()


# ── 最大回撤暂停（high-water mark）──────────────────────────────
class TestMaxDrawdown:
    @pytest.mark.unit
    def test_drawdown_exceeded_from_initial(self, mock_dm, mock_simulator, mock_advisor):
        # peak 未初始化→以 initial 100万 为峰值；当前 80万 → 回撤 20%（超 15%）
        mock_simulator.get_portfolio_summary.return_value = {"total_value": 800_000.0}
        risk = RiskParams(max_drawdown_pct=15.0)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk)
        report = ExecutionReport()

        assert trader._is_max_drawdown_exceeded(report) is True
        assert report.drawdown_pct == pytest.approx(20.0)

    @pytest.mark.unit
    def test_drawdown_uses_high_water_mark_not_initial(self, mock_dm, mock_simulator, mock_advisor):
        # 【关键修复】峰值 120万（曾赚 20%），当前 90万 → 真实回撤 25%。
        # 旧实现用 initial 会算成 (100-90)/100=10% → 不触发；high-water mark 正确触发。
        mock_dm.storage.get_account.return_value = {
            "initial_cash": 1_000_000.0, "peak_value": 1_200_000.0,
        }
        mock_simulator.get_portfolio_summary.return_value = {"total_value": 900_000.0}
        risk = RiskParams(max_drawdown_pct=15.0)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk)
        report = ExecutionReport()

        assert trader._is_max_drawdown_exceeded(report) is True
        assert report.drawdown_pct == pytest.approx(25.0)

    @pytest.mark.unit
    def test_peak_updates_on_new_high(self, mock_dm, mock_simulator, mock_advisor):
        # 当前 130万 > 峰值 120万 → 创新高，回撤 0，峰值写库更新
        mock_dm.storage.get_account.return_value = {
            "initial_cash": 1_000_000.0, "peak_value": 1_200_000.0,
        }
        mock_simulator.get_portfolio_summary.return_value = {"total_value": 1_300_000.0}
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor)
        report = ExecutionReport()

        assert trader._is_max_drawdown_exceeded(report) is False
        assert report.drawdown_pct == 0.0
        mock_dm.storage.update_peak_value.assert_called_once_with(1_300_000.0)

    @pytest.mark.unit
    def test_drawdown_within_threshold_returns_false(self, mock_dm, mock_simulator, mock_advisor):
        mock_simulator.get_portfolio_summary.return_value = {"total_value": 900_000.0}
        risk = RiskParams(max_drawdown_pct=15.0)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk)
        report = ExecutionReport()

        assert trader._is_max_drawdown_exceeded(report) is False
        assert report.drawdown_pct == pytest.approx(10.0)


# ── 回撤分档减仓（circuit-breaker deescalation）──────────────────
class TestDrawdownDeescalation:
    def _setup(self, mock_dm, mock_simulator, peak=1_000_000.0, current=820_000.0,
               qty=1000, available=1000):
        # 回撤 18%：peak 100万，current 82万 → dd=18%（命中第2档 keep 0.5）
        mock_dm.storage.get_account.return_value = {
            "initial_cash": 1_000_000.0, "peak_value": peak,
        }
        mock_simulator.get_portfolio_summary.return_value = {"total_value": current}
        # current_price == cost → pnl 0，避免与止盈止损阶段叠加（隔离测减仓）
        mock_simulator.portfolio.positions = {
            "000001": {"code": "000001", "name": "X",
                       "quantity": qty, "available": available,
                       "cost_price": 10.0, "current_price": 10.0}
        }

    @pytest.mark.unit
    def test_signal_mode_records_trim_suggestion(self, mock_dm, mock_simulator, mock_advisor):
        self._setup(mock_dm, mock_simulator)  # dd 18% → keep 0.5
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor)  # signal 模式
        report = ExecutionReport()
        trader._is_max_drawdown_exceeded(report)  # 设置 report.drawdown_pct
        trader._apply_drawdown_deescalation(report)

        assert report.deescalation_tier == 2
        assert report.deescalation_keep_ratio == pytest.approx(0.5)
        assert len(report.sell_suggestions) == 1
        assert report.sell_suggestions[0]["trim_quantity"] == 500  # 1000 * (1-0.5)
        mock_simulator.place_sell.assert_not_called()  # signal 模式不下单

    @pytest.mark.unit
    def test_auto_mode_executes_trim(self, mock_dm, mock_simulator, mock_advisor):
        self._setup(mock_dm, mock_simulator)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, auto_execute=True)
        report = ExecutionReport()
        trader._is_max_drawdown_exceeded(report)
        trader._apply_drawdown_deescalation(report)

        assert len(report.deescalation_sells) == 1
        mock_simulator.place_sell.assert_called_once_with("000001", 500)

    @pytest.mark.unit
    def test_t1_limits_trim_to_available(self, mock_dm, mock_simulator, mock_advisor):
        # T+1：available 200 < 目标减仓 500 → auto 只卖 200，记 note
        self._setup(mock_dm, mock_simulator, qty=1000, available=200)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, auto_execute=True)
        report = ExecutionReport()
        trader._is_max_drawdown_exceeded(report)
        trader._apply_drawdown_deescalation(report)

        mock_simulator.place_sell.assert_called_once_with("000001", 200)
        assert report.deescalation_sells[0]["sold_quantity"] == 200
        assert "T+1" in report.deescalation_sells[0]["note"]

    @pytest.mark.unit
    def test_shallow_drawdown_no_trim(self, mock_dm, mock_simulator, mock_advisor):
        # 回撤 5% < 10% 第一档 → 不减仓
        self._setup(mock_dm, mock_simulator, current=950_000.0)  # dd 5%
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor)
        report = ExecutionReport()
        trader._is_max_drawdown_exceeded(report)
        trader._apply_drawdown_deescalation(report)

        assert report.deescalation_tier is None
        assert len(report.sell_suggestions) == 0
        mock_simulator.place_sell.assert_not_called()

    @pytest.mark.unit
    def test_deescalation_runs_then_pauses_in_run(self, mock_dm, mock_simulator, mock_advisor):
        # dd 18% > max_drawdown 15% → run() 先减仓再暂停（不再开新仓）
        self._setup(mock_dm, mock_simulator)
        risk = RiskParams(max_drawdown_pct=15.0)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk)
        report = trader.run([{"code": "000001", "name": "X"}], ["ma_cross"])

        assert report.paused_reason  # 暂停
        assert report.deescalation_tier == 2  # 但已先执行减仓
        assert len(report.sell_suggestions) == 1  # signal 模式记了减仓建议

    @pytest.mark.unit
    def test_deescalation_can_be_disabled(self, mock_dm, mock_simulator, mock_advisor):
        self._setup(mock_dm, mock_simulator)
        risk = RiskParams(max_drawdown_pct=15.0, drawdown_deescalation=False)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk)
        report = ExecutionReport()
        trader._is_max_drawdown_exceeded(report)
        trader._apply_drawdown_deescalation(report)
        assert len(report.sell_suggestions) == 0


# ── 行业集中度上限 ───────────────────────────────────────────────
class TestIndustryConcentration:
    @pytest.mark.unit
    def test_over_concentrated_buy_blocked(self, mock_dm, mock_simulator, mock_advisor):
        # 银行股已持仓 60万（占 60%），拟再买银行股 → 超 30% 上限 → 拦截
        import pandas as pd
        mock_dm.get_stock_list.return_value = pd.DataFrame({
            "code": ["000001", "600000"], "name": ["X", "Y"], "industry": ["银行", "银行"]
        })
        mock_simulator.portfolio.positions = {
            "600000": {"code": "600000", "name": "Y", "market_value": 600_000.0}
        }
        mock_simulator.get_portfolio_summary.return_value = {
            "total_value": 1_000_000.0, "market_value": 600_000.0,
        }
        risk = RiskParams(max_position_pct=50.0, max_industry_pct=30.0)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk)
        report = ExecutionReport()

        trader._build_and_maybe_execute(
            [{"code": "000001", "name": "X", "strategies_passed": ["s1"], "score_sum": 1}], report)

        # 应被行业集中度拦截
        assert len(report.buy_suggestions) == 0
        assert len(report.risk_blocked) == 1
        assert "集中度" in report.risk_blocked[0]["reason"]

    @pytest.mark.unit
    def test_no_stock_list_skips_check(self, mock_dm, mock_simulator, mock_advisor):
        # 无 stock_list（industry 不可知）→ 跳过集中度检查，正常产出建议
        mock_dm.get_stock_list.return_value = __import__("pandas").DataFrame()
        mock_simulator.get_portfolio_summary.return_value = {
            "total_value": 1_000_000.0, "market_value": 0.0,
        }
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor,
                            RiskParams(max_position_pct=20.0, max_industry_pct=30.0))
        report = ExecutionReport()
        trader._build_and_maybe_execute(
            [{"code": "000001", "name": "X", "strategies_passed": ["s1"], "score_sum": 1}], report)
        assert len(report.buy_suggestions) == 1


# ── Phase 3: 风控预算 ───────────────────────────────────────────
class TestRiskBudget:
    @pytest.mark.unit
    def test_daily_trade_cap_enforced_in_auto_mode(self, mock_dm, mock_simulator, mock_advisor):
        # auto 模式：单日最大交易 2 次，尝试买 3 只 → 第 3 只被拦截
        risk = RiskParams(max_daily_trades=2, max_position_pct=50.0)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk, auto_execute=True)
        report = ExecutionReport()

        signals = [
            {"code": "c1", "name": "n1", "strategies_passed": ["s1"], "score_sum": 1},
            {"code": "c2", "name": "n2", "strategies_passed": ["s1"], "score_sum": 1},
            {"code": "c3", "name": "n3", "strategies_passed": ["s1"], "score_sum": 1},
        ]
        trader._build_and_maybe_execute(signals, report)

        assert len(report.executed_orders) == 2
        assert len(report.risk_blocked) == 1
        assert "单日最大交易次数" in report.risk_blocked[0]["reason"]

    @pytest.mark.unit
    def test_total_position_cap_blocks_buy(self, mock_dm, mock_simulator, mock_advisor):
        # 总仓位已达 80%（上限 80%）→ 买入被拦截（信号模式）
        mock_simulator.get_portfolio_summary.return_value = {
            "total_value": 1_000_000.0, "market_value": 800_000.0,
        }
        risk = RiskParams(max_total_position_pct=80.0)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk)
        report = ExecutionReport()

        trader._build_and_maybe_execute(
            [{"code": "c1", "name": "n1", "strategies_passed": ["s1"], "score_sum": 1}], report)

        assert len(report.buy_suggestions) == 0
        assert len(report.risk_blocked) == 1
        assert "总仓位" in report.risk_blocked[0]["reason"]

    @pytest.mark.unit
    def test_single_stock_cap_subtracts_existing(self, mock_dm, mock_simulator, mock_advisor):
        # 【关键修复】000001 已持仓 20%（市值 20万），上限 20% → 加仓 headroom=0 → 拦截。
        # 旧实现不减已有持仓会按 20% 再加 → 击穿上限。
        mock_simulator.portfolio.positions = {
            "000001": {"code": "000001", "name": "X", "market_value": 200_000.0}
        }
        mock_simulator.get_portfolio_summary.return_value = {
            "total_value": 1_000_000.0, "market_value": 200_000.0,
        }
        risk = RiskParams(max_position_pct=20.0)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk)
        report = ExecutionReport()

        trader._build_and_maybe_execute(
            [{"code": "000001", "name": "X", "strategies_passed": ["s1"], "score_sum": 1}], report)

        assert len(report.buy_suggestions) == 0
        assert len(report.risk_blocked) == 1
        assert "上限" in report.risk_blocked[0]["reason"]

    @pytest.mark.unit
    def test_add_quantity_subtracts_existing_partial(self, mock_dm, mock_simulator, mock_advisor):
        # 已持仓 12%（市值 12万），上限 20% → 只能加 8%（8万/10元=8000 股），不击穿
        mock_simulator.portfolio.positions = {
            "000001": {"code": "000001", "name": "X", "market_value": 120_000.0}
        }
        mock_simulator.get_portfolio_summary.return_value = {
            "total_value": 1_000_000.0, "market_value": 120_000.0,
        }
        risk = RiskParams(max_position_pct=20.0)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk)
        report = ExecutionReport()

        trader._build_and_maybe_execute(
            [{"code": "000001", "name": "X", "strategies_passed": ["s1"], "score_sum": 1}], report)

        assert len(report.buy_suggestions) == 1
        assert report.buy_suggestions[0]["quantity"] == 8000

    @pytest.mark.unit
    def test_signal_mode_records_suggestions_not_orders(self, mock_dm, mock_simulator, mock_advisor):
        # 信号模式：产出 buy_suggestions，不调用 place_buy
        risk = RiskParams(max_position_pct=20.0)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk)
        report = ExecutionReport()

        trader._build_and_maybe_execute(
            [{"code": "c1", "name": "n1", "strategies_passed": ["s1"], "score_sum": 1}], report)

        assert len(report.buy_suggestions) == 1
        assert len(report.executed_orders) == 0
        mock_simulator.place_buy.assert_not_called()

    @pytest.mark.unit
    def test_price_unavailable_skips(self, mock_dm, mock_simulator, mock_advisor):
        mock_simulator.get_current_price.return_value = None
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor)
        report = ExecutionReport()

        trader._build_and_maybe_execute(
            [{"code": "c1", "name": "n1", "strategies_passed": ["s1"], "score_sum": 1}], report)

        assert len(report.buy_suggestions) == 0
        assert len(report.errors) == 1
        assert "无法获取" in report.errors[0]


# ── 端到端 run() ────────────────────────────────────────────────
class TestRunEndToEnd:
    @pytest.mark.unit
    def test_empty_pool_returns_early(self, mock_dm, mock_simulator, mock_advisor):
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor)
        report = trader.run([], ["ma_cross"])

        assert report.total_executed == 0
        assert not report.paused_reason  # 空池不触发回撤暂停

    @pytest.mark.unit
    def test_max_drawdown_pauses_before_strategies(self, mock_dm, mock_simulator, mock_advisor):
        mock_simulator.get_portfolio_summary.return_value = {"total_value": 800_000.0}
        risk = RiskParams(max_drawdown_pct=15.0)
        trader = AutoTrader(mock_dm, mock_simulator, mock_advisor, risk)

        report = trader.run([{"code": "c1", "name": "n1"}], ["ma_cross"])

        assert report.paused_reason
        assert "回撤" in report.paused_reason
        assert len(report.strategy_candidates) == 0
