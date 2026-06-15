"""基本面选股策略"""
import logging
from typing import List
import pandas as pd

from src.strategy.base import BaseStrategy, ScreenResult, StockEvaluation, ConditionCheck

logger = logging.getLogger(__name__)


class LowValuationStrategy(BaseStrategy):
    """低估值策略：PE和PB同时低于行业均值"""

    name = "low_valuation"
    description = "低估值筛选（PE/PB低于阈值）"

    def __init__(self, max_pe: float = 20, max_pb: float = 2.0, min_roe: float = 8.0):
        self.max_pe = max_pe
        self.max_pb = max_pb
        self.min_roe = min_roe

    def screen(self, stock_pool: pd.DataFrame, data_manager, progress_callback=None) -> List[ScreenResult]:
        codes = stock_pool["code"].tolist()
        if progress_callback:
            progress_callback(0, 1, "获取财务数据", "")
        fin = data_manager.get_latest_financial_batch(codes)
        if fin.empty:
            return []
        results = []
        total = len(fin)
        for i, (_, row) in enumerate(fin.iterrows(), 1):
            if progress_callback:
                progress_callback(i, total, row.get("code", ""), "")
            pe = row.get("pe_ttm")
            pb = row.get("pb")
            roe = row.get("roe")
            if pd.isna(pe) or pd.isna(pb) or pd.isna(roe):
                continue
            if pe <= 0 or pe > self.max_pe:
                continue
            if pb <= 0 or pb > self.max_pb:
                continue
            if roe < self.min_roe:
                continue
            # 评分：PE越低、ROE越高分越高
            score = (self.max_pe - pe) / self.max_pe * 50 + roe / 30 * 50
            name = stock_pool[stock_pool["code"] == row["code"]]["name"].values
            name = name[0] if len(name) > 0 else ""
            results.append(ScreenResult(
                code=row["code"], name=name, score=round(score, 2),
                signals={"pe_ttm": round(pe, 2), "pb": round(pb, 2), "roe": round(roe, 2)},
                reason=f"低估值高ROE: PE={pe:.1f}, PB={pb:.2f}, ROE={roe:.1f}%"
            ))
        return sorted(results, key=lambda x: x.score, reverse=True)

    def get_params(self) -> dict:
        return {"max_pe": self.max_pe, "max_pb": self.max_pb, "min_roe": self.min_roe}

    def evaluate_stock(self, code: str, name: str, data_manager) -> StockEvaluation:
        try:
            fin = data_manager.get_financial_data(code)
            if fin.empty:
                return StockEvaluation(code=code, name=name, strategy_key="low_valuation",
                                       selected=False, score=0, reason="无财务数据")
            row = fin.iloc[0]
            pe = row.get("pe_ttm")
            pb = row.get("pb")
            roe = row.get("roe")
            trace = f"PE={pe} PB={pb} ROE={roe}"
            if pd.isna(pe) or pd.isna(pb) or pd.isna(roe):
                return StockEvaluation(code=code, name=name, strategy_key="low_valuation",
                                       selected=False, score=0, reason="财务数据缺失",
                                       trace_log=trace)
            pe, pb, roe = float(pe), float(pb), float(roe)
            cond1 = 0 < pe <= self.max_pe
            cond2 = 0 < pb <= self.max_pb
            cond3 = roe >= self.min_roe
            conditions = [
                ConditionCheck(f"0<PE≤{self.max_pe}", cond1, f"PE={pe:.2f}"),
                ConditionCheck(f"0<PB≤{self.max_pb}", cond2, f"PB={pb:.2f}"),
                ConditionCheck(f"ROE≥{self.min_roe}%", cond3, f"ROE={roe:.2f}%"),
            ]
            selected = cond1 and cond2 and cond3
            score = round((self.max_pe - pe) / self.max_pe * 50 + roe / 30 * 50, 2) if selected else 0
            return StockEvaluation(
                code=code, name=name, strategy_key="low_valuation",
                selected=selected, score=score,
                indicators={"PE_TTM": round(pe, 2), "PB": round(pb, 2), "ROE": round(roe, 2)},
                conditions=conditions,
                reason=f"低估值高ROE: PE={pe:.1f}, PB={pb:.2f}, ROE={roe:.1f}%" if selected else "不满足低估值条件",
                trace_log=trace
            )
        except Exception as e:
            return StockEvaluation(code=code, name=name, strategy_key="low_valuation",
                                   selected=False, score=0, reason=f"计算异常: {e}")


