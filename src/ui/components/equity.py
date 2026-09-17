"""净值曲线组件：总览仪表盘与模拟交易页共用。

数据底座是 ``equity_snapshots`` 表（``TradingSimulator.snapshot_equity`` 写入）：
- 每应用会话每日自动补记一次当日快照（避免每次页面刷新都打行情接口）；
- 快照不足 2 个点时给出引导，而不是画一根没有信息量的直线。
"""
import logging
from datetime import date

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from src.ui.core import COLOR_DOWN, COLOR_UP, get_simulator

logger = logging.getLogger(__name__)


def _maybe_snapshot_today(simulator) -> None:
    """每会话每日最多自动补记一次当日净值快照（实时行情是一次网络调用）。"""
    if st.session_state.get("_equity_snapshotted") == date.today().isoformat():
        return
    try:
        if simulator.snapshot_equity():
            st.session_state["_equity_snapshotted"] = date.today().isoformat()
    except Exception as e:  # 快照是观测性数据，绝不能阻断页面
        logger.warning(f"自动净值快照失败: {e}")


def render_equity_curve(simulator=None, key_prefix: str = "eq") -> None:
    """渲染净值 + 回撤双联图；数据不足时渲染引导按钮。"""
    sim = simulator or get_simulator()
    storage = sim.portfolio.storage
    df = storage.get_equity_snapshots()
    today = date.today().isoformat()

    if not df.empty and df.iloc[-1]["date"] < today:
        _maybe_snapshot_today(sim)
        df = storage.get_equity_snapshots()

    if len(df) < 2:
        st.info("📈 净值曲线需要至少 2 个快照点。"
                "点击下方按钮记录今日净值，之后每日打开本页会自动补记。")
        if st.button("📸 记录今日净值", key=f"{key_prefix}_snap"):
            with st.spinner("正在计算净值…"):
                snap = sim.snapshot_equity()
            if snap:
                st.success(f"已记录：总资产 ¥{snap['total_value']:,.0f}")
                st.rerun()
            else:
                st.warning("记录失败（行情源不可用？），详见日志。")
        return

    # 收益率以账户初始资金为基准；回撤由 cummax 现算，不落库
    initial = (storage.get_account() or {}).get("initial_cash") or df.iloc[0]["total_value"]
    df = df.copy()
    df["return_pct"] = (df["total_value"] / initial - 1) * 100
    df["drawdown_pct"] = (df["total_value"] / df["total_value"].cummax() - 1) * 100

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.72, 0.28], vertical_spacing=0.04,
                        subplot_titles=("组合净值", "回撤"))
    fig.add_trace(go.Scatter(x=df["date"], y=df["total_value"],
                             mode="lines", name="总资产",
                             line=dict(color=COLOR_UP, width=2),
                             hovertemplate="%{x}<br>¥%{y:,.0f}<extra></extra>"),
                  row=1, col=1)
    fig.add_trace(go.Scatter(x=df["date"], y=df["drawdown_pct"],
                             mode="lines", name="回撤", fill="tozeroy",
                             line=dict(color=COLOR_DOWN, width=1),
                             hovertemplate="%{x}<br>%{y:.2f}%<extra></extra>"),
                  row=2, col=1)
    fig.update_layout(height=380, margin=dict(l=10, r=10, t=40, b=10),
                      showlegend=False, hovermode="x unified")
    fig.update_yaxes(tickprefix="¥", row=1, col=1)
    fig.update_yaxes(ticksuffix="%", row=2, col=1)
    st.plotly_chart(fig, use_container_width=True)

    last = df.iloc[-1]
    c1, c2, c3 = st.columns(3)
    c1.metric("累计收益率", f"{last['return_pct']:+.2f}%")
    c2.metric("当前回撤", f"{last['drawdown_pct']:.2f}%")
    c3.metric("最大回撤", f"{df['drawdown_pct'].min():.2f}%")
