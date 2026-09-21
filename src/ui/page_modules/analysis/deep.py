"""个股深度分析 tab：实时行情 + 技术面 + 相关新闻 + AI 综合分析。

长任务后台化（第四轮巡检 P0 修复）：分析含 LLM 调用（30~90s），同步执行会让
整个 rerun 期间无帧推送 → websocket 静默死亡、结果丢帧。改为后台线程 +
前台短 rerun 轮询（与筛选页同模式），结果同时落库 deep_analysis_reports。
"""
import time

import streamlit as st

from src.ui.components.deep_worker import start_deep_analysis


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
        st.info("请先在「选股筛选」页面运行策略，或从自选股选择 / 直接输入股票代码")
        target_code, target_name = "", ""
        try:
            watchlist = dm.storage.get_watchlist()
        except Exception:
            watchlist = []
        if watchlist:
            wl_opts = {it["code"]: f"⭐ {it.get('name', '')}（{it['code']}）"
                       for it in watchlist}
            pick = st.selectbox(
                "从自选股选择", [""] + list(wl_opts.keys()),
                format_func=lambda x: wl_opts.get(x, "—— 或手动输入 ——"),
                key="deep_watch_pick")
            if pick:
                target_code = pick
                target_name = next(
                    (it.get("name", "") for it in watchlist if it["code"] == pick),
                    "") or pick
        if not target_code:
            col_c, col_n = st.columns(2)
            target_code = col_c.text_input("股票代码（如 000001）", key="deep_code")
            target_name = col_n.text_input("股票名称", key="deep_name")

    # 名称回填：手动只输代码时从 stock_list 查名，避免标题显示「300936（300936）」
    if target_code and (not target_name or target_name == target_code):
        try:
            info = dm.storage.get_stock_detail(target_code).get("info") or {}
            if info.get("name"):
                target_name = info["name"]
        except Exception:
            pass
        if not target_name:
            target_name = target_code

    # ── 启动 / 轮询 / 收尾 ────────────────────────────────────────────
    task = st.session_state.get("deep_task")
    running = bool(task and task.get("status") == "running")

    if target_code and st.button("🔍 开始深度分析", type="primary",
                                 disabled=running):
        st.session_state["deep_task"] = start_deep_analysis(
            dm, advisor, target_code, target_name)
        st.session_state.pop("deep_analysis", None)
        st.rerun()

    task = st.session_state.get("deep_task")
    if task and task.get("status") == "running":
        with st.status(f"深度分析 {task.get('name')}（{task.get('code')}）…",
                       expanded=True) as status:
            st.markdown(f"**当前步骤：{task.get('step', '…')}**")
            for line in task.get("logs", []):
                st.caption(line)
        # 短 rerun 轮询：每 1.5s 一帧，websocket 保活（避免长 rerun 静默假死）
        time.sleep(1.5)
        st.rerun()
    elif task and task.get("status") == "done":
        st.session_state["deep_analysis"] = task["result"]
        st.session_state["deep_task"] = None
        if not task.get("llm_ok", True):
            st.error("⚠️ AI 分析失败（LLM 超时或服务不可用）。"
                     "下面展示的是行情 / 技术面 / 新闻等数据面结果，AI 结论不可用。")
        else:
            st.success("✅ 分析完成，结果已存档（刷新页面不丢失）")
    elif task and task.get("status") == "error":
        st.error(f"❌ 深度分析失败：{task.get('error')}")
        st.session_state["deep_task"] = None

    # 历史回看：刷新后可直接载入上次分析（结果已落库 deep_analysis_reports）
    result = st.session_state.get("deep_analysis")
    if not result and not task and target_code:
        try:
            prev = dm.storage.get_latest_deep_analysis(target_code)
        except Exception:
            prev = None
        if prev:
            with st.expander(
                    f"📂 上次分析（{str(prev['created_at'])[:16]}，"
                    f"{'AI 成功' if prev['llm_ok'] else 'AI 失败'}）"):
                st.caption("结果已存档。点击载入查看，或点「开始深度分析」重新跑。")
                if st.button("📂 载入上次分析", key="deep_load_prev"):
                    st.session_state["deep_analysis"] = prev["result"]
                    st.rerun()

    if result:
        _show_deep_analysis(result)


def _show_deep_analysis(result: dict):
    """展示个股深度分析结果（4个区块）。"""
    code = result.get("code", "")
    name = result.get("name", "")
    analyzed_at = result.get("analyzed_at", "")
    title = f"### {name}（{code}）"
    st.markdown(title)
    if analyzed_at:
        st.caption(f"分析时间：{analyzed_at}（结果已存档，刷新页面不丢失）")

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

    # 区块2：技术面分析（过滤已失效策略，避免占位噪音）
    st.markdown("#### 📈 技术面分析")
    tech = [t for t in result.get("technical", [])
            if "已失效" not in (t.get("strategy_name") or "")]
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

    if llm.get("llm_error"):
        # LLM 失败：不渲染「未知/0%」这类误导性指标，只显式展示失败原因
        st.error(f"AI 分析失败：{llm.get('analysis', '未知原因')[:200]}")
        st.caption("以上为 LLM 返回的原始信息；行情 / 技术面 / 新闻数据不受影响，可稍后重试。")
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