class HighGrowthStrategy(BaseStrategy):
    """高成长策略：营收和净利润同比增速均超过阈值"""

    name = "high_growth"
    description = "高成长筛选（营收/净利润增速超阈值）"

    def __init__(self, min_revenue_yoy: float = 20.0, min_profit_yoy: float = 20.0):
        self.min_revenue_yoy = min_revenue_yoy
        self.min_profit_yoy = min_profit_yoy

    def screen(self, stock_pool: pd.DataFrame, data_manager, progress_callback=None) -> List[ScreenResult]:
        codes = stock_pool["code"].tolist()
        if progress_callback:
            progress_callback(0, 1, "获取财务数据", "")
        fin = data_manager.get_latest_financial_batch(codes)
        if fin.empty:
            return []
        results = []
        total = len(fin)
        for i, (_, row) in enumerate(fin.iterrows(), 1):
            if progress_callback:
                progress_callback(i, total, row.get("code", ""), "")
            rev_yoy = row.get("revenue_yoy")
            profit_yoy = row.get("profit_yoy")
            if pd.isna(rev_yoy) or pd.isna(profit_yoy):
                continue
            if rev_yoy < self.min_revenue_yoy or profit_yoy < self.min_profit_yoy:
                continue
            score = (rev_yoy + profit_yoy) / 2
            name = stock_pool[stock_pool["code"] == row["code"]]["name"].values
            name = name[0] if len(name) > 0 else ""
            results.append(ScreenResult(
                code=row["code"], name=name, score=round(score, 2),
                signals={"revenue_yoy": round(rev_yoy, 1), "profit_yoy": round(profit_yoy, 1)},
                reason=f"高成长: 营收+{rev_yoy:.1f}%, 净利润+{profit_yoy:.1f}%"
            ))
        return sorted(results, key=lambda x: x.score, reverse=True)

    def get_params(self) -> dict:
        return {"min_revenue_yoy": self.min_revenue_yoy, "min_profit_yoy": self.min_profit_yoy}

    def evaluate_stock(self, code: str, name: str, data_manager) -> StockEvaluation:
        try:
            fin = data_manager.get_financial_data(code)
            if fin.empty:
                return StockEvaluation(code=code, name=name, strategy_key="high_growth",
                                       selected=False, score=0, reason="无财务数据")
            row = fin.iloc[0]
            rev_yoy = row.get("revenue_yoy")
            profit_yoy = row.get("profit_yoy")
            trace = f"revenue_yoy={rev_yoy} profit_yoy={profit_yoy}"
            if pd.isna(rev_yoy) or pd.isna(profit_yoy):
                return StockEvaluation(code=code, name=name, strategy_key="high_growth",
                                       selected=False, score=0, reason="成长数据缺失",
                                       trace_log=trace)
            rev_yoy, profit_yoy = float(rev_yoy), float(profit_yoy)
            cond1 = rev_yoy >= self.min_revenue_yoy
            cond2 = profit_yoy >= self.min_profit_yoy
            conditions = [
                ConditionCheck(f"营收同比≥{self.min_revenue_yoy}%", cond1, f"{rev_yoy:.1f}%"),
                ConditionCheck(f"净利润同比≥{self.min_profit_yoy}%", cond2, f"{profit_yoy:.1f}%"),
            ]
            selected = cond1 and cond2
            score = round((rev_yoy + profit_yoy) / 2, 2) if selected else 0
            return StockEvaluation(
                code=code, name=name, strategy_key="high_growth",
                selected=selected, score=score,
                indicators={"营收同比": round(rev_yoy, 1), "净利润同比": round(profit_yoy, 1)},
                conditions=conditions,
                reason=f"高成长: 营收+{rev_yoy:.1f}%, 净利润+{profit_yoy:.1f}%" if selected else "不满足高成长条件",
                trace_log=trace
            )
        except Exception as e:
            return StockEvaluation(code=code, name=name, strategy_key="high_growth",
                                   selected=False, score=0, reason=f"计算异常: {e}")


