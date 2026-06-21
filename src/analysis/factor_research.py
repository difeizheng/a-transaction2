"""因子有效性检验 —— IC / IR / 分层收益 / IC 衰减。

量化铁律：一个因子投入组合配置前，必须先用历史数据检验它的预测力。IC（信息系数）
= 截面上因子值与未来收益的 Spearman 秩相关；IR = mean(IC)/std(IC)；t 统计量检验
IC 是否显著异于 0。分层（分位数）多空价差验证因子是否单调排序收益。**没有 IC 检验
的因子，等于一个没人验证过真假的因子**（见 docs/投资系统审计报告.md P2-B）。

纯函数、零外部依赖（无网络/LLM/重对象）；输入是「日期×股票」面板，便于单测。
Spearman 用「因子值与收益各自求秩，再做 Pearson」实现，不引入 scipy 硬依赖。
"""
from __future__ import annotations

from typing import Dict, Sequence

import numpy as np
import pandas as pd


def rank_ic(factor_values: Sequence[float], forward_returns: Sequence[float]) -> float:
    """单期信息系数：因子值与未来收益的 Spearman 秩相关。

    Spearman = Pearson(rank(x), rank(y))。逐对剔除 NaN；样本 < 3 或任一侧无方差时
    返回 0.0（秩相关无定义）。返回值落在 [-1, 1]。

    >>> rank_ic([1, 2, 3, 4, 5], [10, 20, 30, 40, 50])
    1.0
    """
    df = pd.DataFrame({"factor": factor_values, "return": forward_returns}).dropna()
    if len(df) < 3:
        return 0.0
    rf, rr = df["factor"].rank(), df["return"].rank()
    if rf.std() == 0 or rr.std() == 0:
        return 0.0
    return float(rf.corr(rr))


def information_coefficient(
    factor_panel: pd.DataFrame, forward_return_panel: pd.DataFrame
) -> pd.Series:
    """逐期计算截面 rank IC。

    两个 DataFrame 同形状：index=日期、columns=股票、values=因子值/未来收益。逐行
    （每期）调用 ``rank_ic``，返回 index=日期、name="ic" 的 Series。

    约定：``forward_return_panel`` 在日期 t 的值，是该股自 t 起的未来收益——
    调用方负责用「未来」收益而非当期/历史收益（否则就是前视偏差自欺）。
    """
    if factor_panel.shape != forward_return_panel.shape:
        raise ValueError(
            f"面板形状不一致: factor={factor_panel.shape} vs return={forward_return_panel.shape}"
        )
    common = factor_panel.columns.intersection(forward_return_panel.columns)
    ic = {
        date: rank_ic(
            factor_panel.loc[date, common].values,
            forward_return_panel.loc[date, common].values,
        )
        for date in factor_panel.index
    }
    return pd.Series(ic, name="ic")


def ic_summary(ic_series: Sequence[float]) -> dict:
    """IC 序列统计汇总。

    返回 ``{mean_ic, ic_std, ir, t_stat, hit_rate, n_periods}``：
    - ``ir`` = mean(IC) / std(IC)（标准信息比，越高越稳定有效）；
    - ``t_stat`` = mean(IC) / (std(IC)/sqrt(n))，|t|>2 通常视为统计显著；
    - ``hit_rate`` = IC > 0 的期数占比；
    - std=0（IC 无波动）时 ir / t_stat 返回 NaN（无波动无法检验）。
    """
    s = pd.Series(ic_series, dtype="float64").dropna()
    n = len(s)
    if n == 0:
        return {"mean_ic": 0.0, "ic_std": 0.0, "ir": float("nan"),
                "t_stat": float("nan"), "hit_rate": 0.0, "n_periods": 0}
    mean_ic = float(s.mean())
    # ddof=1 样本标准差；IC 序列完全一致时因浮点噪声可能得到极小非零值，
    # 用容差判定「无波动」→ 此时 IR/t_stat 无意义，返回 nan。
    ic_std = float(s.std(ddof=1)) if n > 1 else 0.0
    if abs(ic_std) < 1e-12:
        ir = t_stat = float("nan")
    else:
        ir = mean_ic / ic_std
        t_stat = mean_ic / (ic_std / np.sqrt(n))
    return {
        "mean_ic": round(mean_ic, 4),
        "ic_std": round(ic_std, 4),
        "ir": round(ir, 4) if not np.isnan(ir) else float("nan"),
        "t_stat": round(t_stat, 2) if not np.isnan(t_stat) else float("nan"),
        "hit_rate": round(float((s > 0).mean()), 4),
        "n_periods": n,
    }


def analyze_factor(
    factor_panel: pd.DataFrame, forward_return_panel: pd.DataFrame
) -> dict:
    """端到端：从因子面板 + 未来收益面板直接给出 IC/IR 汇总（最常用入口）。"""
    ic = information_coefficient(factor_panel, forward_return_panel)
    return ic_summary(ic.tolist())


def quantile_spread(
    factor_values: Sequence[float],
    forward_returns: Sequence[float],
    n_quantiles: int = 5,
) -> dict:
    """单期分层检验：按因子值把股票分 ``n_quantiles`` 层，算每层平均未来收益。

    有效因子的分层平均收益应**单调**，且顶减底（多空价差 ``top_minus_bottom``）为正
    （做多因子）。返回 ``{quantile_returns: [...], top_minus_bottom: float, monotonic: bool}``。
    层数 > 样本数时降级为按样本数分层。
    """
    df = pd.DataFrame({"factor": factor_values, "return": forward_returns}).dropna()
    if len(df) < n_quantiles or n_quantiles < 2:
        n_quantiles = max(2, len(df))
    if len(df) < 2:
        return {"quantile_returns": [], "top_minus_bottom": 0.0, "monotonic": False}
    # q=False: 分位边界均匀分箱；duplicates="drop" 防止边界重复（因子取值集中时）。
    df["q"] = pd.qcut(df["factor"], n_quantiles, labels=False, duplicates="drop")
    n_actual = df["q"].nunique()
    grouped = df.groupby("q")["return"].mean().sort_index()
    # 补齐层数（某些分位被 drop）→ 返回实际层数的平均收益
    quantile_returns = [round(float(v), 6) for v in grouped.values]
    spread = float(grouped.iloc[-1] - grouped.iloc[0])
    monotonic = bool(grouped.is_monotonic_increasing)
    return {
        "quantile_returns": quantile_returns,
        "top_minus_bottom": round(spread, 6),
        "monotonic": monotonic,
        "n_quantiles_actual": n_actual,
    }


def ic_decay(
    factor_panel: pd.DataFrame,
    forward_return_panels: Dict[int, pd.DataFrame],
) -> Dict[int, dict]:
    """IC 衰减：对不同持有期的未来收益面板分别算 IC/IR 汇总。

    ``forward_return_panels`` 是 ``{horizon: 未来收益 DataFrame}``，逐 horizon 出
    ``ic_summary``。因子有效性通常随持有期衰减——衰减越慢，信号越持久。
    """
    out: Dict[int, dict] = {}
    for horizon, ret_panel in sorted(forward_return_panels.items()):
        out[horizon] = analyze_factor(factor_panel, ret_panel)
    return out
