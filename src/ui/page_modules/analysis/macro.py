"""宏观态势 tab：四支柱信号卡片 + 态势仪表 + regime 趋势 + LLM 政策面定性。"""
import pandas as pd
import streamlit as st

from src.analysis.macro import INDICATOR_NAMES, PILLAR_NAMES, compute_macro_stance
from src.ui.components.step_logger import StepLogger
from src.ui.page_modules.analysis.common import (
    _load_macro_snapshot,
    _signal_color,
    _stance_label_cn,
)

_PILLAR_ORDER = ["liquidity", "capital", "fundamental", "external"]


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
