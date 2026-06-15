"""市场分析页面"""
import streamlit as st
import pandas as pd
from src.data.manager import DataManager
from src.analysis.advisor import Advisor
from src.config import get_config
from src.ui.components.step_logger import StepLogger
from src.ui.components.service_info import render_service_info, get_akshare_info, get_llm_info


@st.cache_resource
def get_dm():
    return DataManager()


def render_sidebar():
    st.subheader("分析设置")
    st.session_state["analysis_max_stocks"] = st.slider(
        "批量分析前N只（每只消耗1次API调用）", 1, 10, 3
    )

    st.divider()
    cfg = get_config()
    render_service_info([get_akshare_info(), get_llm_info(cfg)])


def render():
    st.title("🤖 市场分析")
    dm = get_dm()
    cfg = get_config()
    advisor = Advisor(cfg, dm)

    tab1, tab2 = st.tabs(["市场情绪", "个股深度分析"])

    with tab1:
        _render_market_sentiment(dm, advisor)

    with tab2:
        _render_stock_deep_analysis(dm, advisor)


def _render_market_sentiment(dm, advisor):
    st.subheader("整体市场情绪分析")

    # 操作按钮行
    col_btn1, col_btn2, col_btn3 = st.columns([2, 2, 3])
    with col_btn1:
        do_refresh = st.button("🔄 刷新新闻并分析", type="primary")
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
            logger.step("AI分析市场情绪")
            sentiment = advisor.analyze_market()
            st.session_state["market_sentiment"] = sentiment
            logger.complete("分析完成")
        except Exception as e:
            logger.fail(f"分析失败：{e}")
            st.error(str(e))

    # 情绪结果展示
    sentiment = st.session_state.get("market_sentiment")
    if sentiment:
        score = sentiment.get("sentiment_score", 50)
        label = {"bullish": "看涨 📈", "bearish": "看跌 📉", "neutral": "中性 ➡️"}.get(
            sentiment.get("sentiment"), "中性 ➡️"
        )
        col_metric, col_summary = st.columns([1, 3])
        with col_metric:
            st.metric("市场情绪", label, f"情绪分: {score}")
            if st.button("清除分析结果", key="clear_sentiment"):
                del st.session_state["market_sentiment"]
                st.rerun()
        with col_summary:
            st.info(sentiment.get("summary", ""))
            events = sentiment.get("key_events", [])
            if events:
                st.write("**关键事件：**")
                for e in events:
                    st.write(f"- {e}")

    # 分析框架说明
    with st.expander("📖 分析框架说明", expanded=False):
        st.markdown("""
**情绪评分体系：**
- **看涨 (Bullish)**：情绪分 > 60，市场整体偏乐观，利好消息占主导
- **中性 (Neutral)**：情绪分 40–60，多空力量均衡，市场方向不明确
- **看跌 (Bearish)**：情绪分 < 40，市场整体偏悲观，利空消息占主导

**评分维度：**
1. 政策面：央行/证监会等监管政策方向（降准降息、行业政策）
2. 资金面：北向资金、融资融券、成交量变化
3. 行业面：重点行业景气度变化（科技、消费、金融、能源）
4. 事件面：突发事件、财报季、IPO、重大并购等

**关键事件识别规则：**
- 影响面广（涉及多个行业或整体市场）
- 时效性强（24小时内发生）
- 对市场情绪有明确方向性影响（政策利好/利空、业绩超预期/不及预期）

**使用建议：**
- 情绪分仅供参考，不构成投资建议
- 建议结合个股技术面和基本面综合判断
- 多次刷新会自动去重，不会重复计入相同新闻
        """)

    # 新闻列表
    st.subheader("最新财经新闻")
    news_df = dm.get_news(limit=30)
    if news_df.empty:
        st.info("暂无新闻，点击「刷新新闻并分析」获取")
    else:
        for idx, row in news_df.iterrows():
            _render_news_item(dm, row)


def _render_news_item(dm, row):
    """渲染单条新闻（带超链接、摘要、情绪标签）。"""
    news_id = row.get("id")
    title = row.get("title", "")
    url = row.get("url", "") or ""
    source = row.get("source", "")
    pub_time = str(row.get("publish_time", ""))[:16]
    content = row.get("content", "") or ""
    current_sentiment = row.get("sentiment", "") or ""

    # 标题行
    title_md = f"[{title}]({url})" if url else f"**{title}**"
    meta = f"<small style='color:gray'>{pub_time}  |  {source}</small>"
    st.markdown(f"{title_md}  \n{meta}", unsafe_allow_html=True)

    # 内容摘要
    if content:
        summary = content[:120] + "..." if len(content) > 120 else content
        st.caption(summary)

    # 情绪标签操作
    options = ["未标注", "利好 📈", "中性 ➡️", "利空 📉"]
    # 映射存储值到显示值
    sent_map = {"利好": "利好 📈", "中性": "中性 ➡️", "利空": "利空 📉"}
    rev_map = {"利好 📈": "利好", "中性 ➡️": "中性", "利空 📉": "利空", "未标注": ""}
    current_display = sent_map.get(current_sentiment, "未标注")
    idx_default = options.index(current_display) if current_display in options else 0

    col_sel, col_save = st.columns([3, 1])
    with col_sel:
        new_sent_display = st.selectbox(
            "情绪", options, index=idx_default,
            key=f"news_sent_{news_id}",
            label_visibility="collapsed"
        )
    with col_save:
        if st.button("保存", key=f"save_sent_{news_id}", use_container_width=True):
            dm.storage.update_news_sentiment(news_id, rev_map.get(new_sent_display, ""))
            st.rerun()

    st.divider()


