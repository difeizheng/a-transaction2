"""交易日期回退单测 —— 防周末对全市场发起无效拉取烧积分。

`_latest_possible_trading_day` 把周末（周六5/周日6）回退到周五，
使增量更新的 end_date 上界落在最近可能交易日，避免
`latest(周五) < today(周六)` 恒真触发重复拉取。
"""
from datetime import date

import pytest

from src.data.manager import _latest_possible_trading_day


class TestLatestPossibleTradingDay:
    @pytest.mark.unit
    def test_weekday_unchanged(self):
        # 2024-01-03 周三 → 原样
        assert _latest_possible_trading_day(date(2024, 1, 3)) == date(2024, 1, 3)

    @pytest.mark.unit
    def test_saturday_rolls_back_to_friday(self):
        # 2024-01-06 周六 → 2024-01-05 周五
        assert _latest_possible_trading_day(date(2024, 1, 6)) == date(2024, 1, 5)

    @pytest.mark.unit
    def test_sunday_rolls_back_to_friday(self):
        # 2024-01-07 周日 → 2024-01-05 周五（跨过周六）
        assert _latest_possible_trading_day(date(2024, 1, 7)) == date(2024, 1, 5)

    @pytest.mark.unit
    def test_result_is_never_weekend(self):
        # 抽样连续 14 天，结果无一落在周六/周日
        for offset in range(14):
            d = date(2024, 6, 1) + __import__("datetime").timedelta(days=offset)
            result = _latest_possible_trading_day(d)
            assert result.weekday() < 5
            assert result <= d

    @pytest.mark.unit
    def test_returns_date_type(self):
        assert isinstance(_latest_possible_trading_day(date(2024, 1, 6)), date)
