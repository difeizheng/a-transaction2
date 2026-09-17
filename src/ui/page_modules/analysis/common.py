"""市场分析页共享：快照缓存、配色助手、情绪/宏观标签。"""
import streamlit as st

from src.ui.core import COLOR_DOWN, COLOR_UP, get_dm, pct_span  # noqa: F401

# 兼容旧内部命名（涨红跌绿）
_UP_COLOR = COLOR_UP
_DOWN_COLOR = COLOR_DOWN

_pct_span = pct_span  # 子模块沿用旧名


@st.cache_data(ttl=600, show_spinner=False)
def _load_market_snapshot(_dm_id):
    """指数+板块快照缓存 10 分钟（镜像 data_mgmt._load_coverage 的 _dm_id 用法）。
    刷新按钮走 advisor.analyze_market() 的非缓存直连路径，不用 cache_data.clear()
    （避免清掉其他 tab 的缓存）。"""
    return get_dm().get_market_snapshot()


@st.cache_data(ttl=21600, show_spinner=False)
def _load_macro_snapshot(_dm_id):
    """宏观四支柱快照缓存 6h（低频数据：CPI/PMI/M2 月频、SHIBOR/美债日频）。
    刷新走 advisor.analyze_macro() 非缓存直连，不用 cache_data.clear()。"""
    return get_dm().get_macro_snapshot()


def _label_cn(label: str) -> str:
    return {"bullish": "偏多 📈", "bearish": "偏空 📉", "neutral": "中性 ➡️"}.get(label, "中性 ➡️")


def _stance_label_cn(label: str) -> str:
    return {"bullish": "宽松积极 📈", "bearish": "偏紧 📉", "neutral": "中性 ➡️"}.get(label, "中性 ➡️")


def _signal_color(signal: float) -> str:
    return _UP_COLOR if signal > 0 else (_DOWN_COLOR if signal < 0 else "gray")
