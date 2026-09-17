"""策略回测页面

重构要点：
- 默认回测区间动态化（结束=今天，开始=3 年前），不再写死 2022~2024；
- 历史记录支持「交易明细下钻」（trades_detail 列，引擎 notify_trade 收集）；
- 任选两条历史记录并排对比（指标 delta）；
- 历史记录渲染移到结果之后，去掉原先 empty 占位双渲染的时序 hack。
"""
import json
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.backtest.compare import BacktestComparator
from src.backtest.metrics import (
    BENCHMARK_NAMES,
    build_conclusion,
    low_trade_warnings,
    pick_best_strategy,
)
from src.ui.components.service_info import get_akshare_info, render_service_info
from src.ui.components.step_logger import StepLogger
from src.ui.core import get_config, get_dm

STRATEGY_OPTIONS = {
    "ma_cross": "均线多头排列",
    "macd_golden": "MACD金叉",
    "kdj_oversold": "KDJ超卖反弹",
    "low_valuation": "低估值",
    "high_growth": "高成长",
    "multi_factor": "多因子模型",
}

_METRIC_ROWS = [
    ("total_return", "总收益(%)"), ("annual_return", "年化(%)"),
    ("sharpe", "夏普"), ("max_drawdown", "最大回撤(%)"),
    ("win_rate", "胜率(%)"), ("profit_loss_ratio", "盈亏比"),
    ("trades", "交易次数"),
]


def render_sidebar():
    st.subheader("回测参数")

    st.session_state["backtest_strategies"] = st.multiselect(
        "对比策略", list(STRATEGY_OPTIONS.keys()),
        default=["ma_cross", "multi_factor"],
        format_func=lambda x: STRATEGY_OPTIONS.get(x, x),
    )
    today = date.today()
    col1, col2 = st.columns(2)
    st.session_state["backtest_start"] = col1.date_input(
        "开始日期", value=today - timedelta(days=3 * 365))
    st.session_state["backtest_end"] = col2.date_input("结束日期", value=today)
    st.session_state["backtest_stop_loss"] = st.slider("止损比例(%)", 3, 20, 5) / 100
    st.session_state["backtest_take_profit"] = st.slider("止盈比例(%)", 5, 50, 15) / 100
    st.session_state["backtest_top_n"] = st.slider("每策略选股数量", 5, 30, 10)
    st.session_state["backtest_benchmark"] = st.selectbox(
        "基准指数",
        list(BENCHMARK_NAMES.keys()),
        index=0,
        format_func=lambda c: f"{BENCHMARK_NAMES.get(c, c)}（{c}）",
    )
    st.session_state["backtest_run"] = st.button("运行回测", type="primary", use_container_width=True)

    st.divider()
    render_service_info([get_akshare_info()])


def _equity_chart(bt, benchmark):
    """净值曲线：每策略一条 + 基准虚线。"""
    fig = go.Figure()
    for name, curve in bt.get("equity_curves", {}).items():
        dates = curve.get("dates", [])
        values = curve.get("values", [])
        if not dates:
            continue
        fig.add_trace(go.Scatter(
            x=dates, y=values, mode="lines",
            name=STRATEGY_OPTIONS.get(name, name),
        ))
    if benchmark is not None:
        bcurve = benchmark.get("curve", {})
        bdates, bvalues = bcurve.get("dates", []), bcurve.get("values", [])
        if bdates:
            fig.add_trace(go.Scatter(
                x=bdates, y=bvalues, mode="lines",
                name=f"{benchmark['name']}（基准·买入持有）",
                line=dict(dash="dash", color="gray", width=2),
            ))
    if len(fig.data) == 0:
        return None
    title_bench = f"基准：{benchmark['name']}" if benchmark else "无基准"
    fig.update_layout(
        title=f"净值曲线（{title_bench}）",
        xaxis_title="日期", yaxis_title="组合净值（元）",
        hovermode="x unified",
    )
    return fig


def _risk_return_scatter(rows):
    """风险-收益散点：x=年化收益，y=最大回撤。右下=高收益低回撤=优。"""
    valid = [r for r in rows if "error" not in r]
    if not valid:
        return None
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=[r.get("annual_return", 0) for r in valid],
        y=[r.get("max_drawdown", 0) for r in valid],
        mode="markers+text",
        text=[STRATEGY_OPTIONS.get(r["strategy_name"], r["strategy_name"]) for r in valid],
        textposition="top center",
        marker=dict(size=14, color="#1f77b4"),
        hovertemplate="年化: %{x:.2f}%<br>最大回撤: %{y:.2f}%<extra></extra>",
    ))
    fig.update_layout(
        title="风险-收益分布（右下 = 高收益 · 低回撤 = 优）",
        xaxis_title="年化收益(%)", yaxis_title="最大回撤(%)",
    )
    return fig


