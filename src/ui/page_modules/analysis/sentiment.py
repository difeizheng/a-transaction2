"""市场情绪 tab：指数卡片、板块轮动、情绪温度（数据驱动）+ LLM 定性总结、新闻列表。

重构增量：
- 新闻列表顶部显示「数据时效」提示（最新新闻时间 / 距今天数），陈旧时给出刷新引导。
"""
from datetime import datetime

import pandas as pd
import streamlit as st

from src.analysis.sentiment import compute_market_temperature
from src.ui.components.step_logger import StepLogger
from src.ui.page_modules.analysis.common import (
    _label_cn,
    _load_market_snapshot,
    _pct_span,
)


def _render_market_sentiment(dm, advisor):
    st.subheader("整体市场情绪分析")

    # 操作按钮行
    col_btn1, col_btn2, col_btn3 = st.columns([2, 2, 3])
    with col_btn1:
        do_refresh = st.button("🔄 刷新新闻并分析", type="primary",
                               help="拉取最新新闻 + 真实行情算温度 + 1 次 LLM 定性总结")
    with col_btn2:
        if st.button("🗑️ 清空新闻库"):
            dm.storage.delete_all_news()
            st.success("新闻库已清空")
            st.rerun()

    if do_refresh:
        logger = StepLogger("市场情绪分析")
        try:
            logger.step("获取财经新闻")
            dm.fetch_and_save_news()
            logger.log("新闻拉取完成")
            logger.step("拉取市场行情（指数 + 板块）")
            logger.step("AI 定性总结")
            result = advisor.analyze_market()
            st.session_state["market_sentiment"] = result
            logger.complete(f"分析完成：情绪温度 {result.get('temperature', 0):.0f}/100（{_label_cn(result.get('label'))}）")
        except Exception as e:
            logger.fail(f"分析失败：{e}")
            st.error(str(e))

    # ── 行情快照（缓存；优先用最近一次分析结果里的新鲜数据）─────────
    result = st.session_state.get("market_sentiment")
    if result and result.get("indices"):
        display = {"indices": result["indices"], "sectors": result["sectors"],
                   "sector_advances": None, "sector_declines": None,
                   "as_of": result.get("as_of")}
    else:
        snap = _load_market_snapshot(id(dm))
        display = snap

    # 战术层降级防呆（指数走腾讯/板块空时显示冷却提示+切宏观引导）
    _render_degradation_banner(display)
    _render_index_cards(display)
    _render_sector_rotation(display)
    _render_sentiment_summary(dm, display, result)

    # 分析框架说明（诚实化：只声明真正计算的维度）
    with st.expander("📖 分析框架说明", expanded=False):
        st.markdown("""
**情绪温度由真实行情数据驱动（不再由 LLM 凭空打分）：**
- **指数面**（数据驱动）：沪深300 / 上证指数 / 创业板指 / 中证500 当日涨跌，权重 40 / 20 / 20 / 20%
- **行业面**（数据驱动）：东方财富行业板块涨跌**中位数** + 上涨/下跌**家数广度**
- **事件面**（LLM 定性）：基于近期新闻产出总结与关键事件，**只写文字、不给分**
- **政策面 / 资金面**（北向资金、融资融券）：**暂未覆盖**，后续可选扩展

**温度区间：** `> 60` 偏多 ｜ `40 – 60` 中性 ｜ `< 40` 偏空
**积分说明：** 每次分析仅 1 次 LLM 调用（定性总结），分数本地计算、不耗积分。
**口径提示：** 指数为收盘口径，盘中显示的是最近交易日的涨跌。
        """)

    # 新闻列表（只读，去除原 30 个情绪下拉框）
    st.subheader("最新财经新闻")
    news_df = dm.get_news(limit=30)
    if news_df.empty:
        st.info("暂无新闻，点击「刷新新闻并分析」获取")
    else:
        _render_news_freshness(news_df)
        for _, row in news_df.iterrows():
            _render_news_item(row)


