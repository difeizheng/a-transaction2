"""TradingSimulator 单测 —— 涨跌停拦截（交易最后一道防线）。

A 股涨跌停交易规则：
- 涨停（封死涨停板）：只有买单排队，无卖单 → 能卖不能买
- 跌停（封死跌停板）：只有卖单排队，无买单 → 能买不能卖

故 place_buy 拦涨停（price >= limit_up）、place_sell 拦跌停（price <= limit_down）。
"""
import pandas as pd
import pytest

from src.trading.simulator import TradingSimulator


# ── 辅助：构造带历史K线的临时库 + 模拟器 ──────────────────────
def _bars_df(code: str, closes, start="2024-01-01"):
    """生成收盘价序列对应的K线DataFrame，最后一行 close 决定 prev_close。"""
    dates = pd.date_range(start, periods=len(closes), freq="D")
    return pd.DataFrame({
        "code": [code] * len(closes),
        "trade_date": dates,
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [1000] * len(closes), "amount": [10000] * len(closes),
        "pct_chg": [0.0] * len(closes),
    })


class _FakeDM:
    """轻量 DM：storage 用真实临时库，realtime 返回空强制走降级路径。"""

    def __init__(self, storage):
        self.storage = storage

    def get_realtime_quotes(self, codes):
        return pd.DataFrame()  # 空 → get_current_price 降级到最新收盘价


@pytest.fixture
def sim(storage):
    """主板 000001，前收 10.0 → 涨停 11.0 / 跌停 9.0。"""
    storage.upsert_daily_bars(_bars_df("000001", [9.9, 10.0, 10.0]))
    return TradingSimulator(_FakeDM(storage))


@pytest.fixture
def sim_gem(storage):
    """创业板 300750，前收 10.0 → 涨停 12.0 / 跌停 8.0（±20%）。"""
    storage.upsert_daily_bars(_bars_df("300750", [9.9, 10.0, 10.0]))
    return TradingSimulator(_FakeDM(storage))


@pytest.fixture
def sim_bj(storage):
    """北交所 830799，前收 10.0 → 涨停 13.0 / 跌停 7.0（±30%）。"""
    storage.upsert_daily_bars(_bars_df("830799", [9.9, 10.0, 10.0]))
    return TradingSimulator(_FakeDM(storage))


# ── 涨停拦截买入 ────────────────────────────────────────────────
class TestBuyLimitUp:
    @pytest.mark.integration
    def test_buy_at_limit_up_rejected(self, sim):
        # price == limit_up（11.0）→ 拦截（>= 语义，涨停价不可买）
        result = sim.place_buy("000001", "X", 100, price=11.0)
        assert result["success"] is False
        assert "涨停" in result["msg"]

    @pytest.mark.integration
    def test_buy_above_limit_up_rejected(self, sim):
        result = sim.place_buy("000001", "X", 100, price=11.5)
        assert result["success"] is False
        assert "涨停" in result["msg"]

    @pytest.mark.integration
    def test_buy_below_limit_up_succeeds(self, sim):
        # 10.99 < 11.0 → 正常买入
        result = sim.place_buy("000001", "X", 100, price=10.99)
        assert result["success"] is True

    @pytest.mark.integration
    def test_board_specific_gem_20pct(self, sim_gem):
        # 创业板涨停 12.0
        assert sim_gem.place_buy("300750", "X", 100, price=12.0)["success"] is False
        assert sim_gem.place_buy("300750", "X", 100, price=11.9)["success"] is True

    @pytest.mark.integration
    def test_board_specific_bj_30pct(self, sim_bj):
        # 北交所涨停 13.0
        assert sim_bj.place_buy("830799", "X", 100, price=13.0)["success"] is False
        assert sim_bj.place_buy("830799", "X", 100, price=12.9)["success"] is True


# ── 跌停拦截卖出 ────────────────────────────────────────────────
class TestSellLimitDown:
    @pytest.mark.integration
    def test_sell_at_limit_down_rejected(self, sim):
        # price == limit_down（9.0）→ 拦截（<= 语义）
        result = sim.place_sell("000001", 100, price=9.0)
        assert result["success"] is False
        assert "跌停" in result["msg"]

    @pytest.mark.integration
    def test_sell_below_limit_down_rejected(self, sim):
        result = sim.place_sell("000001", 100, price=8.5)
        assert result["success"] is False
        assert "跌停" in result["msg"]

    @pytest.mark.integration
    def test_sell_above_limit_down_passes_limit_check(self, sim):
        # 9.01 > 9.0 → 跌停检查通过（但无持仓 → portfolio 返回未持有）
        result = sim.place_sell("000001", 100, price=9.01)
        assert result["success"] is False
        assert "跌停" not in result["msg"]  # 不是被跌停拦截


# ── 市价降级与价格获取 ──────────────────────────────────────────
class TestPriceResolution:
    @pytest.mark.integration
    def test_market_price_falls_back_to_last_close(self, sim):
        # price=None → 降级到最新收盘价 10.0（< 涨停 11.0）→ 成功
        result = sim.place_buy("000001", "X", 100, price=None)
        assert result["success"] is True
        assert result["order"]["price"] == 10.0

    @pytest.mark.integration
    def test_no_data_no_price_fails(self, storage):
        # 无 K 线 + 无实时 → 无法获取价格
        sim = TradingSimulator(_FakeDM(storage))
        result = sim.place_buy("999999", "X", 100, price=None)
        assert result["success"] is False
        assert "无法获取" in result["msg"] or "价格" in result["msg"]


# ── 完整买卖流程（含涨跌停 + T+1）──────────────────────────────
class TestEndToEndWithLimits:
    @pytest.mark.integration
    def test_full_cycle_respects_limits(self, sim):
        from datetime import date, timedelta
        # 1. 正常买入 @10.0
        buy = sim.place_buy("000001", "X", 100, price=10.0)
        assert buy["success"] is True

        # 2. T+1：次日才能卖
        next_day = (date.today() + timedelta(days=1)).isoformat()
        sim.portfolio.end_of_day(next_day)

        # 3. 跌停价卖出被拦
        sell_blocked = sim.place_sell("000001", 100, price=9.0)
        assert sell_blocked["success"] is False
        assert "跌停" in sell_blocked["msg"]

        # 4. 正常价卖出成功
        sell_ok = sim.place_sell("000001", 100, price=10.5)
        assert sell_ok["success"] is True
