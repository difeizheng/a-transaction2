"""UI 核心层：共享服务单例 + 页面错误边界 + 统一格式助手。

解决 UI 重构前的两个横切问题：
1. 每个页面模块各自 ``@st.cache_resource`` 一个 DataManager —— 7 个页面 = 7 个
   SQLite engine/连接池，且 simulator/advisor 重复构造。收敛为单例 ``get_services()``。
2. 页面 render 无错误边界，任何异常都是 Streamlit 红框糊脸。``page_guard``
   装饰器统一兜底：记日志 + st.error，应用其余部分不受影响。
"""
import logging
from datetime import date, datetime, timedelta

import streamlit as st

from src.config import get_config
from src.data.manager import DataManager
from src.trading.simulator import TradingSimulator
from src.analysis.advisor import Advisor

logger = logging.getLogger(__name__)


@st.cache_resource
def get_services():
    """全应用共享 (DataManager, TradingSimulator, Advisor) 单例。"""
    config = get_config()
    dm = DataManager()
    simulator = TradingSimulator(dm)
    advisor = Advisor(config, dm)
    return dm, simulator, advisor


def get_dm() -> DataManager:
    return get_services()[0]


def get_simulator() -> TradingSimulator:
    return get_services()[1]


def get_advisor() -> Advisor:
    return get_services()[2]


def page_guard(fn):
    """页面渲染错误边界：异常 → 日志 + st.error，不再红框糊脸。"""

    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            logger.exception("页面渲染异常: %s.%s", fn.__module__, fn.__name__)
            st.error(f"😵 页面渲染出错：{e}")
            st.caption("详细堆栈已写入应用日志；其他页面不受影响，可切换继续使用。")

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    wrapper.__module__ = fn.__module__
    return wrapper


# ── 统一格式助手（A 股配色：涨红跌绿）────────────────────────────
COLOR_UP = "#e54545"
COLOR_DOWN = "#1a9e4b"
COLOR_FLAT = "#888888"


def pct_color(value: float) -> str:
    if value > 0:
        return COLOR_UP
    if value < 0:
        return COLOR_DOWN
    return COLOR_FLAT


def pct_span(value: float, digits: int = 2) -> str:
    """涨跌百分比 HTML span（+3.20% 红 / -1.05% 绿），配合 unsafe_allow_html。"""
    sign = "+" if value > 0 else ""
    return (f'<span style="color:{pct_color(value)};font-weight:600">'
            f'{sign}{value:.{digits}f}%</span>')


def money_span(value: float) -> str:
    """盈亏金额 HTML span，零值灰色。"""
    sign = "+" if value > 0 else ""
    return (f'<span style="color:{pct_color(value)};font-weight:600">'
            f'{sign}¥{value:,.0f}</span>')


def today_str() -> str:
    return date.today().isoformat()


def render_llm_health_banner(storage) -> None:
    """LLM 连续失败全局横幅：最近 ≥3 次调用全失败且最近一次在 24h 内 → 告警。

    静默失败是本系统最贵的坑（2026-09：provider=openai 发 claude 模型名 404，
    AI 功能全挂无人知）。任何页面渲染前调用，异常静默（banner 绝不能拖垮页面）。
    """
    try:
        calls = storage.get_llm_call_health(limit=5)
        if len(calls) < 3:
            return
        recent = calls[:3]
        if not all(c["success"] == 0 for c in recent):
            return
        latest = datetime.fromisoformat(calls[0]["created_at"])
        if datetime.now() - latest > timedelta(hours=24):
            return
        err = (calls[0].get("error") or "未知错误").split("\n")[0][:120]
        st.warning(
            f"🤖 **LLM 调用连续失败**（{len(recent)} 次，最近 {latest:%m-%d %H:%M}）：{err}。"
            "AI 分析/问答功能暂不可用，请检查 config/config.yaml 的 llm 配置与 API Key。",
            icon="⚠️",
        )
    except Exception:
        pass


def enrich_profit(storage, summary: dict) -> dict:
    """``Portfolio.summary()`` 只返回 cash/market_value/total_value/position_count，
    盈亏需对照 account.initial_cash 现算。UI 侧补充 total_profit / total_profit_pct。"""
    account = storage.get_account() or {}
    initial = account.get("initial_cash") or summary.get("total_value") or 0
    total = summary.get("total_value", 0)
    profit = total - initial
    pct = round(profit / initial * 100, 2) if initial else 0.0
    return {**summary, "total_profit": round(profit, 2), "total_profit_pct": pct}
