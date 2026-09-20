"""LLM分析引擎：支持Claude API和OpenAI兼容接口。

设计要点：
- 可观测性：每次调用落 ``llm_call_log``（provider/model/endpoint/prompt摘要/raw输出/
  token/延迟/成功与否），供审计回放、成本核算与幻觉追溯（``storage`` 可选注入）。
- 成本：摘要/定性类调用用 light 模型（Haiku，约主模型 1/10 成本），深度推理用主模型；
  Claude 路径对 system 指令启用 prompt caching（读 cache 成本 1/10）。
- 健壮性：所有 SDK 调用受 ``request_timeout`` 约束（默认 60s，防挂起卡死交易循环）；
  JSON 解析容错（strip / 去 markdown 包裹 / 失败记 raw 日志而非静默丢弃）。

公开方法签名向后兼容：``call(prompt)`` / ``chat(messages, system)`` 旧调用不受影响，
新增 ``model`` / ``endpoint`` 可选参数用于分级与埋点。
"""
import json
import logging
import time
from typing import List, Optional

logger = logging.getLogger(__name__)


def _usage_claude(msg) -> dict:
    u = getattr(msg, "usage", None)
    if not u:
        return {}
    return {
        "input_tokens": getattr(u, "input_tokens", None),
        "output_tokens": getattr(u, "output_tokens", None),
    }


def _usage_openai(resp) -> dict:
    u = getattr(resp, "usage", None)
    if not u:
        return {}
    return {
        "input_tokens": getattr(u, "prompt_tokens", None),
        "output_tokens": getattr(u, "completion_tokens", None),
    }