def _render_news_freshness(news_df: pd.DataFrame) -> None:
    """新闻时效提示：显示最新新闻时间；超过 24h 未更新时给出刷新引导。"""
    try:
        latest = pd.to_datetime(news_df["publish_time"]).max()
    except Exception:
        return
    if pd.isna(latest):
        return
    age = datetime.now() - latest.to_pydatetime().replace(tzinfo=None)
    hours = age.total_seconds() / 3600
    if hours >= 24:
        st.warning(f"📰 新闻库最新一条为 {latest:%Y-%m-%d %H:%M}（约 {hours/24:.1f} 天前），"
                   "数据已陈旧，建议点击「刷新新闻并分析」")
    else:
        st.caption(f"📰 新闻库最新：{latest:%Y-%m-%d %H:%M}（{hours:.0f} 小时前）")


def _degradation_level(display: dict) -> str:
    """根据 snapshot 数据源标记判定降级级别: 'none' / 'partial' / 'heavy'。

    - heavy: 指数整组为空(连腾讯备源都拿不到——更严重)；
            或指数全走腾讯 + 板块空(东财 push2 + datacenter 都挂)
    - partial: 指数部分走腾讯,或板块空但指数仍 akshare
    - none: 指数都 akshare + 板块有数据
            **或 display 整个空 / 关键键缺失**(无信号可评估,UI 不显 banner)

    区分"键缺失"与"键存在但空":前者 = 没数据可评估，后者 = 数据缺失。
    """
    indices = display.get("indices")
    sectors = display.get("sectors")
    if indices is None and sectors is None:
        return "none"
    has_tencent = any(i.get("source") == "tencent" for i in (indices or {}).values())
    sectors_empty = not sectors
    indices_empty = not indices
    if indices_empty:
        return "heavy"
    if has_tencent and sectors_empty:
        return "heavy"
    if has_tencent or sectors_empty:
        return "partial"
    return "none"


def _render_degradation_banner(display: dict):
    """情绪 tab 战术层降级时的防呆 + 引导 banner。

    把"东财限流/挂起→等 10-30 分钟冷却→或切到宏观 tab"这条踩坑经验
    固化为 UI 提示，防止用户反复点刷新加重熔断。
    """
    level = _degradation_level(display)
    if level == "none":
        return
    if level == "heavy":
        st.warning(
            "⚠ **战术数据降级**：东方财富 push2 行情接口暂不可用（连接被接受但不响应），"
            "板块暂无数据。建议等 **10–30 分钟**冷却后再试（避免反复点刷新延长熔断），"
            "或切到「**宏观分析**」tab 看战略层（统计局/央行/外管局数据，不受此限流影响）。"
        )
    else:
        st.info(
            "战术数据部分降级：部分指数走腾讯实时备源（东财 push2 暂不可用），"
            "板块数据可能缺失。宏观 tab 的战略层数据不受此影响 →"
        )


def _render_index_cards(snapshot: dict):
    """4 张指数卡片（涨红跌绿）+ 近 30 日迷你走势。"""
    st.markdown("#### 📊 主要指数")
    indices = snapshot.get("indices") or {}
    if not indices:
        st.info("暂无指数数据（数据源不可用或被限流，可稍后重试；情绪温度将退回中性 50）")
        return

    cols = st.columns(len(indices))
    for col, (code, info) in zip(cols, indices.items()):
        pct = info.get("pct_chg", 0.0)
        with col:
            st.metric(info.get("name", code), f"{pct:+.2f}%",
                      delta=f"{pct:+.2f}%", delta_color="inverse")
            st.caption(info.get("trade_date", ""))
            trend = info.get("trend") or []
            if len(trend) >= 2:
                st.line_chart(pd.Series(trend, name="收盘"),
                              use_container_width=True, height=120)


