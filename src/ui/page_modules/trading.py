"""模拟交易页面"""
import streamlit as st
import pandas as pd
from src.data.manager import DataManager
from src.trading.simulator import TradingSimulator
from src.ui.components.service_info import render_service_info, get_akshare_info


@st.cache_resource
def get_dm():
    return DataManager()


def render_sidebar():
    st.subheader("账户概览")
    dm = get_dm()
    simulator = TradingSimulator(dm)
    try:
        summary = simulator.get_portfolio_summary()
        st.metric("总资产", f"¥{summary['total_value']:,.2f}")
        st.metric("可用资金", f"¥{summary['cash']:,.2f}")
        st.metric("持仓市值", f"¥{summary['market_value']:,.2f}")
    except Exception:
        st.caption("账户数据加载失败")

    st.divider()
    render_service_info([get_akshare_info()])


def render():
    st.title("💹 模拟交易")
    dm = get_dm()
    simulator = TradingSimulator(dm)

    tab1, tab2, tab3, tab4 = st.tabs(["持仓管理", "下单交易", "交易记录", "自动交易"])

    with tab1:
        _render_positions(simulator)

    with tab2:
        _render_manual_order(simulator)

    with tab3:
        _render_order_history(simulator)

    with tab4:
        _render_auto_trading(dm, simulator)


def _render_positions(simulator):
    st.subheader("当前持仓")
    if st.button("刷新行情"):
        try:
            simulator.refresh_positions()
            st.success("行情已刷新")
        except Exception as e:
            st.error(f"刷新失败：{e}")

    try:
        summary = simulator.get_portfolio_summary()
        col1, col2, col3 = st.columns(3)
        col1.metric("总资产", f"¥{summary['total_value']:,.2f}")
        col2.metric("可用资金", f"¥{summary['cash']:,.2f}")
        col3.metric("持仓市值", f"¥{summary['market_value']:,.2f}")
    except Exception as e:
        st.error(f"账户数据加载失败：{e}")

    try:
        pos_df = simulator.get_positions_df()
        if pos_df.empty:
            st.info("暂无持仓")
        else:
            display = pos_df[["code", "name", "quantity", "available", "cost_price",
                               "current_price", "profit_loss", "profit_pct"]].copy()
            display.columns = ["代码", "名称", "持仓量", "可卖量", "成本价", "现价", "浮动盈亏", "涨跌幅(%)"]
            st.dataframe(display, use_container_width=True, hide_index=True)
    except Exception as e:
        st.error(f"持仓数据加载失败：{e}")

    if st.button("日终处理（更新T+1可卖数量）"):
        try:
            simulator.end_of_day()
            st.success("日终处理完成")
        except Exception as e:
            st.error(f"日终处理失败：{e}")


def _render_manual_order(simulator):
    st.subheader("下单")
    col1, col2 = st.columns(2)
    with col1:
        st.write("**买入**")
        buy_code = st.text_input("股票代码", key="buy_code", placeholder="如 000001")
        buy_name = st.text_input("股票名称", key="buy_name", placeholder="如 平安银行")
        buy_qty = st.number_input("买入数量（股）", min_value=100, step=100, value=100, key="buy_qty")
        buy_price = st.number_input("委托价格（0=市价）", min_value=0.0, step=0.01, value=0.0, key="buy_price")
        if st.button("确认买入", type="primary"):
            if not buy_code:
                st.error("请输入股票代码")
            elif buy_qty % 100 != 0:
                # 杜绝静默截断：portfolio.buy 会把 150→100 而不提示，下单前显式拦截
                st.error(f"买入数量须为 100 的整数倍（1手=100股），当前 {buy_qty} 股")
            else:
                try:
                    price = buy_price if buy_price > 0 else None
                    result = simulator.place_buy(buy_code, buy_name or buy_code, buy_qty, price)
                    if result["success"]:
                        order = result["order"]
                        st.success(f"买入成功：{order['quantity']}股 @{order['price']:.2f}，手续费{order['commission']:.2f}")
                    else:
                        st.error(result["msg"])
                except Exception as e:
                    st.error(f"买入失败：{e}")

    with col2:
        st.write("**卖出**")
        try:
            pos_df2 = simulator.get_positions_df()
            if pos_df2.empty:
                st.info("暂无持仓可卖")
            else:
                sell_options = {
                    row["code"]: f"{row['name']}({row['code']}) 可卖{row['available']}股"
                    for _, row in pos_df2.iterrows()
                }
                sell_code = st.selectbox("选择持仓", list(sell_options.keys()),
                                         format_func=lambda x: sell_options[x], key="sell_code")
                sell_qty = st.number_input("卖出数量（股）", min_value=100, step=100, value=100, key="sell_qty")
                sell_price = st.number_input("委托价格（0=市价）", min_value=0.0, step=0.01, value=0.0, key="sell_price")
                if st.button("确认卖出", type="primary"):
                    if sell_qty % 100 != 0:
                        st.error(f"卖出数量须为 100 的整数倍，当前 {sell_qty} 股")
                    else:
                        try:
                            price = sell_price if sell_price > 0 else None
                            result = simulator.place_sell(sell_code, sell_qty, price)
                            if result["success"]:
                                order = result["order"]
                                st.success(f"卖出成功：{order['quantity']}股 @{order['price']:.2f}")
                            else:
                                st.error(result["msg"])
                        except Exception as e:
                            st.error(f"卖出失败：{e}")
        except Exception as e:
            st.error(f"持仓数据加载失败：{e}")

    analyses = st.session_state.get("analyses", [])
    if analyses:
        st.divider()
        st.write("**来自AI分析的建议**")
        for a in analyses:
            if a.get("buy_suggestion") == "建议买入":
                st.write(f"- {a['name']}({a['code']}): {a.get('analysis', '')[:80]}...")


