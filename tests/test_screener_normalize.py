"""策略评分归一化单测 —— 跨策略量纲一致性。

各策略原始 score 量纲不同（均线=百分比~1、MACD=价格~元、低估值=0~100、
多因子=Z-score）。直接跨策略平均会让大数值策略吞没小数值策略。
_percentile_rank 在每个策略结果集内归一到 [0,100]，消除量纲差异。
"""
import pandas as pd
import pytest

from src.strategy.base import ScreenResult
from src.strategy.screener import Screener


def _sr(code: str, score: float) -> ScreenResult:
    return ScreenResult(code=code, name=f"n_{code}", score=score)


class _FakeStrategy:
    """最小策略桩：忽略 pool/dm，返回预设结果，供 run_multi_strategy 调用。"""

    def __init__(self, name: str, results):
        self.name = name
        self._results = list(results)

    def screen(self, pool, dm, progress_callback=None):
        return list(self._results)


class _FakeDM:
    """只实现 get_stock_list，供 Screener.get_stock_pool 调用。"""

    def __init__(self, pool_df: pd.DataFrame):
        self._pool = pool_df

    def get_stock_list(self) -> pd.DataFrame:
        return self._pool


def _screener_with_pool(codes=("c1", "c2", "c3")) -> Screener:
    pool = pd.DataFrame({"code": list(codes), "name": [f"n_{c}" for c in codes]})
    return Screener(_FakeDM(pool))


# ── _percentile_rank 单元 ───────────────────────────────────────
class TestPercentileRank:
    @pytest.mark.unit
    def test_empty_returns_empty(self):
        assert Screener._percentile_rank([]) == []

    @pytest.mark.unit
    def test_single_is_100(self):
        assert Screener._percentile_rank([_sr("a", 42.0)]) == [100.0]

    @pytest.mark.unit
    def test_monotonic_increasing(self):
        results = [_sr("a", 1.0), _sr("b", 2.0), _sr("c", 3.0), _sr("d", 4.0)]
        ranks = Screener._percentile_rank(results)
        assert ranks == [25.0, 50.0, 75.0, 100.0]

    @pytest.mark.unit
    def test_alignment_with_input(self):
        # 乱序输入：输出顺序与输入逐位对齐，最高分者得 100
        results = [_sr("a", 9.0), _sr("b", 1.0), _sr("c", 5.0)]
        ranks = Screener._percentile_rank(results)
        assert len(ranks) == len(results)
        assert ranks[0] == 100.0  # a 最高
        assert ranks[1] < ranks[2] < ranks[0]  # b<c<a

    @pytest.mark.unit
    def test_all_in_zero_to_hundred(self):
        results = [_sr(f"c{i}", float(i) * 13.7) for i in range(10)]
        ranks = Screener._percentile_rank(results)
        assert all(0.0 < r <= 100.0 for r in ranks)


# ── run_multi_strategy 跨策略归一 ───────────────────────────────
class TestMultiStrategyNormalization:
    @pytest.mark.unit
    def test_huge_scale_does_not_dominate(self):
        # A 用极小量纲(百分比)，B 用极大量纲(价格元)，但两者排序一致
        strat_a = _FakeStrategy("small", [_sr("c1", 0.9), _sr("c2", 0.5), _sr("c3", 0.1)])
        strat_b = _FakeStrategy("huge", [_sr("c1", 9_000_000.0), _sr("c2", 5_000_000.0), _sr("c3", 1_000_000.0)])
        screener = _screener_with_pool()

        merged = screener.run_multi_strategy([strat_a, strat_b], top_n=10)

        # 1) 排序正确：c1 在两策略都最高 → 综合第一
        assert [m.code for m in merged] == ["c1", "c2", "c3"]
        # 2) 归一化生效：所有得分落在 [0,100]，而非被 B 拉到百万级
        assert all(m.score <= 100.01 for m in merged)
        # 3) c1 综合满分
        assert merged[0].score == pytest.approx(100.0)
        assert merged[1].score == pytest.approx(66.67, abs=0.05)

    @pytest.mark.unit
    def test_intersect_keeps_only_common(self):
        strat_a = _FakeStrategy("a", [_sr("c1", 0.9), _sr("c2", 0.5), _sr("c3", 0.1)])
        strat_b = _FakeStrategy("b", [_sr("c1", 100.0), _sr("c2", 50.0)])  # 不含 c3
        screener = _screener_with_pool()

        merged = screener.run_multi_strategy([strat_a, strat_b], mode="intersect")

        codes = {m.code for m in merged}
        assert codes == {"c1", "c2"}
        assert "c3" not in codes

    @pytest.mark.unit
    def test_union_keeps_all(self):
        strat_a = _FakeStrategy("a", [_sr("c1", 0.9), _sr("c2", 0.5)])
        strat_b = _FakeStrategy("b", [_sr("c2", 1.0), _sr("c3", 0.3)])
        screener = _screener_with_pool()

        merged = screener.run_multi_strategy([strat_a, strat_b], mode="union")
        assert {m.code for m in merged} == {"c1", "c2", "c3"}
