"""选股筛选页面：评分卡可视化 + 流式显示 + 真正的停止/继续 + SQLite 持久化"""
import json
import time
import threading
from collections import defaultdict
import streamlit as st
import pandas as pd
from datetime import datetime

from src.data.manager import DataManager
from src.strategy.screener import Screener
from src.ui.components.service_info import render_service_info, get_akshare_info
from src.ui.components.screening_worker import screening_worker
from src.config import get_config


@st.cache_resource
def get_dm():
    return DataManager()


STRATEGY_OPTIONS = {
    "ma_cross":       "均线多头排列",
    "macd_golden":    "MACD金叉",
    "kdj_oversold":   "KDJ超卖反弹",
    "boll_breakout":  "布林带突破",
    "low_valuation":  "低估值",
    "high_growth":    "高成长",
    "industry_leader":"行业龙头",
    "multi_factor":   "多因子模型",
}


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

    # 历史记录已移至主区域 _render_history_panel()
    st.divider()
    render_service_info([get_akshare_info()])


def _render_strategy_detail(ev: dict):
    """渲染单个策略的指标、条件判定和计算过程（不含外层 expander）。"""
    indicators = json.loads(ev["indicators"]) if ev.get("indicators") else {}
    conditions = json.loads(ev["conditions"]) if ev.get("conditions") else []
    selected = bool(ev.get("selected"))
    score = ev.get("score", 0) or 0
    reason = ev.get("reason", "")

    status_text = "✓ 入选" if selected else "✗ 未入选"
    st.caption(f"{status_text}  |  评分: {score:.2f}  |  {reason}")

    if indicators:
        cols = st.columns(min(len(indicators), 5))
        for i, (k, v) in enumerate(indicators.items()):
            cols[i % len(cols)].metric(k, v)

    if conditions:
        for cond in conditions:
            passed = cond.get("passed", False)
            mark = "✓" if passed else "✗"
            color = "green" if passed else "red"
            detail = cond.get("detail", "")
            st.markdown(
                f":{color}[{mark}] {cond['label']}  "
                f"<span style='color:gray;font-size:0.85em'>{detail}</span>",
                unsafe_allow_html=True,
            )

    trace = ev.get("trace_log", "")
    if trace:
        st.caption("── 计算过程 ──")
        st.code(trace, language="text")


def _render_score_card(ev: dict, expanded: bool):
    """渲染单只股票的评分卡（单策略）。"""
    selected = bool(ev.get("selected"))
    score = ev.get("score", 0) or 0
    icon = "✅" if selected else "❌"
    reason = ev.get("reason", "")
    title = f"{icon} **{ev['code']}** {ev.get('name', '')}  |  评分: {score:.2f}  |  {reason}"

    with st.expander(title, expanded=expanded):
        _render_strategy_detail(ev)
        if selected:
            if st.button("⭐ 加入自选股", key=f"watch_{ev['code']}_{ev.get('strategy_key','')}"):
                dm = get_dm()
                dm.storage.add_to_watchlist({
                    "code": ev["code"],
                    "name": ev.get("name", ""),
                    "score": score,
                    "signals": ev.get("indicators", "{}"),
                    "reason": reason,
                    "source": "screener",
                })
                st.success(f"已加入自选股：{ev.get('name', ev['code'])}")


