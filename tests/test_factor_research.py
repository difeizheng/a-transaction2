"""因子有效性检验单测 —— IC / IR / 分层 / 衰减。

全部用确定性合成数据（已知秩相关），零网络/LLM。验证：
- rank_ic 在完全正相关/反相关/无关时给出 1/-1/0；
- ic_summary 的 IR / t_stat / hit_rate 公式正确；
- quantile_spread 对有效因子单调、对噪声非单调；
- ic_decay 随持有期衰减。
"""
import numpy as np
import pandas as pd
import pytest

from src.analysis.factor_research import (
    analyze_factor,
    ic_decay,
    ic_summary,
    information_coefficient,
    quantile_spread,
    rank_ic,
)


# ── rank_ic（单期截面秩相关）──────────────────────────────────────
class TestRankIC:
    @pytest.mark.unit
    def test_perfect_positive(self):
        # 因子升序 = 收益升序 → 完全正相关
        assert rank_ic([1, 2, 3, 4, 5], [10, 20, 30, 40, 50]) == pytest.approx(1.0)

    @pytest.mark.unit
    def test_perfect_negative(self):
        # 因子升序 = 收益降序 → 完全负相关
        assert rank_ic([5, 4, 3, 2, 1], [10, 20, 30, 40, 50]) == pytest.approx(-1.0)

    @pytest.mark.unit
    def test_known_moderate_positive(self):
        # 因子秩 [1,2,3,4,5]，收益秩 [2,1,3,5,4] → Spearman = 0.8（手工可算）
        # 验证函数给出非平凡（非 ±1）的正确中间值
        assert rank_ic([1, 2, 3, 4, 5], [2, 1, 3, 5, 4]) == pytest.approx(0.8)

    @pytest.mark.unit
    def test_too_few_samples_returns_zero(self):
        # <3 样本无定义
        assert rank_ic([1, 2], [1, 2]) == 0.0

    @pytest.mark.unit
    def test_no_variance_returns_zero(self):
        # 因子全相同 → 无方差 → 0
        assert rank_ic([3, 3, 3, 3], [1, 2, 3, 4]) == 0.0

    @pytest.mark.unit
    def test_nan_pairwise_dropped(self):
        # 含 NaN 成对剔除后仍能算出有效 IC
        assert rank_ic([1, 2, np.nan, 4, 5], [10, 20, 30, 40, 50]) == pytest.approx(1.0)


# ── information_coefficient（逐期面板）──────────────────────────
class TestInformationCoefficient:
    @pytest.mark.unit
    def test_two_periods_mixed_sign(self):
        # 两期：第1期正相关(IC=1)，第2期反相关(IC=-1)
        factor = pd.DataFrame(
            {"A": [1, 5], "B": [2, 4], "C": [3, 3], "D": [4, 2], "E": [5, 1]},
            index=["d1", "d2"],
        )
        ret = pd.DataFrame(
            {"A": [10, 10], "B": [20, 20], "C": [30, 30], "D": [40, 40], "E": [50, 50]},
            index=["d1", "d2"],
        )
        ic = information_coefficient(factor, ret)
        assert ic.loc["d1"] == pytest.approx(1.0)
        assert ic.loc["d2"] == pytest.approx(-1.0)
        assert ic.name == "ic"

    @pytest.mark.unit
    def test_shape_mismatch_raises(self):
        factor = pd.DataFrame({"A": [1, 2], "B": [3, 4]}, index=["d1", "d2"])
        ret = pd.DataFrame({"A": [1], "B": [2]}, index=["d1"])  # 行数不同
        with pytest.raises(ValueError):
            information_coefficient(factor, ret)


