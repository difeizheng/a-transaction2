"""多因子模型：技术面+基本面因子标准化打分"""
import logging
from typing import List, Dict
import pandas as pd
import numpy as np

from src.strategy.base import BaseStrategy, ScreenResult

logger = logging.getLogger(__name__)


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
    多因子模型：对每只股票计算多个因子得分，加权求和排序
    因子：动量(20日涨幅)、低估值(PE倒数)、高成长(利润增速)、高ROE
    """

    name = "multi_factor"
    description = "多因子模型（动量+估值+成长+ROE加权打分）"

    def __init__(
        self,
        weight_momentum: float = 0.25,
        weight_value: float = 0.25,
        weight_growth: float = 0.25,
        weight_roe: float = 0.25,
        momentum_days: int = 20,
    ):
        self.weights = {
            "momentum": weight_momentum,
            "value": weight_value,
            "growth": weight_growth,
            "roe": weight_roe,
        }
        self.momentum_days = momentum_days

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
            # 动量因子
            try:
                df = data_manager.get_daily_bars(code)
                if len(df) >= self.momentum_days + 1:
                    rec["momentum"] = (df["close"].iloc[-1] / df["close"].iloc[-self.momentum_days] - 1) * 100
                else:
                    rec["momentum"] = np.nan
            except Exception:
                rec["momentum"] = np.nan
            # 基本面因子
            f = fin_map.get(code, {})
            pe = f.get("pe_ttm") if f else None
            rec["value"] = (1 / pe) * 100 if pe and pe > 0 else np.nan
            rec["growth"] = f.get("profit_yoy") if f else np.nan
            rec["roe"] = f.get("roe") if f else np.nan
            records.append(rec)

        if not records:
            return []

        df_factors = pd.DataFrame(records)

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