def _render_multi_strategy_card(code_data: dict, expanded: bool,
                                card_key_suffix: str = "", strategy_keys: list = None):
    """渲染多策略评分卡：外层 expander + 内层 tabs（按 session 策略列表建 tab，无数据时提示）。"""
    all_evals = code_data.get("_all_evals", [code_data])
    is_selected = code_data.get("_final_selected", False)
    code = code_data["code"]
    name = code_data.get("name", "")

    # 按 strategy_key 建索引，方便 tab 内查找
    eval_by_key = {e["strategy_key"]: e for e in all_evals}

    # 以 session 的策略列表为准（而非实际存在的评估数），保证分母正确
    display_keys = strategy_keys or list(eval_by_key.keys())
    num_total = len(display_keys)

    scores = [e.get("score", 0) or 0 for e in all_evals if e.get("selected")]
    avg_score = sum(scores) / len(scores) if scores else 0
    passed_count = sum(1 for e in all_evals if e.get("selected"))

    icon = "✅" if is_selected else "❌"
    title = (
        f"{icon} **{code}** {name}  |  均分: {avg_score:.2f}  |  "
        f"{passed_count}/{num_total} 策略通过"
    )

    with st.expander(title, expanded=expanded):
        if num_total > 1:
            tab_names = [STRATEGY_OPTIONS.get(k, k) for k in display_keys]
            tabs = st.tabs(tab_names)
            for tab, key in zip(tabs, display_keys):
                with tab:
                    ev = eval_by_key.get(key)
                    if ev:
                        _render_strategy_detail(ev)
                    else:
                        st.caption("该策略暂无评估数据（筛选可能未完成）")
        else:
            ev = eval_by_key.get(display_keys[0]) if display_keys else None
            if ev:
                _render_strategy_detail(ev)

        if is_selected:
            btn_key = f"watch_multi_{code}_{card_key_suffix}"
            if st.button("⭐ 加入自选股", key=btn_key):
                dm = get_dm()
                dm.storage.add_to_watchlist({
                    "code": code,
                    "name": name,
                    "score": avg_score,
                    "signals": all_evals[0].get("indicators", "{}"),
                    "reason": f"{passed_count}/{num_total} 策略通过",
                    "source": "screener",
                })
                st.success(f"已加入自选股：{name or code}")


def _render_history_panel(dm):
    """在主区域顶部以 expander 展示历史筛选记录，含操作按钮。"""
    with st.expander("📋 历史筛选记录", expanded=False):
        try:
            sessions = dm.storage.get_recent_sessions(limit=20)
        except Exception:
            sessions = []

        if not sessions:
            st.caption("暂无历史记录")
            return

        STATUS_LABEL = {
            "running": "运行中", "stopped": "已停止",
            "completed": "已完成", "failed": "失败",
        }
        rows = []
        for s in sessions:
            keys = json.loads(s.get("strategy_keys", "[]"))
            strategy_names = "、".join(STRATEGY_OPTIONS.get(k, k) for k in keys)
            rows.append({
                "ID": s["id"],
                "时间": (s.get("started_at") or "")[:16],
                "行业": s.get("industry") or "全市场",
                "策略": strategy_names,
                "模式": "交集" if s.get("mode") == "intersect" else "并集",
                "状态": STATUS_LABEL.get(s.get("status", ""), s.get("status", "")),
                "股票池": s.get("pool_size") or "-",
                "入选": s.get("selected_count") or 0,
            })

        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        col_sel, col_act = st.columns([2, 3])
        with col_sel:
            session_ids = [s["id"] for s in sessions]
            selected_id = st.selectbox(
                "选择记录操作",
                [None] + session_ids,
                format_func=lambda x: "选择记录…" if x is None else f"#{x}",
                key="hist_select_id",
            )

        if selected_id:
            sess = next((s for s in sessions if s["id"] == selected_id), None)
            if sess:
                status = sess.get("status", "")
                with col_act:
                    btn_cols = st.columns(3)
                    if btn_cols[0].button("📊 查看", key=f"hist_view_{selected_id}"):
                        st.session_state["screener_view_session"] = selected_id
                        st.rerun()

                    if status == "running":
                        # 刷新后线程已消失，直接将 DB 状态改为 stopped
                        if btn_cols[1].button("⏹️ 停止", key=f"hist_stop_{selected_id}"):
                            # 如果恰好是当前内存中的任务，也取消线程
                            cur_task = st.session_state.get("screening_task", {})
                            if cur_task.get("session_id") == selected_id:
                                cancel_ev = st.session_state.get("screening_cancel_event")
                                if cancel_ev:
                                    cancel_ev.set()
                            dm.storage.update_screening_session(selected_id, {"status": "stopped"})
                            st.rerun()

                    if status == "stopped":
                        if btn_cols[1].button("▶️ 继续", key=f"hist_resume_{selected_id}"):
                            st.session_state["screener_history_resume_id"] = selected_id
                            st.rerun()

                    # 所有状态都可删除
                    if btn_cols[2].button("🗑️ 删除", key=f"hist_del_{selected_id}"):
                        # 如果是当前运行中的任务，先取消线程
                        cur_task = st.session_state.get("screening_task", {})
                        if cur_task.get("session_id") == selected_id:
                            cancel_ev = st.session_state.get("screening_cancel_event")
                            if cancel_ev:
                                cancel_ev.set()
                            st.session_state.pop("screening_task", None)
                            st.session_state.pop("screening_cancel_event", None)
                        dm.storage.delete_screening_session(selected_id)
                        if st.session_state.get("screener_view_session") == selected_id:
                            st.session_state.pop("screener_view_session", None)
                        st.rerun()


