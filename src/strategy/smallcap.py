"""小市值因子策略 —— A 股最稳健的 alpha 之一。

学术与实证一致：A 股长期存在显著的小市值溢价（流动性溢价 + 信息不对称 + 散户偏好）。
本策略按总市值升序选最小的一批，与小市值 alpha 方向一致——区别于系统原有的
``IndustryLeaderStrategy``（选大市值，方向相反）。详见 docs/投资系统审计报告.md P1-D。

注意：小市值 alpha 在 2017 机构化、2016-17 并购重组新规后有所衰减，且 2024「新国九条」
退市常态化对小盘有结构性冲击。属「长期有效但需配合 IC 检验 + 退市风险过滤」的因子，
单独重仓有尾部风险。定位为多因子组合中的一员，而非单一策略重仓。
"""
import logging
from typing import List

import numpy as np
import pandas as pd

from src.strategy.base import BaseStrategy, ScreenResult, StockEvaluation, ConditionCheck

logger = logging.getLogger(__name__)

# 默认小市值阈值（万元）：tushare total_mv 单位为万元，50 亿 = 5_000_000 万元
DEFAULT_MAX_MV_WAN = 5_000_000.0


class SmallCapStrategy(BaseStrategy):
    """小市值策略：按总市值升序选最小 N 只（A 股小市值溢价）。

    - ``screen``：批量取 total_mv，过滤 ST/次新（K 线不足），按市值升序取 top_n；
    - ``evaluate_stock``：单股 selected iff ``0 < total_mv <= max_mv`` 且 K 线充足。
    """

    name = "small_cap"
    description = "小市值因子（A 股小市值溢价，市值升序选最小）"

    def __init__(self, top_n: int = 20, max_mv_wan: float = DEFAULT_MAX_MV_WAN,
                 min_bars: int = 20):
        self.top_n = top_n
        self.max_mv_wan = max_mv_wan
        self.min_bars = min_bars  # 过滤次新股（上市不足 min_bars 个交易日）

    def screen(self, stock_pool: pd.DataFrame, data_manager, progress_callback=None) -> List[ScreenResult]:
        codes = stock_pool["code"].tolist()
        name_map = dict(zip(stock_pool["code"], stock_pool.get("name", stock_pool["code"]))) \
            if "name" in stock_pool.columns else {}

        if progress_callback:
            progress_callback(0, 1, "获取市值数据", "")
        fin = data_manager.get_latest_financial_batch(codes)
        if fin.empty:
            return []

        # 只保留有有效市值的
        fin = fin.dropna(subset=["total_mv"])
        fin = fin[fin["total_mv"] > 0]

        # 过滤次新股：K 线不足 min_bars 的视为次新，跳过（best-effort，失败不计入）
        if self.min_bars > 0:
            keep_codes = []
            for code in fin["code"]:
                try:
                    if len(data_manager.get_daily_bars(code)) >= self.min_bars:
                        keep_codes.append(code)
                except Exception:
                    continue
            fin = fin[fin["code"].isin(keep_codes)]

        fin = fin.sort_values("total_mv", ascending=True).head(self.top_n)
        if fin.empty:
            return []

        # 评分：市值越小分越高。用所选范围内的 min-max 归一化反转到 [0,100]。
        mv_min, mv_max = fin["total_mv"].min(), fin["total_mv"].max()
        results = []
        total = len(fin)
        for i, (_, row) in enumerate(fin.iterrows(), 1):
            if progress_callback:
                progress_callback(i, total, row["code"], name_map.get(row["code"], ""))
            mv = float(row["total_mv"])
            score = (mv_max - mv) / (mv_max - mv_min) * 100 if mv_max > mv_min else 50.0
            results.append(ScreenResult(
                code=row["code"], name=name_map.get(row["code"], ""),
                score=round(score, 2),
                signals={"total_mv_yi": round(mv / 1e4, 2)},  # 万元 → 亿元
                reason=f"小市值: {mv/1e4:.2f} 亿"
            ))
        return results  # 已按市值升序（=分数降序）

    def evaluate_stock(self, code: str, name: str, data_manager) -> StockEvaluation:
        try:
            fin = data_manager.get_financial_data(code)
            trace = []
            if fin.empty:
                return StockEvaluation(code=code, name=name, strategy_key="small_cap",
                                       selected=False, score=0, reason="无财务数据",
                                       trace_log="\n".join(trace))
            row = fin.iloc[0]
            mv = row.get("total_mv")
            trace.append(f"total_mv={mv}（万元），阈值上限 {self.max_mv_wan}")
            if pd.isna(mv) or mv is None:
                return StockEvaluation(code=code, name=name, strategy_key="small_cap",
                                       selected=False, score=0, reason="市值数据缺失",
                                       trace_log="\n".join(trace))
            mv = float(mv)

            # 次新股检查
            enough_bars = True
            try:
                enough_bars = len(data_manager.get_daily_bars(code)) >= self.min_bars
            except Exception:
                enough_bars = True  # 取数失败不阻断（保守视为够）

            cond1 = 0 < mv <= self.max_mv_wan
            cond2 = enough_bars
            mv_yi = round(mv / 1e4, 2)
            conditions = [
                ConditionCheck(f"0<市值≤{self.max_mv_wan/1e4:.0f}亿", cond1, f"{mv_yi}亿"),
                ConditionCheck(f"K线≥{self.min_bars}（非次新）", cond2, f"min_bars={self.min_bars}"),
            ]
            selected = cond1 and cond2
            # 分数：相对阈值上限，越小越高（上限处=0，极小处趋近100）
            score = round(max(0.0, (1 - mv / self.max_mv_wan) * 100), 2) if selected else 0
            return StockEvaluation(
                code=code, name=name, strategy_key="small_cap",
                selected=selected, score=score,
                indicators={"市值(亿)": mv_yi, "阈值(亿)": self.max_mv_wan / 1e4},
                conditions=conditions,
                reason=f"小市值: {mv_yi} 亿" if selected else f"市值 {mv_yi} 亿 不满足小市值条件",
                trace_log="\n".join(trace)
            )
        except Exception as e:
            return StockEvaluation(code=code, name=name, strategy_key="small_cap",
                                   selected=False, score=0, reason=f"计算异常: {e}")

    def get_params(self) -> dict:
        return {"top_n": self.top_n, "max_mv_wan": self.max_mv_wan, "min_bars": self.min_bars}
