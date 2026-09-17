"""选股筛选页共享常量。"""
from src.ui.core import get_dm  # noqa: F401  （子模块统一从这里取共享单例）

STRATEGY_OPTIONS = {
    "ma_cross":       "均线多头排列",
    "macd_golden":    "MACD金叉",
    "kdj_oversold":   "KDJ超卖反弹",
    "boll_breakout":  "布林带突破",
    "low_valuation":  "低估值",
    "high_growth":    "高成长",
    "industry_leader": "行业龙头",
    "multi_factor":   "多因子模型",
}