def _group_evals_by_code(evals: list, mode: str, num_strategies: int) -> tuple:
    """
    按 code 分组 evaluations，根据 mode 判断最终入选状态。
    返回 (final_selected, final_unselected) 两个列表，每项附带 _all_evals / _final_selected。
    """
    code_groups: dict = defaultdict(list)
    for e in evals:
        code_groups[e["code"]].append(e)

    final_selected = []
    final_unselected = []
    for code, group in code_groups.items():
        passed_evals = [e for e in group if e.get("selected")]
        if mode == "intersect":
            is_final = len(passed_evals) == num_strategies
        else:
            is_final = len(passed_evals) > 0

        # 代表性记录：优先取入选策略中评分最高的，否则取第一条
        rep = max(group, key=lambda e: (e.get("selected", 0), e.get("score", 0) or 0))
        rep = dict(rep)  # 浅拷贝，避免污染原始数据
        rep["_final_selected"] = is_final
        rep["_all_evals"] = group

        if is_final:
            final_selected.append(rep)
        else:
            final_unselected.append(rep)

    final_selected.sort(key=lambda e: e.get("score", 0) or 0, reverse=True)
    final_unselected.sort(key=lambda e: e.get("score", 0) or 0, reverse=True)
    return final_selected, final_unselected


def _render_evaluations(session_id: int, dm, is_running: bool = False, search_query: str = ""):
    """从 SQLite 读取评估结果并渲染评分卡。running 时只显示入选股票计数，不渲染卡片。"""
    session = dm.storage.get_session(session_id) or {}
    mode = session.get("mode", "union")
    strategy_keys = json.loads(session.get("strategy_keys", "[]"))
    num_strategies = len(strategy_keys)

    if is_running:
        evals = dm.storage.get_session_evaluations(session_id)
        # 股票优先模式：按 code 分组，完成所有策略的才算"已处理"
        from collections import Counter
        code_strategy_count = Counter(e["code"] for e in evals)
        fully_done = sum(1 for cnt in code_strategy_count.values() if cnt >= num_strategies)
        code_pass_counts: dict = defaultdict(int)
        for e in evals:
            if e.get("selected"):
                code_pass_counts[e["code"]] += 1
        if mode == "intersect":
            selected_count = sum(1 for c, cnt in code_pass_counts.items()
                                 if cnt == num_strategies)
        else:
            selected_count = len(code_pass_counts)
        st.caption(f"已处理 {fully_done} 只 | 入选 {selected_count} 只")
        return

    evals = dm.storage.get_session_evaluations(session_id)
    if not evals:
        return

    # 模糊搜索过滤
    if search_query:
        q = search_query.strip().lower()
        evals = [e for e in evals if q in e.get("code", "").lower()
                 or q in (e.get("name") or "").lower()]

    final_selected, final_unselected = _group_evals_by_code(evals, mode, num_strategies)

    # 多策略时提供视图切换
    view_mode = "按股票分组"
    if num_strategies > 1:
        view_mode = st.radio(
            "显示方式", ["按股票分组", "按策略分组"],
            horizontal=True, key=f"view_mode_{session_id}",
        )

    if view_mode == "按策略分组":
        _render_by_strategy(session_id, dm, evals, strategy_keys, mode, num_strategies)
        return

    # ── 按股票分组展示 ──
    if final_selected:
        col_title, col_btn = st.columns([3, 1])
        col_title.markdown(f"### 入选股票（{len(final_selected)} 只）")
        if col_btn.button("⭐ 全部加入自选股", key=f"watch_all_{session_id}"):
            batch = [
                {
                    "code": ev["code"],
                    "name": ev.get("name", ""),
                    "score": ev.get("score", 0) or 0,
                    "signals": ev.get("indicators", "{}"),
                    "reason": ev.get("reason", ""),
                    "source": "screener",
                }
                for ev in final_selected
            ]
            dm.storage.batch_add_to_watchlist(batch)
            st.success(f"已将 {len(batch)} 只股票加入自选股")
        for ev in final_selected:
            if num_strategies > 1:
                _render_multi_strategy_card(ev, expanded=True,
                                            card_key_suffix=str(session_id),
                                            strategy_keys=strategy_keys)
            else:
                _render_score_card(ev, expanded=True)

    if final_unselected:
        st.markdown(f"### 未入选股票（{len(final_unselected)} 只）")
        page_size = 50
        total_pages = (len(final_unselected) + page_size - 1) // page_size
        if total_pages > 1:
            cols = st.columns([1, 3])
            page = cols[0].number_input("页码", min_value=1, max_value=total_pages, value=1,
                                        key=f"unsel_page_{session_id}") - 1
            cols[1].caption(f"共 {len(final_unselected)} 只，每页 {page_size} 只，第 {page+1}/{total_pages} 页")
        else:
            page = 0
        for ev in final_unselected[page * page_size: (page + 1) * page_size]:
            if num_strategies > 1:
                _render_multi_strategy_card(ev, expanded=False,
                                            card_key_suffix=f"{session_id}_unsel",
                                            strategy_keys=strategy_keys)
            else:
                _render_score_card(ev, expanded=False)


