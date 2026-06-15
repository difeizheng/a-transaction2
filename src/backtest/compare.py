"""多策略回测对比"""
import logging
from typing import List, Dict
import pandas as pd

from src.backtest.engine import BacktestEngine
from src.strategy.screener import Screener, STRATEGY_REGISTRY
from src.data.manager import DataManager

logger = logging.getLogger(__name__)


class _AsOfProxy:
    """回测选股时包装 DataManager：get_daily_bars 只返回 as_of 之前的数据，
    避免用回测期间的未来K线选股（前视偏差）。其余方法透传原 DataManager。
    注：财务数据（报告期）未截断，存在轻微时点偏差，已知局限。"""

    def __init__(self, dm, as_of: str):
        self._dm = dm
        self._as_of = as_of

    def get_daily_bars(self, code, start_date=None, end_date=None, **kwargs):
        return self._dm.get_daily_bars(code, start_date=start_date, end_date=self._as_of)

    def __getattr__(self, name):
        return getattr(self._dm, name)


class BacktestComparator:
    def __init__(self, config: dict, data_manager: DataManager):
        self.engine = BacktestEngine(config)
        self.dm = data_manager
        self.screener = Screener(data_manager)

    def compare(
        self,
        strategy_names: List[str],
        industry: str = None,
        start_date: str = "2022-01-01",
        end_date: str = "2024-12-31",
        top_n: int = 10,
        stop_loss: float = 0.05,
        take_profit: float = 0.15,
        status_callback=None,
    ) -> pd.DataFrame:
        """
        对多个策略分别运行回测，返回对比结果DataFrame
        status_callback(strategy_idx, total, stage, detail)
        """
        def _cb(idx, stage, detail=""):
            if status_callback:
                status_callback(idx, len(strategy_names), stage, detail)

        results = []
        for idx, name in enumerate(strategy_names):
            logger.info(f"回测策略: {name}")
            try:
                _cb(idx, "选股", name)
                strategy = self.screener.create_strategy(name)
                # 关键：选股只能用 start_date 之前的数据，否则等于用未来信息选股（前视偏差）。
                # 用 _AsOfProxy 包装 dm，使策略的 get_daily_bars 截断到 start_date。
                proxy_dm = _AsOfProxy(self.dm, start_date)
                temp_screener = Screener(proxy_dm)
                screen_results = temp_screener.run_strategy(strategy, industry=industry, top_n=top_n)
                signal_codes = [r.code for r in screen_results]

                if not signal_codes:
                    logger.warning(f"策略 {name} 未选出股票，跳过")
                    continue

                # 拉取所有选中股票的历史数据
                bars_dict = {}
                total_codes = len(signal_codes)
                for i, code in enumerate(signal_codes, 1):
                    _cb(idx, "拉取数据", f"{code} ({i}/{total_codes})")
                    df = self.dm.get_daily_bars(code, start_date=start_date, end_date=end_date)
                    if not df.empty:
                        bars_dict[code] = df

                _cb(idx, "运行回测", name)
                result = self.engine.run(
                    bars_dict=bars_dict,
                    signal_codes=signal_codes,
                    start_date=start_date,
                    end_date=end_date,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    strategy_name=name,
                )
                result["selected_stocks"] = len(signal_codes)
                results.append(result)

                # 保存到数据库
                self.dm.storage.save_backtest_result(result)

            except Exception as e:
                logger.error(f"策略 {name} 回测失败: {e}")
                results.append({"strategy_name": name, "error": str(e)})

        if status_callback:
            status_callback(len(strategy_names), len(strategy_names), "完成", "")
        return pd.DataFrame(results)

    @staticmethod
    def format_comparison(df: pd.DataFrame) -> pd.DataFrame:
        """格式化对比结果，用于UI展示"""
        cols = ["strategy_name", "total_return", "annual_return", "sharpe",
                "max_drawdown", "win_rate", "profit_loss_ratio", "trades", "selected_stocks"]
        available = [c for c in cols if c in df.columns]
        df = df[available].copy()
        rename = {
            "strategy_name": "策略",
            "total_return": "总收益(%)",
            "annual_return": "年化收益(%)",
            "sharpe": "夏普比率",
            "max_drawdown": "最大回撤(%)",
            "win_rate": "胜率(%)",
            "profit_loss_ratio": "盈亏比",
            "trades": "交易次数",
            "selected_stocks": "选股数量",
        }
        return df.rename(columns=rename)