def _record_label(row) -> str:
    name = STRATEGY_OPTIONS.get(row["strategy_name"], row["strategy_name"])
    return (f"#{row['id']} {name} | {row['start_date']}~{row['end_date']} "
            f"| 收益 {row['total_return']}% | {str(row.get('created_at', ''))[:16]}")


def _render_trade_drilldown(history: pd.DataFrame) -> None:
    """交易明细下钻：选一条历史记录，展开逐笔平仓交易。"""
    st.markdown("**🔍 交易明细下钻**")
    options = { _record_label(r): r["id"] for _, r in history.iterrows() }
    choice = st.selectbox("选择一条回测记录", list(options.keys()),
                          index=None, placeholder="选择记录查看逐笔交易…")
    if choice is None:
        return
    row = history[history["id"] == options[choice]].iloc[0]
    raw = row.get("trades_detail")
    if not raw or (isinstance(raw, float) and pd.isna(raw)):
        st.caption("该记录无逐笔明细（旧格式回测结果；重新运行回测后即有）")
        return
    trades = json.loads(raw)
    if not trades:
        st.caption("本次回测无平仓交易")
        return
    tdf = pd.DataFrame(trades).rename(columns={
        "code": "代码", "open_date": "买入日", "close_date": "卖出日",
        "open_price": "买入价", "close_price": "卖出价",
        "size": "数量", "pnl": "毛利", "pnlcomm": "净利(含费用)",
    })
    st.dataframe(tdf, use_container_width=True, hide_index=True)
    total = tdf["净利(含费用)"].sum()
    win = (tdf["净利(含费用)"] > 0).sum()
    st.caption(f"共 {len(tdf)} 笔平仓：盈利 {win} 笔 / 亏损 {len(tdf) - win} 笔，"
               f"合计净利 ¥{total:,.0f}")


def _render_run_comparison(history: pd.DataFrame) -> None:
    """任选两条历史记录并排对比（含指标 delta）。"""
    st.markdown("**⚖️ 两次回测对比**")
    options = {_record_label(r): r["id"] for _, r in history.iterrows()}
    picks = st.multiselect("选择两条记录对比", list(options.keys()), max_selections=2)
    if len(picks) != 2:
        st.caption("选择两条记录后展示对比")
        return
    rows = [history[history["id"] == options[p]].iloc[0] for p in picks]
    table = {"指标": [label for _, label in _METRIC_ROWS]}
    for i, r in enumerate(rows):
        table[f"记录{chr(65 + i)}"] = [r.get(k) for k, _ in _METRIC_ROWS]
    deltas = []
    for k, _ in _METRIC_ROWS:
        a, b = rows[0].get(k), rows[1].get(k)
        try:
            deltas.append(round(float(b) - float(a), 2))
        except (TypeError, ValueError):
            deltas.append(None)
    table["差异(B-A)"] = deltas
    st.dataframe(pd.DataFrame(table), use_container_width=True, hide_index=True)


def _render_history(dm) -> None:
    st.subheader("历史回测记录")
    history = dm.storage.get_backtest_results()
    if history.empty:
        st.caption("暂无历史记录")
        return
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

    tab_drill, tab_cmp = st.tabs(["交易明细下钻", "两次回测对比"])
    with tab_drill:
        _render_trade_drilldown(history)
    with tab_cmp:
        _render_run_comparison(history)


def render():
    st.title("📉 策略回测")
    dm = get_dm()
    cfg = get_config()

    if st.session_state.get("backtest_run"):
        _run_backtest(dm, cfg)

    # 历史记录放最后：运行完回测自然渲染到最新数据（含本次写入）
    _render_history(dm)