def _render_by_strategy(session_id: int, dm, evals: list, strategy_keys: list, mode: str, num_strategies: int):
    """按策略分 Tab 展示评估结果。"""
    _, final_unselected_codes = _group_evals_by_code(evals, mode, num_strategies)
    unselected_code_set = {e["code"] for e in final_unselected_codes}

    tab_names = [STRATEGY_OPTIONS.get(k, k) for k in strategy_keys]
    tabs = st.tabs(tab_names)
    for tab, key in zip(tabs, strategy_keys):
        with tab:
            strategy_evals = [e for e in evals if e.get("strategy_key") == key]
            sel = [e for e in strategy_evals if e.get("selected")]
            unsel = [e for e in strategy_evals if not e.get("selected")]
            st.caption(f"该策略入选 {len(sel)} 只 / 未入选 {len(unsel)} 只")
            for ev in sel:
                _render_score_card(ev, expanded=True)
            if unsel:
                st.markdown(f"**未入选（{len(unsel)} 只）**")
                page_size = 50
                total_pages = (len(unsel) + page_size - 1) // page_size
                page = 0
                if total_pages > 1:
                    page = st.number_input("页码", min_value=1, max_value=total_pages, value=1,
                                           key=f"strat_page_{session_id}_{key}") - 1
                for ev in unsel[page * page_size: (page + 1) * page_size]:
                    _render_score_card(ev, expanded=False)


