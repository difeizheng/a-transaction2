"""Backtrader回测引擎封装"""
import logging
import json
from datetime import datetime
from typing import List, Optional
import pandas as pd
import backtrader as bt

from src.strategy.base import BaseStrategy
from src.trading.rules import (
    STAMP_DUTY_RATE,
    TRANSFER_FEE_RATE,
    is_at_limit_up,
    is_at_limit_down,
)
from src.trading.risk import cap_size_by_volume

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


# ── A股手续费模型（佣金+过户费双边 + 卖出印花税） ───────────────
class AShareCommissionInfo(bt.CommInfoBase):
    """A股手续费：买卖各收佣金 + 双边过户费，卖出额外收印花税。
    backtrader 内置 setcommission 只支持对称费率，无法表达"仅卖出收印花税"，
    故自定义 CommissionInfo。percabs=True 表示各项费率均为比例小数。

    与实盘 ``trading.rules.calc_commission`` 口径一致（2023.8 印花税 0.05% /
    2022.4 过户费 0.001% 双边），避免「实盘严谨、回测放水」的两套口径。
    """

    params = (
        ("commission", 0.0003),
        ("stamp_duty", 0.0005),   # 印花税：卖出单边（2023.8.28 起 0.05%）
        ("transfer_fee", 0.0001), # 过户费：双边（2022.4.29 起沪深统一 0.001%）
        ("mult", 1.0),
        ("commtype", bt.CommInfoBase.COMM_PERC),
        ("stocklike", True),
        ("percabs", True),
    )

    def _getcommission(self, size, price, pseudoexec=False):
        cost = abs(size) * price
        fee = cost * self.p.commission + cost * self.p.transfer_fee  # 佣金 + 过户费（双边）
        if size < 0:  # 仅卖出收印花税
            fee += cost * self.p.stamp_duty
        return fee


# ── 通用Backtrader策略包装器 ──────────────────────────────────────
class BTStrategyWrapper(bt.Strategy):
    """将选股信号转换为Backtrader买卖逻辑的通用包装。

    实盘约束（与 ``trading.rules`` / ``trading.auto_trader`` 一致，避免回测放水）：
    - **涨跌停/一字板**：涨停买不进、跌停卖不掉（``is_at_limit_up/down``）；
    - **成交量上限**：单笔不超过当日成交量 25%（``cap_size_by_volume``），防无限流动性幻觉；
    - **T+1**：买入当 bar 不可卖（记录 ``_buy_bar``）；
    - **成交价**：backtrader 默认在**下一根 bar 开盘价**成交（非当日收盘价闭环）。
    """

    params = (
        ("signal_codes", []),   # 选股结果中的股票代码列表
        ("stop_loss", 0.05),    # 止损比例
        ("take_profit", 0.15),  # 止盈比例
        ("position_pct", 0.1),  # 每只股票仓位占总资金比例
        ("vol_cap_pct", 0.25),  # 单笔占当日成交量上限
    )

    def __init__(self):
        self.orders = {}
        self.buy_prices = {}
        self.traded_once = set()  # 已建过仓的代码，卖出后不回补，避免无限循环放大交易次数
        self._buy_bar = {}        # code -> 买入决策 bar 序号（T+1 判定）
        self._open_lots = {}      # code -> (open_date, open_price, size)，供逐笔交易记录
        self._closed_trades = []  # 已平仓交易明细（UI 下钻用）

    def next(self):
        for i, data in enumerate(self.datas):
            code = data._name
            pos = self.getposition(data)
            bar_idx = len(data)  # 当前 bar 在该 data 序列中的序号
            # prev_close：前一日收盘（涨跌停基准）；首 bar 无前值，用当日收盘兜底
            prev_close = data.close[-1] if bar_idx > 1 else data.close[0]
            current = data.close[0]

            if pos.size > 0:
                # 持仓中：止损止盈
                # T+1：买入决策当 bar 不可卖；跌停日卖不出（realistic pessimism）
                bought_bar = self._buy_bar.get(code)
                t1_locked = bought_bar is not None and bar_idx <= bought_bar
                if t1_locked or is_at_limit_down(code, prev_close, current):
                    continue
                cost = self.buy_prices.get(code, current)
                pct = (current - cost) / cost
                if pct <= -self.p.stop_loss or pct >= self.p.take_profit:
                    self.sell(data=data, size=pos.size)
            else:
                # 未持仓：在信号列表中且首次触及才买入
                # （卖出后 traded_once 命中，不再回补，避免止损后反复买卖放大交易次数）
                if code not in self.p.signal_codes or code in self.traded_once:
                    continue
                # 涨停/一字板买不进
                if is_at_limit_up(code, prev_close, current):
                    continue
                cash = self.broker.getcash()
                target_value = self.broker.getvalue() * self.p.position_pct
                raw_size = int(target_value / current / 100) * 100  # 整手
                # 成交量上限：防无限流动性（小盘股大单打飞价格）
                size = cap_size_by_volume(raw_size, data.volume[0],
                                          max_pct=self.p.vol_cap_pct)
                if size > 0 and cash >= size * current:
                    self.buy(data=data, size=size)
                    self.buy_prices[code] = current
                    self._buy_bar[code] = bar_idx
                    self.traded_once.add(code)

    def notify_order(self, order):
        if order.status in [order.Completed]:
            action = "买入" if order.isbuy() else "卖出"
            logger.debug(f"{order.data._name} {action} {order.executed.size}股 @{order.executed.price:.2f}")
            if order.isbuy():
                d = bt.num2date(order.executed.dt).date().isoformat()
                self._open_lots[order.data._name] = (
                    d, float(order.executed.price), int(order.executed.size))

    def notify_trade(self, trade):
        """平仓时记录逐笔交易（供 UI「交易明细下钻」与复盘）。

        本策略每股同时只有一仓（next() 仅在 pos.size==0 时买入），
        故 _open_lots 按 code 单条对应，无需 FIFO 队列。
        """
        if not trade.isclosed:
            return
        code = trade.data._name
        open_date, open_price, size = self._open_lots.pop(
            code, (bt.num2date(trade.dtopen).date().isoformat(), float(trade.price), 0))
        close_price = round(open_price + trade.pnl / size, 3) if size else None
        self._closed_trades.append({
            "code": code,
            "open_date": open_date,
            "close_date": bt.num2date(trade.dtclose).date().isoformat(),
            "open_price": round(open_price, 3),
            "close_price": close_price,
            "size": size,
            "pnl": round(float(trade.pnl), 2),
            "pnlcomm": round(float(trade.pnlcomm), 2),
        })