def _run_backtest(dm, cfg) -> None:
    selected = st.session_state.get("backtest_strategies", [])
    if not selected:
        st.warning("请至少选择一个策略")
        return

    start_date = str(st.session_state.get("backtest_start"))
    end_date = str(st.session_state.get("backtest_end"))
    stop_loss = st.session_state.get("backtest_stop_loss", 0.05)
    take_profit = st.session_state.get("backtest_take_profit", 0.15)
    top_n = st.session_state.get("backtest_top_n", 10)
    benchmark_code = st.session_state.get("backtest_benchmark", "000300")

    logger = StepLogger(f"策略回测（{len(selected)} 个策略）")
    logger.step("初始化回测引擎")

    try:
        comparator = BacktestComparator(cfg, dm)
        cb = logger.make_backtest_callback(STRATEGY_OPTIONS, selected)
        bt = comparator.compare(
            strategy_names=selected,
            start_date=start_date,
            end_date=end_date,
            top_n=top_n,
            stop_loss=stop_loss,
            take_profit=take_profit,
            status_callback=cb,
            benchmark_code=benchmark_code,
        )
    except Exception as e:
        logger.fail(f"回测失败：{e}")
        st.error(str(e))
        return

    summary_df = bt["summary"]
    benchmark = bt["benchmark"]

    if summary_df.empty:
        logger.fail("无有效结果")
        st.error("回测失败，请检查数据是否完整")
        return

    rows = summary_df.to_dict("records")
    valid_count = len([r for r in rows if "error" not in r and r.get("trades", 0) > 0])
    best = pick_best_strategy(rows)

    bench_name = benchmark["name"] if benchmark else "无基准"
    logger.complete(f"回测完成（{valid_count} 个有效策略，基准：{bench_name}）")

    st.divider()

    # ① 结论摘要
    st.info(build_conclusion(rows, benchmark, start_date, end_date))

    # 基准缺失提示（不阻断）
    if benchmark is None:
        st.warning(
            f"基准指数「{BENCHMARK_NAMES.get(benchmark_code, benchmark_code)}」数据获取失败，"
            "已跳过基准对比（其余结果不受影响）。"
        )

    # ② 低交易次数警告
    warnings = low_trade_warnings(rows)
    if warnings:
        st.warning("⚠️ " + "、".join(warnings) + " 交易次数过少，胜率/盈亏比统计意义不足，请谨慎参考。")

    # ③ 净值曲线（主图）
    eq_fig = _equity_chart(bt, benchmark)
    if eq_fig is not None:
        st.plotly_chart(eq_fig, use_container_width=True)

    # ④ 风险-收益散点
    rr_fig = _risk_return_scatter(rows)
    if rr_fig is not None:
        st.plotly_chart(rr_fig, use_container_width=True)

    # ⑤ 最优策略绝对金额
    if best is not None:
        best_row = next((r for r in rows if r.get("strategy_name") == best), None)
        if best_row:
            st.caption(f"最优策略：**{STRATEGY_OPTIONS.get(best, best)}**")
            c1, c2, c3 = st.columns(3)
            c1.metric("初始资金", f"¥{best_row.get('initial_cash', 0):,.0f}")
            c2.metric("最终市值", f"¥{best_row.get('final_value', 0):,.0f}")
            tr = best_row.get("total_return", 0)
            c3.metric("总收益", f"{tr:.2f}%", delta=f"{tr:.2f}%")

    # ⑥ 指标对比表（含基准行）
    formatted = BacktestComparator.format_comparison(summary_df)
    if benchmark is not None:
        bench_row = {col: "" for col in formatted.columns}
        bench_row["策略"] = f"{benchmark['name']}（基准·买入持有）"
        bench_row["总收益(%)"] = benchmark["total_return"]
        bench_row["年化收益(%)"] = benchmark["annual_return"]
        bench_row["初始资金"] = cfg["backtest"]["initial_cash"]
        bench_row["最终市值"] = round(
            cfg["backtest"]["initial_cash"] * (1 + benchmark["total_return"] / 100), 2
        )
        formatted = pd.concat([formatted, pd.DataFrame([bench_row])], ignore_index=True)
    st.dataframe(formatted, use_container_width=True, hide_index=True)

    # ⑦ 选中股票明细
    selected_map = bt.get("selected", {})
    if selected_map:
        st.subheader("选中股票明细")
        for name, stocks in selected_map.items():
            label = STRATEGY_OPTIONS.get(name, name)
            with st.expander(f"{label}（{len(stocks)} 只）", expanded=False):
                if stocks:
                    sdf = pd.DataFrame(stocks)
                    sdf = sdf.rename(columns={"code": "代码", "name": "名称", "score": "评分"})
                    st.dataframe(
                        sdf[["代码", "名称", "评分"]],
                        use_container_width=True, hide_index=True,
                    )
                else:
                    st.caption("无")