def _render_progress(task: dict, session_id: int, dm):
    """渲染进度条 + 流式评分卡，running 时 sleep+rerun 轮询。"""
    status = task.get("status", "idle")

    if status == "running":
        thread = task.get("thread")
        if thread and not thread.is_alive():
            task["status"] = "failed"
            task["error"] = "筛选线程意外终止"
            status = "failed"

    if status == "running":
        elapsed = time.time() - task.get("start_time", time.time())
        cur = task.get("current_progress", 0)
        total = task.get("current_total", 1)
        overall = cur / max(total, 1)
        num_strategies = task.get("strategies_total", 1)

        with st.status(f"筛选中：{task.get('current_stock', '初始化…')}（{cur}/{total} 只 × {num_strategies} 策略）",
                       expanded=False):
            for line in task.get("logs", [])[-6:]:
                st.caption(line)
            st.progress(min(overall, 1.0))
            if cur > 0 and elapsed > 0:
                speed = cur / elapsed
                eta = f"预计剩余 {max(total - cur, 0) / speed:.0f}s" if speed > 0 else ""
            else:
                eta = "计算中…"
            st.caption(f"`{task.get('current_stock', '')}` | {cur}/{total} 只 | 已用 {elapsed:.0f}s | {eta}")

        _render_evaluations(session_id, dm, is_running=True)
        time.sleep(1)
        st.rerun()

    elif status == "stopped":
        elapsed = time.time() - task.get("start_time", time.time())
        st.warning(f"筛选已停止（已用 {elapsed:.0f}s）。点击「继续筛选」从上次停止处继续。")
        _render_evaluations(session_id, dm)

    elif status == "completed":
        elapsed = time.time() - task.get("start_time", time.time())
        final = task.get("final_results", [])
        st.success(f"筛选完成，{len(final)} 只入选（耗时 {elapsed:.0f}s）")
        if final:
            # 转为 ScreenResult 供 analysis 页面使用
            from src.strategy.base import ScreenResult
            st.session_state["screen_results"] = [
                ScreenResult(code=e["code"], name=e.get("name", ""),
                             score=e.get("score", 0),
                             signals=json.loads(e["indicators"]) if e.get("indicators") else {},
                             reason=e.get("reason", ""))
                for e in final
            ]
        _render_evaluations(session_id, dm)

    elif status == "failed":
        st.error(f"筛选失败：{task.get('error', '未知错误')}")
        _render_evaluations(session_id, dm)


def _start_screening(dm, screener, selected_strategies, industry, mode, top_n,
                     resume: bool = False):
    """创建 session、cancel_event，启动后台线程。"""
    selected_industry = None if industry == "全市场" else industry

    # 如果 resume，复用上次 session_id
    if resume:
        old_task = st.session_state.get("screening_task", {})
        session_id = old_task.get("session_id")
        if not session_id:
            resume = False

    if not resume:
        session_id = dm.storage.create_screening_session({
            "strategy_keys": selected_strategies,
            "industry": selected_industry,
            "mode": mode,
            "top_n": top_n,
        })

    cancel_event = threading.Event()
    task_state = {
        "status": "running",
        "thread": None,
        "session_id": session_id,
        "strategies_total": len(selected_strategies),
        "strategies_done": 0,
        "current_strategy": "初始化…",
        "current_stock": "",
        "current_progress": 0,
        "current_total": 0,
        "start_time": time.time(),
        "logs": ["断点续跑…" if resume else "开始筛选…"],
        "final_results": None,
        "error": None,
    }
    # 新筛选清空旧的问答历史
    if not resume:
        st.session_state.pop("screener_qa_history", None)
    t = threading.Thread(
        target=screening_worker,
        args=(task_state, dm, screener, selected_strategies,
              STRATEGY_OPTIONS, selected_industry, top_n, mode,
              cancel_event, session_id),
        daemon=True,
    )
    task_state["thread"] = t
    st.session_state["screening_task"] = task_state
    st.session_state["screening_cancel_event"] = cancel_event
    t.start()


