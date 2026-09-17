"""AI 问答区：基于筛选结果的多轮对话 + token 成本展示。

token 用量来自 llm_call_log（LLMAnalyzer 每次调用自动落库），
以本筛选 session 的问答起点（screener_qa_since）为下界统计。
"""
from datetime import datetime

import streamlit as st

from src.config import get_config


def _build_qa_context(dm, session_id: int) -> str:
    """组装LLM系统提示：入选股票摘要 + 指标 + 价格 + 财务 + 近期新闻。"""
    evals = dm.storage.get_session_evaluations(session_id, selected_only=True)
    if not evals:
        return "你是一位A股投资分析助手。当前没有筛选结果。"

    stock_lines = []
    for ev in evals[:20]:
        code = ev["code"]
        name = ev.get("name", "")
        score = ev.get("score", 0) or 0
        reason = ev.get("reason", "")
        indicators_str = ev.get("indicators", "{}")

        line = f"- {name}({code}): 评分{score:.2f}, {reason}, 指标:{indicators_str}"

        try:
            bars_df = dm.storage.get_daily_bars(code)
            if not bars_df.empty:
                last = bars_df.iloc[-1]
                line += f", 最新收盘价{last.get('close', 'N/A')}"
        except Exception:
            pass

        try:
            fin_df = dm.storage.get_financial_data(code)
            if not fin_df.empty:
                latest = fin_df.iloc[0]
                pe = latest.get("pe_ttm", "N/A")
                pb = latest.get("pb", "N/A")
                line += f", PE={pe}, PB={pb}"
        except Exception:
            pass

        stock_lines.append(line)

    news_section = ""
    try:
        news_df = dm.get_news(limit=10)
        if not news_df.empty:
            news_items = [
                f"- [{r.get('publish_time', '')}] {r.get('title', '')}"
                for _, r in news_df.head(10).iterrows()
            ]
            news_section = "\n近期市场新闻：\n" + "\n".join(news_items)
    except Exception:
        pass

    return f"""你是一位专业的A股投资分析助手。以下是当前筛选结果和市场数据，请基于这些信息回答用户问题。

筛选入选股票（共{len(evals)}只）：
{chr(10).join(stock_lines)}
{news_section}

注意：回答简洁专业，使用中文；不要编造数据；投资建议需附带风险提示。"""


def _render_token_usage(dm) -> None:
    """显示本次问答会话的累计 token 消耗（读 llm_call_log）。"""
    since = st.session_state.get("screener_qa_since")
    if not since:
        return
    try:
        usage = dm.storage.get_llm_token_usage(since)
    except Exception:
        return
    if usage["calls"] > 0:
        st.caption(
            f"🔢 本次问答累计：{usage['calls']} 次调用 · "
            f"输入 {usage['input_tokens']:,} tokens · 输出 {usage['output_tokens']:,} tokens"
        )


def _render_qa_section(dm, session_id: int):
    """在筛选结果下方渲染LLM问答区。"""
    st.divider()
    st.subheader("💬 AI问答")
    st.caption("基于当前筛选结果向AI提问，每次提问消耗1次API调用")

    if "screener_qa_history" not in st.session_state:
        st.session_state["screener_qa_history"] = []

    history = st.session_state["screener_qa_history"]

    # 显示对话历史
    for msg in history:
        icon = "🧑" if msg["role"] == "user" else "🤖"
        label = "你" if msg["role"] == "user" else "AI"
        st.markdown(f"**{icon} {label}：** {msg['content']}")

    if len(history) >= 20:
        st.caption("对话历史已超过10轮，较早的对话将被截断")

    # 输入表单
    with st.form("qa_form", clear_on_submit=True):
        user_input = st.text_area("输入问题", height=80,
                                  placeholder="例如：这些股票中哪只最适合短线操作？")
        submitted = st.form_submit_button("发送", type="primary")

    if submitted and user_input.strip():
        # 首次提问时打点，作为 token 统计下界
        st.session_state.setdefault("screener_qa_since", datetime.now().isoformat())

        history.append({"role": "user", "content": user_input.strip()})

        # 超过10轮时截断最早的一对
        if len(history) > 20:
            history = history[-20:]

        context = _build_qa_context(dm, session_id)
        cfg = get_config()
        from src.analysis.llm_analyzer import LLMAnalyzer
        llm = LLMAnalyzer(cfg)

        with st.spinner("AI思考中…"):
            reply = llm.chat(messages=history, system=context)

        history.append({"role": "assistant", "content": reply})
        st.session_state["screener_qa_history"] = history
        st.rerun()

    _render_token_usage(dm)

    if history:
        if st.button("清空对话", key="qa_clear"):
            st.session_state["screener_qa_history"] = []
            st.session_state.pop("screener_qa_since", None)
            st.rerun()
