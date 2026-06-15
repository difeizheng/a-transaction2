"""技术指标单测 —— _calc_indicators 与 MACross 策略选股逻辑。

_calc_indicators 是所有技术策略的基础（SMA/MACD/KDJ/Bollinger），
它算错会直接导致选股错误。这里验证：指标列生成、SMA 数值精确性、
多头排列判定、数据不足早退。
"""
import numpy as np
import pandas as pd
import pytest

from src.strategy.technical import (
    MACrossStrategy,
    MACDGoldenCrossStrategy,
    KDJOversoldStrategy,
    BollingerBreakoutStrategy,
    _calc_indicators,
)


# ── 辅助 ────────────────────────────────────────────────────────
def _make_bars(closes, start="2024-01-01"):
    """由收盘价序列构造完整K线DataFrame。"""
    n = len(closes)
    dates = pd.date_range(start, periods=n, freq="D")
    closes = list(closes)
    return pd.DataFrame({
        "trade_date": dates,
        "open": closes,
        "high": [c + 0.3 for c in closes],
        "low": [c - 0.3 for c in closes],
        "close": closes,
        "volume": [1000] * n,
        "amount": [10000] * n,
    })


class _FakeDM:
    """返回固定K线的轻量 DM。"""

    def __init__(self, bars_df):
        self._bars = bars_df

    def get_daily_bars(self, code, **kwargs):
        return self._bars


# ── _calc_indicators 基础正确性 ─────────────────────────────────
class TestCalcIndicators:
    @pytest.mark.unit
    def test_generates_all_indicator_columns(self):
        df = _make_bars([10 + i * 0.5 for i in range(70)])
        result = _calc_indicators(df)
        # 均线列
        for col in ("ma5", "ma10", "ma20", "ma60"):
            assert col in result.columns, f"缺少 {col}"
        # MACD 列（pandas_ta 命名：MACD_12_26_9 / MACDh_ / MACDs_）
        assert any(c.startswith("MACD_") for c in result.columns)
        assert any(c.startswith("MACDs_") for c in result.columns)
        # KDJ 列
        assert any(c.startswith("STOCHk_") for c in result.columns)
        assert any(c.startswith("STOCHd_") for c in result.columns)
        # 布林带列
        assert any(c.startswith("BBL_") for c in result.columns)
        assert any(c.startswith("BBM_") for c in result.columns)
        assert any(c.startswith("BBU_") for c in result.columns)

    @pytest.mark.unit
    def test_sma_value_constant_series(self):
        # 常数序列 close=10 → 所有 SMA = 10.0（最干净的数值验证）
        df = _make_bars([10.0] * 70)
        result = _calc_indicators(df)
        assert result["ma5"].iloc[-1] == pytest.approx(10.0)
        assert result["ma10"].iloc[-1] == pytest.approx(10.0)
        assert result["ma20"].iloc[-1] == pytest.approx(10.0)
        assert result["ma60"].iloc[-1] == pytest.approx(10.0)

    @pytest.mark.unit
    def test_sma_value_linear_series(self):
        # 线性递增 close = 10 + 0.5*i，ma5(末) = mean(后5) = 43.5
        df = _make_bars([10 + 0.5 * i for i in range(70)])
        result = _calc_indicators(df)
        expected_ma5 = np.mean([10 + 0.5 * i for i in range(65, 70)])
        assert result["ma5"].iloc[-1] == pytest.approx(expected_ma5)

    @pytest.mark.unit
    def test_ma60_nan_when_insufficient(self):
        # 不足 60 条 → ma60 为 NaN
        df = _make_bars([10.0] * 30)
        result = _calc_indicators(df)
        assert pd.isna(result["ma60"].iloc[-1])
        assert result["ma5"].iloc[-1] == pytest.approx(10.0)  # ma5 仍可算

    @pytest.mark.unit
    def test_preserves_row_count_and_order(self):
        closes = [10 + 0.5 * i for i in range(70)]
        df = _make_bars(closes)
        result = _calc_indicators(df)
        assert len(result) == 70
        # 按日期升序
        assert result["trade_date"].is_monotonic_increasing


# ── MACrossStrategy 选股逻辑 ───────────────────────────────────
class TestMACrossStrategy:
    @pytest.mark.unit
    def test_uptrend_selects(self):
        # 线性递增序列 → 多头排列（MA5>MA10>MA20>MA60）+ close>MA20 → 入选
        bars = _make_bars([10 + 0.5 * i for i in range(70)])
        dm = _FakeDM(bars)
        ev = MACrossStrategy().evaluate_stock("000001", "X", dm)
        assert ev.selected is True
        assert ev.score > 0
        assert ev.indicators["MA5"] > ev.indicators["MA60"]

    @pytest.mark.unit
    def test_downtrend_not_selected(self):
        # 线性递减序列 → 空头排列，不入选
        bars = _make_bars([44 - 0.5 * i for i in range(70)])
        dm = _FakeDM(bars)
        ev = MACrossStrategy().evaluate_stock("000002", "Y", dm)
        assert ev.selected is False

    @pytest.mark.unit
    def test_insufficient_bars_not_selected(self):
        # 30 条 < min_bars(60) → 不入选，reason 标注不足
        bars = _make_bars([10.0] * 30)
        dm = _FakeDM(bars)
        ev = MACrossStrategy().evaluate_stock("000003", "Z", dm)
        assert ev.selected is False
        assert "不足" in ev.reason


# ── 其他策略：数据不足边界保护 ─────────────────────────────────
class TestStrategyEdgeCases:
    @pytest.mark.unit
    def test_macd_insufficient_bars(self):
        bars = _make_bars([10.0] * 30)  # < 60
        dm = _FakeDM(bars)
        ev = MACDGoldenCrossStrategy().evaluate_stock("c", "n", dm)
        assert ev.selected is False

    @pytest.mark.unit
    def test_kdj_insufficient_bars(self):
        bars = _make_bars([10.0] * 20)  # < 30
        dm = _FakeDM(bars)
        ev = KDJOversoldStrategy().evaluate_stock("c", "n", dm)
        assert ev.selected is False

    @pytest.mark.unit
    def test_bollinger_insufficient_bars(self):
        bars = _make_bars([10.0] * 20)  # < 30
        dm = _FakeDM(bars)
        ev = BollingerBreakoutStrategy().evaluate_stock("c", "n", dm)
        assert ev.selected is False

    @pytest.mark.unit
    def test_all_strategies_have_evaluate(self):
        # 回归：四个策略都应实现 evaluate_stock（supports_evaluate 返回 True）
        for cls in (MACrossStrategy, MACDGoldenCrossStrategy,
                    KDJOversoldStrategy, BollingerBreakoutStrategy):
            assert cls().supports_evaluate() is True, f"{cls.__name__} 未实现 evaluate_stock"
