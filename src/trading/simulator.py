"""模拟交易引擎"""
import logging
from datetime import date
from typing import Dict, List, Optional
import pandas as pd

from src.trading.portfolio import Portfolio
from src.data.manager import DataManager, _latest_possible_trading_day
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
        self._auto_end_of_day_if_stale()

    def _auto_end_of_day_if_stale(self) -> int:
        """启动时补做日终处理：解锁 buy_date 早于最近交易日的持仓（T+1）。

        旧实现 T+1 解锁只靠 UI「日终处理」手动按钮——用户忘点则持仓永远
        ``available=0`` 卖不出（docs/投资系统审计报告.md 附录待确认点 1）。
        构造 Simulator 时自动补做：存在 ``buy_date < 最近交易日``（周末回退
        到周五，节假日保守不解锁）且仍未解锁的持仓时，触发一次
        ``end_of_day(today)``。幂等，无副作用。

        Returns:
            本次解锁的持仓只数（0 = 无需处理）。
        """
        try:
            latest = _latest_possible_trading_day().isoformat()
            stale = [
                code for code, pos in self.portfolio.positions.items()
                if pos.get("buy_date", "") < latest
                and pos.get("available", 0) < pos.get("quantity", 0)
            ]
            if stale:
                self.portfolio.end_of_day(date.today().isoformat())
                logger.info(f"启动自动日终处理：解锁 {len(stale)} 只 T+1 持仓 {stale}")
            return len(stale)
        except Exception as e:
            logger.warning(f"启动自动日终处理失败（不影响使用）: {e}")
            return 0

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

    def _get_prev_close(self, code: str) -> Optional[float]:
        """本地最新日K收盘价，供 Portfolio 涨跌停校验（无数据返回 None → 不拦截）。"""
        df = self.dm.storage.get_daily_bars(code)
        if not df.empty:
            return float(df.iloc[-1]["close"])
        return None

    def place_buy(self, code: str, name: str, quantity: int, price: float = None) -> dict:
        """下买单，price为None时用当前市价。

        涨跌停校验由 :meth:`Portfolio.buy` 兜底（传 prev_close 避免重复查库）。
        """
        trade_date = date.today().isoformat()
        if price is None:
            price = self.get_current_price(code)
            if price is None:
                return {"success": False, "msg": f"无法获取{code}价格"}

        return self.portfolio.buy(code, name, price, quantity, trade_date,
                                  prev_close=self._get_prev_close(code))

    def place_sell(self, code: str, quantity: int, price: float = None) -> dict:
        """下卖单。跌停校验由 :meth:`Portfolio.sell` 兜底。"""
        trade_date = date.today().isoformat()
        if price is None:
            price = self.get_current_price(code)
            if price is None:
                return {"success": False, "msg": f"无法获取{code}价格"}

        return self.portfolio.sell(code, price, quantity, trade_date,
                                   prev_close=self._get_prev_close(code))

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
