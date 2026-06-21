"""多因子动量方向修复 + 行业中性化单测。

核心验证（审计报告 P1-C2）：
- 标准截面动量 12-1 跳过最近 1 个月的短期反转区（A 股 1~3 周是反转，旧 20 日正权方向反）；
- 近期暴涨被 skip 区排除（不被当作动量）；
- 多因子 screen 端到端可用，行业中性化不报错并改变聚集型因子的分布。
"""
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from src.strategy.multifactor import MultiFactorStrategy, compute_cross_sectional_momentum


def _rising_prices(n: int, start: float = 10.0, end: float = 30.0) -> pd.Series:
    """n 个交易日线性上涨的收盘价。"""
    return pd.Series(np.linspace(start, end, n))


class TestCrossSectionalMomentum:
    @pytest.mark.unit
    def test_steady_uptrend_positive(self):
        # 300 日稳定上涨 → 12-1 动量为正
        close = _rising_prices(300)
        m = compute_cross_sectional_momentum(close, long_window=252, skip=21)
        assert m > 0

    @pytest.mark.unit
    def test_recent_spike_excluded_by_skip(self):
        # 【关键】前 279 日横盘(=10)，最后 21 日暴涨到 20。
        # skip=21 排除最近 21 日 → end=P[-22]=10（暴涨前），动量 ≈ 0（不把短期暴涨当动量）。
        # 旧 20 日口径会把这次暴涨算成 +100% 动量——正是 A 股反转效应要规避的。
        close = pd.Series([10.0] * 279 + list(np.linspace(10.0, 20.0, 21)))
        m = compute_cross_sectional_momentum(close, long_window=252, skip=21)
        assert abs(m) < 1.0  # 暴涨被 skip 区排除，动量≈0

    @pytest.mark.unit
    def test_long_term_decline_negative(self):
        # 300 日持续下跌 → 动量为负
        close = _rising_prices(300, start=30.0, end=10.0)
        m = compute_cross_sectional_momentum(close, long_window=252, skip=21)
        assert m < 0

    @pytest.mark.unit
    def test_insufficient_history_fallback(self):
        # 历史不足 252+21+1 → 回退到「跳过 skip、用全部可用历史」
        close = _rising_prices(60, start=10.0, end=20.0)  # 60 日，不足全窗
        m = compute_cross_sectional_momentum(close, long_window=252, skip=21)
        # 回退：end=P[-22] ≈ 18.x，start=P[0]=10 → 正
        assert m > 0

    @pytest.mark.unit
    def test_too_short_returns_nan(self):
        close = pd.Series([10.0] * 15)  # 不足 skip+2
        assert np.isnan(compute_cross_sectional_momentum(close, skip=21))


# ── 多因子 screen 端到端 ─────────────────────────────────────────
def _mock_dm(bars_len=300):
    dm = MagicMock()
    dm.get_latest_financial_batch.return_value = pd.DataFrame([
        {"code": "a", "pe_ttm": 8.0, "profit_yoy": 30.0, "roe": 15.0},
        {"code": "b", "pe_ttm": 50.0, "profit_yoy": 5.0, "roe": 8.0},
    ])

    def _bars(code):
        # a 持续上涨（高动量），b 持续下跌（低动量）
        if code == "a":
            close = _rising_prices(bars_len, 10.0, 30.0)
        else:
            close = _rising_prices(bars_len, 30.0, 10.0)
        return pd.DataFrame({"trade_date": pd.date_range("2024-01-01", periods=bars_len, freq="D"),
                             "close": close})
    dm.get_daily_bars.side_effect = _bars
    return dm


class TestMultiFactorScreen:
    @pytest.mark.unit
    def test_ranks_by_combined_factors(self):
        dm = _mock_dm()
        pool = pd.DataFrame({"code": ["a", "b"], "name": ["A", "B"]})
        results = MultiFactorStrategy().screen(pool, dm)
        assert len(results) == 2
        # a：高动量+低PE+高成长+高ROE，应排第一
        assert results[0].code == "a"

    @pytest.mark.unit
    def test_neutralization_runs_with_industry(self):
        # 带行业列 + neutralize_industry=True → 不报错，仍产出结果
        dm = _mock_dm()
        pool = pd.DataFrame({"code": ["a", "b"], "name": ["A", "B"],
                             "industry": ["X", "Y"]})
        results = MultiFactorStrategy(neutralize_industry=True).screen(pool, dm)
        assert len(results) == 2

    @pytest.mark.unit
    def test_neutralization_no_industry_column_skipped(self):
        # 无 industry 列 → 中性化静默跳过（不报错）
        dm = _mock_dm()
        pool = pd.DataFrame({"code": ["a", "b"], "name": ["A", "B"]})
        results = MultiFactorStrategy(neutralize_industry=True).screen(pool, dm)
        assert len(results) == 2

    @pytest.mark.unit
    def test_legacy_momentum_days_override(self):
        # 显式传 momentum_days → 走旧短期口径（向后兼容）
        dm = _mock_dm()
        pool = pd.DataFrame({"code": ["a", "b"], "name": ["A", "B"]})
        results = MultiFactorStrategy(momentum_days=20).screen(pool, dm)
        assert len(results) == 2