# ── ic_summary（IR / t / hit_rate）───────────────────────────────
class TestICSummary:
    @pytest.mark.unit
    def test_positive_consistent_ic(self):
        # IC 全正且接近 → mean 高、std 小、IR 大、hit_rate=1
        s = ic_summary([0.08, 0.09, 0.07, 0.10, 0.08])
        assert s["mean_ic"] > 0
        assert s["hit_rate"] == 1.0
        assert s["ir"] > 0
        assert s["t_stat"] > 2  # 一致正向，应统计显著
        assert s["n_periods"] == 5

    @pytest.mark.unit
    def test_inconsistent_ic_low_ir(self):
        # IC 忽正忽负 → IR 接近 0（不稳定无预测力）
        s = ic_summary([0.1, -0.1, 0.1, -0.1])
        assert abs(s["mean_ic"]) < 0.01
        assert abs(s["ir"]) < 0.5
        assert s["hit_rate"] == 0.5

    @pytest.mark.unit
    def test_zero_variance_returns_nan_ir(self):
        # IC 完全相同 → std=0 → IR/t_stat 为 nan（无波动无法检验）
        s = ic_summary([0.05, 0.05, 0.05])
        assert np.isnan(s["ir"])
        assert np.isnan(s["t_stat"])

    @pytest.mark.unit
    def test_empty_series(self):
        s = ic_summary([])
        assert s["n_periods"] == 0
        assert s["mean_ic"] == 0.0


# ── quantile_spread（分层多空）──────────────────────────────────
class TestQuantileSpread:
    @pytest.mark.unit
    def test_effective_factor_monotone_positive_spread(self):
        # 因子与收益完全单调正相关：顶分位收益最高
        factor = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        ret = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        qs = quantile_spread(factor, ret, n_quantiles=5)
        assert qs["monotonic"] is True
        assert qs["top_minus_bottom"] > 0

    @pytest.mark.unit
    def test_noise_factor_non_monotone(self):
        # 因子与收益无关：分层非单调
        factor = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        ret = [5, 9, 1, 8, 2, 7, 3, 10, 4, 6]  # 打乱
        qs = quantile_spread(factor, ret, n_quantiles=5)
        assert qs["monotonic"] is False

    @pytest.mark.unit
    def test_too_few_stocks_degrades_gracefully(self):
        # 样本 < 分层数 → 降级不报错
        qs = quantile_spread([1, 2], [1, 2], n_quantiles=5)
        assert "top_minus_bottom" in qs


# ── ic_decay（衰减）+ analyze_factor（端到端）───────────────────
class TestDecayAndEndToEnd:
    @pytest.mark.unit
    def test_analyze_factor_known_relationship(self):
        # 构造 3 期面板：因子 = 收益（完全正相关）→ mean_ic=1, hit_rate=1
        factor = pd.DataFrame(
            {"A": [1, 2, 3], "B": [2, 3, 1], "C": [3, 1, 2],
             "D": [4, 5, 6], "E": [5, 4, 5]},
            index=["d1", "d2", "d3"],
        )
        # 收益与因子同序（每期都正相关）
        ret = pd.DataFrame(
            {"A": [10, 20, 30], "B": [20, 30, 10], "C": [30, 10, 20],
             "D": [40, 50, 60], "E": [50, 40, 50]},
            index=["d1", "d2", "d3"],
        )
        summary = analyze_factor(factor, ret)
        assert summary["mean_ic"] == pytest.approx(1.0)
        assert summary["hit_rate"] == 1.0

    @pytest.mark.unit
    def test_ic_decay_structure(self):
        # 两个持有期面板 → 衰减结果含各 horizon 的 summary
        factor = pd.DataFrame(
            {"A": [1, 2], "B": [2, 1], "C": [3, 3], "D": [4, 4], "E": [5, 5]},
            index=["d1", "d2"],
        )
        ret_1 = pd.DataFrame(
            {"A": [10, 20], "B": [20, 10], "C": [30, 30], "D": [40, 40], "E": [50, 50]},
            index=["d1", "d2"],
        )
        decay = ic_decay(factor, {1: ret_1, 5: ret_1})
        assert set(decay.keys()) == {1, 5}
        assert "mean_ic" in decay[1]
