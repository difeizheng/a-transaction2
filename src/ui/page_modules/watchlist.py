"""自选股管理页面

重构新增：标签（tags）分组筛选 + 排序；共享服务单例（src.ui.core）。
标签存 watchlist.tags 列（逗号分隔），例：'白马,观察'。
"""
import json
import streamlit as st

from src.ui.components.freshness import bars_freshness_badge
from src.ui.components.service_info import render_service_info, get_akshare_info
from src.ui.core import get_dm

_ALL_TAG = "（全部）"


def _parse_tags(item: dict) -> list:
    return [t.strip() for t in (item.get("tags") or "").split(",") if t.strip()]


def _all_tags(items: list) -> list:
    tags = set()
    for it in items:
        tags.update(_parse_tags(it))
    return sorted(tags)


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
        tags_input = st.text_input("标签（逗号分隔，可空）", placeholder="如 白马,观察")
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
                "tags": tags_input.strip(),
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


def _render_item(dm, item: dict, latest=None) -> None:
    code = item["code"]
    name = item.get("name", code)
    score = item.get("score") or 0
    reason = item.get("reason", "")
    note = item.get("note", "") or ""
    tags = item.get("tags", "") or ""
    added_at = (item.get("added_at") or "")[:16]
    source = item.get("source", "")
    tag_str = f"  |  🏷️ {tags}" if tags else ""
    st_mark = "  |  ⚠️ ST股" if "ST" in name.upper() else ""

    with st.expander(f"**{code}** {name}  |  评分: {score:.2f}  |  {reason}{tag_str}{st_mark}",
                     expanded=False):
        col_info, col_actions = st.columns([3, 2])

        with col_info:
            st.caption(f"来源: {source}  |  添加时间: {added_at}")
            # 最新收盘价（本地K线）：放在快照信号上方，避免 5 个月前的快照 MA 被当成现价
            if latest:
                close, close_date = latest
                lc1, lc2, _ = st.columns(3)
                lc1.metric("最新收盘价", f"¥{close:.2f}")
                lc2.caption(f"\n\n本地数据 {close_date}（非盘中实时）")
            signals = {}
            try:
                signals = json.loads(item.get("signals") or "{}")
            except Exception:
                pass
            if signals:
                sig_cols = st.columns(min(len(signals), 4))
                for i, (k, v) in enumerate(list(signals.items())[:4]):
                    sig_cols[i].metric(f"{k}（快照）", f"{v:.2f}" if isinstance(v, (int, float)) else v)
                st.caption(
                    f"信号为加入时快照（{added_at or '时间未知'}），非实时数据；"
                    "最新信号请重新运行选股筛选")

            # 标签编辑
            new_tags = st.text_input("标签（逗号分隔）", value=tags, key=f"tags_{code}")
            if st.button("保存标签", key=f"save_tags_{code}"):
                dm.storage.update_watchlist_tags(code, new_tags.strip())
                st.rerun()

            # 备注编辑
            new_note = st.text_area("备注", value=note, key=f"note_{code}", height=68)
            if st.button("保存备注", key=f"save_note_{code}"):
                dm.storage.update_watchlist_note(code, new_note)
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
                st.session_state["watchlist_nav_msg"] = (
                    f"已将 {name}({code}) 设为分析目标，请切换到「AI 与市场分析」页面")
                st.rerun()

            if st.button("💹 去交易", key=f"trade_{code}", use_container_width=True):
                st.session_state["buy_code"] = code
                st.session_state["buy_name"] = name
                st.session_state["watchlist_nav_msg"] = (
                    f"已预填 {name}({code})，请切换到「模拟交易」页面")
                st.rerun()

            if st.button("🗑️ 移除", key=f"remove_{code}", use_container_width=True):
                dm.storage.remove_from_watchlist(code)
                st.rerun()


