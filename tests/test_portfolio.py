"""Portfolio 集成测试 —— 买卖/资金/T+1/盈亏，用临时 SQLite 库。

依赖 storage / portfolio fixture（每测独立临时库，见 conftest.py）。
覆盖交易系统资金正确性的核心路径。
"""
import json  # noqa: F401  (保留给后续断言扩展)
import pandas as pd
import pytest


# ── 买入 ────────────────────────────────────────────────────────
class TestBuy:
    @pytest.mark.integration
    def test_buy_deducts_cash_with_commission(self, portfolio):
        result = portfolio.buy("000001", "平安银行", 10.00, 100, "2024-01-01")
        assert result["success"] is True
        order = result["order"]
        # 100×10=1000；佣金 max(0.3,5)=5 + 过户费 1000*0.0001=0.1 = 5.1 → 总成本 1005.1
        assert order["amount"] == 1000.0
        assert order["commission"] == 5.1
        assert portfolio.cash == pytest.approx(1_000_000.0 - 1005.1)

        pos = portfolio.get_position("000001")
        assert pos["quantity"] == 100
        assert pos["available"] == 0  # T+1 当日不可卖
        assert pos["cost_price"] == 10.00

    @pytest.mark.integration
    def test_buy_truncates_non_lot(self, portfolio):
        # portfolio 仍会截断到 100 整数倍（UI 已前置拦截，这里固化合约行为）
        result = portfolio.buy("000002", "X", 10.00, 150, "2024-01-01")
        assert result["success"] is True
        assert result["order"]["quantity"] == 100  # 150 → 100

    @pytest.mark.integration
    def test_buy_insufficient_cash_fails(self, portfolio):
        # 10000×100=1,000,000 + 佣金 300 = 1,000,300 > 1,000,000
        result = portfolio.buy("000003", "Y", 100.00, 10_000, "2024-01-01")
        assert result["success"] is False
        assert "资金不足" in result["msg"]
        assert portfolio.get_position("000003") is None

    @pytest.mark.integration
    def test_buy_below_one_lot_fails(self, portfolio):
        result = portfolio.buy("000004", "Z", 10.00, 50, "2024-01-01")  # 50 → 0
        assert result["success"] is False


# ── T+1 卖出 ────────────────────────────────────────────────────
class TestT1Selling:
    @pytest.mark.integration
    def test_cannot_sell_same_day(self, portfolio):
        portfolio.buy("000001", "平安银行", 10.00, 100, "2024-01-01")
        # 同日卖出：available=0 → 拒绝
        result = portfolio.sell("000001", 11.00, 100, "2024-01-01")
        assert result["success"] is False
        assert "T+1" in result["msg"]

    @pytest.mark.integration
    def test_end_of_day_unlocks_for_next_day(self, portfolio):
        portfolio.buy("000001", "平安银行", 10.00, 100, "2024-01-01")
        portfolio.end_of_day("2024-01-02")  # 次日日终：解锁可卖
        assert portfolio.get_position("000001")["available"] == 100

        result = portfolio.sell("000001", 11.00, 100, "2024-01-02")
        assert result["success"] is True
        assert result["order"]["direction"] == "SELL"
        # 卖出后持仓清空
        assert portfolio.get_position("000001") is None

    @pytest.mark.integration
    def test_end_of_day_same_day_does_not_unlock(self, portfolio):
        # 关键边界：当日日终（buy_date == trade_date）不解锁，严格遵守 T+1
        portfolio.buy("000001", "X", 10.00, 100, "2024-01-01")
        portfolio.end_of_day("2024-01-01")
        assert portfolio.get_position("000001")["available"] == 0

    @pytest.mark.integration
    def test_sell_more_than_available_rejected(self, portfolio):
        portfolio.buy("000001", "X", 10.00, 200, "2024-01-01")
        portfolio.end_of_day("2024-01-02")
        # 可卖 200，尝试卖 300 → 拒绝
        result = portfolio.sell("000001", 11.00, 300, "2024-01-02")
        assert result["success"] is False
        assert "可卖" in result["msg"]


