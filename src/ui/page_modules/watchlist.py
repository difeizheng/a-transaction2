"""自选股管理页面"""
import json
import streamlit as st
from datetime import datetime

from src.data.manager import DataManager
from src.ui.components.service_info import render_service_info, get_akshare_info


@st.cache_resource
def get_dm():
    return DataManager()


def render_sidebar():
    st.subheader("自选股")
    dm = get_dm()

    items = dm.storage.get_watchlist()
    st.metric("自选股数量", len(items))

    st.divider()
    st.caption("手动添加")
    with st.form("manual_add_form", clear_on_submit=True):
        code_input = st.text_input("股票代码", placeholder="如 000001")
        name_input = st.text_input("股票名称", placeholder="如 平安银行")
        submitted = st.form_submit_button("添加", use_container_width=True)
    if submitted:
        code_input = code_input.strip()
        if not code_input:
            st.error("请输入股票代码")
        else:
            dm.storage.add_to_watchlist({
                "code": code_input,
                "name": name_input.strip() or code_input,
                "score": 0,
                "signals": "{}",
                "reason": "手动添加",
                "source": "manual",
            })
            st.success(f"已添加 {code_input}")
            st.rerun()

    # 从筛选结果批量添加
    screen_results = st.session_state.get("screen_results", [])
    if screen_results:
        st.divider()
        st.caption(f"来自选股结果（{len(screen_results)} 只）")
        if st.button("全部加入自选股", use_container_width=True):
            batch = [
                {
                    "code": sr.code,
                    "name": sr.name,
                    "score": sr.score,
                    "signals": json.dumps(sr.signals, ensure_ascii=False),
                    "reason": sr.reason,
                    "source": "screener",
                }
                for sr in screen_results
            ]
            dm.storage.batch_add_to_watchlist(batch)
            st.success(f"已添加 {len(batch)} 只")
            st.rerun()

    st.divider()
    render_service_info([get_akshare_info()])


def render():
    st.title("⭐ 自选股")
    dm = get_dm()

    items = dm.storage.get_watchlist()

    if not items:
        st.info("自选股列表为空。在「选股筛选」页面完成筛选后，点击股票卡片上的「加入自选股」按钮，或在左侧手动添加。")
        return

    # 操作提示（跨页面导航）
    nav_msg = st.session_state.pop("watchlist_nav_msg", None)
    if nav_msg:
        st.success(nav_msg)

    col_top, col_clear = st.columns([4, 1])
    col_top.caption(f"共 {len(items)} 只，点击操作按钮后请在左侧导航切换页面")
    with col_clear:
        with st.popover("🗑️ 清空", use_container_width=True):
            st.warning(f"确定要清空全部 {len(items)} 只自选股吗？此操作不可撤销。")
            if st.button("确认清空", type="primary", key="confirm_clear_all"):
                dm.storage.clear_watchlist()
                st.rerun()

    for item in items:
        code = item["code"]
        name = item.get("name", code)
        score = item.get("score") or 0
        reason = item.get("reason", "")
        note = item.get("note", "") or ""
        added_at = (item.get("added_at") or "")[:16]
        source = item.get("source", "")

        with st.expander(f"**{code}** {name}  |  评分: {score:.2f}  |  {reason}", expanded=False):
            col_info, col_actions = st.columns([3, 2])

            with col_info:
                st.caption(f"来源: {source}  |  添加时间: {added_at}")
                signals = {}
                try:
                    signals = json.loads(item.get("signals") or "{}")
                except Exception:
                    pass
                if signals:
                    sig_cols = st.columns(min(len(signals), 4))
                    for i, (k, v) in enumerate(list(signals.items())[:4]):
                        sig_cols[i].metric(k, v)

                # 备注编辑
                new_note = st.text_area("备注", value=note, key=f"note_{code}", height=68)
                if st.button("保存备注", key=f"save_note_{code}"):
                    dm.storage.update_watchlist_note(code, new_note)
                    st.success("备注已保存")
                    st.rerun()

            with col_actions:
                st.write("")  # 垂直对齐

                if st.button("🤖 去分析", key=f"analyze_{code}", use_container_width=True):
                    from src.strategy.base import ScreenResult
                    sr = ScreenResult(
                        code=code, name=name, score=score,
                        signals=signals, reason=reason,
                    )
                    st.session_state["screen_results"] = [sr]
                    st.session_state["watchlist_nav_msg"] = f"已将 {name}({code}) 设为分析目标，请切换到「市场分析」页面"
                    st.rerun()

                if st.button("💹 去交易", key=f"trade_{code}", use_container_width=True):
                    st.session_state["buy_code"] = code
                    st.session_state["buy_name"] = name
                    st.session_state["watchlist_nav_msg"] = f"已预填 {name}({code})，请切换到「模拟交易」页面"
                    st.rerun()

                if st.button("🗑️ 移除", key=f"remove_{code}", use_container_width=True):
                    dm.storage.remove_from_watchlist(code)
                    st.rerun()
