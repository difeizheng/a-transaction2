"""持仓管理"""
import logging
from datetime import date
from typing import Dict, Optional
import pandas as pd

from src.trading.rules import (
    calc_commission, round_to_lot, is_t1_available,
    is_at_limit_up, is_at_limit_down,
)
from src.data.storage import Storage

logger = logging.getLogger(__name__)


class Portfolio:
    def __init__(self, storage: Storage, initial_cash: float):
        self.storage = storage
        self._initial_cash = initial_cash
        self._positions: Dict[str, dict] = {}  # code -> position dict
        self._cash = self._load_cash(initial_cash)  # 资金持久化，重启后恢复
        self._load_positions()

    def _load_cash(self, default: float) -> float:
        """从 account 表恢复可用资金；首次启动则落盘初始资金。"""
        acc = self.storage.get_account()
        if acc:
            return acc["cash"]
        self.storage.upsert_account(default, default)
        return default

    def _save_cash(self):
        self.storage.upsert_account(self._cash, self._initial_cash)

    def _load_positions(self):
        """从数据库恢复持仓"""
        df = self.storage.get_positions()
        for _, row in df.iterrows():
            self._positions[row["code"]] = row.to_dict()

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def positions(self) -> dict:
        return self._positions

    def get_position(self, code: str) -> Optional[dict]:
        return self._positions.get(code)

    def market_value(self, prices: Dict[str, float] = None) -> float:
        total = 0.0
        for code, pos in self._positions.items():
            price = (prices or {}).get(code, pos.get("current_price", pos["cost_price"]))
            total += pos["quantity"] * price
        return total

    def total_value(self, prices: Dict[str, float] = None) -> float:
        return self._cash + self.market_value(prices)

    def buy(self, code: str, name: str, price: float, quantity: int,
            trade_date: str, prev_close: float = None) -> dict:
        """执行买入，返回成交记录。

        ``prev_close`` 为昨收价（用于涨跌停校验）；调用方可显式传入避免重复查库，
        未传则自查本地最新日K。无历史数据（新上市/空库）时不拦截。
        """
        quantity = round_to_lot(quantity)
        if quantity <= 0:
            return {"success": False, "msg": "数量不足1手（100股）"}

        # 涨跌停底层兜底：防绕过 Simulator 的路径（UI 手动加仓等）按不可成交价建仓。
        prev_close = self._resolve_prev_close(code, prev_close)
        if prev_close > 0 and is_at_limit_up(code, prev_close, price):
            return {"success": False, "msg": f"{code}已涨停（{price}≥涨停价），无法买入"}

        amount = price * quantity
        commission = calc_commission(amount, is_buy=True)
        total_cost = amount + commission

        if total_cost > self._cash:
            return {"success": False, "msg": f"资金不足，需要{total_cost:.2f}，可用{self._cash:.2f}"}

        self._cash -= total_cost
        self._save_cash()

        if code in self._positions:
            pos = self._positions[code]
            old_qty = pos["quantity"]
            old_cost = pos["cost_price"]
            old_available = pos.get("available", 0)
            new_qty = old_qty + quantity
            pos["cost_price"] = (old_qty * old_cost + amount) / new_qty
            pos["quantity"] = new_qty
            # 新买入部分受 T+1 约束：available 维持旧可卖数（不含本次加仓），
            # 并把 buy_date 更新为本次交易日——否则 end_of_day 会因 buy_date 仍是
            # 首次买入日而误判，把当日加仓部分也一并解锁（T+0 违规）。
            pos["available"] = old_available
            pos["buy_date"] = trade_date
        else:
            self._positions[code] = {
                "code": code, "name": name,
                "quantity": quantity,
                "available": 0,  # T+1，当日买入不可卖
                "cost_price": price,
                "current_price": price,
                "market_value": amount,
                "profit_loss": 0.0,
                "buy_date": trade_date,
            }

        self._save_position(code)
        order = {
            "code": code, "name": name, "direction": "BUY",
            "price": price, "quantity": quantity, "amount": amount,
            "commission": commission, "status": "FILLED",
            "order_date": trade_date, "fill_date": trade_date,
        }
        self.storage.save_order(order)
        return {"success": True, "order": order}

    def sell(self, code: str, price: float, quantity: int,
             trade_date: str, prev_close: float = None) -> dict:
        """执行卖出。``prev_close`` 语义同 :meth:`buy`。"""
        # 跌停底层兜底：价格非法（跌停卖不出）先于持仓充足性校验——即使未持仓，
        # 跌停价卖单也按"跌停"拒绝（与 simulator 原行为一致；也是
        # test_sell_above_limit_down_passes_limit_check 期望的校验顺序）。
        prev_close = self._resolve_prev_close(code, prev_close)
        if prev_close > 0 and is_at_limit_down(code, prev_close, price):
            return {"success": False, "msg": f"{code}已跌停（{price}≤跌停价），无法卖出"}

        pos = self._positions.get(code)
        if not pos:
            return {"success": False, "msg": f"未持有{code}"}

        available = pos.get("available", 0)
        if quantity > available:
            return {"success": False, "msg": f"可卖数量不足，可卖{available}股，尝试卖{quantity}股（T+1限制）"}

        quantity = round_to_lot(min(quantity, available))
        if quantity <= 0:
            return {"success": False, "msg": "可卖数量不足1手"}

        amount = price * quantity
        commission = calc_commission(amount, is_buy=False)
        net_income = amount - commission

        self._cash += net_income
        self._save_cash()
        pos["quantity"] -= quantity
        pos["available"] -= quantity

        if pos["quantity"] <= 0:
            del self._positions[code]
            self.storage.delete_position(code)
        else:
            self._save_position(code)

        order = {
            "code": code, "name": pos["name"], "direction": "SELL",
            "price": price, "quantity": quantity, "amount": amount,
            "commission": commission, "status": "FILLED",
            "order_date": trade_date, "fill_date": trade_date,
        }
        self.storage.save_order(order)
        return {"success": True, "order": order}

    def update_prices(self, prices: Dict[str, float]):
        """更新持仓市价和浮动盈亏"""
        for code, price in prices.items():
            if code in self._positions:
                pos = self._positions[code]
                pos["current_price"] = price
                pos["market_value"] = pos["quantity"] * price
                pos["profit_loss"] = (price - pos["cost_price"]) * pos["quantity"]
                self._save_position(code)

    def end_of_day(self, trade_date: str):
        """日终处理：T+1，当日买入的股票次日可卖"""
        for code, pos in self._positions.items():
            buy_date = pos.get("buy_date", "")
            if buy_date != trade_date:
                pos["available"] = pos["quantity"]
            self._save_position(code)

    def _save_position(self, code: str):
        if code in self._positions:
            self.storage.upsert_position(self._positions[code])

    def _resolve_prev_close(self, code: str, prev_close: float = None) -> float:
        """涨跌停校验用的昨收价：调用方传入优先，否则查本地最新日K收盘价。

        无历史数据（新上市股票、测试空库）时返回 0.0 → 调用方据此跳过涨跌停
        校验，避免误拦正常下单。查库异常同样降级为 0.0 并 warning。
        """
        if prev_close is not None:
            return float(prev_close)
        try:
            df = self.storage.get_daily_bars(code)
            if not df.empty:
                return float(df.iloc[-1]["close"])
        except Exception as e:
            logger.warning(f"取 {code} 昨收失败（跳过涨跌停校验）: {e}")
        return 0.0

    def summary(self, prices: Dict[str, float] = None) -> dict:
        mv = self.market_value(prices)
        total = self._cash + mv
        return {
            "cash": round(self._cash, 2),
            "market_value": round(mv, 2),
            "total_value": round(total, 2),
            "position_count": len(self._positions),
        }