# ── 资金与盈亏 ──────────────────────────────────────────────────
class TestValuation:
    @pytest.mark.integration
    def test_sell_net_income_added_to_cash(self, portfolio):
        portfolio.buy("000001", "X", 10.00, 100, "2024-01-01")
        cash_before_sell = portfolio.cash
        portfolio.end_of_day("2024-01-02")
        portfolio.sell("000001", 12.00, 100, "2024-01-02")
        # 卖出 100×12=1200；佣金 max(0.36,5)=5 + 过户 0.12 + 印花 1200*0.0005=0.6 = 5.72 → 净到账 1194.28
        assert portfolio.cash == pytest.approx(cash_before_sell + 1194.28)

    @pytest.mark.integration
    def test_update_prices_recomputes_pnl(self, portfolio):
        portfolio.buy("000001", "X", 10.00, 100, "2024-01-01")
        portfolio.update_prices({"000001": 12.00})
        pos = portfolio.get_position("000001")
        assert pos["current_price"] == 12.00
        assert pos["market_value"] == 1200.0
        assert pos["profit_loss"] == pytest.approx(200.0)

    @pytest.mark.integration
    def test_summary_totals(self, portfolio):
        portfolio.buy("000001", "X", 10.00, 100, "2024-01-01")
        portfolio.update_prices({"000001": 12.00})
        s = portfolio.summary({"000001": 12.00})
        assert s["position_count"] == 1
        assert s["market_value"] == 1200.0
        # 总资产 = 现金(1,000,000-1005.1) + 市值 1200
        assert s["total_value"] == pytest.approx(1_000_000.0 - 1005.1 + 1200.0)


def _seed_prev_close(storage, code="000001", close=10.0, trade_date="2024-01-01"):
    """往临时库插一根日K，作为涨跌停校验的 prev_close 来源（主板 ±10%）。"""
    storage.upsert_daily_bars(pd.DataFrame([{
        "code": code, "trade_date": trade_date,
        "open": close, "high": close, "low": close, "close": close,
        "volume": 1000.0, "amount": 10000.0, "pct_chg": 0.0,
    }]))


# ── 加仓 T+1（回归：旧实现加仓后 buy_date 不更新，end_of_day 误解锁当日加仓部分）──
class TestAddBuyT1:
    @pytest.mark.integration
    def test_same_day_add_buy_stays_locked(self, portfolio):
        portfolio.buy("000001", "X", 10.00, 100, "2024-01-01")
        portfolio.buy("000001", "X", 10.00, 100, "2024-01-01")  # 同日加仓
        pos = portfolio.get_position("000001")
        assert pos["quantity"] == 200
        assert pos["available"] == 0  # 当天买入全部不可卖
        portfolio.end_of_day("2024-01-01")
        assert portfolio.get_position("000001")["available"] == 0  # 当日日终不解锁
        portfolio.end_of_day("2024-01-02")
        assert portfolio.get_position("000001")["available"] == 200  # 次日全解锁

    @pytest.mark.integration
    def test_cross_day_add_buy_new_portion_locked_at_eod(self, portfolio):
        """核心回归：跨日加仓后，当日日终不得解锁新加仓部分（旧 bug 会解锁）。"""
        portfolio.buy("000001", "X", 10.00, 100, "2024-01-01")
        portfolio.end_of_day("2024-01-02")
        assert portfolio.get_position("000001")["available"] == 100  # 第1批解锁

        portfolio.buy("000001", "X", 10.00, 100, "2024-01-03")  # 跨日加仓
        pos = portfolio.get_position("000001")
        assert pos["quantity"] == 200
        assert pos["available"] == 100  # 旧 100 可卖，新 100 锁定

        portfolio.end_of_day("2024-01-03")
        # 旧 bug：buy_date 仍为 01-01 → != 01-03 → 把全部 200 解锁（T+0 违规）
        assert portfolio.get_position("000001")["available"] == 100

        portfolio.end_of_day("2024-01-04")
        assert portfolio.get_position("000001")["available"] == 200  # 次日才全解锁


# ── 涨跌停校验（Portfolio 层兜底）──────────────────────────────
class TestPriceLimit:
    @pytest.mark.integration
    def test_buy_at_limit_up_rejected(self, storage, portfolio):
        _seed_prev_close(storage, close=10.00)  # 主板 ±10% → 涨停价 11.00
        result = portfolio.buy("000001", "X", 11.00, 100, "2024-01-02")
        assert result["success"] is False
        assert "涨停" in result["msg"]
        assert portfolio.get_position("000001") is None  # 未建仓

    @pytest.mark.integration
    def test_buy_below_limit_up_ok(self, storage, portfolio):
        _seed_prev_close(storage, close=10.00)
        result = portfolio.buy("000001", "X", 10.80, 100, "2024-01-02")  # < 11.00
        assert result["success"] is True

    @pytest.mark.integration
    def test_sell_at_limit_down_rejected(self, storage, portfolio):
        _seed_prev_close(storage, close=10.00)  # 跌停价 9.00
        portfolio.buy("000001", "X", 9.50, 100, "2024-01-01")  # 9.5 未触涨跌停
        portfolio.end_of_day("2024-01-02")
        result = portfolio.sell("000001", 9.00, 100, "2024-01-02")
        assert result["success"] is False
        assert "跌停" in result["msg"]

    @pytest.mark.integration
    def test_no_prev_close_skips_check(self, portfolio):
        # 空库（无 daily_bars）不拦截，保证新上市/无数据场景正常下单
        result = portfolio.buy("000001", "X", 10.00, 100, "2024-01-01")
        assert result["success"] is True
