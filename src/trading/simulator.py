"""模拟交易引擎"""
import logging
from datetime import date
from typing import Dict, List, Optional
import pandas as pd

from src.trading.portfolio import Portfolio
from src.trading.rules import get_price_limit
from src.data.manager import DataManager
from src.data.storage import Storage
from src.config import get_config

logger = logging.getLogger(__name__)


class TradingSimulator:
    def __init__(self, data_manager: DataManager):
        self.dm = data_manager
        cfg = get_config()
        self.portfolio = Portfolio(
            storage=data_manager.storage,
            initial_cash=cfg["trading"]["initial_cash"],
        )

    def get_current_price(self, code: str) -> Optional[float]:
        """获取实时价格（优先实时，降级到最新收盘价）"""
        try:
            quotes = self.dm.get_realtime_quotes([code])
            if not quotes.empty:
                return float(quotes.iloc[0]["price"])
        except Exception:
            pass
        # 降级：取本地最新收盘价
        df = self.dm.storage.get_daily_bars(code)
        if not df.empty:
            return float(df.iloc[-1]["close"])
        return None

    def place_buy(self, code: str, name: str, quantity: int, price: float = None) -> dict:
        """下买单，price为None时用当前市价"""
        trade_date = date.today().isoformat()
        if price is None:
            price = self.get_current_price(code)
            if price is None:
                return {"success": False, "msg": f"无法获取{code}价格"}

        # 检查涨跌停
        df = self.dm.storage.get_daily_bars(code)
        if not df.empty:
            prev_close = float(df.iloc[-1]["close"])
            limit_up, limit_down = get_price_limit(code, prev_close)
            if price >= limit_up:
                return {"success": False, "msg": f"{code}已涨停（{limit_up}），无法买入"}

        return self.portfolio.buy(code, name, price, quantity, trade_date)

    def place_sell(self, code: str, quantity: int, price: float = None) -> dict:
        """下卖单"""
        trade_date = date.today().isoformat()
        if price is None:
            price = self.get_current_price(code)
            if price is None:
                return {"success": False, "msg": f"无法获取{code}价格"}

        # 检查跌停
        df = self.dm.storage.get_daily_bars(code)
        if not df.empty:
            prev_close = float(df.iloc[-1]["close"])
            _, limit_down = get_price_limit(code, prev_close)
            if price <= limit_down:
                return {"success": False, "msg": f"{code}已跌停（{limit_down}），无法卖出"}

        return self.portfolio.sell(code, price, quantity, trade_date)

    def refresh_positions(self):
        """刷新持仓市价"""
        codes = list(self.portfolio.positions.keys())
        if not codes:
            return
        try:
            quotes = self.dm.get_realtime_quotes(codes)
            prices = {row["code"]: float(row["price"]) for _, row in quotes.iterrows()}
            self.portfolio.update_prices(prices)
        except Exception as e:
            logger.warning(f"刷新持仓价格失败: {e}")

    def get_portfolio_summary(self) -> dict:
        self.refresh_positions()
        return self.portfolio.summary()

    def get_positions_df(self) -> pd.DataFrame:
        self.refresh_positions()
        if not self.portfolio.positions:
            return pd.DataFrame()
        rows = list(self.portfolio.positions.values())
        df = pd.DataFrame(rows)
        df["profit_pct"] = ((df["current_price"] - df["cost_price"]) / df["cost_price"] * 100).round(2)
        return df

    def get_orders_df(self) -> pd.DataFrame:
        return self.dm.storage.get_orders()

    def end_of_day(self):
        """日终处理（手动触发或定时调用）"""
        self.portfolio.end_of_day(date.today().isoformat())
        logger.info("日终处理完成，T+1限制已更新")
