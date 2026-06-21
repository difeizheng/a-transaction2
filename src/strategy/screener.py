"""选股引擎：组合多个策略，支持单策略和多策略交集/并集"""
import logging
from typing import List, Optional
import pandas as pd

from src.strategy.base import BaseStrategy, ScreenResult
from src.strategy.technical import MACrossStrategy, MACDGoldenCrossStrategy, KDJOversoldStrategy, BollingerBreakoutStrategy
from src.strategy.fundamental import LowValuationStrategy, HighGrowthStrategy, IndustryLeaderStrategy
from src.strategy.multifactor import MultiFactorStrategy
from src.strategy.smallcap import SmallCapStrategy

logger = logging.getLogger(__name__)

# 所有可用策略注册表。is_deprecated=True 的策略保留可复现历史，但从默认策略集/UI 下拉
# 中排除（见 list_strategies active_only）——这些策略在 A 股机构化后失效（审计报告 P1-D）。
STRATEGY_REGISTRY: dict = {
    "ma_cross": MACrossStrategy,
    "macd_golden": MACDGoldenCrossStrategy,
    "kdj_oversold": KDJOversoldStrategy,        # is_deprecated=True
    "boll_breakout": BollingerBreakoutStrategy,  # is_deprecated=True
    "low_valuation": LowValuationStrategy,
    "high_growth": HighGrowthStrategy,
    "industry_leader": IndustryLeaderStrategy,
    "small_cap": SmallCapStrategy,
    "multi_factor": MultiFactorStrategy,
}

# 默认策略集：UI 下拉/AutoTrader 默认勾选。排除两个已失效技术策略。
DEFAULT_STRATEGY_KEYS = [
    "ma_cross", "macd_golden", "low_valuation", "high_growth",
    "industry_leader", "small_cap", "multi_factor",
]


class Screener:
    def __init__(self, data_manager):
        self.dm = data_manager

    def get_stock_pool(self, industry: Optional[str] = None) -> pd.DataFrame:
        """获取选股范围"""
        if industry:
            df = self.dm.get_industry_stocks(industry)
        else:
            df = self.dm.get_stock_list()
        return df

    def run_strategy(
        self,
        strategy: BaseStrategy,
        industry: Optional[str] = None,
        top_n: int = 20,
        progress_callback=None,
    ) -> List[ScreenResult]:
        """运行单个策略"""
        pool = self.get_stock_pool(industry)
        logger.info(f"运行策略 [{strategy.name}]，股票池 {len(pool)} 只")
        results = strategy.screen(pool, self.dm, progress_callback=progress_callback)
        return results[:top_n]

    def run_multi_strategy(
        self,
        strategies: List[BaseStrategy],
        industry: Optional[str] = None,
        top_n: int = 20,
        mode: str = "union",  # union=并集取最高分, intersect=交集
        progress_callback=None,
    ) -> List[ScreenResult]:
        """
        运行多个策略并合并结果
        union: 取所有策略结果的并集，按平均分排序
        intersect: 只保留所有策略都选中的股票

        注意：各策略原始 score 量纲不一致（均线=百分比、MACD=价格、低估值=0~100、
        多因子=Z-score），直接跨策略取平均会让量纲大的策略吞没小的。这里先在
        每个策略的结果集内做百分位排名归一到 [0,100]，再跨策略平均。
        """
        pool = self.get_stock_pool(industry)
        all_results: dict = {}  # code -> {result, norm_scores, strategies}

        for strategy in strategies:
            logger.info(f"运行策略 [{strategy.name}]")
            results = strategy.screen(pool, self.dm, progress_callback=progress_callback)
            norm_scores = self._percentile_rank(results)  # 与 results 逐位对齐
            for r, ns in zip(results, norm_scores):
                if r.code not in all_results:
                    all_results[r.code] = {"result": r, "norm_scores": [], "strategies": []}
                all_results[r.code]["norm_scores"].append(ns)
                all_results[r.code]["strategies"].append(strategy.name)

        if mode == "intersect":
            # 只保留所有策略都选中的
            n = len(strategies)
            all_results = {k: v for k, v in all_results.items() if len(v["strategies"]) == n}

        # 按（跨策略平均）归一化分排序
        merged = []
        for code, data in all_results.items():
            r = data["result"]
            avg_score = sum(data["norm_scores"]) / len(data["norm_scores"])
            r.score = round(avg_score, 4)
            r.reason = f"策略: {', '.join(data['strategies'])} | 归一化得分 {avg_score:.1f} | {r.reason}"
            merged.append(r)

        return sorted(merged, key=lambda x: x.score, reverse=True)[:top_n]

    @staticmethod
    def _percentile_rank(results: List[ScreenResult]) -> List[float]:
        """将本策略各股票原始分转为 [0,100] 百分位排名，消除量纲差异。
        结果与传入 results 逐位对齐。空列表返回 []。"""
        if not results:
            return []
        scores = pd.Series([r.score for r in results])
        # rank(pct=True)：值越大排名越接近1；乘100得百分位。并列取平均排名。
        ranks = (scores.rank(pct=True) * 100).round(2)
        return ranks.tolist()

    @staticmethod
    def list_strategies(active_only: bool = True) -> dict:
        """返回 {key: description}。

        :param active_only: True（默认）排除 ``is_deprecated=True`` 的失效策略；
            False 返回全部（含失效，供历史复现/高级用户）。
        """
        out = {}
        for k, cls in STRATEGY_REGISTRY.items():
            if active_only and getattr(cls, "is_deprecated", False):
                continue
            out[k] = cls.description if hasattr(cls, "description") else k
        return out

    @staticmethod
    def create_strategy(name: str, **kwargs) -> BaseStrategy:
        cls = STRATEGY_REGISTRY.get(name)
        if cls is None:
            raise ValueError(f"未知策略: {name}，可用: {list(STRATEGY_REGISTRY.keys())}")
        return cls(**kwargs)
