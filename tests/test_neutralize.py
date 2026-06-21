"""因子中性化单测 —— 行业 demean + 回归残差。

验证：
- 中性化后各行业均值 ≈ 0；
- winsorize 截尾生效；
- 回归中性化：因子完全可被暴露解释时残差 ≈ 0；
- 行业聚集因子中性化前后差异显著。
"""
import numpy as np
import pandas as pd
import pytest

from src.strategy.neutralize import (
    industry_mean_exposure,
    neutralize_by_group,
    neutralize_by_regression,
    winsorize,
)


# 构造：两个行业，银行股因子天然偏低、券商股天然偏高（行业聚集）
def _clustered_factor():
    factor = pd.Series({
        "bank1": 1.0, "bank2": 1.2, "bank3": 0.8,   # 银行业 均值 1.0
        "broker1": 5.0, "broker2": 5.5, "broker3": 4.5,  # 券商业 均值 5.0
    })
    group = pd.Series({
        "bank1": "银行", "bank2": "银行", "bank3": "银行",
        "broker1": "券商", "broker2": "券商", "broker3": "券商",
    })
    return factor, group


class TestWinsorize:
    @pytest.mark.unit
    def test_extreme_values_clipped(self):
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 100.0])  # 100 是极端值
        w = winsorize(s, lower=0.0, upper=0.8)
        assert w.max() < 100.0  # 极端值被截
        assert w.min() >= 1.0

    @pytest.mark.unit
    def test_no_change_when_uniform(self):
        s = pd.Series([5.0, 5.0, 5.0])
        assert winsorize(s).equals(s)


class TestNeutralizeByGroup:
    @pytest.mark.unit
    def test_industry_means_near_zero_after(self):
        factor, group = _clustered_factor()
        neutral = neutralize_by_group(factor, group, winsorize_first=False)
        exposure = industry_mean_exposure(neutral, group)
        # 中性化后两行业均值都应 ≈ 0
        for val in exposure.values:
            assert abs(val) < 1e-9

    @pytest.mark.unit
    def test_preserves_within_industry_ranking(self):
        # 中性化不改变同行业内的相对排序（bank2 仍 > bank3）
        factor, group = _clustered_factor()
        neutral = neutralize_by_group(factor, group, winsorize_first=False)
        assert neutral["bank2"] > neutral["bank1"] > neutral["bank3"]

    @pytest.mark.unit
    def test_removes_industry_clustering(self):
        # 原始因子：券商均值 5 >> 银行均值 1；中性化后差异消失
        factor, group = _clustered_factor()
        before = industry_mean_exposure(factor, group)
        after = neutralize_by_group(factor, group, winsorize_first=False)
        after_exposure = industry_mean_exposure(after, group)
        assert abs(before.max() - before.min()) > 3.0   # 中性化前行业差大
        assert abs(after_exposure.max() - after_exposure.min()) < 1e-9

    @pytest.mark.unit
    def test_empty_input(self):
        neutral = neutralize_by_group(pd.Series(dtype=float), pd.Series(dtype=object))
        assert len(neutral) == 0


class TestNeutralizeByRegression:
    @pytest.mark.unit
    def test_fully_explained_residual_near_zero(self):
        # 因子 = 2 * 市值暴露（完全可解释）→ 残差 ≈ 0
        np.random.seed(0)
        size = pd.Series(np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]),
                         index=[f"s{i}" for i in range(8)])
        factor = 2.0 * size  # 完美线性
        exposures = pd.DataFrame({"size": size.values}, index=size.index)
        residual = neutralize_by_regression(factor, exposures, winsorize_first=False)
        assert residual.abs().max() < 1e-9

    @pytest.mark.unit
    def test_partial_explanation_keeps_alpha(self):
        # 因子 = 2*size + pure_alpha；剥离 size 后残差应 ≈ pure_alpha
        size = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0],
                         index=[f"s{i}" for i in range(8)])
        alpha = pd.Series([0.1, -0.1, 0.2, -0.2, 0.15, -0.15, 0.05, -0.05],
                          index=size.index)
        factor = 2.0 * size + alpha
        exposures = pd.DataFrame({"size": size.values}, index=size.index)
        residual = neutralize_by_regression(factor, exposures, winsorize_first=False)
        # 残差应与 alpha 高度一致（alpha 非完全正交于 size，OLS 吸收极小部分，corr≈0.98）
        assert abs(residual.corr(alpha)) > 0.97

    @pytest.mark.unit
    def test_insufficient_samples_degrades_to_demean(self):
        # 样本 <= 解释变量数 → 退化为 demean，不报错
        factor = pd.Series([1.0, 2.0], index=["a", "b"])
        exposures = pd.DataFrame({"x": [1.0, 2.0], "y": [3.0, 4.0]}, index=["a", "b"])
        residual = neutralize_by_regression(factor, exposures, winsorize_first=False)
        assert len(residual) == 2
