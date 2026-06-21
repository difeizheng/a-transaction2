"""自动交易引擎：策略信号 + 风控 → 交易建议/下单。

【方案 B 定位（2026-06 重构）】本模块默认为**信号生成器**（``auto_execute=False``）：
跑完「止盈止损扫描 + 策略筛选 + 风控预算」后，产出**买入/卖出建议报告**，
**不自动下单**。用户审阅后手动执行（或纸面模拟盘）。彻底规避实盘自动交易风险。

LLM（Advisor）在此流程中只做**展示注释**——不再作为买卖决策门。旧实现把 LLM
自报的 ``confidence``/``buy_suggestion`` 当硬门，是未校准的随机门，已移除。
真正决定能否下单的是风控预算（单股仓位、总仓位、单日笔数、回撤）。

如确需自动下单（如纸面模拟盘自动化），构造时传 ``auto_execute=True``——风险自担。
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Callable

from src.data.manager import DataManager
from src.trading.simulator import TradingSimulator
from src.trading.risk import (
    compute_drawdown_pct,
    compute_add_buy_quantity,
    deescalation_level,
    compute_trim_quantity,
    would_breach_concentration,
)

logger = logging.getLogger(__name__)

# AI 分析的候选股上限（控制 LLM token 成本）。AI 仅做注释，不再过滤，故只分析
# 综合得分最高的前 N 只；其余候选照常进入风控预算（无 AI 注释）。
AI_ANALYSIS_TOP_N = 5


@dataclass
class RiskParams:
    """风控参数"""
    max_position_pct: float = 20.0        # 单股最大仓位占比 (%)
    max_total_position_pct: float = 80.0  # 总仓位上限 (%)
    max_daily_trades: int = 5             # 单日最大交易次数
    stop_loss_pct: float = 8.0            # 止损线 (%)
    take_profit_pct: float = 20.0         # 止盈线 (%)
    max_drawdown_pct: float = 15.0        # 最大回撤暂停线 (%)（基于 high-water mark）
    drawdown_deescalation: bool = True    # 回撤分档减仓（不止暂停开仓，深套时主动 trim）
    max_industry_pct: float = 30.0        # 单一行业集中度上限 (%)（组合层约束，0 或 100=不检）
    min_ai_confidence: float = 60.0       # 保留字段：仅用于 UI 展示，**不再是决策门**


@dataclass
class ExecutionReport:
    """执行/建议报告"""
    timestamp: str = ""
    mode: str = "signal"                  # signal（建议，默认） | auto（自动下单）
    strategy_keys: List[str] = field(default_factory=list)  # 输入策略键（落库可审计）
    drawdown_pct: Optional[float] = None  # 当前回撤（high-water mark 口径，供 UI 展示）
    deescalation_tier: Optional[int] = None        # 回撤减仓命中档位（0=最轻…N-1=最深）
    deescalation_keep_ratio: Optional[float] = None  # 命中档位的目标保留比例
    strategy_candidates: List[dict] = field(default_factory=list)
    ai_recommendations: List[dict] = field(default_factory=list)   # AI 注释（展示用，非决策门）
    risk_blocked: List[dict] = field(default_factory=list)
    executed_orders: List[dict] = field(default_factory=list)      # 仅 auto 模式
    buy_suggestions: List[dict] = field(default_factory=list)      # signal 模式：建议买入
    sell_suggestions: List[dict] = field(default_factory=list)     # signal 模式：建议止盈止损/回撤减仓卖出
    stop_loss_sells: List[dict] = field(default_factory=list)      # 仅 auto 模式
    take_profit_sells: List[dict] = field(default_factory=list)    # 仅 auto 模式
    deescalation_sells: List[dict] = field(default_factory=list)   # 仅 auto 模式：回撤分档减仓
    errors: List[str] = field(default_factory=list)
    paused_reason: str = ""

    @property
    def total_executed(self) -> int:
        return (len(self.executed_orders) + len(self.stop_loss_sells)
                + len(self.take_profit_sells) + len(self.deescalation_sells))


class AutoTrader:
    def __init__(self, data_manager: DataManager, simulator: TradingSimulator,
                 advisor, risk_params: RiskParams = None,
                 auto_execute: bool = False):
        """
        :param auto_execute: False（默认）= 信号生成器，只产出建议不下单；
            True = 自动下单（纸面模拟盘自动化，风险自担）。
        """
        self.dm = data_manager
        self.simulator = simulator
        self.advisor = advisor
        self.risk = risk_params or RiskParams()
        self.auto_execute = auto_execute

    def run(self, stock_pool: List[dict], strategy_keys: List[str],
            status_callback: Optional[Callable] = None) -> ExecutionReport:
        """
        执行一轮扫描：止盈止损 → 回撤检查 → 策略筛选 → 风控预算 → 建议/下单。
        stock_pool: [{"code": "000001", "name": "平安银行"}, ...]
        strategy_keys: ["ma_cross", "macd_golden", ...]
        status_callback(phase: str, detail: str)
        """
        report = ExecutionReport(
            timestamp=datetime.now().isoformat(),
            mode="auto" if self.auto_execute else "signal",
            strategy_keys=list(strategy_keys or []),
        )

        def cb(phase: str, detail: str = ""):
            logger.info(f"[AutoTrader] {phase}: {detail}")
            if status_callback:
                status_callback(phase, detail)

        # Phase 0: 止盈止损检查（auto 模式自动卖出；signal 模式记为卖出建议）
        cb("止盈止损检查", "检查现有持仓...")
        self._check_stop_loss_take_profit(report)

        # 回撤检查（high-water mark 口径，设置 report.drawdown_pct + 更新峰值）
        drawdown_exceeded = self._is_max_drawdown_exceeded(report)

        # 回撤分档减仓：无论是否触发硬暂停，深套时都先做防御性 trim
        # （旧实现熔断只暂停开仓，已深套持仓照旧裸奔——审计报告 P1-B）
        self._apply_drawdown_deescalation(report)

        if drawdown_exceeded:
            report.paused_reason = (
                f"组合回撤 {report.drawdown_pct:.1f}% 超过 {self.risk.max_drawdown_pct}%，"
                f"暂停新开仓（已执行回撤分档减仓，但不再开新仓）"
            )
            cb("暂停", report.paused_reason)
            self._persist_report(report)
            return report

        if not stock_pool:
            cb("完成", "股票池为空")
            self._persist_report(report)
            return report

        # Phase 1: 策略筛选
        cb("策略筛选", f"对 {len(stock_pool)} 只股票运行 {len(strategy_keys)} 个策略...")
        candidates = self._run_strategies(stock_pool, strategy_keys, report)
        cb("策略筛选完成", f"产生 {len(candidates)} 个候选信号")

        if not candidates:
            cb("完成", "策略未产生买入信号")
            self._persist_report(report)
            return report

        # Phase 2: AI 注释（仅展示，非决策门；限 top N 控制 token 成本）
        cb("AI注释", f"分析 top {AI_ANALYSIS_TOP_N} 候选股...")
        self._annotate_with_ai(candidates, report)
        cb("AI注释完成", f"{len(report.ai_recommendations)} 只已注释")

        # Phase 3: 风控预算 → 建议/下单
        action = "执行" if self.auto_execute else "生成建议"
        cb(f"风控+{action}", f"处理 {len(candidates)} 个信号...")
        self._build_and_maybe_execute(candidates, report)
        cb("完成", self._completion_summary(report))

        self._persist_report(report)
        return report

    def _persist_report(self, report: ExecutionReport) -> None:
        """落库 ExecutionReport（可审计回放）。best-effort：取数失败不影响主流程。"""
        try:
            from dataclasses import asdict
            self.dm.storage.save_execution_report(asdict(report))
        except Exception as e:
            logger.warning(f"落库运行报告失败（不影响主流程）: {e}")

    def _completion_summary(self, report: ExecutionReport) -> str:
        if self.auto_execute:
            return f"执行完毕：{len(report.executed_orders)} 笔买入成交"
        return f"生成完毕：{len(report.buy_suggestions)} 条买入建议、{len(report.sell_suggestions)} 条卖出建议"

    # ── Phase 0: 止盈止损 ────────────────────────────────────────

    def _check_stop_loss_take_profit(self, report: ExecutionReport):
        """检查现有持仓的止盈止损。auto 模式自动卖出；signal 模式记为卖出建议。"""
        positions = self.simulator.portfolio.positions
        if not positions:
            return

        self.simulator.refresh_positions()

        for code, pos in list(positions.items()):
            cost = pos.get("cost_price", 0)
            if cost <= 0:
                continue
            current = pos.get("current_price", cost)
            available = pos.get("available", 0)

            pnl_pct = (current - cost) / cost * 100
            triggered = None
            if pnl_pct <= -self.risk.stop_loss_pct:
                triggered = (
                    "stop_loss",
                    f"止损：亏损 {pnl_pct:.1f}%（阈值 -{self.risk.stop_loss_pct}%）",
                )
            elif pnl_pct >= self.risk.take_profit_pct:
                triggered = (
                    "take_profit",
                    f"止盈：盈利 {pnl_pct:.1f}%（阈值 +{self.risk.take_profit_pct}%）",
                )
            if not triggered:
                continue

            kind, reason = triggered
            entry = {
                "code": code, "name": pos.get("name", code),
                "reason": reason, "pnl_pct": round(pnl_pct, 2),
                "available": available,
            }

            if self.auto_execute and available > 0:
                # T+1：available<=0 当日不可卖，记录但不执行
                result = self.simulator.place_sell(code, available)
                entry["result"] = result
                if kind == "stop_loss":
                    report.stop_loss_sells.append(entry)
                else:
                    report.take_profit_sells.append(entry)
                logger.info(f"{reason} 卖出 {code}")
            else:
                # signal 模式 或 available=0：记为建议
                if available <= 0:
                    entry["note"] = "T+1：当日买入不可卖，建议次日处理"
                report.sell_suggestions.append(entry)

    def _is_max_drawdown_exceeded(self, report: ExecutionReport) -> bool:
        """检查组合是否触发最大回撤暂停线（high-water mark 口径，持久化峰值）。"""
        try:
            summary = self.simulator.get_portfolio_summary()
            current = summary["total_value"]
            acc = self.dm.storage.get_account()
            peak = acc.get("peak_value")
            initial = acc.get("initial_cash") or current
            if peak is None:
                peak = initial  # 首次：以初始资金为起点峰值

            # 更新历史峰值（创新高才写库）
            if current > peak:
                self.dm.storage.update_peak_value(current)
                peak = current

            drawdown = compute_drawdown_pct(peak, current)
            report.drawdown_pct = round(drawdown, 2)
            return drawdown >= self.risk.max_drawdown_pct
        except Exception as e:
            logger.warning(f"回撤检查异常（按未触发处理）: {e}")
            return False

    def _apply_drawdown_deescalation(self, report: ExecutionReport):
        """回撤分档减仓：回撤越深，把每只持仓 trim 到越低比例（真正的下行保护）。

        signal 模式记 ``sell_suggestions``；auto 模式部分减仓（受 T+1 ``available`` 约束，
        只卖可卖部分，不足记 note 建议次日分批）。``report.drawdown_pct`` 由
        ``_is_max_drawdown_exceeded`` 设置。
        """
        if not self.risk.drawdown_deescalation:
            return
        dd = report.drawdown_pct
        if dd is None or dd <= 0:
            return
        keep_ratio, tier = deescalation_level(dd)
        if keep_ratio >= 1.0:
            return  # 未命中减仓档（回撤较浅）
        report.deescalation_tier = tier
        report.deescalation_keep_ratio = keep_ratio

        positions = self.simulator.portfolio.positions
        if not positions:
            return
        self.simulator.refresh_positions()

        for code, pos in list(positions.items()):
            qty = int(pos.get("quantity", 0) or 0)
            if qty <= 0:
                continue
            trim = compute_trim_quantity(qty, keep_ratio)
            if trim <= 0:
                continue
            available = int(pos.get("available", 0) or 0)
            entry = {
                "code": code, "name": pos.get("name", code),
                "tier": tier, "keep_ratio": keep_ratio,
                "current_quantity": qty, "trim_quantity": trim,
                "available": available,
                "reason": (f"回撤减仓：回撤 {dd:.1f}% 命中第{tier}档，"
                           f"持仓 trim 到 {keep_ratio*100:.0f}%（减 {trim} 股）"),
            }
            if self.auto_execute and available > 0:
                sell_qty = min(trim, available)  # T+1：只能卖 available
                result = self.simulator.place_sell(code, sell_qty)
                entry["result"] = result
                entry["sold_quantity"] = sell_qty
                if sell_qty < trim:
                    entry["note"] = f"T+1：可卖 {sell_qty} 不足目标 {trim}，剩余次日处理"
                report.deescalation_sells.append(entry)
                logger.info(f"回撤减仓卖出 {code} {sell_qty} 股（第{tier}档）")
            else:
                if available < trim:
                    entry["note"] = f"T+1：可卖 {available} 不足目标减仓 {trim}，建议分批"
                report.sell_suggestions.append(entry)

    # ── Phase 1: 策略筛选 ────────────────────────────────────────

    def _run_strategies(self, stock_pool: List[dict], strategy_keys: List[str],
                        report: ExecutionReport) -> List[dict]:
        """运行策略，返回满足条件的候选股列表（按综合得分降序）。"""
        from src.strategy.screener import STRATEGY_REGISTRY

        candidates = {}  # code -> {code, name, strategies_passed, score_sum, indicators}

        for key in strategy_keys:
            cls = STRATEGY_REGISTRY.get(key)
            if cls is None:
                continue
            strategy = cls()
            if not strategy.supports_evaluate():
                continue

            for stock in stock_pool:
                code = stock["code"]
                name = stock.get("name", code)
                try:
                    ev = strategy.evaluate_stock(code, name, self.dm)
                    if ev.selected:
                        if code not in candidates:
                            candidates[code] = {
                                "code": code, "name": name,
                                "strategies_passed": [], "score_sum": 0,
                                "indicators": ev.indicators,
                            }
                        candidates[code]["strategies_passed"].append(key)
                        candidates[code]["score_sum"] += ev.score
                except Exception as e:
                    report.errors.append(f"策略 {key} 评估 {code} 失败: {e}")

        result = sorted(candidates.values(), key=lambda c: c.get("score_sum", 0), reverse=True)
        report.strategy_candidates = result
        return result

    # ── Phase 2: AI 注释（展示用，非决策门）──────────────────────

    def _annotate_with_ai(self, candidates: List[dict], report: ExecutionReport):
        """对综合得分最高的前 N 只候选做 AI 分析，作为展示注释。

        不再用 confidence/buy_suggestion 过滤候选——AI 短期预测力极弱，作为硬门
        是负价值。分析结果仅附在 ai_recommendations 供用户参考。
        """
        if self.advisor is None:
            return
        from src.strategy.base import ScreenResult

        for c in candidates[:AI_ANALYSIS_TOP_N]:
            code, name = c["code"], c["name"]
            try:
                sr = ScreenResult(
                    code=code, name=name,
                    score=c.get("score_sum", 0),
                    signals=c.get("indicators", {}),
                )
                analysis = self.advisor.analyze_stock(sr)
                report.ai_recommendations.append({
                    **c, "ai_analysis": analysis,
                    "confidence": analysis.get("confidence", 0),
                    "suggestion": analysis.get("buy_suggestion", ""),
                })
            except Exception as e:
                report.errors.append(f"AI注释 {code} 失败: {e}")

    # ── Phase 3: 风控预算 → 建议/下单 ────────────────────────────

    def _build_and_maybe_execute(self, candidates: List[dict], report: ExecutionReport):
        """风控预算后产出买入建议（signal 模式）或下单（auto 模式）。

        关键修复：
        - 加仓数量扣除已有持仓市值（``compute_add_buy_quantity``），不再击穿单股上限；
        - **每处理一只就重算 total_value / market_value**，避免同轮多股用循环外快照
          导致总仓位上限被击穿。
        """
        daily_trade_count = 0

        for signal in candidates:
            code = signal["code"]
            name = signal["name"]

            # 每只重算组合快照（修复同轮多股无联动）
            summary = self.simulator.get_portfolio_summary()
            total_value = summary["total_value"]
            market_value = summary["market_value"]

            # 检查单日交易次数（auto 模式按成交计；signal 模式不计入但保留提示）
            if self.auto_execute and daily_trade_count >= self.risk.max_daily_trades:
                report.risk_blocked.append({
                    "code": code, "name": name,
                    "reason": f"已达单日最大交易次数 {self.risk.max_daily_trades}"
                })
                continue

            # 检查总仓位上限
            total_position_pct = market_value / total_value * 100 if total_value > 0 else 0
            if total_position_pct >= self.risk.max_total_position_pct:
                report.risk_blocked.append({
                    "code": code, "name": name,
                    "reason": f"总仓位 {total_position_pct:.1f}% 已达上限 {self.risk.max_total_position_pct}%"
                })
                continue

            price = self.simulator.get_current_price(code)
            if not price or price <= 0:
                report.errors.append(f"无法获取 {code} 价格，跳过")
                continue

            # 计算加仓数量：扣除已有持仓市值（修复单股上限击穿）
            positions = self.simulator.portfolio.positions
            existing_pos_value = (
                positions[code].get("market_value", 0) if code in positions else 0
            )
            quantity, block_reason = compute_add_buy_quantity(
                total_value=total_value,
                existing_pos_value=existing_pos_value,
                price=price,
                max_position_pct=self.risk.max_position_pct,
            )
            if quantity <= 0:
                report.risk_blocked.append({
                    "code": code, "name": name,
                    "reason": block_reason or "计算买入数量为0",
                    "existing_pos_pct": round(existing_pos_value / total_value * 100, 1) if total_value > 0 else 0,
                })
                continue

            # 行业/风格集中度预检（best-effort：无 stock_list 或 industry 缺失时跳过，不阻断）
            if 0 < self.risk.max_industry_pct < 100:
                try:
                    ind_weights, code_industry = self._industry_market_values()
                    buy_industry = code_industry.get(code, "未知")
                    buy_amount = quantity * price
                    if ind_weights and would_breach_concentration(
                        ind_weights, buy_industry, buy_amount, self.risk.max_industry_pct
                    ):
                        report.risk_blocked.append({
                            "code": code, "name": name,
                            "reason": (f"行业「{buy_industry}」集中度将超上限 "
                                       f"{self.risk.max_industry_pct}%（买入 {buy_amount:.0f} 元后）"),
                        })
                        continue
                except Exception as e:
                    logger.debug(f"行业集中度预检跳过 {code}: {e}")

            order_intent = {
                "code": code, "name": name,
                "quantity": quantity, "price": price,
                "amount": round(quantity * price, 2),
                "strategies": signal.get("strategies_passed", []),
                "score": signal.get("score_sum", 0),
            }

            if self.auto_execute:
                result = self.simulator.place_buy(code, name, quantity, price)
                order_intent["result"] = result
                report.executed_orders.append(order_intent)
                if result.get("success"):
                    daily_trade_count += 1
            else:
                report.buy_suggestions.append(order_intent)

    def _industry_market_values(self) -> tuple:
        """返回 ``({行业: 持仓市值}, {code: 行业})``，供行业集中度预检。

        行业映射取自 ``stock_list.industry``（best-effort：取数失败或 industry 缺失时返回
        空 dict，调用方据此跳过集中度检查，不阻断交易）。
        """
        try:
            sl = self.dm.get_stock_list()
        except Exception:
            return {}, {}
        if sl is None or sl.empty or "industry" not in sl.columns:
            return {}, {}
        code_industry = dict(zip(sl["code"], sl["industry"]))
        out: dict = {}
        for code, pos in self.simulator.portfolio.positions.items():
            ind = code_industry.get(code) or "未知"
            mv = float(pos.get("market_value", 0) or 0)
            if mv <= 0:
                continue
            out[ind] = out.get(ind, 0.0) + mv
        return out, code_industry
