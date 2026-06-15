"""策略回测页面"""
import streamlit as st
import plotly.graph_objects as go
import pandas as pd
from src.data.manager import DataManager
from src.backtest.compare import BacktestComparator
from src.config import get_config
from src.ui.components.step_logger import StepLogger
from src.ui.components.service_info import render_service_info, get_akshare_info


@st.cache_resource
def get_dm():
    return DataManager()


STRATEGY_OPTIONS = {
    "ma_cross": "均线多头排列",
    "macd_golden": "MACD金叉",
    "kdj_oversold": "KDJ超卖反弹",
    "low_valuation": "低估值",
    "high_growth": "高成长",
    "multi_factor": "多因子模型",
}


def render_sidebar():
    st.subheader("回测参数")

    st.session_state["backtest_strategies"] = st.multiselect(
        "对比策略", list(STRATEGY_OPTIONS.keys()),
        default=["ma_cross", "multi_factor"],
        format_func=lambda x: STRATEGY_OPTIONS.get(x, x),
    )
    col1, col2 = st.columns(2)
    st.session_state["backtest_start"] = col1.date_input("开始日期", value=pd.Timestamp("2022-01-01"))
    st.session_state["backtest_end"] = col2.date_input("结束日期", value=pd.Timestamp("2024-12-31"))
    st.session_state["backtest_stop_loss"] = st.slider("止损比例(%)", 3, 20, 5) / 100
    st.session_state["backtest_take_profit"] = st.slider("止盈比例(%)", 5, 50, 15) / 100
    st.session_state["backtest_top_n"] = st.slider("每策略选股数量", 5, 30, 10)
    st.session_state["backtest_run"] = st.button("运行回测", type="primary", use_container_width=True)

    st.divider()
    render_service_info([get_akshare_info()])


def render():
    st.title("📉 策略回测")
    dm = get_dm()
    cfg = get_config()

    # 历史回测记录
    st.subheader("历史回测记录")
    history = dm.storage.get_backtest_results()
    if not history.empty:
        display_cols = [c for c in ["strategy_name", "start_date", "end_date", "total_return",
                                    "annual_return", "sharpe", "max_drawdown", "win_rate", "trades"]
                        if c in history.columns]
        rename = {
            "strategy_name": "策略", "start_date": "开始", "end_date": "结束",
            "total_return": "总收益(%)", "annual_return": "年化(%)",
            "sharpe": "夏普", "max_drawdown": "最大回撤(%)",
            "win_rate": "胜率(%)", "trades": "交易次数",
        }
        st.dataframe(history[display_cols].rename(columns=rename),
                     use_container_width=True, hide_index=True)

    if not st.session_state.get("backtest_run"):
        st.info("在左侧配置回测参数后点击「运行回测」")
        return

    selected = st.session_state.get("backtest_strategies", [])
    if not selected:
        st.warning("请至少选择一个策略")
        return

    start_date = str(st.session_state.get("backtest_start", "2022-01-01"))
    end_date = str(st.session_state.get("backtest_end", "2024-12-31"))
    stop_loss = st.session_state.get("backtest_stop_loss", 0.05)
    take_profit = st.session_state.get("backtest_take_profit", 0.15)
    top_n = st.session_state.get("backtest_top_n", 10)

    logger = StepLogger(f"策略回测（{len(selected)} 个策略）")
    logger.step("初始化回测引擎")

    try:
        comparator = BacktestComparator(cfg, dm)
        cb = logger.make_backtest_callback(STRATEGY_OPTIONS, selected)
        result_df = comparator.compare(
            strategy_names=selected,
            start_date=start_date,
            end_date=end_date,
            top_n=top_n,
            stop_loss=stop_loss,
            take_profit=take_profit,
            status_callback=cb,
        )
        logger.complete("回测完成")
    except Exception as e:
        logger.fail(f"回测失败：{e}")
        st.error(str(e))
        return

    if result_df.empty:
        st.error("回测失败，请检查数据是否完整")
        return

    st.success("回测完成")
    formatted = BacktestComparator.format_comparison(result_df)
    st.dataframe(formatted, use_container_width=True, hide_index=True)

    if "总收益(%)" in formatted.columns and "策略" in formatted.columns:
        fig = go.Figure()
        for m in ["总收益(%)", "年化收益(%)", "夏普比率"]:
            if m in formatted.columns:
                fig.add_trace(go.Bar(name=m, x=formatted["策略"], y=formatted[m]))
        fig.update_layout(barmode="group", title="策略回测对比",
                          xaxis_title="策略", yaxis_title="数值")
        st.plotly_chart(fig, use_container_width=True)
