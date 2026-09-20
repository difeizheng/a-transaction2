"""评分卡渲染：策略明细、单策略/多策略卡片、按股票/策略分组的结果列表。"""
import json
from collections import Counter, defaultdict

import streamlit as st

from src.ui.page_modules.screener.common import STRATEGY_OPTIONS, get_dm


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
    st_mark = "  |  ⚠️ ST股" if "ST" in str(ev.get("name", "")).upper() else ""
    title = f"{icon} **{ev['code']}** {ev.get('name', '')}  |  评分: {score:.2f}  |  {reason}{st_mark}"

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
    st_mark = "  |  ⚠️ ST股" if "ST" in str(name).upper() else ""
    title = (
        f"{icon} **{code}** {name}  |  均分: {avg_score:.2f}  |  "
        f"{passed_count}/{num_total} 策略通过{st_mark}"
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
