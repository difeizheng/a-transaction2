"""策略基类：所有选股策略的统一接口"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Dict, Any
import pandas as pd


@dataclass
class ScreenResult:
    code: str
    name: str
    score: float          # 综合评分，越高越好
    signals: Dict[str, Any] = field(default_factory=dict)  # 各指标信号详情
    reason: str = ""      # 选中理由摘要


@dataclass
class ConditionCheck:
    """单个条件的判定结果"""
    label: str            # 条件描述，如 "MA5>MA10>MA20>MA60"
    passed: bool          # True=✓ False=✗
    detail: str = ""      # 数值详情，如 "12.3 > 11.8 > 10.5 > 9.8"


@dataclass
class StockEvaluation:
    """单只股票的完整评估结果，用于评分卡可视化"""
    code: str
    name: str
    strategy_key: str
    selected: bool                                          # 是否入选
    score: float                                            # 未入选为 0
    indicators: Dict[str, Any] = field(default_factory=dict)   # 指标值，如 {"MA5": 12.3}
    conditions: List[ConditionCheck] = field(default_factory=list)  # 条件判定列表
    reason: str = ""
    trace_log: str = ""                                     # 完整计算过程（调试用）


@dataclass
class ExitSignal:
    """对称出场信号：入场条件的反向触发（审计报告 P2-E 入场出场对称化）。

    原系统 4 个技术策略只有入场条件、无出场——趋势/突破策略只在突破日买入却永不出场，
    导致信号不完整。本类封装「该策略下是否应离场」的对称判定。
    """
    code: str
    name: str
    strategy_key: str
    should_exit: bool                                       # 是否触发离场
    reason: str = ""
    indicators: Dict[str, Any] = field(default_factory=dict)


class BaseStrategy(ABC):
    """选股策略基类"""

    name: str = "base"
    description: str = ""

    @abstractmethod
    def screen(self, stock_pool: pd.DataFrame, data_manager, progress_callback=None) -> List[ScreenResult]:
        """
        从股票池中筛选并评分
        :param stock_pool: DataFrame，至少含 code/name 列
        :param data_manager: DataManager 实例，用于获取行情/财务数据
        :param progress_callback: 可选回调 callback(current, total, code, name)
        :return: 按 score 降序排列的 ScreenResult 列表
        """

    def evaluate_stock(self, code: str, name: str, data_manager) -> StockEvaluation:
        """
        对单只股票进行完整评估，返回含指标值和条件判定的 StockEvaluation。
        子类覆盖此方法以支持评分卡可视化和股票级断点续跑。
        """
        raise NotImplementedError

    def supports_evaluate(self) -> bool:
        """返回 True 表示该策略已实现 evaluate_stock()，可逐股处理。"""
        return type(self).evaluate_stock is not BaseStrategy.evaluate_stock

    def evaluate_exit(self, code: str, name: str, data_manager) -> "ExitSignal":
        """对称出场判定：返回是否应离场（入场反信号）。

        默认实现返回 ``should_exit=False``（未定义出场）。子类按各自入场条件的反向触发
        覆盖（如均线策略入场要求多头排列，出场即排列破位）。供持仓管理/止盈止损之外的
        信号化离场使用。
        """
        return ExitSignal(code=code, name=name, strategy_key=self.name,
                          should_exit=False, reason="该策略未定义出场逻辑")

    def get_params(self) -> dict:
        """返回策略参数，用于UI展示和回测记录"""
        return {}
