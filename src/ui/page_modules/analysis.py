"""市场分析页面"""
import streamlit as st
import pandas as pd
from src.data.manager import DataManager
from src.analysis.advisor import Advisor
from src.analysis.sentiment import compute_market_temperature
from src.analysis.macro import compute_macro_stance, PILLAR_NAMES, INDICATOR_NAMES
from src.config import get_config
from src.ui.components.step_logger import StepLogger
from src.ui.components.service_info import render_service_info, get_akshare_info, get_llm_info


@st.cache_resource
def get_dm():
    return DataManager()


@st.cache_data(ttl=600, show_spinner=False)
def _load_market_snapshot(_dm_id):
    """指数+板块快照缓存 10 分钟（镜像 data_mgmt._load_coverage 的 _dm_id 用法）。
    刷新按钮走 advisor.analyze_market() 的非缓存直连路径，不用 cache_data.clear()
    （避免清掉其他 tab 的缓存）。"""
    return get_dm().get_market_snapshot()


@st.cache_data(ttl=21600, show_spinner=False)
def _load_macro_snapshot(_dm_id):
    """宏观四支柱快照缓存 6h（低频数据：CPI/PMI/M2 月频、SHIBOR/美债日频）。
    刷新走 advisor.analyze_macro() 非缓存直连，不用 cache_data.clear()。"""
    return get_dm().get_macro_snapshot()


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

    tab1, tab2, tab3 = st.tabs(["市场情绪", "个股深度分析", "宏观分析"])

    with tab1:
        _render_market_sentiment(dm, advisor)

    with tab2:
        _render_stock_deep_analysis(dm, advisor)

    with tab3:
        _render_macro(dm, advisor)


# ── 配色：A 股涨红跌绿（与 Streamlit 默认相反）────────────────────
_UP_COLOR = "#e74c3c"   # 涨：红
_DOWN_COLOR = "#27ae60"  # 跌：绿


def _pct_span(pct: float) -> str:
    color = _UP_COLOR if pct > 0 else (_DOWN_COLOR if pct < 0 else "gray")
    return f"<span style='color:{color};font-weight:600'>{pct:+.2f}%</span>"


def _label_cn(label: str) -> str:
    return {"bullish": "偏多 📈", "bearish": "偏空 📉", "neutral": "中性 ➡️"}.get(label, "中性 ➡️")


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
        for _, row in news_df.iterrows():
            _render_news_item(row)


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


# ── 宏观态势 tab ───────────────────────────────────────────────
_PILLAR_ORDER = ["liquidity", "capital", "fundamental", "external"]


def _stance_label_cn(label: str) -> str:
    return {"bullish": "宽松积极 📈", "bearish": "偏紧 📉", "neutral": "中性 ➡️"}.get(label, "中性 ➡️")


def _signal_color(signal: float) -> str:
    return _UP_COLOR if signal > 0 else (_DOWN_COLOR if signal < 0 else "gray")


def _render_macro(dm, advisor):
    st.subheader("宏观态势分析")

    col_btn1, col_btn2 = st.columns([2, 3])
    with col_btn1:
        do_refresh = st.button("🔄 刷新宏观分析", type="primary",
                               help="拉取四支柱真实数据 + 算态势分 + 1 次 LLM 政策面定性")
    with col_btn2:
        if st.button("🗑️ 清除本次分析"):
            st.session_state.pop("macro_analysis", None)
            st.rerun()

    if do_refresh:
        logger = StepLogger("宏观态势分析")
        try:
            logger.step("拉取四支柱宏观数据（流动性/资金面/基本面/外部）")
            logger.step("AI 政策面定性")
            result = advisor.analyze_macro()
            st.session_state["macro_analysis"] = result
            logger.complete(
                f"分析完成：宏观态势 {result.get('score', 0):.0f}/100"
                f"（{_stance_label_cn(result.get('label'))}）"
            )
        except Exception as e:
            logger.fail(f"分析失败：{e}")
            st.error(str(e))

    # 数据：优先用分析结果（含 LLM 文字），否则缓存快照本地算分（无需 LLM）
    result = st.session_state.get("macro_analysis")
    if result and result.get("components"):
        components = result["components"]
        score, label = result["score"], result["label"]
        indicators_meta = result.get("indicators_meta", {})
        summary = result.get("summary", "")
        policy_read = result.get("policy_read", "")
        key_risks = result.get("key_risks_json", [])
    else:
        snap = _load_macro_snapshot(id(dm))
        comp = compute_macro_stance(snap["indicators"])
        components = comp["components"]
        score, label = comp["score"], comp["label"]
        indicators_meta = snap["indicators"]
        summary, policy_read, key_risks = "", "", []

    _render_macro_pillars(components, indicators_meta)
    _render_macro_stance(dm, score, label, components, summary, policy_read, key_risks, result)

    with st.expander("📖 宏观分析框架说明", expanded=False):
        st.markdown(_macro_framework_md())


