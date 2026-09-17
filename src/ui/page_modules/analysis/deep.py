"""个股深度分析 tab：实时行情 + 技术面 + 相关新闻 + AI 综合分析。"""
import streamlit as st

from src.ui.components.step_logger import StepLogger


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
