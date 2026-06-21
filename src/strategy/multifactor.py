"""多因子模型：技术面+基本面因子标准化打分"""
import logging
from typing import List, Dict
import pandas as pd
import numpy as np

from src.strategy.base import BaseStrategy, ScreenResult

logger = logging.getLogger(__name__)


def compute_cross_sectional_momentum(
    close: pd.Series, long_window: int = 252, skip: int = 21
) -> float:
    """标准截面动量（12 月减最近 1 月）：``P[t-skip] / P[t-skip-long] - 1``。

    A 股 **1~3 周是反转效应**（短期超买回落），旧实现用 20 日涨幅正加权 = 赌反转的反方向
    = 稳定亏钱。标准截面动量跳过最近 1 个月（``skip=21`` 交易日），用过去 ~12 个月
    （``long_window=252``）的涨幅——这是学术与业界公认的 A 股中期动量正确口径
    （审计报告 P1-C2）。

    历史不足 ``skip+long_window+1`` 时回退到「跳过最近 skip、用全部可用历史」；
    连 skip+1 都不足则返回 NaN。
    """
    n = len(close)
    if n < skip + 2:
        return float("nan")
    end = close.iloc[-(skip + 1)]
    if n >= skip + long_window + 1:
        start = close.iloc[-(skip + long_window + 1)]
    else:
        start = close.iloc[0]  # 回退：用全部可用历史
    if start <= 0 or pd.isna(start) or pd.isna(end):
        return float("nan")
    return float(end / start - 1) * 100


def _zscore(series: pd.Series) -> pd.Series:
    """Z-score标准化：先 winsorize 截断极端值，缺失因子按市场平均(0)中性填充。"""
    s = series.astype(float)
    if s.dropna().empty:
        return pd.Series(0, index=s.index)
    # winsorize: 截断到 1%/99% 分位数，抑制极端值（如 +5000% 利润增速）主导全市场打分
    lo, hi = s.quantile(0.01), s.quantile(0.99)
    if pd.notna(lo) and pd.notna(hi) and lo != hi:
        s = s.clip(lower=lo, upper=hi)
    mean, std = s.mean(), s.std()
    if std == 0 or pd.isna(std):
        return pd.Series(0, index=s.index)
    return ((s - mean) / std).fillna(0)  # 缺失因子视为市场平均，不整行剔除


class MultiFactorStrategy(BaseStrategy):
    """
    多因子模型：对每只股票计算多个因子得分，加权求和排序。
    因子：动量（12月减最近1月，截面动量标准口径）、低估值(PE倒数)、高成长(利润增速)、高ROE。

    可选**行业中性化**（``neutralize_industry=True``）：价值/成长因子行业聚集性强，中性化
    剥离行业系统性暴露，只保留同行业内相对 alpha（审计报告 P1-C）。
    """

    name = "multi_factor"
    description = "多因子模型（动量+估值+成长+ROE加权打分）"

    def __init__(
        self,
        weight_momentum: float = 0.25,
        weight_value: float = 0.25,
        weight_growth: float = 0.25,
        weight_roe: float = 0.25,
        momentum_days: int = None,           # 废弃保留：旧 20 日短期涨幅（方向可能反），默认 None 走标准口径
        momentum_long: int = 252,
        momentum_skip: int = 21,
        neutralize_industry: bool = False,
    ):
        self.weights = {
            "momentum": weight_momentum,
            "value": weight_value,
            "growth": weight_growth,
            "roe": weight_roe,
        }
        # momentum_days 仅用于显式指定旧的短期窗口（向后兼容/对比）；None 时用标准 12-1 口径
        self.momentum_days = momentum_days
        self.momentum_long = momentum_long
        self.momentum_skip = momentum_skip
        self.neutralize_industry = neutralize_industry

    def screen(self, stock_pool: pd.DataFrame, data_manager, progress_callback=None) -> List[ScreenResult]:
        codes = stock_pool["code"].tolist()
        records = []

        # 1. 获取财务数据
        if progress_callback:
            progress_callback(0, len(codes), "获取财务数据", "")
        fin = data_manager.get_latest_financial_batch(codes)
        fin_map = {row["code"]: row for _, row in fin.iterrows()} if not fin.empty else {}

        # 2. 计算各因子原始值
        total = len(stock_pool)
        for i, (_, row) in enumerate(stock_pool.iterrows(), 1):
            code, name = row["code"], row.get("name", "")
            if progress_callback:
                progress_callback(i, total, code, name)
            rec = {"code": code, "name": name}
            # 动量因子（默认标准截面动量 12-1，跳过最近 1 个月的短期反转区）
            try:
                df = data_manager.get_daily_bars(code)
                close = df["close"] if not df.empty else pd.Series(dtype=float)
                if self.momentum_days is not None:
                    # 旧口径（显式指定时）：近 momentum_days 涨幅（含方向风险）
                    rec["momentum"] = (
                        (close.iloc[-1] / close.iloc[-self.momentum_days] - 1) * 100
                        if len(close) >= self.momentum_days + 1 else np.nan
                    )
                else:
                    rec["momentum"] = compute_cross_sectional_momentum(
                        close, self.momentum_long, self.momentum_skip
                    )
            except Exception:
                rec["momentum"] = np.nan
            # 基本面因子
            f = fin_map.get(code)  # 值为 Series 或 None（不能 if f ——Series 真值歧义）
            pe = f.get("pe_ttm") if f is not None else None
            rec["value"] = (1 / pe) * 100 if pe is not None and pe > 0 else np.nan
            rec["growth"] = f.get("profit_yoy") if f is not None else np.nan
            rec["roe"] = f.get("roe") if f is not None else np.nan
            records.append(rec)

        if not records:
            return []

        df_factors = pd.DataFrame(records)

        # 2.5 可选行业中性化：剥离行业系统性暴露（价值/成长因子行业聚集性强）
        if self.neutralize_industry and "industry" in stock_pool.columns:
            from src.strategy.neutralize import neutralize_by_group
            group = stock_pool.set_index("code")["industry"]
            for factor in ["momentum", "value", "growth", "roe"]:
                aligned = df_factors.set_index("code")[factor]
                neutral = neutralize_by_group(aligned, group.reindex(aligned.index))
                df_factors[factor] = neutral.reindex(df_factors.index).values

        # 3. Z-score标准化各因子
        for factor in ["momentum", "value", "growth", "roe"]:
            col = df_factors[factor].astype(float)
            df_factors[f"{factor}_z"] = _zscore(col)

        # 4. 加权求和
        df_factors["score"] = sum(
            df_factors[f"{f}_z"] * w for f, w in self.weights.items()
        )

        # 5. 过滤NaN并排序
        df_factors = df_factors.dropna(subset=["score"]).sort_values("score", ascending=False)

        results = []
        for _, row in df_factors.iterrows():
            results.append(ScreenResult(
                code=row["code"],
                name=row["name"],
                score=round(float(row["score"]), 4),
                signals={
                    "momentum_20d": round(row.get("momentum", 0) or 0, 2),
                    "pe_ttm": round(1 / row["value"] * 100, 2) if row.get("value") else None,
                    "profit_yoy": round(row.get("growth", 0) or 0, 1),
                    "roe": round(row.get("roe", 0) or 0, 2),
                },
                reason=f"多因子综合评分: {row['score']:.3f}"
            ))
        return results

    def get_params(self) -> dict:
        return {**self.weights, "momentum_days": self.momentum_days}