# ── 净值曲线分析器 ─────────────────────────────────────────────────
class EquityCurveAnalyzer(bt.Analyzer):
    """逐 bar 记录组合净值 (date, broker.getvalue())，用于绘制净值曲线。

    比 TimeReturn 重建更精确：直接读取每根 bar 的真实组合总价值，无复利累积误差。
    """

    def start(self):
        self.dates = []
        self.values = []

    def next(self):
        # strategy.datetime 是 backtrader 的自动主时钟，date(0) 为当前 bar 日期。
        self.dates.append(self.strategy.datetime.date(0))
        self.values.append(self.strategy.broker.getvalue())

    def get_analysis(self):
        return {"dates": list(self.dates), "values": list(self.values)}


# ── 回测引擎 ─────────────────────────────────────────────────────
class BacktestEngine:
    def __init__(self, config: dict):
        bt_cfg = config.get("backtest", {}) or {}
        # 用 .get + 规则常量兜底，兼容旧 config.yaml（缺少 transfer_fee/slippage 时不报错）
        self.initial_cash = bt_cfg.get("initial_cash", 1_000_000)
        self.commission = bt_cfg.get("commission", 0.0003)
        self.stamp_duty = bt_cfg.get("stamp_duty", STAMP_DUTY_RATE)
        self.transfer_fee = bt_cfg.get("transfer_fee", TRANSFER_FEE_RATE)
        self.slippage = bt_cfg.get("slippage", 0.001)

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
        # A股手续费：佣金+过户费双边 + 卖出印花税（与 trading.rules 口径一致）
        cerebro.broker.addcommissioninfo(AShareCommissionInfo(
            commission=self.commission, stamp_duty=self.stamp_duty,
            transfer_fee=self.transfer_fee,
        ))
        # 滑点：防「无限流动性、零冲击」的回测虚高（默认单边 0.1%）
        if self.slippage and self.slippage > 0:
            cerebro.broker.set_slippage_perc(perc=self.slippage)

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
        # riskfreerate 为**每周期(日)**无风险利率：backtrader 按 bar 减去再年化，
        # 传 0.03 会被当成 3%/日（年化 1095%，荒谬）。改为年化 3% 折算到日。
        cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe",
                            riskfreerate=0.03 / 252, annualize=True, timeframe=bt.TimeFrame.Days)
        cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
        cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
        cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")
        cerebro.addanalyzer(EquityCurveAnalyzer, _name="equity")

        results = cerebro.run()
        strat = results[0]

        final_value = cerebro.broker.getvalue()
        total_return = (final_value - self.initial_cash) / self.initial_cash * 100

        # 净值曲线
        equity = strat.analyzers.equity.get_analysis()

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
            # 逐笔平仓明细（JSON 字符串，随 save_backtest_result 落库 trades_detail 列）
            "trades_detail": json.dumps(strat._closed_trades, ensure_ascii=False),
            "equity_curve": {
                "dates": equity.get("dates", []),
                "values": equity.get("values", []),
            },
        }
