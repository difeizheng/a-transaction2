"""综合建议生成器：结合选股信号+新闻+LLM分析"""
import json
import logging
from datetime import date
from typing import List
import pandas as pd

from src.analysis.llm_analyzer import LLMAnalyzer
from src.strategy.base import ScreenResult
from src.data.manager import DataManager

logger = logging.getLogger(__name__)


class Advisor:
    def __init__(self, config: dict, data_manager: DataManager):
        self.llm = LLMAnalyzer(config)
        self.dm = data_manager

    def analyze_market(self) -> dict:
        """分析整体市场情绪：真实数据算温度 + LLM 定性总结 + 存库。

        温度由 ``sentiment.compute_market_temperature`` 从指数涨跌 + 板块轮动
        确定性算出（不再由 LLM 凭空给分）；LLM 仅产出 100 字定性总结与关键事件
        （**总 LLM 调用 = 1**，与改造前一致，积分不增）。结果按交易日存库，
        UI 可画情绪温度趋势。

        Returns:
            富 dict 供 UI 全量渲染——temperature/label/components（数据驱动）+
            indices/sectors（行情明细）+ summary/key_events（LLM 文字）+ snapshot_date。
        """
        from src.analysis import sentiment

        snapshot = self.dm.get_market_snapshot()
        index_moves = {code: v["pct_chg"] for code, v in snapshot["indices"].items()}
        sectors = snapshot["sectors"]
        sector_returns = [s["pct_chg"] for s in sectors]

        temp = sentiment.compute_market_temperature(
            index_moves,
            sector_returns,
            sector_advances=snapshot.get("sector_advances"),
            sector_declines=snapshot.get("sector_declines"),
        )

        # LLM 定性总结（1 次调用）
        news_df = self.dm.get_news(limit=30)
        news_list = news_df.to_dict("records") if not news_df.empty else []
        top_sectors = [s["name"] for s in sorted(sectors, key=lambda x: x["pct_chg"], reverse=True)[:5]]
        prose = self.llm.summarize_market(
            news_list,
            temp["temperature"],
            temp["label"],
            index_moves=index_moves,
            top_sectors=top_sectors,
        )

        snapshot_date = snapshot.get("as_of") or date.today().isoformat()
        sector_summary = {
            "median": float(pd.Series(sector_returns).median()) if sector_returns else 0.0,
            "top": [{"name": s["name"], "pct_chg": s["pct_chg"]}
                    for s in sorted(sectors, key=lambda x: x["pct_chg"], reverse=True)[:5]],
            "bottom": [{"name": s["name"], "pct_chg": s["pct_chg"]}
                       for s in sorted(sectors, key=lambda x: x["pct_chg"])[:5]],
        }

        record = {
            "snapshot_date": snapshot_date,
            "temperature": temp["temperature"],
            "label": temp["label"],
            "index_moves_json": index_moves,
            "sector_summary_json": sector_summary,
            "summary": prose.get("summary", ""),
            "key_events_json": prose.get("key_events", []),
        }
        try:
            self.dm.storage.save_market_sentiment(record)
        except Exception as e:
            logger.warning(f"市场情绪快照存库失败: {e}")

        return {
            **record,
            "components": temp["components"],
            "indices": snapshot["indices"],
            "sectors": sectors,
            "as_of": snapshot.get("as_of"),
            "source": snapshot.get("source"),
        }

    def analyze_macro(self) -> dict:
        """分析宏观态势：真实四支柱数据算分 + LLM 政策面定性 + 存库。

        态势分由 ``macro.compute_macro_stance`` 从流动性 / 资金面 / 基本面 / 外部
        确定性算出（**不再由 LLM 给分**）；LLM 仅产出综合定性 + 政策面解读 + 关键
        风险（政策面是纯函数算不了的，正是 LLM 在宏观里唯一不可替代的贡献）。
        **总 LLM 调用 = 1**，积分不增。结果按交易日存库，UI 可画 regime 趋势。

        Returns:
            富 dict 供 UI 全量渲染——score/label/stance/components/indicators（数据
            驱动）+ summary/policy_read/key_risks（LLM 文字）+ indicators_meta（带
            as_of 的原始指标，UI 展示用）+ snapshot_date。
        """
        from src.analysis import macro

        snapshot = self.dm.get_macro_snapshot()
        result = macro.compute_macro_stance(snapshot["indicators"])

        # LLM 政策面定性（1 次调用）
        news_df = self.dm.get_news(limit=30)
        news_list = news_df.to_dict("records") if not news_df.empty else []
        prose = self.llm.summarize_macro(
            result["score"], result["label"], result["components"],
            snapshot["indicators"], news_list,
        )

        snapshot_date = snapshot.get("as_of") or date.today().isoformat()
        record = {
            "snapshot_date": snapshot_date,
            "score": result["score"],
            "label": result["label"],
            "stance": result["stance"],
            "components_json": result["components"],
            "indicators_json": result["indicators"],
            "summary": prose.get("summary", ""),
            "key_risks_json": prose.get("key_risks", []),
        }
        try:
            self.dm.storage.save_macro_snapshot(record)
        except Exception as e:
            logger.warning(f"宏观态势快照存库失败: {e}")

        return {
            **record,
            "policy_read": prose.get("policy_read", ""),
            "indicators_meta": snapshot["indicators"],   # 带 latest/reference/as_of，UI 展示用
            "indicator_names": snapshot.get("indicator_names", {}),
            "as_of": snapshot.get("as_of"),
            "source": snapshot.get("source"),
        }

    def analyze_stock(self, screen_result: ScreenResult) -> dict:
        """对单只选中股票做深度分析，返回建议"""
        code, name = screen_result.code, screen_result.name

        # 获取个股新闻
        news_df = self.dm.get_news(code=code, limit=10)
        news_list = news_df.to_dict("records") if not news_df.empty else []

        # 获取最近5日K线
        bars_df = self.dm.get_daily_bars(code)
        recent_bars = bars_df.tail(5).to_dict("records") if not bars_df.empty else []

        result = self.llm.analyze_stock_trend(
            code=code,
            name=name,
            news_list=news_list,
            screen_signals=screen_result.signals,
            recent_bars=recent_bars,
        )
        result["code"] = code
        result["name"] = name
        result["screen_score"] = screen_result.score
        result["screen_reason"] = screen_result.reason
        return result

    def batch_analyze(self, screen_results: List[ScreenResult], max_stocks: int = 5,
                      status_callback=None) -> List[dict]:
        """批量分析选股结果（限制数量控制API成本）
        status_callback(current, total, code, name, stage)
        """
        targets = screen_results[:max_stocks]
        total = len(targets)
        results = []
        for i, sr in enumerate(targets, 1):
            logger.info(f"分析 {sr.name}({sr.code})...")
            try:
                if status_callback:
                    status_callback(i, total, sr.code, sr.name, "拉取新闻")
                advice = self.analyze_stock(sr)
                if status_callback:
                    status_callback(i, total, sr.code, sr.name, "完成")
                results.append(advice)
            except Exception as e:
                logger.error(f"分析 {sr.code} 失败: {e}")
        return results

    def analyze_stock_deep(self, code: str, name: str,
                           strategy_keys: list = None) -> dict:
        """
        多维度深度分析单只股票。
        返回: {code, name, realtime, technical, news, llm_analysis}
        """
        from src.strategy.screener import STRATEGY_REGISTRY

        result = {"code": code, "name": name}

        # 1. 实时行情
        try:
            quotes = self.dm.get_realtime_quotes([code])
            if not quotes.empty:
                q = quotes.iloc[0]
                result["realtime"] = {
                    "price": float(q["price"]),
                    "pct_chg": float(q["pct_chg"]),
                    "volume": float(q["volume"]),
                    "amount": float(q["amount"]),
                }
            else:
                result["realtime"] = None
        except Exception:
            result["realtime"] = None

        # 2. 技术策略评估
        tech_keys = strategy_keys or ["ma_cross", "macd_golden", "kdj_oversold", "boll_breakout"]
        tech_results = []
        for key in tech_keys:
            cls = STRATEGY_REGISTRY.get(key)
            if cls is None:
                continue
            strategy = cls()
            if not strategy.supports_evaluate():
                continue
            try:
                ev = strategy.evaluate_stock(code, name, self.dm)
                tech_results.append({
                    "strategy_key": key,
                    "strategy_name": getattr(strategy, "description", key),
                    "selected": ev.selected,
                    "score": ev.score,
                    "indicators": ev.indicators,
                    "conditions": [
                        {"label": c.label, "passed": c.passed, "detail": c.detail}
                        for c in ev.conditions
                    ],
                    "reason": ev.reason,
                })
            except Exception as e:
                logger.warning(f"策略 {key} 评估 {code} 失败: {e}")
        result["technical"] = tech_results

        # 3. 个股新闻（含URL和情绪标签）
        news_df = self.dm.get_news(code=code, limit=10)
        news_list = []
        if not news_df.empty:
            for _, row in news_df.iterrows():
                news_list.append({
                    "title": row.get("title", ""),
                    "url": row.get("url", "") or "",
                    "sentiment": row.get("sentiment", "") or "",
                    "content": row.get("content", "") or "",
                    "publish_time": str(row.get("publish_time", "")),
                })
        result["news"] = news_list

        # 4. 增强版LLM综合分析
        result["llm_analysis"] = self._call_enhanced_llm(
            code, name, result["realtime"], tech_results, news_list
        )

        return result

    def _call_enhanced_llm(self, code: str, name: str, realtime: dict,
                           tech_results: list, news_list: list) -> dict:
        """构建增强版LLM提示词，综合多维度数据。"""
        # 实时行情段
        if realtime:
            price_section = (
                f"实时行情：现价 {realtime['price']}，涨跌幅 {realtime['pct_chg']}%，"
                f"成交量 {realtime['volume']:.0f} 股，成交额 {realtime['amount']:.0f} 元"
            )
        else:
            price_section = "实时行情：暂无数据（非交易时段）"

        # 技术面段
        tech_lines = []
        for t in tech_results:
            status = "满足" if t["selected"] else "不满足"
            ind_str = "、".join(f"{k}={v}" for k, v in t["indicators"].items())
            tech_lines.append(f"- {t.get('strategy_name', t['strategy_key'])}：{status}（{ind_str}）")
        tech_section = "\n".join(tech_lines) if tech_lines else "暂无技术指标数据"

        # 新闻段
        news_lines = []
        for n in news_list[:10]:
            sent = f"[{n['sentiment']}]" if n.get("sentiment") else ""
            news_lines.append(f"- {sent} {n['title']}")
        news_section = "\n".join(news_lines) if news_lines else "暂无相关新闻"

        # 近5日K线
        bars_text = ""
        try:
            bars_df = self.dm.get_daily_bars(code)
            if not bars_df.empty:
                recent = bars_df.tail(5)
                cols = [c for c in ["trade_date", "open", "close", "high", "low", "pct_chg"] if c in recent.columns]
                bars_text = "近5日K线：\n" + recent[cols].to_string(index=False)
        except Exception:
            pass

        prompt = f"""你是一位资深A股投资分析师。请对以下股票进行全面深度分析。

股票：{name}（{code}）

一、{price_section}

{bars_text}

二、技术面分析：
{tech_section}

三、消息面：
{news_section}

请以JSON格式返回（不要有其他内容）：
{{
  "trend": "上涨/震荡/下跌",
  "confidence": 0到100（置信度）,
  "buy_suggestion": "建议买入/观望/不建议",
  "entry_price_note": "建议入场价位说明",
  "stop_loss_pct": 止损百分比（如8表示8%）,
  "take_profit_pct": 止盈百分比（如20表示20%）,
  "technical_summary": "技术面综合解读（100字以内）",
  "news_summary": "消息面综合解读（100字以内）",
  "analysis": "综合投资建议（200字以内）",
  "risk_warning": "主要风险提示",
  "key_factors": ["关键影响因素1", "关键影响因素2", "关键影响因素3"]
}}"""

        raw = self.llm.call(prompt)
        try:
            start = raw.find("{")
            end = raw.rfind("}") + 1
            return json.loads(raw[start:end])
        except Exception:
            return {
                "trend": "未知", "confidence": 0,
                "buy_suggestion": "观望", "analysis": raw[:300],
                "stop_loss_pct": 8, "take_profit_pct": 20,
            }