def render():
    st.title("⭐ 自选股")
    dm = get_dm()

    items = dm.storage.get_watchlist()
    st.caption(bars_freshness_badge(dm.storage, "K线数据截至"))

    if not items:
        st.info("自选股列表为空。在「选股筛选」页面完成筛选后，点击股票卡片上的「加入自选股」按钮，或在左侧手动添加。")
        return

    # 操作提示（跨页面导航）
    nav_msg = st.session_state.pop("watchlist_nav_msg", None)
    if nav_msg:
        st.success(nav_msg)

    # ── 工具栏：标签筛选 + 排序 + 清空 ──────────────────────────
    col_filter, col_sort, col_clear = st.columns([3, 2, 1])
    with col_filter:
        selected_tags = st.multiselect(
            "按标签筛选", _all_tags(items), default=[],
            placeholder="全部标签", label_visibility="collapsed")
    with col_sort:
        sort_by = st.selectbox(
            "排序", ["评分（高→低）", "添加时间（新→旧）", "代码"],
            label_visibility="collapsed")
    with col_clear:
        with st.popover("🗑️ 清空", use_container_width=True):
            st.warning(f"确定要清空全部 {len(items)} 只自选股吗？此操作不可撤销。")
            if st.button("确认清空", type="primary", key="confirm_clear_all"):
                dm.storage.clear_watchlist()
                st.rerun()

    # ── 批量维护标签 ──────────────────────────────────────────
    with st.expander("🏷️ 批量维护标签"):
        # 应用成功后下一轮 rerun 在控件创建前清空选择，避免残留旧选择/旧警告
        if st.session_state.pop("_batch_clear_pending", False):
            st.session_state["batch_codes_input"] = []
            st.session_state["batch_tags_input"] = ""
        name_map = {it["code"]: it.get("name", "") for it in items}
        bc1, bc2, bc3 = st.columns([3, 2, 2])
        with bc1:
            batch_codes = st.multiselect(
                "选择股票", list(name_map.keys()), key="batch_codes_input",
                format_func=lambda c: f"{name_map.get(c, '')}（{c}）")
        with bc2:
            batch_tags = st.text_input("标签（逗号分隔）", key="batch_tags_input",
                                       placeholder="如 白马,观察")
        with bc3:
            batch_mode = st.radio("方式", ["追加", "覆盖"], horizontal=True)
        if st.button("应用标签", key="batch_tag_apply"):
            new_tags = [t.strip() for t in batch_tags.split(",") if t.strip()]
            if not batch_codes or not new_tags:
                st.warning("请先选择股票并填写标签")
            else:
                for c in batch_codes:
                    if batch_mode == "追加":
                        cur = _parse_tags(next(it for it in items if it["code"] == c))
                        merged = ",".join(dict.fromkeys(cur + new_tags))
                    else:
                        merged = ",".join(new_tags)
                    dm.storage.update_watchlist_tags(c, merged)
                st.session_state["_batch_clear_pending"] = True
                st.session_state["watchlist_nav_msg"] = f"已{batch_mode}标签到 {len(batch_codes)} 只股票"
                st.rerun()

    # ── 价格提醒（本地最新收盘越线触发，非盘中实时）────────────
    with st.expander("🔔 价格提醒"):
        alerts = dm.storage.get_price_alerts(active_only=True)
        ac1, ac2, ac3, ac4 = st.columns([3, 2, 2, 1])
        with ac1:
            alert_code = st.selectbox(
                "股票", list(name_map.keys()), key="alert_code",
                format_func=lambda c: f"{name_map.get(c, '')}（{c}）")
        with ac2:
            alert_dir = st.radio("方向", ["above", "below"], horizontal=True,
                                 format_func=lambda d: "≥ 目标价" if d == "above" else "≤ 目标价")
        with ac3:
            _bars = dm.storage.get_daily_bars(alert_code)
            _last = float(_bars.iloc[-1]["close"]) if not _bars.empty else 10.0
            alert_price = st.number_input("目标价", min_value=0.01,
                                          value=round(_last, 2), step=0.01)
        with ac4:
            st.write("")
            if st.button("添加", key="alert_add"):
                dm.storage.add_price_alert(alert_code, name_map.get(alert_code, ""),
                                           alert_dir, alert_price)
                st.rerun()
        if alerts:
            for a in alerts:
                row1, row2 = st.columns([6, 1])
                sym = "≥" if a["direction"] == "above" else "≤"
                row1.caption(f"{a['name']}（{a['code']}） {sym} {a['target_price']:.2f}"
                             f" ｜ 创建于 {str(a['created_at'])[:16]}")
                if row2.button("删除", key=f"alert_del_{a['id']}"):
                    dm.storage.deactivate_price_alert(a["id"])
                    st.rerun()
        else:
            st.caption("暂无生效中的提醒。触发基于本地最新收盘价（dashboard 打开时检查），非盘中实时。")
        fired = [a for a in dm.storage.get_price_alerts(active_only=False)
                 if a.get("triggered_at")][:5]
        if fired:
            st.caption("最近已触发：" + "；".join(
                f"{a['name']} {str(a['triggered_at'])[:16]}" for a in fired))

    # 筛选
    if selected_tags:
        wanted = set(selected_tags)
        items = [it for it in items if wanted & set(_parse_tags(it))]

    # 排序
    if sort_by.startswith("评分"):
        items.sort(key=lambda x: x.get("score") or 0, reverse=True)
    elif sort_by.startswith("添加时间"):
        items.sort(key=lambda x: x.get("added_at") or "", reverse=True)
    else:
        items.sort(key=lambda x: x.get("code") or "")

    st.caption(f"显示 {len(items)} 只")
    # 批量预取最新收盘价（一次连接循环主键查询，33 只约毫秒级）
    try:
        closes = dm.storage.get_latest_closes([it["code"] for it in items])
    except Exception:
        closes = {}
    for item in items:
        _render_item(dm, item, latest=closes.get(item["code"]))
