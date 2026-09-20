"""LLMAnalyzer 纯逻辑测试（不触网、不调真实 LLM）。

覆盖 2026-09 真实故障：provider=openai 时默认模型错用 claude 模型名 →
dashscope 404，AI 功能静默全挂，错误文本还污染 market_sentiment.summary。
"""
import pytest

from src.analysis.llm_analyzer import LLMAnalyzer


def _make(provider="openai", **llm_cfg):
    cfg = {"llm": {"provider": provider,
                   "model_claude": "claude-sonnet-4-6",
                   "model_openai": "qwen3.5-plus",
                   **llm_cfg}}
    return LLMAnalyzer(cfg, storage=None)


# ── 模型默认跟随 provider ────────────────────────────────────────

class TestProviderAwareModel:
    def test_openai_provider_uses_openai_model(self):
        a = _make("openai")
        assert a._model_main == "qwen3.5-plus"
        assert a._model_light == "qwen3.5-plus"  # light 未配置退化为 main

    def test_openai_light_override(self):
        a = _make("openai", model_openai_light="qwen-turbo")
        assert a._model_main == "qwen3.5-plus"
        assert a._model_light == "qwen-turbo"

    def test_claude_provider_uses_claude_model(self):
        a = _make("claude")
        assert a._model_main == "claude-sonnet-4-6"
        assert a._model_light == "claude-sonnet-4-6"

    def test_claude_light_override(self):
        a = _make("claude", model_claude_light="claude-haiku-4-5")
        assert a._model_light == "claude-haiku-4-5"


# ── 失败哨兵 ────────────────────────────────────────────────────

class TestFailureSentinel:
    def test_is_failure(self):
        assert LLMAnalyzer.is_failure("对话失败: Error code: 404")
        assert LLMAnalyzer.is_failure("分析失败: timeout")
        assert not LLMAnalyzer.is_failure("正常文本")
        assert not LLMAnalyzer.is_failure(None)
        assert not LLMAnalyzer.is_failure("")


# ── 失败响应不落进摘要字段 ──────────────────────────────────────

class TestFailureNotPersisted:
    def test_summarize_market_failure_returns_empty(self, monkeypatch):
        a = _make("openai")
        monkeypatch.setattr(a, "chat", lambda *_, **__: "对话失败: Error code: 404 - boom")
        r = a.summarize_market([], 50.0, "neutral")
        assert r["summary"] == ""
        assert r["llm_ok"] is False
        assert "404" not in str(r)

    def test_summarize_macro_failure_returns_empty(self, monkeypatch):
        a = _make("openai")
        monkeypatch.setattr(a, "chat", lambda *_, **__: "对话失败: boom")
        r = a.summarize_macro(41.0, "bearish", {}, {}, [])
        assert r["summary"] == "" and r["policy_read"] == ""
        assert r["llm_ok"] is False

    def test_analyze_stock_trend_failure_returns_empty(self, monkeypatch):
        a = _make("openai")
        monkeypatch.setattr(a, "call", lambda *_, **__: "分析失败: boom")
        r = a.analyze_stock_trend("600519", "贵州茅台", [], {}, [])
        assert r["analysis"] == ""
        assert r["llm_ok"] is False

    def test_news_sentiment_failure_returns_empty(self, monkeypatch):
        a = _make("openai")
        monkeypatch.setattr(a, "call", lambda *_, **__: "分析失败: boom")
        r = a.analyze_news_sentiment([{"title": "t"}])
        assert r["summary"] == ""
        assert r["llm_ok"] is False

    def test_non_failure_unparsed_text_still_fallback(self, monkeypatch):
        """非失败的不可解析文本仍走原文截断兜底（保留旧行为）。"""
        a = _make("openai")
        monkeypatch.setattr(a, "chat", lambda *_, **__: "这不是JSON但也不是错误")
        r = a.summarize_market([], 50.0, "neutral")
        assert r["summary"] == "这不是JSON但也不是错误"
        assert "llm_ok" not in r


# ── storage 健康查询 ────────────────────────────────────────────

@pytest.mark.integration
class TestLLMCallHealth:
    def test_recent_calls(self, tmp_path):
        from src.data.storage import Storage
        s = Storage(str(tmp_path / "t.db"))
        for i, ok in enumerate([True, False, False, False]):
            s.save_llm_call_log({
                "provider": "openai", "model": "m", "endpoint": "call",
                "prompt_excerpt": "", "raw_response": "",
                "input_tokens": None, "output_tokens": None,
                "latency_ms": 1, "success": ok, "error": None if ok else "boom",
            })
        calls = s.get_llm_call_health(limit=5)
        assert len(calls) == 4
        assert [c["success"] for c in calls[:3]] == [0, 0, 0]  # 倒序，最近 3 次全失败
        assert calls[0]["error"] == "boom"
