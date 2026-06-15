"""自动交易引擎：策略信号 + AI分析 + 风控 → 自动下单"""
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import List, Optional, Callable

from src.data.manager import DataManager
from src.trading.simulator import TradingSimulator
from src.trading.rules import round_to_lot

logger = logging.getLogger(__name__)


@dataclass
class RiskParams:
    """风控参数"""
    max_position_pct: float = 20.0        # 单股最大仓位占比 (%)
    max_total_position_pct: float = 80.0  # 总仓位上限 (%)
    max_daily_trades: int = 5             # 单日最大交易次数
    stop_loss_pct: float = 8.0            # 止损线 (%)
    take_profit_pct: float = 20.0         # 止盈线 (%)
    max_drawdown_pct: float = 15.0        # 最大回撤暂停线 (%)
    min_ai_confidence: float = 60.0       # AI分析最低置信度


@dataclass
class ExecutionReport:
    """执行报告"""
    timestamp: str = ""
    strategy_candidates: List[dict] = field(default_factory=list)
    ai_recommendations: List[dict] = field(default_factory=list)
    risk_blocked: List[dict] = field(default_factory=list)
    executed_orders: List[dict] = field(default_factory=list)
    stop_loss_sells: List[dict] = field(default_factory=list)
    take_profit_sells: List[dict] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    paused_reason: str = ""

    @property
    def total_executed(self) -> int:
        return len(self.executed_orders) + len(self.stop_loss_sells) + len(self.take_profit_sells)