def _render_macro_pillars(components: dict, indicators_meta: dict):
    """四支柱卡片：支柱信号（涨红跌绿）+ 其下各指标值与 as_of。"""
    st.markdown("#### 🏛️ 四支柱信号")
    if not components:
        st.info("暂无宏观数据（数据源不可用或被限流，稍后重试；态势分将退回中性 50）")
        return

    cols = st.columns(len(_PILLAR_ORDER))
    for col, pillar in zip(cols, _PILLAR_ORDER):
        with col:
            pname = PILLAR_NAMES.get(pillar, pillar)
            comp = components.get(pillar)
            if comp is None:
                st.markdown(f"**{pname}**")
                st.caption("数据缺失")
                continue
            signal = comp["signal"]
            color = _signal_color(signal)
            st.markdown(f"**{pname}**")
            st.markdown(
                f"<div style='color:{color};font-weight:700;font-size:1.6em'>{signal:+.2f}</div>",
                unsafe_allow_html=True,
            )
            tag = "bullish" if signal > 0.1 else ("bearish" if signal < -0.1 else "neutral")
            st.caption(_stance_label_cn(tag))
            # 支柱下各指标（名 + latest + as_of）
            for key, meta in (indicators_meta.get(pillar) or {}).items():
                latest = meta.get("latest", 0)
                ao = str(meta.get("as_of", ""))[:10]
                try:
                    val_txt = f"{latest:.4g}"
                except (TypeError, ValueError):
                    val_txt = str(latest)
                st.caption(f"{INDICATOR_NAMES.get(key, key)}: {val_txt} · {ao}")


def _render_macro_stance(dm, score: float, label: str, components: dict,
                         summary: str, policy_read: str, key_risks: list, result):
    """宏观态势仪表 + regime 趋势 + LLM 综合定性/政策面/风险。"""
    st.markdown("#### 🎯 宏观态势")

    col_metric, col_trend = st.columns([1, 2])
    with col_metric:
        st.metric("宏观态势", f"{score:.0f} / 100", _stance_label_cn(label))
        comp_str = " ｜ ".join(
            f"{PILLAR_NAMES.get(p, p)} {v['signal']:+.2f}" for p, v in components.items()
        )
        if comp_str:
            st.caption(comp_str)
    with col_trend:
        history = dm.storage.get_macro_history(60)
        if history:
            series = pd.Series(
                [h["score"] for h in reversed(history)],
                index=[h["snapshot_date"] for h in reversed(history)],
                name="宏观态势",
            )
            st.line_chart(series, use_container_width=True, height=160)
            st.caption(f"近 {len(history)} 日宏观态势趋势")
        else:
            st.info("暂无历史数据，运行一次分析后此处显示 regime 趋势")

    if summary:
        st.info(summary)
    elif result is None:
        st.caption("💡 态势分已由四支柱真实数据算出。点击「刷新宏观分析」可获取 AI 政策面解读与关键风险（仅 1 次调用）。")
    if policy_read:
        st.write(f"**政策面解读：** {policy_read}")
    if key_risks:
        st.write("**关键风险：**")
        for r in key_risks:
            st.write(f"- {r}")


def _macro_framework_md() -> str:
    return """
**宏观态势由真实数据驱动（四支柱 + 政策面 LLM 定性）：**
- **流动性**（数据驱动）：M1-M2 剪刀差、SHIBOR 隔夜（vs 60日中位）、社融滚动12月同比
- **资金面**（数据驱动）：融资余额近5日变化（**北向资金日度净流入自 2024-08-19 起交易所不再公布，故不含**）
- **基本面**（数据驱动）：制造业 PMI（vs 荣枯线50）、PPI-CPI 剪刀差
- **外部环境**（数据驱动）：美10债（vs 60日中位）、USD/CNY 中间价（20日变化）
- **政策面**（LLM 定性）：央行/国常会/监管动向，由 AI 从近期新闻提炼，**纯函数无法量化**

**态势区间：** `> 55` 宽松积极 ｜ `45 – 55` 中性 ｜ `< 45` 偏紧
**积分说明：** 每次分析仅 1 次 LLM 调用（政策面定性），分数本地计算、不耗积分。
**口径提示：** 宏观为月/季频，各指标 `as_of` 标注数据日期（如 6 月中旬看 CPI 是 5 月数据）。**态势分是启发式非绝对真理**——透明展示四支柱分量，请结合政策面解读综合判断。
"""


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