def _render_sector_rotation(snapshot: dict):
    """领涨 / 领跌 Top5 板块（涨红跌绿）。"""
    st.markdown("#### 🏭 板块轮动")
    sectors = snapshot.get("sectors") or []
    if not sectors:
        st.info("暂无板块数据（数据源不可用或被限流，稍后重试）")
        return

    ordered = sorted(sectors, key=lambda x: x["pct_chg"], reverse=True)
    gainers = ordered[:5]
    losers = list(reversed(ordered[-5:]))

    col_g, col_l = st.columns(2)
    with col_g:
        st.caption("🔺 领涨板块")
        rows = "".join(
            f"<tr><td>{s['name']}</td><td style='text-align:right'>{_pct_span(s['pct_chg'])}</td></tr>"
            for s in gainers
        )
        st.markdown(f"<table style='width:100%'>{rows}</table>", unsafe_allow_html=True)
    with col_l:
        st.caption("🔻 领跌板块")
        rows = "".join(
            f"<tr><td>{s['name']}</td><td style='text-align:right'>{_pct_span(s['pct_chg'])}</td></tr>"
            for s in losers
        )
        st.markdown(f"<table style='width:100%'>{rows}</table>", unsafe_allow_html=True)


def _render_sentiment_summary(dm, display: dict, result: dict | None):
    """情绪温度（数据驱动，始终可算）+ 历史趋势 + LLM 定性总结。"""
    st.markdown("#### 🌡️ 情绪温度")

    # 温度始终可由缓存快照算出；LLM 文字仅分析后才有
    idx_moves = {c: v["pct_chg"] for c, v in (display.get("indices") or {}).items()}
    secs = [s["pct_chg"] for s in (display.get("sectors") or [])]
    base = compute_market_temperature(
        idx_moves, secs,
        sector_advances=display.get("sector_advances"),
        sector_declines=display.get("sector_declines"),
    )

    if result:
        temperature = result.get("temperature", base["temperature"])
        label = result.get("label", base["label"])
        components = result.get("components", base["components"])
        summary = result.get("summary", "")
        key_events = result.get("key_events_json", [])
    else:
        temperature, label, components = (
            base["temperature"], base["label"], base["components"]
        )
        summary = ""
        key_events = []

    col_metric, col_trend = st.columns([1, 2])
    with col_metric:
        st.metric("情绪温度", f"{temperature:.0f} / 100", _label_cn(label))
        comp = components or {}
        st.caption(
            f"指数贡献 {comp.get('index', 0):+.1f} ｜ "
            f"板块中位 {comp.get('sector_median', 0):+.1f} ｜ "
            f"广度 {comp.get('sector_breadth', 0):+.1f}"
        )
        if st.button("清除本次分析结果", key="clear_sentiment"):
            st.session_state.pop("market_sentiment", None)
            st.rerun()
    with col_trend:
        history = dm.storage.get_market_sentiment_history(30)
        if history:
            # 倒序存储 → 反转成时间正序画趋势
            series = pd.Series(
                [h["temperature"] for h in reversed(history)],
                index=[h["snapshot_date"] for h in reversed(history)],
                name="情绪温度",
            )
            st.line_chart(series, use_container_width=True, height=160)
            st.caption(f"近 {len(history)} 个交易日情绪温度趋势")
        else:
            st.info("暂无历史数据，运行一次分析后此处显示温度趋势")

    # LLM 定性总结
    if summary:
        st.info(summary)
    elif result is None:
        st.caption("💡 温度已由行情数据算出。点击「刷新新闻并分析」可获取 AI 定性总结与关键事件（仅 1 次调用）。")
    if key_events:
        st.write("**关键事件：**")
        for e in key_events:
            st.write(f"- {e}")


def _render_news_item(row):
    """只读渲染单条新闻（标题链接、来源时间、摘要、已有情绪标签）。"""
    title = row.get("title", "")
    url = row.get("url", "") or ""
    source = row.get("source", "")
    pub_time = str(row.get("publish_time", ""))[:16]
    content = row.get("content", "") or ""
    current_sentiment = row.get("sentiment", "") or ""

    title_md = f"[{title}]({url})" if url else f"**{title}**"
    meta = f"<small style='color:gray'>{pub_time}  |  {source}</small>"
    sent_tag = ""
    if current_sentiment:
        tag_map = {"利好": "🟥 利好", "利空": "🟩 利空", "中性": "⬜ 中性"}
        sent_tag = f"  `{tag_map.get(current_sentiment, current_sentiment)}`"
    st.markdown(f"{title_md}{sent_tag}  \n{meta}", unsafe_allow_html=True)

    if content:
        snippet = content[:120] + "..." if len(content) > 120 else content
        st.caption(snippet)
    st.divider()
