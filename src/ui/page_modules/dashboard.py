"""总览仪表盘

组合概览 + 净值曲线 + 市场情绪 + 持仓/自选/回测快照。
配色统一走 src.ui.core（A 股：涨红跌绿）；净值曲线实现见
src/ui/components/equity.py（与模拟交易页共用）。
"""
from datetime import date, datetime

import pandas as pd
import streamlit as st

from src.ui.components.equity import render_equity_curve
from src.ui.components.service_info import get_akshare_info, get_llm_info, render_service_info
from src.ui.core import enrich_profit, get_config, get_dm, get_services, money_span, pct_span


def _sentiment_label_cn(label: str) -> str:
    return {"bullish": "🐂 偏多", "bearish": "🐻 偏空", "neutral": "😐 中性"}.get(label, label)


def _render_sentiment_card(storage) -> None:
    """市场情绪温度卡：只读最新 market_sentiment 快照，不触发 LLM。"""
    history = storage.get_market_sentiment_history(limit=1)
    if not history:
        st.caption("市场情绪：暂无快照（在「AI 与市场分析」页运行一次盘后分析生成）")
        return
    snap = history[0]
    temp = snap.get("temperature", 0)
    st.metric(
        f"市场情绪温度（{snap.get('snapshot_date', '?')}）",
        f"{temp:+.2f}",
        _sentiment_label_cn(snap.get("label", "")),
    )
    if snap.get("summary"):
        st.caption(snap["summary"][:120])


def _render_holdings_table(positions_df: pd.DataFrame) -> None:
    """持仓表（HTML，盈亏红涨绿跌）。"""
    if positions_df.empty:
        st.caption("暂无持仓")
        return
    rows = []
    for _, p in positions_df.iterrows():
        rows.append(
            f"<tr><td>{p['code']}</td><td>{p['name']}</td>"
            f"<td style='text-align:right'>{int(p['quantity'])}</td>"
            f"<td style='text-align:right'>¥{p['cost_price']:.2f}</td>"
            f"<td style='text-align:right'>¥{p['current_price']:.2f}</td>"
            f"<td style='text-align:right'>{pct_span(p['profit_pct'])}</td>"
            f"<td style='text-align:right'>{money_span(p['profit_loss'])}</td></tr>"
        )
    st.markdown(
        "<table style='width:100%;font-size:14px'>"
        "<thead><tr style='color:#888;border-bottom:1px solid #ddd'>"
        "<th style='text-align:left'>代码</th><th style='text-align:left'>名称</th>"
        "<th style='text-align:right'>数量</th><th style='text-align:right'>成本</th>"
        "<th style='text-align:right'>现价</th><th style='text-align:right'>盈亏%</th>"
        "<th style='text-align:right'>盈亏额</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>",
        unsafe_allow_html=True,
    )


def render():
    dm, simulator, _ = get_services()
    storage = dm.storage

    st.header("📊 总览仪表盘")
    st.caption(f"数据截至 {date.today().isoformat()}")

    # ── 账户概览 + 市场情绪 ─────────────────────────────────────
    try:
        summary = enrich_profit(storage, simulator.get_portfolio_summary())
    except Exception as e:
        st.warning(f"账户数据加载失败（行情源可能不可用）：{e}")
        summary = None

    if summary:
        c1, c2, c3, c4, c5 = st.columns([1, 1, 1, 1, 1.2])
        c1.metric("总资产", f"¥{summary['total_value']:,.0f}")
        c2.metric("现金", f"¥{summary['cash']:,.0f}")
        c3.metric("持仓市值", f"¥{summary['market_value']:,.0f}")
        c4.metric("总盈亏", f"¥{summary['total_profit']:+,.0f}",
                  f"{summary['total_profit_pct']:+.2f}%")
        with c5:
            _render_sentiment_card(storage)

    # ── 净值曲线 ────────────────────────────────────────────────
    with st.container(border=True):
        render_equity_curve(simulator, key_prefix="dash")

    # ── 持仓 + 自选股 ───────────────────────────────────────────
    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("💼 当前持仓")
        try:
            _render_holdings_table(simulator.get_positions_df())
        except Exception as e:
            st.warning(f"持仓加载失败：{e}")

    with col_right:
        st.subheader("⭐ 自选股（按评分 Top5）")
        watchlist = storage.get_watchlist()
        if watchlist:
            wdf = pd.DataFrame(watchlist)
            if "score" in wdf.columns:
                wdf = wdf.sort_values("score", ascending=False)
            st.dataframe(
                wdf.head(5)[["code", "name", "score", "reason"]],
                hide_index=True, use_container_width=True,
            )
            st.caption(f"共 {len(watchlist)} 只 · 完整管理见「⭐ 自选股」页")
        else:
            st.caption("暂无自选股（在「选股筛选」页添加）")

    # ── 最近回测 ────────────────────────────────────────────────
    st.subheader("📈 最近回测结果")
    backtests = storage.get_backtest_results()
    if not backtests.empty:
        cols = [c for c in ("strategy_name", "start_date", "end_date", "total_return",
                            "sharpe", "max_drawdown", "win_rate") if c in backtests.columns]
        st.dataframe(backtests[cols].head(5), hide_index=True, use_container_width=True)
    else:
        st.caption("暂无回测记录（在「策略回测」页运行对比）")


def render_sidebar():
    st.subheader("⚙️ 服务状态")
    render_service_info([get_akshare_info(), get_llm_info(get_config())])
    st.divider()
    st.caption("💡 在「数据管理」页更新股票列表、K 线与财务数据")