class LLMAnalyzer:
    def __init__(self, config: dict, storage=None):
        self.cfg = config["llm"]
        self.storage = storage  # 可选：传入则每次调用落 llm_call_log
        self.provider = self.cfg.get("provider", "claude")
        # SDK 默认 timeout 600s：网络挂起时 AutoTrader 串行调 top5，任一挂起→run()
        # 卡 10 分钟。默认 60s（兼容端点长 prompt 首调可超 30s），可由 config ``request_timeout`` 覆盖。
        self._timeout = float(self.cfg.get("request_timeout", 60.0))
        self._claude_client = None
        self._openai_client = None
        # 模型分级：摘要/定性用 light（约 1/10 成本），深度推理用 main。
        # light 未配置时退化为 main（不破坏仅配了单一模型的旧 config）。
        # 主模型必须跟 provider 走：openai 兼容端点不认识 claude 模型名，
        # 混用会 404（2026-09 真实故障：provider=openai 却发 claude-sonnet-4-6）。
        self._model_openai = self.cfg.get("model_openai", "gpt-4o")
        if self.provider == "openai":
            self._model_main = self._model_openai
            self._model_light = self.cfg.get("model_openai_light", self._model_main)
        else:
            self._model_main = self.cfg.get("model_claude", "claude-sonnet-4-6")
            self._model_light = self.cfg.get("model_claude_light", self._model_main)

    # ── client 缓存（含 timeout）─────────────────────────────────
    def _get_claude_client(self):
        if self._claude_client is None:
            import anthropic
            self._claude_client = anthropic.Anthropic(
                api_key=self.cfg["claude_api_key"], timeout=self._timeout,
            )
        return self._claude_client

    def _get_openai_client(self):
        if self._openai_client is None:
            from openai import OpenAI
            kwargs = {"api_key": self.cfg["openai_api_key"], "timeout": self._timeout}
            if self.cfg.get("openai_base_url"):
                kwargs["base_url"] = self.cfg["openai_base_url"]
            self._openai_client = OpenAI(**kwargs)
        return self._openai_client

    # ── 底层调用：返回 (text, usage) ──────────────────────────────
    def _call_claude(self, prompt: str, model: str = None) -> tuple:
        client = self._get_claude_client()
        msg = client.messages.create(
            model=model or self._model_main,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text, _usage_claude(msg)

    def _call_openai(self, prompt: str, model: str = None) -> tuple:
        client = self._get_openai_client()
        resp = client.chat.completions.create(
            model=model or self._model_openai,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2048,
        )
        return resp.choices[0].message.content, _usage_openai(resp)

    def _chat_claude(self, messages: list, system: str = None, model: str = None) -> tuple:
        client = self._get_claude_client()
        kwargs = {
            "model": model or self._model_main,
            "max_tokens": 2048,
            "messages": messages,
        }
        if system:
            # prompt caching：system 命中缓存（Anthropic 读 cache 成本 1/10）。
            # 用带 cache_control 的 block 结构而非纯字符串。
            kwargs["system"] = [{
                "type": "text", "text": system,
                "cache_control": {"type": "ephemeral"},
            }]
        msg = client.messages.create(**kwargs)
        return msg.content[0].text, _usage_claude(msg)

    def _chat_openai(self, messages: list, system: str = None, model: str = None) -> tuple:
        client = self._get_openai_client()
        full = []
        if system:
            full.append({"role": "system", "content": system})
        full.extend(messages)
        resp = client.chat.completions.create(
            model=model or self._model_openai,
            messages=full,
            max_tokens=2048,
        )
        return resp.choices[0].message.content, _usage_openai(resp)

    # ── 公开入口（带埋点）────────────────────────────────────────
    def call(self, prompt: str, model: str = None, endpoint: str = "call") -> str:
        """单轮调用。``model=None`` 用主模型；自动埋点 llm_call_log。"""
        used = model or self._model_main
        t0 = time.time()
        try:
            if self.provider == "openai":
                text, usage = self._call_openai(prompt, used)
            else:
                text, usage = self._call_claude(prompt, used)
            self._log(endpoint, used, prompt, text, usage, True, None, t0)
            return text
        except Exception as e:
            self._log(endpoint, used, prompt, "", None, False, str(e), t0)
            logger.error(f"LLM调用失败: {e}")
            return f"分析失败: {e}"

    def chat(self, messages: list, system: str = None,
             model: str = None, endpoint: str = "chat") -> str:
        """多轮对话。``messages = [{"role": "user"/"assistant", "content": "..."}]``"""
        used = model or self._model_main
        t0 = time.time()
        try:
            if self.provider == "openai":
                text, usage = self._chat_openai(messages, system, used)
            else:
                text, usage = self._chat_claude(messages, system, used)
            self._log(endpoint, used, str(messages), text, usage, True, None, t0)
            return text
        except Exception as e:
            self._log(endpoint, used, str(messages), "", None, False, str(e), t0)
            logger.error(f"LLM对话失败: {e}")
            return f"对话失败: {e}"

    # 失败响应哨兵：call/chat 失败时把错误当文本返回（保持字符串契约），
    # 上层落库/展示前必须用 is_failure 拦截，否则错误文本会污染快照表。
    FAIL_PREFIXES = ("分析失败:", "对话失败:")

    @classmethod
    def is_failure(cls, text) -> bool:
        return isinstance(text, str) and text.startswith(cls.FAIL_PREFIXES)

    def _log(self, endpoint, model, prompt, response, usage,
             success: bool, error, t0: float) -> None:
        """落库 + 结构化日志。落库失败只 warning，不阻断主流程。"""
        latency = int((time.time() - t0) * 1000)
        excerpt = (prompt or "")[:500]
        rec = {
            "provider": self.provider, "model": model, "endpoint": endpoint,
            "prompt_excerpt": excerpt,
            "raw_response": (response or "")[:2000] or None,
            "input_tokens": (usage or {}).get("input_tokens"),
            "output_tokens": (usage or {}).get("output_tokens"),
            "latency_ms": latency, "success": 1 if success else 0, "error": error,
        }
        if self.storage is not None:
            try:
                self.storage.save_llm_call_log(rec)
            except Exception as e:
                logger.warning(f"llm_call_log 落库失败: {e}")
        logger.info(
            f"LLM {endpoint} provider={self.provider} model={model} "
            f"ok={success} {latency}ms in={rec['input_tokens']} out={rec['output_tokens']}"
        )

    @staticmethod
    def _parse_json_lenient(raw: str, endpoint: str = "parse") -> Optional[dict]:
        """容错 JSON 提取：strip → 去 markdown 代码块 → 取首尾花括号 → loads。

        模型在 JSON 前后多吐解释、返回 ```json 代码块、key 漏逗号等都会让旧实现
        ``raw.find('{')`` 失败 → 静默 fallback「观望+confidence=0」（系统性永不买入）。
        本方法失败时记 raw 日志（可观测），返回 None 由调用方走 fallback。
        """
        if not raw:
            return None
        text = raw.strip()
        if text.startswith("```"):
            nl = text.find("\n")
            text = text[nl + 1:] if nl >= 0 else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except Exception as e:
                logger.warning(f"JSON 解析失败({endpoint}): {e}; raw[:200]={raw[:200]!r}")
        else:
            logger.warning(f"未找到 JSON 体({endpoint}); raw[:200]={raw[:200]!r}")
        return None

    # ── 业务方法 ─────────────────────────────────────────────────
    def analyze_news_sentiment(self, news_list: List[dict]) -> dict:
        """批量分析新闻情绪，返回 {sentiment, summary, key_events}。用 light 模型。"""
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
        raw = self.call(prompt, model=self._model_light, endpoint="news_sentiment")
        data = self._parse_json_lenient(raw, "news_sentiment")
        if data:
            return data
        if self.is_failure(raw):
            return {"sentiment": "neutral", "summary": "", "key_events": [], "llm_ok": False}
        return {"sentiment": "neutral", "summary": raw[:200], "key_events": []}

    def summarize_market(
        self,
        news_list: List[dict],
        temperature: float,
        label: str,
        index_moves: dict = None,
        top_sectors: list = None,
    ) -> dict:
        """给定**已由数据算出**的情绪温度，让 LLM 只产出定性总结 + 关键事件。

        走 chat 接口（system 启用 prompt caching）+ light 模型，降本。温度来自
        ``sentiment.compute_market_temperature``，LLM 不给分。保持总调用 = 1。

        Returns:
            ``{summary: str, key_events: list[str]}``
        """
        news_text = "\n".join([
            f"- [{n.get('publish_time', '')}] {n.get('title', '')}"
            for n in (news_list or [])[:20]
        ]) or "暂无财经新闻"

        label_cn = {"bullish": "偏多（看涨）", "bearish": "偏空（看跌）", "neutral": "中性"}.get(label, "中性")
        index_desc = ""
        if index_moves:
            parts = [f"{c} {v:+.2f}%" for c, v in index_moves.items()]
            index_desc = f"\n主要指数涨跌：{', '.join(parts)}"
        sector_desc = ""
        if top_sectors:
            sector_desc = f"\n领涨板块：{', '.join(top_sectors)}"

        system = (
            "你是一位专业的A股市场分析师。市场情绪温度已由真实行情数据算出，"
            "请不要输出任何分数，只基于信息产出定性解读。严格以JSON返回，"
            "包含 summary（100字内的市场情绪定性总结）与 key_events（关键事件列表），"
            "不要包含任何数字评分，不要输出 JSON 以外的内容。"
        )
        user = (
            f"市场情绪温度：{temperature:.1f} / 100（{label_cn}）"
            f"{index_desc}{sector_desc}\n\n近期新闻：\n{news_text}\n\n"
            f'请返回 JSON：{{"summary": "...", "key_events": ["...", "...", "..."]}}'
        )
        raw = self.chat(
            [{"role": "user", "content": user}], system=system,
            model=self._model_light, endpoint="summarize_market",
        )
        data = self._parse_json_lenient(raw, "summarize_market")
        if data:
            return {"summary": data.get("summary", ""), "key_events": data.get("key_events", [])}
        if self.is_failure(raw):
            return {"summary": "", "key_events": [], "llm_ok": False}
        return {"summary": raw[:200], "key_events": []}

    def summarize_macro(
        self,
        score: float,
        label: str,
        components: dict,
        indicators: dict,
        news_list: List[dict],
    ) -> dict:
        """给定**已由数据算出**的宏观态势分，让 LLM 只产出综合定性 + 政策面 + 关键风险。

        走 chat 接口（system caching）+ light 模型。态势分来自 ``macro.compute_macro_stance``，
        LLM 不给分；政策面是纯函数算不了的，正由 LLM 从新闻提炼。保持总调用 = 1。

        Returns:
            ``{summary: str, policy_read: str, key_risks: list[str]}``
        """
        news_text = "\n".join([
            f"- [{n.get('publish_time', '')}] {n.get('title', '')}"
            for n in (news_list or [])[:20]
        ]) or "暂无财经新闻"

        label_cn = {"bullish": "宽松积极", "bearish": "偏紧", "neutral": "中性"}.get(label, "中性")
        comp_desc = "；".join(
            f"{p} {v['signal']:+.2f}" for p, v in (components or {}).items()
        ) or "无"

        system = (
            "你是一位专业的A股宏观分析师。宏观态势分已由真实数据算出，"
            "请不要输出任何分数，只基于支柱信号与政策新闻产出定性解读。严格以JSON返回，"
            "包含 summary（100字内）、policy_read（80字内的政策面解读）、key_risks（列表），"
            "不含数字评分，不输出 JSON 以外的内容。"
        )
        user = (
            f"宏观态势分：{score:.1f} / 100（{label_cn}）\n"
            f"支柱信号（-1 偏空 ~ +1 偏多）：{comp_desc}\n\n"
            f"近期新闻：\n{news_text}\n\n"
            f'请返回 JSON：{{"summary": "...", "policy_read": "...", "key_risks": ["...", "..."]}}'
        )
        raw = self.chat(
            [{"role": "user", "content": user}], system=system,
            model=self._model_light, endpoint="summarize_macro",
        )
        data = self._parse_json_lenient(raw, "summarize_macro")
        if data:
            return {
                "summary": data.get("summary", ""),
                "policy_read": data.get("policy_read", ""),
                "key_risks": data.get("key_risks", []),
            }
        if self.is_failure(raw):
            return {"summary": "", "policy_read": "", "key_risks": [], "llm_ok": False}
        return {"summary": raw[:200], "policy_read": "", "key_risks": []}

    def analyze_stock_trend(
        self,
        code: str,
        name: str,
        news_list: List[dict],
        screen_signals: dict,
        recent_bars: List[dict],
    ) -> dict:
        """综合分析单只股票趋势。用主模型（深度推理）。"""
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
        raw = self.call(prompt, endpoint="stock_trend")
        data = self._parse_json_lenient(raw, "stock_trend")
        if data:
            return data
        if self.is_failure(raw):
            return {
                "trend": "未知", "confidence": 0,
                "buy_suggestion": "观望", "analysis": "",
                "stop_loss_pct": 5, "take_profit_pct": 15,
                "llm_ok": False,
            }
        return {
            "trend": "未知", "confidence": 0,
            "buy_suggestion": "观望", "analysis": raw[:300],
            "stop_loss_pct": 5, "take_profit_pct": 15,
        }
