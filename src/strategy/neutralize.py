"""因子中性化 —— 行业 / 风险暴露正交化（纯函数）。

价值/成长/动量等因子有强**行业聚集性**：银行股 PE 天然低、券商股天然高波动。
直接用原始因子打分 = 在选行业，不是在选个股。中性化剥离「行业系统性暴露」，
只保留**同行业内**的相对 alpha——这是多因子模型的标配预处理（审计报告 P1-C）。

两种口径：
- ``neutralize_by_group``：减行业均值（轻量，最常用）；
- ``neutralize_by_regression``：对风险暴露矩阵（行业哑变量 + 市值/beta 等）做 OLS 取残差
  （更彻底，同时剥离多个风险因子）。

纯函数、零外部依赖（OLS 用 numpy 最小二乘，不引入 statsmodels）。
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


def winsorize(series: pd.Series, lower: float = 0.01, upper: float = 0.99) -> pd.Series:
    """截尾到 [lower, upper] 分位数，抑制极端值（如 +5000% 利润增速）主导打分。

    全 NaN 或无方差时原样返回。返回新 Series，不修改入参。
    """
    s = series.astype(float)
    non_na = s.dropna()
    if non_na.empty:
        return s
    lo, hi = non_na.quantile(lower), non_na.quantile(upper)
    if pd.isna(lo) or pd.isna(hi) or lo == hi:
        return s
    return s.clip(lower=lo, upper=hi)


def neutralize_by_group(
    factor: pd.Series,
    group: pd.Series,
    winsorize_first: bool = True,
    winsorize_bounds: tuple = (0.01, 0.99),
) -> pd.Series:
    """行业中性化：因子值减去其所属行业的均值（同行业内 demean）。

    Args:
        factor: index=股票、values=因子值的 Series。
        group: index=股票、values=行业标签的 Series（与 factor 对齐）。
        winsorize_first: 中性化前先对**全市场**因子做 winsorize 截尾（默认 True，
            防极端值污染行业均值）。
        winsorize_bounds: winsorize 的分位边界。

    Returns:
        同 index 的中性化因子 Series。中性化后**每个行业的均值 ≈ 0**。
        缺失行业标签的样本视为独立一类（自成一行业，demean 后为 0）。
    """
    if len(factor) == 0:
        return factor.astype(float)
    aligned = pd.concat([factor.astype(float), group.astype("category")], axis=1)
    aligned.columns = ["factor", "group"]
    f = aligned["factor"]
    if winsorize_first:
        f = winsorize(f, *winsorize_bounds)
    aligned["factor"] = f
    # transform("mean"): 每行替换为该行业的因子均值；逐行相减即 demean
    industry_mean = aligned.groupby("group", observed=True)["factor"].transform("mean")
    return (aligned["factor"] - industry_mean)


def neutralize_by_regression(
    factor: pd.Series,
    exposures: pd.DataFrame,
    winsorize_first: bool = True,
    winsorize_bounds: tuple = (0.01, 0.99),
) -> pd.Series:
    """多元回归中性化：因子对风险暴露矩阵做 OLS，取残差。

    比分组 demean 更彻底——可同时剥离行业（哑变量列）+ 市值 + beta 等连续风险因子。

    Args:
        factor: index=股票、values=因子值。
        exposures: index 与 factor 对齐、每列为一个风险暴露（如市值、各行业 0/1 哑变量）。
        winsorize_first: 回归前先对连续因子做 winsorize（仅对非 0/1 的连续列生效）。

    Returns:
        残差 Series（因子中无法被风险暴露解释的部分 = 纯 alpha）。完全可解释时残差 ≈ 0。
    """
    # 按 index 对齐后逐对 dropna（不依赖列名，避免与 exposures 内名为 "y" 的列冲突）
    aligned_idx = factor.index.intersection(exposures.index)
    y_raw = factor.loc[aligned_idx].astype(float)
    X_df = exposures.loc[aligned_idx].astype(float)
    mask = y_raw.notna() & X_df.notna().all(axis=1)
    y_raw = y_raw[mask]
    X_df = X_df[mask]
    n, k = X_df.shape
    if n == 0:
        return pd.Series(dtype=float, index=factor.index)
    y = y_raw.values
    X = X_df.values
    if n <= k:
        # 样本不足做回归 → 退化为全市场 demean（仍保证可用）
        return y_raw - y_raw.mean()

    if winsorize_first:
        y = winsorize(pd.Series(y, index=y_raw.index)).values

    # OLS: beta = (X'X)^-1 X'y；加截距项（全 1 列）
    X_with_const = np.column_stack([np.ones(n), X])
    beta, *_ = np.linalg.lstsq(X_with_const, y, rcond=None)
    residual = y - X_with_const @ beta
    return pd.Series(residual, index=y_raw.index)


def industry_mean_exposure(factor: pd.Series, group: pd.Series) -> pd.Series:
    """诊断用：返回每个行业的因子均值暴露（中性化后应接近 0）。"""
    df = pd.concat([factor.astype(float).rename("factor"),
                    group.astype("category").rename("group")], axis=1).dropna()
    return df.groupby("group", observed=True)["factor"].mean().sort_values(ascending=False)
