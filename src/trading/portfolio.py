"""持仓管理"""
import logging
from datetime import date
from typing import Dict, Optional
import pandas as pd

from src.trading.rules import calc_commission, round_to_lot, is_t1_available
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

    def buy(self, code: str, name: str, price: float, quantity: int, trade_date: str) -> dict:
        """执行买入，返回成交记录"""
        quantity = round_to_lot(quantity)
        if quantity <= 0:
            return {"success": False, "msg": "数量不足1手（100股）"}

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
            new_qty = old_qty + quantity
            pos["cost_price"] = (old_qty * old_cost + amount) / new_qty
            pos["quantity"] = new_qty
            pos["available"] = pos.get("available", 0)  # 当日买入不可卖
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

    def sell(self, code: str, price: float, quantity: int, trade_date: str) -> dict:
        """执行卖出"""
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

    def summary(self, prices: Dict[str, float] = None) -> dict:
        mv = self.market_value(prices)
        total = self._cash + mv
        return {
            "cash": round(self._cash, 2),
            "market_value": round(mv, 2),
            "total_value": round(total, 2),
            "position_count": len(self._positions),
        }
