"""Backtrader回测引擎封装"""
import logging
from datetime import datetime
from typing import List, Optional
import pandas as pd
import backtrader as bt

from src.strategy.base import BaseStrategy

logger = logging.getLogger(__name__)


# ── Backtrader数据源适配 ──────────────────────────────────────────
class PandasData(bt.feeds.PandasData):
    params = (
        ("datetime", None),
        ("open", "open"),
        ("high", "high"),
        ("low", "low"),
        ("close", "close"),
        ("volume", "volume"),
        ("openinterest", -1),
    )


# ── A股手续费模型（佣金双边 + 卖出印花税） ──────────────────────
class AShareCommissionInfo(bt.CommInfoBase):
    """A股手续费：买卖各收佣金，卖出额外收印花税。
    backtrader 内置 setcommission 只支持对称费率，无法表达"仅卖出收印花税"，
    故自定义 CommissionInfo。percabs=True 表示 commission/stamp_duty 均为比例小数。
    """

    params = (
        ("commission", 0.0003),
        ("stamp_duty", 0.001),
        ("mult", 1.0),
        ("commtype", bt.CommInfoBase.COMM_PERC),
        ("stocklike", True),
        ("percabs", True),
    )

    def _getcommission(self, size, price, pseudoexec=False):
        cost = abs(size) * price
        fee = cost * self.p.commission
        if size < 0:  # 仅卖出收印花税
            fee += cost * self.p.stamp_duty
        return fee


# ── 通用Backtrader策略包装器 ──────────────────────────────────────
class BTStrategyWrapper(bt.Strategy):
    """将我们的选股信号转换为Backtrader买卖逻辑的通用包装"""

    params = (
        ("signal_codes", []),   # 选股结果中的股票代码列表
        ("stop_loss", 0.05),    # 止损比例
        ("take_profit", 0.15),  # 止盈比例
        ("position_pct", 0.1),  # 每只股票仓位占总资金比例
    )

    def __init__(self):
        self.orders = {}
        self.buy_prices = {}
        self.traded_once = set()  # 已建过仓的代码，卖出后不回补，避免无限循环放大交易次数

    def next(self):
        for i, data in enumerate(self.datas):
            code = data._name
            pos = self.getposition(data)

            if pos.size > 0:
                # 持仓中：检查止损止盈
                cost = self.buy_prices.get(code, data.close[0])
                pct = (data.close[0] - cost) / cost
                if pct <= -self.p.stop_loss or pct >= self.p.take_profit:
                    self.sell(data=data, size=pos.size)
            else:
                # 未持仓：在信号列表中且首次触及才买入
                # （卖出后 traded_once 命中，不再回补，避免止损后反复买卖放大交易次数）
                if code in self.p.signal_codes and code not in self.traded_once:
                    cash = self.broker.getcash()
                    target_value = self.broker.getvalue() * self.p.position_pct
                    size = int(target_value / data.close[0] / 100) * 100  # 整手
                    if size > 0 and cash >= size * data.close[0]:
                        self.buy(data=data, size=size)
                        self.buy_prices[code] = data.close[0]
                        self.traded_once.add(code)

    def notify_order(self, order):
        if order.status in [order.Completed]:
            action = "买入" if order.isbuy() else "卖出"
            logger.debug(f"{order.data._name} {action} {order.executed.size}股 @{order.executed.price:.2f}")


# ── 回测引擎 ─────────────────────────────────────────────────────
class BacktestEngine:
    def __init__(self, config: dict):
        self.initial_cash = config["backtest"]["initial_cash"]
        self.commission = config["backtest"]["commission"]
        self.stamp_duty = config["backtest"]["stamp_duty"]

    def run(
        self,
        bars_dict: dict,          # {code: DataFrame}
        signal_codes: List[str],  # 选股结果代码列表
        start_date: str,
        end_date: str,
        stop_loss: float = 0.05,
        take_profit: float = 0.15,
        position_pct: float = 0.1,
        strategy_name: str = "custom",
    ) -> dict:
        """
        运行回测
        :param bars_dict: 每只股票的日K线DataFrame
        :param signal_codes: 选股策略选出的股票代码
        :return: 回测结果字典
        """
        cerebro = bt.Cerebro()
        cerebro.broker.setcash(self.initial_cash)
        # A股手续费：佣金双边 + 卖出印花税（setcommission 不支持非对称印花税）
        cerebro.broker.addcommissioninfo(AShareCommissionInfo(
            commission=self.commission, stamp_duty=self.stamp_duty
        ))

        # 添加数据
        added = 0
        for code, df in bars_dict.items():
            df = df.copy()
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            df = df.set_index("trade_date").sort_index()
            df = df[(df.index >= start_date) & (df.index <= end_date)]
            if len(df) < 10:
                continue
            data = PandasData(dataname=df, name=code)
            cerebro.adddata(data)
            added += 1

        if added == 0:
            return {"error": "没有有效数据"}

        cerebro.addstrategy(
            BTStrategyWrapper,
            signal_codes=signal_codes,
            stop_loss=stop_loss,
            take_profit=take_profit,
            position_pct=position_pct,
        )

        # 分析器
        cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", riskfreerate=0.03)
        cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
        cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
        cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")

        results = cerebro.run()
        strat = results[0]

        final_value = cerebro.broker.getvalue()
        total_return = (final_value - self.initial_cash) / self.initial_cash * 100

        # 提取分析结果
        sharpe = strat.analyzers.sharpe.get_analysis().get("sharperatio") or 0
        dd = strat.analyzers.drawdown.get_analysis()
        max_dd = dd.get("max", {}).get("drawdown", 0)
        trade_analysis = strat.analyzers.trades.get_analysis()
        total_trades = trade_analysis.get("total", {}).get("closed", 0)
        won = trade_analysis.get("won", {}).get("total", 0)
        win_rate = (won / total_trades * 100) if total_trades > 0 else 0
        avg_win = trade_analysis.get("won", {}).get("pnl", {}).get("average", 0) or 0
        avg_loss = abs(trade_analysis.get("lost", {}).get("pnl", {}).get("average", 1) or 1)
        profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 0

        # 年化收益
        days = (pd.to_datetime(end_date) - pd.to_datetime(start_date)).days
        annual_return = ((1 + total_return / 100) ** (365 / max(days, 1)) - 1) * 100

        return {
            "strategy_name": strategy_name,
            "start_date": start_date,
            "end_date": end_date,
            "initial_cash": self.initial_cash,
            "final_value": round(final_value, 2),
            "total_return": round(total_return, 2),
            "annual_return": round(annual_return, 2),
            "sharpe": round(float(sharpe), 3),
            "max_drawdown": round(float(max_dd), 2),
            "win_rate": round(win_rate, 1),
            "profit_loss_ratio": round(profit_loss_ratio, 2),
            "trades": total_trades,
        }