class AutoTrader:
    def __init__(self, data_manager: DataManager, simulator: TradingSimulator,
                 advisor, risk_params: RiskParams = None):
        self.dm = data_manager
        self.simulator = simulator
        self.advisor = advisor
        self.risk = risk_params or RiskParams()

    def run(self, stock_pool: List[dict], strategy_keys: List[str],
            status_callback: Optional[Callable] = None) -> ExecutionReport:
        """
        执行一轮自动交易。
        stock_pool: [{"code": "000001", "name": "平安银行"}, ...]
        strategy_keys: ["ma_cross", "macd_golden", ...]
        status_callback(phase: str, detail: str)
        """
        report = ExecutionReport(timestamp=datetime.now().isoformat())

        def cb(phase: str, detail: str = ""):
            logger.info(f"[AutoTrader] {phase}: {detail}")
            if status_callback:
                status_callback(phase, detail)

        # Phase 0: 止盈止损检查
        cb("止盈止损检查", "检查现有持仓...")
        self._check_stop_loss_take_profit(report)

        # 检查最大回撤暂停
        if self._is_max_drawdown_exceeded():
            report.paused_reason = f"组合回撤超过 {self.risk.max_drawdown_pct}%，暂停新开仓"
            cb("暂停", report.paused_reason)
            return report

        if not stock_pool:
            cb("完成", "股票池为空")
            return report

        # Phase 1: 策略筛选
        cb("策略筛选", f"对 {len(stock_pool)} 只股票运行 {len(strategy_keys)} 个策略...")
        candidates = self._run_strategies(stock_pool, strategy_keys, report)
        cb("策略筛选完成", f"产生 {len(candidates)} 个候选信号")

        if not candidates:
            cb("完成", "策略未产生买入信号")
            return report

        # Phase 2: AI分析
        cb("AI分析", f"分析 {len(candidates)} 只候选股...")
        buy_signals = self._run_ai_analysis(candidates, report)
        cb("AI分析完成", f"{len(buy_signals)} 只通过AI验证")

        if not buy_signals:
            cb("完成", "AI分析未产生买入建议")
            return report

        # Phase 3: 风控过滤 + 下单
        cb("风控过滤+执行", f"过滤并执行 {len(buy_signals)} 个信号...")
        self._execute_buy_orders(buy_signals, report)
        cb("完成", f"执行完毕：{len(report.executed_orders)} 笔买入成交")

        return report

    # ── Phase 0: 止盈止损 ────────────────────────────────────────

    def _check_stop_loss_take_profit(self, report: ExecutionReport):
        """检查现有持仓的止盈止损，自动卖出。"""
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
            if available <= 0:
                continue  # T+1，当日买入不可卖

            pnl_pct = (current - cost) / cost * 100

            if pnl_pct <= -self.risk.stop_loss_pct:
                result = self.simulator.place_sell(code, available)
                entry = {
                    "code": code, "name": pos.get("name", code),
                    "reason": f"止损：亏损 {pnl_pct:.1f}%（阈值 -{self.risk.stop_loss_pct}%）",
                    "result": result,
                }
                report.stop_loss_sells.append(entry)
                logger.info(f"止损卖出 {code}: {entry['reason']}")

            elif pnl_pct >= self.risk.take_profit_pct:
                result = self.simulator.place_sell(code, available)
                entry = {
                    "code": code, "name": pos.get("name", code),
                    "reason": f"止盈：盈利 {pnl_pct:.1f}%（阈值 +{self.risk.take_profit_pct}%）",
                    "result": result,
                }
                report.take_profit_sells.append(entry)
                logger.info(f"止盈卖出 {code}: {entry['reason']}")

    def _is_max_drawdown_exceeded(self) -> bool:
        """检查组合是否触发最大回撤暂停线。"""
        try:
            summary = self.simulator.get_portfolio_summary()
            # 用初始资金作为基准（简化：从config读取）
            from src.config import get_config
            initial = get_config()["trading"]["initial_cash"]
            total = summary["total_value"]
            drawdown = (initial - total) / initial * 100
            return drawdown >= self.risk.max_drawdown_pct
        except Exception:
            return False

    # ── Phase 1: 策略筛选 ────────────────────────────────────────

    def _run_strategies(self, stock_pool: List[dict], strategy_keys: List[str],
                        report: ExecutionReport) -> List[dict]:
        """运行策略，返回满足条件的候选股列表。"""
        from src.strategy.screener import STRATEGY_REGISTRY

        candidates = {}  # code -> {code, name, strategies_passed, score_sum}

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

        result = list(candidates.values())
        report.strategy_candidates = result
        return result

    # ── Phase 2: AI分析 ──────────────────────────────────────────

    def _run_ai_analysis(self, candidates: List[dict],
                         report: ExecutionReport) -> List[dict]:
        """对候选股做AI分析，返回通过置信度阈值且建议买入的股票。"""
        from src.strategy.base import ScreenResult

        buy_signals = []
        for c in candidates:
            code, name = c["code"], c["name"]
            try:
                sr = ScreenResult(
                    code=code, name=name,
                    score=c.get("score_sum", 0),
                    signals=c.get("indicators", {}),
                )
                analysis = self.advisor.analyze_stock(sr)
                confidence = analysis.get("confidence", 0)
                suggestion = analysis.get("buy_suggestion", "")

                ai_entry = {**c, "ai_analysis": analysis,
                            "confidence": confidence, "suggestion": suggestion}
                report.ai_recommendations.append(ai_entry)

                if (confidence >= self.risk.min_ai_confidence
                        and suggestion == "建议买入"):
                    buy_signals.append(ai_entry)
            except Exception as e:
                report.errors.append(f"AI分析 {code} 失败: {e}")

        return buy_signals

    # ── Phase 3: 风控过滤 + 下单 ─────────────────────────────────

    def _execute_buy_orders(self, buy_signals: List[dict],
                            report: ExecutionReport):
        """风控过滤后执行买入。"""
        daily_trade_count = 0
        summary = self.simulator.get_portfolio_summary()
        total_value = summary["total_value"]
        market_value = summary["market_value"]

        for signal in buy_signals:
            code = signal["code"]
            name = signal["name"]

            # 检查单日交易次数
            if daily_trade_count >= self.risk.max_daily_trades:
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

            # 检查单股仓位上限
            positions = self.simulator.portfolio.positions
            if code in positions:
                pos = positions[code]
                pos_value = pos.get("market_value", 0)
                pos_pct = pos_value / total_value * 100 if total_value > 0 else 0
                if pos_pct >= self.risk.max_position_pct:
                    report.risk_blocked.append({
                        "code": code, "name": name,
                        "reason": f"该股仓位 {pos_pct:.1f}% 已达上限 {self.risk.max_position_pct}%"
                    })
                    continue

            # 计算买入数量（等权分配：单股最大仓位 × 总资产 / 当前价）
            price = self.simulator.get_current_price(code)
            if not price or price <= 0:
                report.errors.append(f"无法获取 {code} 价格，跳过")
                continue

            max_amount = total_value * self.risk.max_position_pct / 100
            quantity = round_to_lot(int(max_amount / price))
            if quantity <= 0:
                report.risk_blocked.append({
                    "code": code, "name": name,
                    "reason": "计算买入数量为0（资金不足或价格过高）"
                })
                continue

            # 执行买入
            result = self.simulator.place_buy(code, name, quantity, price)
            order_entry = {
                "code": code, "name": name,
                "quantity": quantity, "price": price,
                "strategies": signal.get("strategies_passed", []),
                "confidence": signal.get("confidence", 0),
                "result": result,
            }
            report.executed_orders.append(order_entry)

            if result.get("success"):
                daily_trade_count += 1
                # 更新市值估算（避免重复查询）
                market_value += quantity * price
