"""小市值策略 + 策略失效标记（deprecation）单测。

验证：
- SmallCapStrategy.screen 按市值升序选最小，分数「越小越高」；
- 次新股过滤（K线不足排除）；
- evaluate_stock 单股 selected iff 市值<=阈值 且 K线充足；
- screener 默认排除 is_deprecated 策略（kdj_oversold / boll_breakout）。
"""
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.strategy.screener import DEFAULT_STRATEGY_KEYS, STRATEGY_REGISTRY, Screener
from src.strategy.smallcap import SmallCapStrategy


def _mock_dm(fin_df: pd.DataFrame, bar_len: int = 100):
    """Mock DataManager：批量财务返回给定 df，每只 K 线返回 bar_len 条。"""
    dm = MagicMock()
    dm.get_latest_financial_batch.return_value = fin_df
    dm.get_financial_data.side_effect = lambda code: fin_df[fin_df["code"] == code].reset_index(drop=True)

    def _bars(code):
        dates = pd.date_range("2024-01-01", periods=bar_len, freq="D")
        return pd.DataFrame({"trade_date": dates, "close": [10.0] * bar_len,
                             "open": [10.0] * bar_len, "high": [10.0] * bar_len,
                             "low": [10.0] * bar_len, "volume": [1000] * bar_len})
    dm.get_daily_bars.side_effect = _bars
    return dm


def _pool():
    return pd.DataFrame({"code": ["a", "b", "c", "d"], "name": ["A", "B", "C", "D"]})


def _fin(mv_a=3e6, mv_b=8e6, mv_c=1e6, mv_d=2e7):
    """total_mv 单位万元。a=30亿 b=80亿 c=10亿 d=200亿"""
    return pd.DataFrame([
        {"code": "a", "total_mv": mv_a}, {"code": "b", "total_mv": mv_b},
        {"code": "c", "total_mv": mv_c}, {"code": "d", "total_mv": mv_d},
    ])


class TestSmallCapScreen:
    @pytest.mark.unit
    def test_selects_smallest_first(self):
        dm = _mock_dm(_fin())
        s = SmallCapStrategy(top_n=3)
        results = s.screen(_pool(), dm)
        assert len(results) == 3
        # 升序：c(10亿) < a(30亿) < b(80亿)
        assert results[0].code == "c"
        assert results[1].code == "a"
        assert results[2].code == "b"

    @pytest.mark.unit
    def test_score_smaller_is_higher(self):
        dm = _mock_dm(_fin())
        s = SmallCapStrategy(top_n=3)
        results = s.screen(_pool(), dm)
        # 最小市值分数最高
        scores = [r.score for r in results]
        assert scores[0] > scores[-1]
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.unit
    def test_excludes_new_listings(self):
        # b 是次新股（K线不足 min_bars）→ 应被排除
        dm = _mock_dm(_fin())
        s = SmallCapStrategy(top_n=10, min_bars=50)
        # 让 b 的 K 线不足
        def _bars(code):
            n = 10 if code == "b" else 100
            dates = pd.date_range("2024-01-01", periods=n, freq="D")
            return pd.DataFrame({"trade_date": dates, "close": [10.0] * n})
        dm.get_daily_bars.side_effect = _bars
        results = s.screen(_pool(), dm)
        codes = [r.code for r in results]
        assert "b" not in codes
        assert "c" in codes  # 正常小市值保留

    @pytest.mark.unit
    def test_empty_financial_returns_empty(self):
        dm = _mock_dm(pd.DataFrame())
        assert SmallCapStrategy().screen(_pool(), dm) == []


class TestSmallCapEvaluate:
    @pytest.mark.unit
    def test_small_cap_selected(self):
        dm = _mock_dm(pd.DataFrame([{"code": "a", "total_mv": 2e6}]))  # 20亿
        s = SmallCapStrategy(max_mv_wan=5e6)  # 阈值 50亿
        ev = s.evaluate_stock("a", "A", dm)
        assert ev.selected is True
        assert ev.score > 0

    @pytest.mark.unit
    def test_large_cap_not_selected(self):
        dm = _mock_dm(pd.DataFrame([{"code": "a", "total_mv": 2e7}]))  # 200亿
        s = SmallCapStrategy(max_mv_wan=5e6)  # 阈值 50亿
        ev = s.evaluate_stock("a", "A", dm)
        assert ev.selected is False
        assert ev.score == 0

    @pytest.mark.unit
    def test_supports_evaluate(self):
        # AutoTrader 需 supports_evaluate()=True
        assert SmallCapStrategy().supports_evaluate() is True


class TestDeprecation:
    @pytest.mark.unit
    def test_active_only_excludes_deprecated(self):
        active = Screener.list_strategies(active_only=True)
        assert "kdj_oversold" not in active
        assert "boll_breakout" not in active
        # 活跃策略仍在
        assert "small_cap" in active
        assert "ma_cross" in active

    @pytest.mark.unit
    def test_all_includes_deprecated(self):
        all_strats = Screener.list_strategies(active_only=False)
        assert "kdj_oversold" in all_strats
        assert "boll_breakout" in all_strats

    @pytest.mark.unit
    def test_default_keys_exclude_deprecated(self):
        # 默认策略集不含失效策略
        assert "kdj_oversold" not in DEFAULT_STRATEGY_KEYS
        assert "boll_breakout" not in DEFAULT_STRATEGY_KEYS
        assert "small_cap" in DEFAULT_STRATEGY_KEYS
        # 默认集全部在注册表中
        for k in DEFAULT_STRATEGY_KEYS:
            assert k in STRATEGY_REGISTRY

    @pytest.mark.unit
    def test_small_cap_registered(self):
        assert STRATEGY_REGISTRY["small_cap"] is SmallCapStrategy
