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
    """自动交易模式：策略信号 + AI分析 + 风控 → 自动下单。"""
    from src.trading.auto_trader import AutoTrader, RiskParams
    from src.analysis.advisor import Advisor
    from src.config import get_config
    from src.strategy.screener import STRATEGY_REGISTRY

    st.subheader("自动交易模式")
    st.info("自动交易流程：策略筛选 → AI分析验证 → 风控过滤 → 自动下单。点击「开始自动交易」执行一轮扫描。")

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
    strategy_options = {k: v.description if hasattr(v, "description") else k
                        for k, v in STRATEGY_REGISTRY.items()
                        if hasattr(v(), "supports_evaluate") and v().supports_evaluate()}
    selected_strategies = st.multiselect(
        "选择策略（至少1个）",
        list(strategy_options.keys()),
        default=["ma_cross", "macd_golden"],
        format_func=lambda x: strategy_options.get(x, x),
        key="at_strategies"
    )

    # ── 风控参数 ──
    with st.expander("⚙️ 风控参数配置", expanded=False):
        col1, col2 = st.columns(2)
        with col1:
            max_pos_pct = st.slider("单股最大仓位 (%)", 5, 50, 20, key="at_max_pos")
            max_total_pct = st.slider("总仓位上限 (%)", 20, 100, 80, key="at_max_total")
            max_daily = st.slider("单日最大交易次数", 1, 20, 5, key="at_max_daily")
            min_confidence = st.slider("AI最低置信度", 30, 90, 60, key="at_min_conf")
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
        min_ai_confidence=float(min_confidence),
    )

    # ── 执行按钮 ──
    can_run = bool(stock_pool) and bool(selected_strategies)
    if not can_run:
        st.warning("请配置股票池和至少一个策略")

    if st.button("🚀 开始自动交易", type="primary", disabled=not can_run):
        advisor = Advisor(cfg, dm)
        auto_trader = AutoTrader(dm, simulator, advisor, risk)

        progress_placeholder = st.empty()
        log_lines = []

        def on_status(phase: str, detail: str = ""):
            log_lines.append(f"**{phase}**：{detail}" if detail else f"**{phase}**")
            progress_placeholder.markdown("\n\n".join(log_lines[-6:]))

        with st.spinner("自动交易执行中..."):
            try:
                report = auto_trader.run(stock_pool, selected_strategies, on_status)
                st.session_state["auto_trade_report"] = report
                st.success(f"执行完毕：共 {report.total_executed} 笔成交")
            except Exception as e:
                st.error(f"自动交易失败：{e}")

    # ── 执行报告 ──
    report = st.session_state.get("auto_trade_report")
    if report:
        st.markdown("---")
        st.markdown("#### 执行报告")
        st.caption(f"执行时间：{report.timestamp[:19]}")

        if report.paused_reason:
            st.warning(f"⚠️ {report.paused_reason}")

        # 汇总指标
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("策略候选", len(report.strategy_candidates))
        c2.metric("AI通过", len(report.ai_recommendations))
        c3.metric("风控拦截", len(report.risk_blocked))
        c4.metric("实际成交", len(report.executed_orders))

        # 止盈止损
        if report.stop_loss_sells or report.take_profit_sells:
            st.markdown("**止盈止损执行：**")
            for item in report.stop_loss_sells:
                res = item["result"]
                icon = "✅" if res.get("success") else "❌"
                st.write(f"{icon} 止损卖出 {item['name']}（{item['code']}）— {item['reason']}")
            for item in report.take_profit_sells:
                res = item["result"]
                icon = "✅" if res.get("success") else "❌"
                st.write(f"{icon} 止盈卖出 {item['name']}（{item['code']}）— {item['reason']}")

        # 买入成交明细
        if report.executed_orders:
            st.markdown("**买入成交明细：**")
            rows = []
            for o in report.executed_orders:
                res = o["result"]
                rows.append({
                    "代码": o["code"], "名称": o["name"],
                    "数量": o["quantity"], "价格": o["price"],
                    "策略": "/".join(o.get("strategies", [])),
                    "AI置信度": f"{o.get('confidence', 0):.0f}%",
                    "状态": "✅ 成功" if res.get("success") else f"❌ {res.get('msg', '')}",
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

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

