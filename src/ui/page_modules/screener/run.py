"""筛选执行：后台线程启动 + 进度/流式结果渲染。"""
import json
import threading
import time

import streamlit as st

from src.ui.components.freshness import bars_freshness_badge
from src.ui.components.screening_worker import screening_worker
from src.ui.page_modules.screener.cards import _render_evaluations
from src.ui.page_modules.screener.common import STRATEGY_OPTIONS


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
        st.caption(bars_freshness_badge(dm.storage, "信号基于K线截至"))
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
        st.session_state.pop("screener_qa_since", None)
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