def _build_qa_context(dm, session_id: int) -> str:
    """组装LLM系统提示：入选股票摘要 + 指标 + 价格 + 财务 + 近期新闻。"""
    evals = dm.storage.get_session_evaluations(session_id, selected_only=True)
    if not evals:
        return "你是一位A股投资分析助手。当前没有筛选结果。"

    stock_lines = []
    for ev in evals[:20]:
        code = ev["code"]
        name = ev.get("name", "")
        score = ev.get("score", 0) or 0
        reason = ev.get("reason", "")
        indicators_str = ev.get("indicators", "{}")

        line = f"- {name}({code}): 评分{score:.2f}, {reason}, 指标:{indicators_str}"

        try:
            bars_df = dm.storage.get_daily_bars(code)
            if not bars_df.empty:
                last = bars_df.iloc[-1]
                line += f", 最新收盘价{last.get('close', 'N/A')}"
        except Exception:
            pass

        try:
            fin_df = dm.storage.get_financial_data(code)
            if not fin_df.empty:
                latest = fin_df.iloc[0]
                pe = latest.get("pe_ttm", "N/A")
                pb = latest.get("pb", "N/A")
                line += f", PE={pe}, PB={pb}"
        except Exception:
            pass

        stock_lines.append(line)

    news_section = ""
    try:
        news_df = dm.get_news(limit=10)
        if not news_df.empty:
            news_items = [
                f"- [{r.get('publish_time', '')}] {r.get('title', '')}"
                for _, r in news_df.head(10).iterrows()
            ]
            news_section = "\n近期市场新闻：\n" + "\n".join(news_items)
    except Exception:
        pass

    return f"""你是一位专业的A股投资分析助手。以下是当前筛选结果和市场数据，请基于这些信息回答用户问题。

筛选入选股票（共{len(evals)}只）：
{chr(10).join(stock_lines)}
{news_section}

注意：回答简洁专业，使用中文；不要编造数据；投资建议需附带风险提示。"""


def _render_qa_section(dm, session_id: int):
    """在筛选结果下方渲染LLM问答区。"""
    st.divider()
    st.subheader("💬 AI问答")
    st.caption("基于当前筛选结果向AI提问，每次提问消耗1次API调用")

    if "screener_qa_history" not in st.session_state:
        st.session_state["screener_qa_history"] = []

    history = st.session_state["screener_qa_history"]

    # 显示对话历史
    for msg in history:
        icon = "🧑" if msg["role"] == "user" else "🤖"
        label = "你" if msg["role"] == "user" else "AI"
        st.markdown(f"**{icon} {label}：** {msg['content']}")

    if len(history) >= 20:
        st.caption("对话历史已超过10轮，较早的对话将被截断")

    # 输入表单
    with st.form("qa_form", clear_on_submit=True):
        user_input = st.text_area("输入问题", height=80,
                                  placeholder="例如：这些股票中哪只最适合短线操作？")
        submitted = st.form_submit_button("发送", type="primary")

    if submitted and user_input.strip():
        history.append({"role": "user", "content": user_input.strip()})

        # 超过10轮时截断最早的一对
        if len(history) > 20:
            history = history[-20:]

        context = _build_qa_context(dm, session_id)
        cfg = get_config()
        from src.analysis.llm_analyzer import LLMAnalyzer
        llm = LLMAnalyzer(cfg)

        with st.spinner("AI思考中…"):
            reply = llm.chat(messages=history, system=context)

        history.append({"role": "assistant", "content": reply})
        st.session_state["screener_qa_history"] = history
        st.rerun()

    if history:
        if st.button("清空对话", key="qa_clear"):
            st.session_state["screener_qa_history"] = []
            st.rerun()


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
        st.info("在左侧配置策略参数后点击「开始筛选」")
