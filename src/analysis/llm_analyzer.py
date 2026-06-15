"""LLM分析引擎：支持Claude API和OpenAI兼容接口"""
import json
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


class LLMAnalyzer:
    def __init__(self, config: dict):
        self.cfg = config["llm"]
        self.provider = self.cfg.get("provider", "claude")

    def _call_claude(self, prompt: str) -> str:
        import anthropic
        client = anthropic.Anthropic(api_key=self.cfg["claude_api_key"])
        msg = client.messages.create(
            model=self.cfg.get("model_claude", "claude-sonnet-4-6"),
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text

    def _call_openai(self, prompt: str) -> str:
        from openai import OpenAI
        kwargs = {"api_key": self.cfg["openai_api_key"]}
        if self.cfg.get("openai_base_url"):
            kwargs["base_url"] = self.cfg["openai_base_url"]
        client = OpenAI(**kwargs)
        resp = client.chat.completions.create(
            model=self.cfg.get("model_openai", "gpt-4o"),
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2048,
        )
        return resp.choices[0].message.content

    def call(self, prompt: str) -> str:
        try:
            if self.provider == "openai":
                return self._call_openai(prompt)
            return self._call_claude(prompt)
        except Exception as e:
            logger.error(f"LLM调用失败: {e}")
            return f"分析失败: {e}"

    def _chat_openai(self, messages: list, system: str = None) -> str:
        from openai import OpenAI
        kwargs = {"api_key": self.cfg["openai_api_key"]}
        if self.cfg.get("openai_base_url"):
            kwargs["base_url"] = self.cfg["openai_base_url"]
        client = OpenAI(**kwargs)
        full_messages = []
        if system:
            full_messages.append({"role": "system", "content": system})
        full_messages.extend(messages)
        resp = client.chat.completions.create(
            model=self.cfg.get("model_openai", "gpt-4o"),
            messages=full_messages,
            max_tokens=2048,
        )
        return resp.choices[0].message.content

    def _chat_claude(self, messages: list, system: str = None) -> str:
        import anthropic
        client = anthropic.Anthropic(api_key=self.cfg["claude_api_key"])
        kwargs = {
            "model": self.cfg.get("model_claude", "claude-sonnet-4-6"),
            "max_tokens": 2048,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        msg = client.messages.create(**kwargs)
        return msg.content[0].text

    def chat(self, messages: list, system: str = None) -> str:
        """多轮对话。messages = [{"role": "user"/"assistant", "content": "..."}]"""
        try:
            if self.provider == "openai":
                return self._chat_openai(messages, system)
            return self._chat_claude(messages, system)
        except Exception as e:
            logger.error(f"LLM对话失败: {e}")
            return f"对话失败: {e}"

    def analyze_news_sentiment(self, news_list: List[dict]) -> dict:
        """批量分析新闻情绪，返回 {sentiment, summary, key_events}"""
        if not news_list:
            return {"sentiment": "neutral", "summary": "无新闻数据", "key_events": []}

        news_text = "\n".join([
            f"- [{n.get('publish_time', '')}] {n.get('title', '')}"
            for n in news_list[:20]  # 最多20条，控制token
        ])
        prompt = f"""你是一位专业的A股市场分析师。请分析以下财经新闻，给出市场情绪判断。

新闻列表：
{news_text}

请以JSON格式返回（不要有其他内容）：
{{
  "sentiment": "bullish/bearish/neutral",
  "sentiment_score": 0到100的数字（100最看涨），
  "summary": "一句话总结市场情绪",
  "key_events": ["关键事件1", "关键事件2"]
}}"""
        raw = self.call(prompt)
        try:
            # 提取JSON部分
            start = raw.find("{")
            end = raw.rfind("}") + 1
            return json.loads(raw[start:end])
        except Exception:
            return {"sentiment": "neutral", "summary": raw[:200], "key_events": []}

    def analyze_stock_trend(
        self,
        code: str,
        name: str,
        news_list: List[dict],
        screen_signals: dict,
        recent_bars: List[dict],
    ) -> dict:
        """
        综合分析单只股票趋势，给出买卖建议
        返回: {trend, buy_suggestion, stop_loss, take_profit, analysis}
        """
        news_text = "\n".join([
            f"- {n.get('title', '')}"
            for n in news_list[:10]
        ]) or "暂无相关新闻"

        signals_text = json.dumps(screen_signals, ensure_ascii=False, indent=2)

        price_text = ""
        if recent_bars:
            last = recent_bars[-1]
            price_text = f"最新收盘价: {last.get('close', 'N/A')}，近5日涨跌幅: {last.get('pct_chg', 'N/A')}%"

        prompt = f"""你是一位专业的A股投资顾问。请综合分析以下信息，给出投资建议。

股票：{name}（{code}）
{price_text}

技术/基本面信号：
{signals_text}

相关新闻：
{news_text}

请以JSON格式返回（不要有其他内容）：
{{
  "trend": "上涨/震荡/下跌",
  "confidence": 0到100（置信度）,
  "buy_suggestion": "建议买入/观望/不建议",
  "entry_price_note": "建议入场价位说明",
  "stop_loss_pct": 止损百分比（如5表示5%）,
  "take_profit_pct": 止盈百分比（如15表示15%）,
  "analysis": "100字以内的综合分析",
  "risk_warning": "主要风险提示"
}}"""
        raw = self.call(prompt)
        try:
            start = raw.find("{")
            end = raw.rfind("}") + 1
            return json.loads(raw[start:end])
        except Exception:
            return {
                "trend": "未知", "confidence": 0,
                "buy_suggestion": "观望", "analysis": raw[:300],
                "stop_loss_pct": 5, "take_profit_pct": 15,
            }