def _render_order_history(simulator):
    st.subheader("交易记录")
    try:
        orders_df = simulator.get_orders_df()
        if orders_df.empty:
            st.info("暂无交易记录")
        else:
            show_cols = [c for c in ["fill_date", "code", "name", "direction",
                                      "price", "quantity", "amount", "commission"]
                         if c in orders_df.columns]
            rename = {"fill_date": "日期", "code": "代码", "name": "名称",
                      "direction": "方向", "price": "价格", "quantity": "数量",
                      "amount": "金额", "commission": "手续费"}
            st.dataframe(orders_df[show_cols].rename(columns=rename),
                         use_container_width=True, hide_index=True)
    except Exception as e:
        st.error(f"交易记录加载失败：{e}")


def _render_auto_trading(dm, simulator):
    """信号生成器模式（方案 B）：策略筛选 + 风控预算 → 交易**建议**，不自动下单。"""
    from src.trading.auto_trader import AutoTrader, RiskParams
    from src.analysis.advisor import Advisor
    from src.config import get_config
    from src.strategy.screener import STRATEGY_REGISTRY

    st.subheader("信号生成器（方案 B）")
    st.warning(
        "⚠️ **信号生成模式**：本模块跑完「止盈止损扫描 + 策略筛选 + 风控预算」后产出"
        "**交易建议**，**不会自动下单**。请审阅后手动执行。AI 分析仅作参考注释，"
        "不再作为买卖决策门。"
    )

    cfg = get_config()

    # ── 股票池配置 ──
    st.markdown("**股票池**")
    pool_source = st.radio("来源", ["自选股", "手动输入"], horizontal=True, key="at_pool_source")

    stock_pool = []
    if pool_source == "自选股":
        watchlist = dm.storage.get_watchlist()
        if watchlist:
            stock_pool = [{"code": w["code"], "name": w.get("name", w["code"])} for w in watchlist]
            st.caption(f"自选股：{len(stock_pool)} 只")
        else:
            st.warning("自选股为空，请先添加股票")
    else:
        codes_input = st.text_area("股票代码（每行一个，或逗号分隔）", key="at_codes",
                                   placeholder="000001\n600519\n000858")
        if codes_input:
            codes = [c.strip() for c in codes_input.replace(",", "\n").splitlines() if c.strip()]
            stock_pool = [{"code": c, "name": c} for c in codes]
            st.caption(f"已输入 {len(stock_pool)} 只")

    # ── 策略选择 ──
    st.markdown("**策略选择**")
    # 排除 is_deprecated 的失效策略（kdj/boll，A股机构化后失效，见审计报告 P1-D）
    strategy_options = {k: v.description if hasattr(v, "description") else k
                        for k, v in STRATEGY_REGISTRY.items()
                        if not getattr(v, "is_deprecated", False)
                        and hasattr(v(), "supports_evaluate") and v().supports_evaluate()}
    selected_strategies = st.multiselect(
        "选择策略（至少1个）",
        list(strategy_options.keys()),
        default=["ma_cross", "macd_golden", "small_cap"],
        format_func=lambda x: strategy_options.get(x, x),
        key="at_strategies"
    )

    # ── 风控参数（仅用于建议的预算计算，非自动下单）──
    with st.expander("⚙️ 风控参数配置（用于建议的仓位预算）", expanded=False):
        col1, col2 = st.columns(2)
        with col1:
            max_pos_pct = st.slider("单股最大仓位 (%)", 5, 50, 20, key="at_max_pos")
            max_total_pct = st.slider("总仓位上限 (%)", 20, 100, 80, key="at_max_total")
            max_daily = st.slider("单日最大交易次数", 1, 20, 5, key="at_max_daily")
        with col2:
            stop_loss = st.slider("止损线 (%)", 3, 20, 8, key="at_stop_loss")
            take_profit = st.slider("止盈线 (%)", 5, 50, 20, key="at_take_profit")
            max_drawdown = st.slider("最大回撤暂停线 (%)", 5, 30, 15, key="at_max_dd")

    risk = RiskParams(
        max_position_pct=float(max_pos_pct),
        max_total_position_pct=float(max_total_pct),
        max_daily_trades=int(max_daily),
        stop_loss_pct=float(stop_loss),
        take_profit_pct=float(take_profit),
        max_drawdown_pct=float(max_drawdown),
    )

    # ── 执行按钮 ──
    can_run = bool(stock_pool) and bool(selected_strategies)
    if not can_run:
        st.warning("请配置股票池和至少一个策略")

    if st.button("🚀 生成交易信号", type="primary", disabled=not can_run):
        advisor = Advisor(cfg, dm)
        # auto_execute=False（默认）= 信号生成器，不下单
        auto_trader = AutoTrader(dm, simulator, advisor, risk)

        progress_placeholder = st.empty()
        log_lines = []

        def on_status(phase: str, detail: str = ""):
            log_lines.append(f"**{phase}**：{detail}" if detail else f"**{phase}**")
            progress_placeholder.markdown("\n\n".join(log_lines[-6:]))

        with st.spinner("生成交易信号中..."):
            try:
                report = auto_trader.run(stock_pool, selected_strategies, on_status)
                st.session_state["auto_trade_report"] = report
                st.success(
                    f"生成完毕：{len(report.buy_suggestions)} 条买入建议、"
                    f"{len(report.sell_suggestions)} 条卖出建议（未自动下单）"
                )
            except Exception as e:
                st.error(f"信号生成失败：{e}")

    # ── 建议报告 ──
    report = st.session_state.get("auto_trade_report")
    if report:
        st.markdown("---")
        st.markdown("#### 交易建议报告")
        st.caption(f"生成时间：{report.timestamp[:19]} · 模式：{report.mode}")

        if report.paused_reason:
            st.warning(f"⚠️ {report.paused_reason}")
        elif report.drawdown_pct is not None:
            st.caption(f"当前回撤 {report.drawdown_pct:.1f}%（high-water mark 口径）")

        # 汇总指标
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("策略候选", len(report.strategy_candidates))
        c2.metric("AI注释", len(report.ai_recommendations))
        c3.metric("买入建议", len(report.buy_suggestions))
        c4.metric("卖出建议", len(report.sell_suggestions))
        c5.metric("风控拦截", len(report.risk_blocked))

        # 卖出建议（止盈止损）
        if report.sell_suggestions:
            st.markdown("**卖出建议（止盈/止损）：**")
            for item in report.sell_suggestions:
                note = f" — {item['note']}" if item.get("note") else ""
                st.write(f"🔻 {item['name']}（{item['code']}）— {item['reason']}{note}")

        # 买入建议明细
        if report.buy_suggestions:
            st.markdown("**买入建议明细：**")
            rows = []
            for o in report.buy_suggestions:
                rows.append({
                    "代码": o["code"], "名称": o["name"],
                    "建议数量": o["quantity"], "现价": o["price"],
                    "建议金额": o.get("amount", round(o["quantity"] * o["price"], 2)),
                    "策略": "/".join(o.get("strategies", [])),
                    "综合得分": o.get("score", 0),
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            st.caption("以上为风控预算后的建议，**不会自动下单**，请自行决策。")

        # AI 注释（参考用）
        if report.ai_recommendations:
            with st.expander(f"AI 分析注释（{len(report.ai_recommendations)} 只，仅供参考，非决策门）"):
                for a in report.ai_recommendations:
                    conf = a.get("confidence", 0)
                    sug = a.get("suggestion", "")
                    analysis = (a.get("ai_analysis", {}) or {}).get("analysis", "")
                    st.write(
                        f"- {a['name']}（{a['code']}）：AI置信度 {conf:.0f}，建议「{sug}」"
                        + (f"— {analysis[:80]}..." if analysis else "")
                    )

        # 风控拦截
        if report.risk_blocked:
            with st.expander(f"风控拦截明细（{len(report.risk_blocked)} 条）"):
                for b in report.risk_blocked:
                    st.write(f"- {b['name']}（{b['code']}）：{b['reason']}")

        # 错误信息
        if report.errors:
            with st.expander(f"错误信息（{len(report.errors)} 条）"):
                for e in report.errors:
                    st.write(f"- {e}")

