"""Streamlit主应用入口"""
import sys
import importlib
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

st.set_page_config(
    page_title="A股智能交易系统",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 全局样式
st.markdown("""
<style>
.metric-card { background: #f0f2f6; border-radius: 8px; padding: 12px; }
.profit { color: #e74c3c; font-weight: bold; }
.loss { color: #27ae60; font-weight: bold; }
</style>
""", unsafe_allow_html=True)

PAGES = {
    "总览仪表盘": {"label": "📊 总览仪表盘", "module": "src.ui.page_modules.dashboard"},
    "数据管理":   {"label": "🗄️ 数据管理",   "module": "src.ui.page_modules.data_mgmt"},
    "选股筛选":   {"label": "🔍 选股筛选",   "module": "src.ui.page_modules.screener"},
    "自选股":     {"label": "⭐ 自选股",     "module": "src.ui.page_modules.watchlist"},
    "策略回测":   {"label": "📉 策略回测",   "module": "src.ui.page_modules.backtest"},
    "市场分析":   {"label": "🤖 AI 与市场分析", "module": "src.ui.page_modules.analysis"},
    "模拟交易":   {"label": "💹 模拟交易",   "module": "src.ui.page_modules.trading"},
}

# ── 侧边栏：上半导航 + 下半页面设置 ──────────────────────────────────────
with st.sidebar:
    st.markdown("### 导航")
    page_key = st.radio(
        "页面",
        list(PAGES.keys()),
        format_func=lambda k: PAGES[k]["label"],
        label_visibility="collapsed",
    )
    st.divider()

    # 动态加载当前页面模块，调用其 render_sidebar()（如果存在）
    from src.ui.core import page_guard
    mod = importlib.import_module(PAGES[page_key]["module"])
    if hasattr(mod, "render_sidebar"):
        page_guard(mod.render_sidebar)()

# ── 主内容区 ──────────────────────────────────────────────────────────────
# 页面级错误边界：单页渲染异常不再拖垮整个 app（traceback 收进日志）
page_guard(mod.render)()
