"""外部服务状态面板：在侧边栏展示服务名称、地址、认证状态。"""
import streamlit as st


def _mask_key(key: str) -> str:
    """脱敏 API key：保留前3位和后4位。"""
    if not key or len(key) <= 7:
        return "***"
    return f"{key[:3]}***{key[-4:]}"


def get_akshare_info() -> dict:
    return {
        "name": "AKShare 数据源",
        "url": "akshare（本地Python库）",
        "auth": "无需认证",
        "ok": True,
    }


def get_llm_info(config: dict) -> dict:
    cfg = config.get("llm", {})
    provider = cfg.get("provider", "openai")
    if provider == "claude":
        url = "https://api.anthropic.com"
        model = cfg.get("model_claude", "claude-sonnet-4-6")
        key = cfg.get("claude_api_key", "")
    else:
        url = cfg.get("openai_base_url", "https://api.openai.com")
        model = cfg.get("model_openai", "")
        key = cfg.get("openai_api_key", "")
    return {
        "name": f"LLM 分析（{model}）",
        "url": url,
        "auth": f"已配置 {_mask_key(key)}" if key else "未配置 API Key",
        "ok": bool(key),
    }


def get_sqlite_info() -> dict:
    return {
        "name": "SQLite 本地数据库",
        "url": "data/stock.db",
        "auth": "本地文件",
        "ok": True,
    }


def render_service_info(services: list):
    """在 expander 中展示服务状态列表。"""
    with st.expander("外部服务状态", expanded=False):
        for svc in services:
            icon = "🟢" if svc.get("ok") else "🔴"
            st.markdown(
                f"{icon} **{svc['name']}**  \n"
                f"&nbsp;&nbsp;&nbsp;&nbsp;地址：`{svc['url']}`  \n"
                f"&nbsp;&nbsp;&nbsp;&nbsp;认证：{svc['auth']}"
            )