def _render_stock_deep_analysis(dm, advisor):
    st.subheader("个股深度分析")
    screen_results = st.session_state.get("screen_results", [])

    # 输入区
    if screen_results:
        options = {sr.code: f"{sr.name}（{sr.code}）" for sr in screen_results}
        selected_code = st.selectbox("选择股票", list(options.keys()),
                                     format_func=lambda x: options[x])
        sr = next(s for s in screen_results if s.code == selected_code)
        target_code, target_name = sr.code, sr.name
    else:
        st.info("请先在「选股筛选」页面运行策略，或直接输入股票代码")
        col_c, col_n = st.columns(2)
        target_code = col_c.text_input("股票代码（如 000001）", key="deep_code")
        target_name = col_n.text_input("股票名称", key="deep_name")
        if not target_name:
            target_name = target_code

    if target_code and st.button("🔍 开始深度分析", type="primary"):
        logger = StepLogger(f"深度分析 {target_name}（{target_code}）")
        try:
            logger.step("拉取个股新闻")
            dm.fetch_and_save_news(code=target_code)
            logger.step("获取实时行情 + 技术指标")
            logger.step("AI综合分析")
            result = advisor.analyze_stock_deep(target_code, target_name)
            st.session_state["deep_analysis"] = result
            logger.complete("分析完成")
        except Exception as e:
            logger.fail(f"分析失败：{e}")
            st.error(str(e))

    result = st.session_state.get("deep_analysis")
    if result:
        _show_deep_analysis(result)


def _show_deep_analysis(result: dict):
    """展示个股深度分析结果（4个区块）。"""
    code = result.get("code", "")
    name = result.get("name", "")
    st.markdown(f"### {name}（{code}）")

    # 区块1：实时行情
    st.markdown("#### 📊 实时行情")
    rt = result.get("realtime")
    if rt:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("现价", f"¥{rt['price']:.2f}")
        pct = rt['pct_chg']
        # A股配色：涨红跌绿，与 Streamlit 默认（涨绿跌红）相反，用 inverse 反转
        c2.metric("涨跌幅", f"{pct:.2f}%", delta=f"{pct:.2f}%", delta_color="inverse")
        c3.metric("成交量（手）", f"{rt['volume']/100:,.0f}")
        c4.metric("成交额（万）", f"{rt['amount']/10000:,.1f}")
    else:
        st.caption("暂无实时行情数据（非交易时段或数据源不可用）")

    # 区块2：技术面分析
    st.markdown("#### 📈 技术面分析")
    tech = result.get("technical", [])
    if tech:
        for t in tech:
            icon = "✅" if t["selected"] else "❌"
            with st.expander(f"{icon} {t.get('strategy_name', t['strategy_key'])}  |  评分: {t['score']:.2f}"):
                indicators = t.get("indicators", {})
                if indicators:
                    ind_cols = st.columns(min(len(indicators), 4))
                    for i, (k, v) in enumerate(indicators.items()):
                        ind_cols[i % len(ind_cols)].metric(k, str(v))
                conditions = t.get("conditions", [])
                for cond in conditions:
                    icon_c = "🟢" if cond["passed"] else "🔴"
                    st.write(f"{icon_c} **{cond['label']}**：{cond['detail']}")
                if t.get("reason"):
                    st.caption(t["reason"])
    else:
        st.caption("暂无技术指标数据（可能缺少历史K线）")

    # 区块3：相关新闻
    st.markdown("#### 📰 相关新闻")
    news = result.get("news", [])
    if news:
        for n in news[:8]:
            title = n.get("title", "")
            url = n.get("url", "") or ""
            sent = n.get("sentiment", "") or ""
            pub_time = str(n.get("publish_time", ""))[:16]
            sent_tag = f" `{sent}`" if sent else ""
            link = f"[{title}]({url})" if url else title
            st.markdown(f"- {link}{sent_tag}  <small style='color:gray'>{pub_time}</small>",
                        unsafe_allow_html=True)
    else:
        st.caption("暂无相关新闻")

    # 区块4：AI综合分析
    st.markdown("#### 🤖 AI综合分析")
    llm = result.get("llm_analysis", {})
    if not llm:
        st.caption("AI分析暂无结果")
        return

    c1, c2, c3 = st.columns(3)
    c1.metric("趋势判断", llm.get("trend", "未知"))
    c2.metric("置信度", f"{llm.get('confidence', 0)}%")
    c3.metric("操作建议", llm.get("buy_suggestion", "观望"))

    c4, c5 = st.columns(2)
    c4.metric("建议止损", f"-{llm.get('stop_loss_pct', 5)}%")
    c5.metric("建议止盈", f"+{llm.get('take_profit_pct', 15)}%")

    if llm.get("entry_price_note"):
        st.write(f"**入场价位：** {llm['entry_price_note']}")
    if llm.get("technical_summary"):
        st.write(f"**技术面解读：** {llm['technical_summary']}")
    if llm.get("news_summary"):
        st.write(f"**消息面解读：** {llm['news_summary']}")
    if llm.get("analysis"):
        st.write(f"**综合分析：** {llm['analysis']}")
    if llm.get("key_factors"):
        st.write("**关键影响因素：**")
        for f in llm["key_factors"]:
            st.write(f"- {f}")
    if llm.get("risk_warning"):
        st.warning(f"⚠️ 风险提示：{llm['risk_warning']}")

