"""历史筛选记录面板。"""
import json

import pandas as pd
import streamlit as st

from src.ui.page_modules.screener.common import STRATEGY_OPTIONS


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
