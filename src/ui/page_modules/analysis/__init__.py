"""AI 与市场分析页面（原「市场分析」）。

包结构（2026-07 重构，原 606 行单文件拆分）：
- common.py    快照缓存 / 配色 / 标签
- sentiment.py 市场情绪 tab（指数、板块、温度、新闻）
- macro.py     宏观态势 tab（四支柱）
- deep.py      个股深度分析 tab
"""
import streamlit as st

from src.config import get_config
from src.ui.components.service_info import get_akshare_info, get_llm_info, render_service_info
from src.ui.core import get_advisor, get_dm
from src.ui.page_modules.analysis.deep import _render_stock_deep_analysis
from src.ui.page_modules.analysis.macro import _render_macro
from src.ui.page_modules.analysis.sentiment import _degradation_level, _render_market_sentiment

# tests/test_resilience.py 从包根导入该函数，保持再导出
__all__ = ["render", "render_sidebar", "_degradation_level"]


def render_sidebar():
    st.subheader("分析设置")
    st.session_state["analysis_max_stocks"] = st.slider(
        "批量分析前N只（每只消耗1次API调用）", 1, 10, 3
    )

    st.divider()
    cfg = get_config()
    render_service_info([get_akshare_info(), get_llm_info(cfg)])


def render():
    st.title("🤖 AI 与市场分析")
    dm = get_dm()
    advisor = get_advisor()

    tab1, tab2, tab3 = st.tabs(["市场情绪", "个股深度分析", "宏观分析"])

    with tab1:
        _render_market_sentiment(dm, advisor)

    with tab2:
        _render_stock_deep_analysis(dm, advisor)

    with tab3:
        _render_macro(dm, advisor)
