"""A股交易规则单测 —— 纯函数，零外部依赖。

规则模块是交易系统正确性的基石（涨跌停、手续费、手数、T+1），
任何回归都会直接造成成交价/费用错误，故全覆盖。
"""
import pytest

from src.trading.rules import (
    STAMP_DUTY_RATE,
    TRANSFER_FEE_RATE,
    calc_commission,
    get_price_limit,
    is_at_limit_down,
    is_at_limit_up,
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


# ── 涨跌停触及判定（回测/实盘共用，避免无限流动性幻觉）─────────
class TestLimitTouch:
    @pytest.mark.unit
    def test_at_limit_up_true(self):
        # 主板 10 元 → 涨停 11.00，触及即买不进
        assert is_at_limit_up("000001", 10.00, 11.00) is True
        assert is_at_limit_up("000001", 10.00, 11.50) is True

    @pytest.mark.unit
    def test_at_limit_up_false_below(self):
        assert is_at_limit_up("000001", 10.00, 10.50) is False

    @pytest.mark.unit
    def test_at_limit_down_true(self):
        # 主板 10 元 → 跌停 9.00，触及即卖不出
        assert is_at_limit_down("000001", 10.00, 9.00) is True
        assert is_at_limit_down("000001", 10.00, 8.50) is True

    @pytest.mark.unit
    def test_at_limit_down_false_above(self):
        assert is_at_limit_down("000001", 10.00, 9.50) is False

    @pytest.mark.unit
    def test_zero_prev_close_not_touched(self):
        # 数据异常（停牌/缺失）不误判为涨跌停
        assert is_at_limit_up("000001", 0.0, 5.0) is False
        assert is_at_limit_down("000001", 0.0, 5.0) is False


# ── 手续费（印花税 0.05% / 过户费 0.01% 双边，2023-2022 新规）─────
class TestCalcCommission:
    @pytest.mark.unit
    def test_buy_below_minimum_returns_5_plus_transfer(self):
        # 小额：佣金 0.3 < 5 取 5；加过户费 1000*0.0001=0.1 → 5.1（买入无印花税）
        assert calc_commission(1_000.0, is_buy=True) == 5.1

    @pytest.mark.unit
    def test_buy_above_minimum_is_rate_plus_transfer(self):
        # 10万 × 0.0003 = 30 佣金 + 过户费 10万*0.0001=10 = 40（买入无印花税）
        assert calc_commission(100_000.0, is_buy=True) == 40.0

    @pytest.mark.unit
    def test_sell_adds_stamp_duty_and_transfer(self):
        # 卖出 = 佣金 30 + 过户费 10 + 印花税 100000*0.0005=50 = 90
        assert calc_commission(100_000.0, is_buy=False) == 90.0

    @pytest.mark.unit
    def test_sell_small_amount_keeps_minimum_plus_fees(self):
        # 卖 1000：佣金 5 + 过户 0.1 + 印花 0.5 = 5.6
        assert calc_commission(1_000.0, is_buy=False) == 5.6

    @pytest.mark.unit
    def test_stamp_duty_is_new_rate_not_legacy(self):
        # 防回归：印花税必须是 0.0005（2023.8.28 起），不是旧的 0.001
        assert STAMP_DUTY_RATE == 0.0005

    @pytest.mark.unit
    def test_transfer_fee_is_bilateral(self):
        # 过户费双边：买卖都收，且为 0.0001
        assert TRANSFER_FEE_RATE == 0.0001
        # 买入也含过户费（确认双边）
        buy_fee = calc_commission(100_000.0, is_buy=True)
        assert buy_fee == 40.0  # 30 佣金 + 10 过户

    @pytest.mark.unit
    def test_custom_rate_respected(self):
        # 自定义佣金 0.001：10万×0.001=100 + 过户 10 = 110（买入）
        assert calc_commission(100_000.0, is_buy=True, commission_rate=0.001) == 110.0


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
