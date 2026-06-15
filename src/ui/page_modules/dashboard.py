"""总览仪表盘页面"""
import streamlit as st
import plotly.graph_objects as go
import pandas as pd
from src.data.manager import DataManager
from src.trading.simulator import TradingSimulator


@st.cache_resource
def get_dm():
    return DataManager()


def render():
    st.title("📊 总览仪表盘")
    dm = get_dm()
    simulator = TradingSimulator(dm)

    summary = simulator.get_portfolio_summary()
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("总资产", f"¥{summary['total_value']:,.2f}")
    col2.metric("可用资金", f"¥{summary['cash']:,.2f}")
    col3.metric("持仓市值", f"¥{summary['market_value']:,.2f}")
    col4.metric("持仓数量", f"{summary['position_count']} 只")

    st.divider()

    col_left, col_right = st.columns([2, 1])

    with col_left:
        st.subheader("持仓概览")
        pos_df = simulator.get_positions_df()
        if pos_df.empty:
            st.info("暂无持仓，前往「模拟交易」页面买入股票")
        else:
            display_cols = ["code", "name", "quantity", "available", "cost_price",
                            "current_price", "market_value", "profit_loss", "profit_pct"]
            display_cols = [c for c in display_cols if c in pos_df.columns]
            rename = {
                "code": "代码", "name": "名称", "quantity": "持仓量",
                "available": "可卖量", "cost_price": "成本价",
                "current_price": "现价", "market_value": "市值",
                "profit_loss": "浮动盈亏", "profit_pct": "涨跌幅(%)"
            }
            st.dataframe(
                pos_df[display_cols].rename(columns=rename),
                use_container_width=True,
                hide_index=True,
            )

    with col_right:
        st.subheader("最近回测结果")
        bt_df = dm.storage.get_backtest_results()
        if bt_df.empty:
            st.info("暂无回测记录")
        else:
            recent = bt_df.head(5)[["strategy_name", "total_return", "sharpe", "max_drawdown"]]
            recent.columns = ["策略", "总收益(%)", "夏普", "最大回撤(%)"]
            st.dataframe(recent, use_container_width=True, hide_index=True)

    # 最近交易记录
    st.subheader("最近交易记录")
    orders_df = dm.storage.get_orders()
    if orders_df.empty:
        st.info("暂无交易记录")
    else:
        show_cols = ["code", "name", "direction", "price", "quantity", "amount", "fill_date"]
        show_cols = [c for c in show_cols if c in orders_df.columns]
        rename = {"code": "代码", "name": "名称", "direction": "方向",
                  "price": "价格", "quantity": "数量", "amount": "金额", "fill_date": "成交日期"}
        st.dataframe(
            orders_df.head(20)[show_cols].rename(columns=rename),
            use_container_width=True, hide_index=True
        )