class IndustryLeaderStrategy(BaseStrategy):
    """行业龙头策略：市值排名靠前且ROE高"""

    name = "industry_leader"
    description = "行业龙头（市值前列+高ROE）"

    def __init__(self, top_n: int = 10, min_roe: float = 10.0):
        self.top_n = top_n
        self.min_roe = min_roe

    def screen(self, stock_pool: pd.DataFrame, data_manager, progress_callback=None) -> List[ScreenResult]:
        codes = stock_pool["code"].tolist()
        if progress_callback:
            progress_callback(0, 1, "获取财务数据", "")
        fin = data_manager.get_latest_financial_batch(codes)
        if fin.empty:
            return []
        # 按市值排序取前 N*3 作为候选
        fin = fin.dropna(subset=["total_mv"])
        fin = fin.sort_values("total_mv", ascending=False).head(self.top_n * 3)
        name_map = dict(zip(stock_pool["code"], stock_pool["name"])) if "name" in stock_pool.columns else {}

        # 第一遍：收集通过 ROE 过滤的候选
        candidates = []
        for _, row in fin.iterrows():
            roe = row.get("roe")
            if pd.isna(roe) or roe < self.min_roe:
                continue
            candidates.append({
                "code": row["code"],
                "name": name_map.get(row["code"], ""),
                "roe": float(roe),
                "mv": float(row.get("total_mv", 0)),
            })
        if not candidates:
            return []

        # 第二遍：ROE 与市值分别 min-max 归一化到 [0,50] 再求和 → [0,100]。
        # 修复旧式 roe + mv/1e6：市值(万元)项对大盘股可达上百（茅台约160），
        # 完全淹没 ROE，使排序退化为"纯最大市值"。归一化后两者等权可比。
        def _norm(v: float, lo: float, hi: float) -> float:
            if hi == lo:
                return 25.0  # 候选只有单一取值时给中性分
            return (v - lo) / (hi - lo) * 50

        roe_vals = [c["roe"] for c in candidates]
        mv_vals = [c["mv"] for c in candidates]
        roe_lo, roe_hi = min(roe_vals), max(roe_vals)
        mv_lo, mv_hi = min(mv_vals), max(mv_vals)

        results = []
        total = len(candidates)
        for i, c in enumerate(candidates, 1):
            if progress_callback:
                progress_callback(i, total, c["code"], "")
            score = _norm(c["roe"], roe_lo, roe_hi) + _norm(c["mv"], mv_lo, mv_hi)
            results.append(ScreenResult(
                code=c["code"], name=c["name"], score=round(score, 2),
                signals={"total_mv_yi": round(c["mv"] / 1e4, 1), "roe": round(c["roe"], 2)},
                reason=f"行业龙头: 市值{c['mv']/1e4:.0f}亿, ROE={c['roe']:.1f}%"
            ))
        return sorted(results, key=lambda x: x.score, reverse=True)[:self.top_n]

    def get_params(self) -> dict:
        return {"top_n": self.top_n, "min_roe": self.min_roe}

    def evaluate_stock(self, code: str, name: str, data_manager) -> StockEvaluation:
        try:
            fin = data_manager.get_financial_data(code)
            if fin.empty:
                return StockEvaluation(code=code, name=name, strategy_key="industry_leader",
                                       selected=False, score=0, reason="无财务数据")
            row = fin.iloc[0]
            roe = row.get("roe")
            mv = row.get("total_mv")
            trace = f"ROE={roe} total_mv={mv}"
            if pd.isna(roe) or pd.isna(mv):
                return StockEvaluation(code=code, name=name, strategy_key="industry_leader",
                                       selected=False, score=0, reason="财务数据缺失",
                                       trace_log=trace)
            roe, mv = float(roe), float(mv)
            mv_yi = round(mv / 1e4, 1)
            cond1 = roe >= self.min_roe
            # 市值条件：无法在单股评估时做跨股排名，改为展示市值供参考
            conditions = [
                ConditionCheck(f"ROE≥{self.min_roe}%", cond1, f"ROE={roe:.2f}%"),
                ConditionCheck("市值（参考）", True, f"{mv_yi}亿"),
            ]
            selected = cond1
            # 单股评估无法跨股 min-max 归一化，改用 ROE 强度（相对 30% 参考上限），
            # 避免旧式 roe + mv/1e6 对大盘股产生无意义的高分。
            score = round(min(max(roe / 30 * 100, 0), 100), 2) if selected else 0
            return StockEvaluation(
                code=code, name=name, strategy_key="industry_leader",
                selected=selected, score=score,
                indicators={"ROE": round(roe, 2), "市值(亿)": mv_yi},
                conditions=conditions,
                reason=f"行业龙头: 市值{mv_yi}亿, ROE={roe:.1f}%" if selected else f"ROE={roe:.1f}%不满足条件",
                trace_log=trace
            )
        except Exception as e:
            return StockEvaluation(code=code, name=name, strategy_key="industry_leader",
                                   selected=False, score=0, reason=f"计算异常: {e}")
