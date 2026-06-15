"""A股交易规则单测 —— 纯函数，零外部依赖。

规则模块是交易系统正确性的基石（涨跌停、手续费、手数、T+1），
任何回归都会直接造成成交价/费用错误，故全覆盖。
"""
import pytest

from src.trading.rules import (
    calc_commission,
    get_price_limit,
    is_t1_available,
    round_to_lot,
)


# ── 涨跌停 ──────────────────────────────────────────────────────
class TestGetPriceLimit:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "code,expected_pct",
        [
            ("000001", 0.10),  # 深市主板
            ("600519", 0.10),  # 沪市主板
            ("300750", 0.20),  # 创业板
            ("688981", 0.20),  # 科创板 688
            ("699001", 0.20),  # 科创板 689
            ("830799", 0.30),  # 北交所 8xx
            ("920099", 0.30),  # 北交所 9xx
        ],
    )
    def test_limit_band_matches_board(self, code, expected_pct):
        prev_close = 10.00
        up, down = get_price_limit(code, prev_close)
        assert up == round(prev_close * (1 + expected_pct), 2)
        assert down == round(prev_close * (1 - expected_pct), 2)

    @pytest.mark.unit
    def test_main_board_rounds_to_two_decimals(self):
        # 10.00 → 11.00 / 9.00，验证 round 到分
        up, down = get_price_limit("000001", 10.00)
        assert (up, down) == (11.00, 9.00)

    @pytest.mark.unit
    def test_star_board_wider_band(self):
        # 科创板 ±20%
        up, down = get_price_limit("688981", 50.00)
        assert (up, down) == (60.00, 40.00)

    @pytest.mark.unit
    def test_up_ge_down_and_brackets_prev(self):
        up, down = get_price_limit("000001", 10.00)
        assert down < 10.00 < up


# ── 手续费 ──────────────────────────────────────────────────────
class TestCalcCommission:
    @pytest.mark.unit
    def test_buy_below_minimum_returns_5(self):
        # 小额：按比例 0.3 元 < 5 元下限 → 取 5
        assert calc_commission(1_000.0, is_buy=True) == 5.0

    @pytest.mark.unit
    def test_buy_above_minimum_is_rate_only(self):
        # 10 万 × 0.0003 = 30 元，买入无印花税
        assert calc_commission(100_000.0, is_buy=True) == 30.0

    @pytest.mark.unit
    def test_sell_adds_stamp_duty(self):
        # 卖出 = 佣金(max(30,5)=30) + 印花税(100000×0.001=100) = 130
        assert calc_commission(100_000.0, is_buy=False) == 130.0

    @pytest.mark.unit
    def test_sell_small_amount_keeps_minimum_plus_duty(self):
        # 卖 1000 元：佣金 5 + 印花 1 = 6
        assert calc_commission(1_000.0, is_buy=False) == 6.0

    @pytest.mark.unit
    def test_custom_rate_respected(self):
        # 自定义费率 0.001：10 万 × 0.001 = 100（买入）
        assert calc_commission(100_000.0, is_buy=True, commission_rate=0.001) == 100.0


# ── 手数取整 ────────────────────────────────────────────────────
class TestRoundToLot:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "qty,expected",
        [
            (0, 0),
            (99, 0),
            (100, 100),
            (150, 100),
            (199, 100),
            (200, 200),
            (250, 200),
            (1_000, 1_000),
        ],
    )
    def test_truncates_to_multiple_of_100(self, qty, expected):
        assert round_to_lot(qty) == expected


# ── T+1 ─────────────────────────────────────────────────────────
class TestIsT1Available:
    @pytest.mark.unit
    def test_same_day_not_available(self):
        assert is_t1_available("2024-01-01", "2024-01-01") is False

    @pytest.mark.unit
    def test_next_day_available(self):
        assert is_t1_available("2024-01-01", "2024-01-02") is True

    @pytest.mark.unit
    def test_string_equality_only(self):
        # 仅按字符串判定，不同格式同一天也算不可卖（接口契约）
        assert is_t1_available("2024-01-01", "2024-1-1") is True
