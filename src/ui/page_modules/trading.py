"""模拟交易

账户/持仓/下单/订单 + 净值曲线（与总览页共用 equity 组件）。
T+1 日终处理由 TradingSimulator 构造时自动完成（跨交易日解锁），
页面上不再提供手动按钮，只显示状态说明。
"""
from datetime import date

import streamlit as st

from src.ui.components.equity import render_equity_curve
from src.ui.components.service_info import get_akshare_info, get_llm_info, render_service_info
from src.ui.core import enrich_profit, get_config, get_dm, get_simulator


def render():
    simulator = get_simulator()

    st.header("💼 模拟交易")

    tab1, tab2, tab3 = st.tabs(["📈 净值走势", "💰 账户与持仓", "📋 订单记录"])

    # ── 净值走势 ────────────────────────────────────────────────
    with tab1:
        render_equity_curve(simulator, key_prefix="trading")

    # ── 账户与持仓 ──────────────────────────────────────────────
    with tab2:
        try:
            summary = enrich_profit(simulator.dm.storage, simulator.get_portfolio_summary())
        except Exception as e:
            st.warning(f"账户数据加载失败（行情源可能不可用）：{e}")
            summary = None

        if summary:
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("总资产", f"¥{summary['total_value']:,.0f}")
            c2.metric("可用现金", f"¥{summary['cash']:,.0f}")
            c3.metric("持仓市值", f"¥{summary['market_value']:,.0f}")
            c4.metric("总盈亏", f"¥{summary['total_profit']:+,.0f}",
                      f"{summary['total_profit_pct']:+.2f}%")

        st.subheader("当前持仓")
        try:
            positions_df = simulator.get_positions_df()
        except Exception as e:
            st.warning(f"持仓加载失败：{e}")
            positions_df = None

        if positions_df is not None and not positions_df.empty:
            st.dataframe(positions_df, hide_index=True, use_container_width=True)
        else:
            st.caption("暂无持仓")

        # 手动下单（人工断点：自动交易由 AutoTrader 调度执行，UI 只做手动单）
        with st.expander("📝 手动下单"):
            # 联动入口：自选股页「去交易」按钮会预填 buy_code；此处消费
            if "buy_code" in st.session_state:
                st.session_state["manual_code"] = st.session_state.pop("buy_code")
                st.session_state.pop("buy_name", None)

            # 快速选择：自选股 + 当前持仓，选中即填入代码
            quick: dict = {}
            try:
                for it in get_dm().storage.get_watchlist():
                    quick[it["code"]] = (it.get("name", "") or it["code"], "⭐")
            except Exception:
                pass
            if positions_df is not None and not positions_df.empty:
                for _, row in positions_df.iterrows():
                    quick.setdefault(str(row["code"]),
                                     (row.get("name", "") or str(row["code"]), "💼"))

            def _on_quick_pick():
                picked = st.session_state.get("quick_pick")
                if picked:
                    st.session_state["manual_code"] = picked

            if quick:
                st.selectbox(
                    "从自选股/持仓快速选择", [""] + list(quick.keys()),
                    format_func=lambda x: f"{quick[x][1]} {quick[x][0]}（{x}）" if x else "—— 或手动输入 ——",
                    key="quick_pick", on_change=_on_quick_pick)

            col1, col2, col3 = st.columns(3)
            with col1:
                code = st.text_input("股票代码", placeholder="600519", key="manual_code")
                action = st.selectbox("方向", ["buy", "sell"])
            with col2:
                quantity = st.number_input("数量（股）", min_value=100, step=100, value=100)
                price = st.number_input("价格（0=市价，取实时报价）", min_value=0.0, value=0.0)
            with col3:
                st.write("")
                st.write("")
                if st.button("提交订单", type="primary"):
                    if code:
                        try:
                            p = price if price > 0 else None
                            name = quick.get(code, (code, ""))[0]
                            if action == "buy":
                                result = simulator.place_buy(code, name, int(quantity), p)
                            else:
                                result = simulator.place_sell(code, int(quantity), p)
                            if result.get("success"):
                                st.success(result.get("msg", "成交"))
                                st.rerun()
                            else:
                                st.error(result.get("msg", "下单失败"))
                        except Exception as e:
                            st.error(f"下单失败：{e}")
                    else:
                        st.warning("请输入股票代码")

    # ── 订单记录 ────────────────────────────────────────────────
    with tab3:
        orders_df = simulator.get_orders_df()
        if not orders_df.empty:
            st.dataframe(orders_df, hide_index=True, use_container_width=True)
        else:
            st.caption("暂无订单记录")

        st.divider()
        st.caption(
            f"✅ T+1 解锁：已随系统启动自动处理（今日 {date.today().isoformat()}）。"
            "跨交易日打开应用时，昨日前买入的持仓自动转为可用。"
        )


def render_sidebar():
    st.subheader("⚙️ 服务状态")
    render_service_info([get_akshare_info(), get_llm_info(get_config())])
    st.divider()
    st.caption("💡 模拟盘为纸上交易，价格取实时行情，含佣金与印花税")
