"""选股筛选页面：评分卡可视化 + 流式显示 + 真正的停止/继续 + SQLite 持久化。

包结构（2026-07 重构，原 754 行单文件拆分）：
- common.py   常量/共享服务
- cards.py    评分卡与结果列表渲染
- history.py  历史筛选记录面板
- run.py      后台线程启动与进度渲染
- qa.py       AI 问答区（含 token 成本展示）
"""
import json

import streamlit as st

from src.strategy.screener import Screener
from src.ui.components.service_info import get_akshare_info, render_service_info
from src.ui.page_modules.screener.cards import _render_evaluations
from src.ui.page_modules.screener.common import STRATEGY_OPTIONS, get_dm
from src.ui.page_modules.screener.history import _render_history_panel
from src.ui.page_modules.screener.qa import _render_qa_section
from src.ui.page_modules.screener.run import _render_progress, _start_screening

__all__ = ["STRATEGY_OPTIONS", "render", "render_sidebar"]


def render_sidebar():
    st.subheader("筛选参数")
    dm = get_dm()

    try:
        industry_df = dm.get_industry_list()
        cols = industry_df.columns.tolist()
        # 识别名称列和代码列（兼容 AKShare "板块名称"/"板块代码" 和 Tushare "name"/"index_code"）
        name_col = next((c for c in cols if c in ("板块名称", "name", "industry_name")), cols[0])
        code_col = next((c for c in cols if c in ("板块代码", "index_code", "industry_code")), None)
        # 避免 name_col 和 code_col 指向同一列
        if code_col == name_col:
            code_col = None
        names = industry_df[name_col].tolist()
        if code_col:
            codes = industry_df[code_col].tolist()
            label_map = {name: f"{code}  {name}" for name, code in zip(names, codes)}
        else:
            label_map = {}
        industries = ["全市场"] + names
    except Exception:
        industries = ["全市场"]
        label_map = {}
    st.session_state["screener_industry"] = st.selectbox(
        "行业/板块", industries,
        format_func=lambda x: label_map.get(x, x),
    )
    dm.local_only = st.checkbox(
        "仅使用本地数据（不联网补拉）",
        value=getattr(dm, "local_only", False),
        help="网络降级/数据源限流时开启：筛选只读本地库，宁可跑陈旧数据也不卡死")
    if dm.local_only:
        st.caption("⚠️ 本地模式：K线/财务不补拉，结果仅供快速验证")
    st.session_state["screener_strategies"] = st.multiselect(
        "选择策略（可多选）",
        list(STRATEGY_OPTIONS.keys()),
        default=["ma_cross"],
        format_func=lambda x: STRATEGY_OPTIONS.get(x, x),
    )
    st.session_state["screener_mode"] = st.radio(
        "多策略合并方式", ["union", "intersect"],
        format_func=lambda x: "并集（任一策略选中）" if x == "union" else "交集（所有策略都选中）",
    )
    st.session_state["screener_top_n"] = st.slider("展示前N只", 5, 50, 20)

    task = st.session_state.get("screening_task", {})
    cancel_event = st.session_state.get("screening_cancel_event")
    is_running = (
        task.get("status") == "running"
        and task.get("thread") and task["thread"].is_alive()
    )
    is_stopped = task.get("status") == "stopped"

    if is_running:
        if st.button("停止筛选", type="secondary", use_container_width=True):
            if cancel_event:
                cancel_event.set()
    elif is_stopped:
        col1, col2 = st.columns(2)
        with col1:
            st.session_state["screener_run"] = st.button(
                "继续筛选", type="primary", use_container_width=True
            )
            st.session_state["screener_resume"] = True
        with col2:
            if st.button("重新开始", use_container_width=True):
                st.session_state["screener_run"] = True
                st.session_state["screener_resume"] = False
    else:
        st.session_state["screener_run"] = st.button(
            "开始筛选", type="primary", use_container_width=True
        )
        st.session_state["screener_resume"] = False

    # 历史记录已移至主区域 history._render_history_panel()
    st.divider()
    render_service_info([get_akshare_info()])


def render():
    st.title("🔍 选股筛选")
    dm = get_dm()
    screener = Screener(dm)

    # 历史记录面板（主区域顶部）
    _render_history_panel(dm)

    # 从历史记录直接继续（绕过侧边栏按钮，避免 render_sidebar 覆盖 screener_run）
    history_resume_id = st.session_state.pop("screener_history_resume_id", None)
    if history_resume_id:
        sess = dm.storage.get_session(history_resume_id)
        if sess:
            keys = json.loads(sess.get("strategy_keys", "[]"))
            st.session_state["screening_task"] = {"session_id": history_resume_id, "status": "stopped"}
            _start_screening(dm, screener, keys,
                             sess.get("industry") or "全市场",
                             sess.get("mode", "union"),
                             sess.get("top_n", 20),
                             resume=True)
            st.rerun()

    selected_strategies = st.session_state.get("screener_strategies", [])
    industry = st.session_state.get("screener_industry", "全市场")
    mode = st.session_state.get("screener_mode", "union")
    top_n = st.session_state.get("screener_top_n", 20)

    # 查看历史 session
    view_session = st.session_state.get("screener_view_session")
    if view_session:
        sess = dm.storage.get_session(view_session)
        if not sess:
            st.session_state.pop("screener_view_session", None)
            st.rerun()
            return
        st.info(f"历史记录 #{view_session} | {(sess.get('started_at') or '')[:16]} | "
                f"状态: {sess.get('status')} | 入选: {sess.get('selected_count', 0)} 只")
        search_query = st.text_input(
            "🔍 搜索股票（代码或名称）",
            key=f"hist_search_{view_session}",
            placeholder="输入股票代码或名称关键字…",
        )
        if st.button("关闭历史记录"):
            st.session_state.pop("screener_view_session", None)
            st.rerun()
        _render_evaluations(view_session, dm, search_query=search_query)
        return

    # 开始/继续筛选
    if st.session_state.get("screener_run"):
        if not selected_strategies:
            st.warning("请至少选择一个策略")
        else:
            task = st.session_state.get("screening_task", {})
            is_running = (task.get("status") == "running"
                          and task.get("thread") and task["thread"].is_alive())
            if not is_running:
                resume = st.session_state.get("screener_resume", False)
                _start_screening(dm, screener, selected_strategies, industry, mode, top_n, resume)
                st.rerun()

    # 进度 + 评分卡
    task = st.session_state.get("screening_task")
    if task and task.get("status") not in (None, "idle"):
        session_id = task.get("session_id")
        if session_id:
            _render_progress(task, session_id, dm)
            # 筛选完成后显示问答区
            if task.get("status") == "completed":
                _render_qa_section(dm, session_id)
    else:
        st.info("从上方「历史筛选记录」选择查看，或在左侧配置策略参数后点击「开始筛选」")
