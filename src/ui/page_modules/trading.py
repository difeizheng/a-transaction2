"""模拟交易

账户/持仓/下单/订单 + 净值曲线（与总览页共用 equity 组件）。
T+1 日终处理由 TradingSimulator 构造时自动完成（跨交易日解锁），
页面上不再提供手动按钮，只显示状态说明。
"""
from datetime import date

import pandas as pd
import streamlit as st

from src.ui.components.equity import render_equity_curve
from src.ui.components.service_info import get_akshare_info, get_llm_info, render_service_info
from src.ui.core import enrich_profit, get_config, get_dm, get_simulator


def render():
    simulator = get_simulator()

    st.header("💼 模拟交易")

    tab1, tab2, tab3, tab4 = st.tabs(["📈 净值走势", "💰 账户与持仓", "📋 订单记录", "🤖 自动交易决策"])

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

        # ── 组合风控（展示型：阈值与 AutoTrader RiskParams 默认一致）──
        if summary and summary.get("total_value"):
            from src.trading.auto_trader import RiskParams
            from src.trading.risk import compute_drawdown_pct
            rp = RiskParams()
            total_v = summary["total_value"]
            pos_pct = summary["market_value"] / total_v * 100
            try:
                snaps = simulator.dm.storage.get_equity_snapshots()
                peak = max([total_v] + snaps["total_value"].tolist()) if not snaps.empty else total_v
            except Exception:
                peak = total_v
            dd = compute_drawdown_pct(peak, total_v)
            try:
                _pos_for_risk = simulator.get_positions_df()
            except Exception:
                _pos_for_risk = pd.DataFrame()
            max_single = (_pos_for_risk["market_value"].max() / total_v * 100
                          if not _pos_for_risk.empty else 0.0)
            over_sl = (_pos_for_risk[_pos_for_risk["profit_pct"] <= -rp.stop_loss_pct]
                       if not _pos_for_risk.empty else pd.DataFrame())

            st.subheader("🛡️ 组合风控")
            r1, r2, r3 = st.columns(3)
            r1.metric("总仓位", f"{pos_pct:.1f}%", f"上限 {rp.max_total_position_pct:.0f}%",
                      delta_color="inverse" if pos_pct > rp.max_total_position_pct else "off")
            r2.metric("单票最大占比", f"{max_single:.1f}%", f"上限 {rp.max_position_pct:.0f}%",
                      delta_color="inverse" if max_single > rp.max_position_pct else "off")
            r3.metric("当前回撤", f"{dd:.2f}%", f"熔断线 {rp.max_drawdown_pct:.0f}%",
                      delta_color="inverse" if dd >= rp.max_drawdown_pct else "off")
            if pos_pct > rp.max_total_position_pct:
                st.warning(f"⚠️ 总仓位 {pos_pct:.1f}% 超过上限 {rp.max_total_position_pct:.0f}%")
            if max_single > rp.max_position_pct:
                st.warning(f"⚠️ 单票集中度 {max_single:.1f}% 超过上限 {rp.max_position_pct:.0f}%")
            if dd >= rp.max_drawdown_pct:
                st.error(f"🚨 回撤 {dd:.2f}% 已达熔断线（AutoTrader 将暂停开仓）")
            if not over_sl.empty:
                names = "、".join(f"{r['name']}({r['profit_pct']:+.1f}%)" for _, r in over_sl.iterrows())
                st.warning(f"⚠️ {len(over_sl)} 只持仓浮亏超止损线（-{rp.stop_loss_pct:.0f}%）：{names}")

        st.subheader("当前持仓")
        try:
            positions_df = simulator.get_positions_df()
        except Exception as e:
            st.warning(f"持仓加载失败：{e}")
            positions_df = None

        if positions_df is not None and not positions_df.empty:
            st.dataframe(positions_df, hide_index=True, use_container_width=True)
        else:
            st.caption("暂无持仓——可通过下方「手动下单」或自选股「💹 去交易」建仓。")

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

    # ── 自动交易决策（AutoTrader 运行报告，可审计回放）───────────
    with tab4:
        reports = get_dm().storage.get_execution_reports(limit=20)
        if not reports:
            st.info("尚无 AutoTrader 运行记录。")
            st.caption("自动交易由 AutoTrader 按调度执行（本 UI 只做手动单）；"
                       "每次运行会落库一份决策报告：候选 → AI 注释 → 风控拦截 → 建议/成交，"
                       "运行后即可在此回放「为什么买 / 为什么不买」。")
        for rec in reports:
            rep = rec.get("report") or {}
            head = (f"{str(rec.get('created_at',''))[:19]} ｜ {rec.get('mode','')} ｜ "
                    f"候选 {rec.get('n_candidates',0)} · 买建 {rec.get('n_buy_suggestions',0)} · "
                    f"卖建 {rec.get('n_sell_suggestions',0)} · 成交 {rec.get('n_executed',0)} · "
                    f"拦截 {rec.get('n_blocked',0)}")
            with st.expander(head):
                if rec.get("paused_reason"):
                    st.warning(f"⏸️ 暂停原因：{rec['paused_reason']}")
                if rec.get("drawdown_pct") is not None:
                    st.caption(f"当前回撤：{rec['drawdown_pct']:.2f}%"
                               + (f"（减仓档位 {rec['deescalation_tier']}）"
                                  if rec.get('deescalation_tier') is not None else ""))
                for title, key in [("🟢 买入建议", "buy_suggestions"),
                                   ("🔴 卖出建议", "sell_suggestions"),
                                   ("✅ 已成交", "executed_orders"),
                                   ("🚫 风控拦截", "risk_blocked"),
                                   ("🧠 AI 注释", "ai_recommendations")]:
                    items = rep.get(key) or []
                    if items:
                        st.markdown(f"**{title}（{len(items)}）**")
                        st.dataframe(pd.DataFrame(items), hide_index=True,
                                     use_container_width=True)
                if rep.get("errors"):
                    st.error("；".join(str(e) for e in rep["errors"]))


def render_sidebar():
    st.subheader("⚙️ 服务状态")
    render_service_info([get_akshare_info(), get_llm_info(get_config())])
    st.divider()
    st.caption("💡 模拟盘为纸上交易，价格取实时行情，含佣金与印花税")
