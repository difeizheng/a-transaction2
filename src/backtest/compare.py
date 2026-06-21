"""多策略回测对比"""
import logging
from typing import List, Dict
import pandas as pd

from src.backtest.engine import BacktestEngine
from src.backtest.metrics import (
    compute_buyhold_equity,
    compute_buyhold_return,
    BENCHMARK_NAMES,
)
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
        benchmark_code: str = "000300",
    ) -> Dict:
        """
        对多个策略分别运行回测，返回对比结果。

        status_callback(strategy_idx, total, stage, detail)。

        返回 dict：
          - summary: pd.DataFrame，每策略一行（含可能的 error 行）
          - equity_curves: {策略名: {"dates":[...], "values":[...]}} 逐 bar 净值
          - selected: {策略名: [{"code","name","score"}, ...]} 各策略选中的股票
          - benchmark: {"code","name","total_return","annual_return","curve"} 或 None
        """
        def _cb(idx, stage, detail=""):
            if status_callback:
                status_callback(idx, len(strategy_names), stage, detail)

        # 基准买入持有（取一次，全策略共用）。失败则降级为 None，不阻断回测。
        benchmark = self._build_benchmark(benchmark_code, start_date, end_date)

        results = []
        equity_curves: Dict[str, dict] = {}
        selected: Dict[str, list] = {}

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

                selected[name] = [
                    {"code": r.code, "name": r.name, "score": r.score}
                    for r in screen_results
                ]

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

                # 净值曲线随结果返回（不持久化），用于 UI 绘图
                equity_curves[name] = result.pop("equity_curve", {"dates": [], "values": []})

                results.append(result)

                # 保存到数据库（result 已不含 equity_curve）
                self.dm.storage.save_backtest_result(result)

            except Exception as e:
                logger.error(f"策略 {name} 回测失败: {e}")
                results.append({"strategy_name": name, "error": str(e)})

        if status_callback:
            status_callback(len(strategy_names), len(strategy_names), "完成", "")

        return {
            "summary": pd.DataFrame(results),
            "equity_curves": equity_curves,
            "selected": selected,
            "benchmark": benchmark,
        }

    def _build_benchmark(
        self,
        benchmark_code: str,
        start_date: str,
        end_date: str,
    ):
        """构建买入持有基准。失败/无数据返回 None（不阻断主回测）。"""
        try:
            idx_df = self.dm.get_index_daily_bars(benchmark_code, start_date, end_date)
        except Exception as e:
            logger.warning(f"基准指数 {benchmark_code} 获取失败，跳过基准对比: {e}")
            return None
        if idx_df is None or idx_df.empty:
            logger.warning(f"基准指数 {benchmark_code} 无数据，跳过基准对比")
            return None

        idx_df = idx_df.sort_values("trade_date").reset_index(drop=True)
        dates = idx_df["trade_date"].tolist()
        closes = idx_df["close"].astype(float).tolist()
        days = (pd.to_datetime(end_date) - pd.to_datetime(start_date)).days
        total_ret, annual_ret = compute_buyhold_return(closes, days)
        curve = compute_buyhold_equity(dates, closes, self.engine.initial_cash)
        return {
            "code": benchmark_code,
            "name": BENCHMARK_NAMES.get(benchmark_code, benchmark_code),
            "total_return": total_ret,
            "annual_return": annual_ret,
            "curve": curve,
        }

    @staticmethod
    def format_comparison(df: pd.DataFrame) -> pd.DataFrame:
        """格式化对比结果，用于UI展示。"""
        cols = ["strategy_name", "initial_cash", "final_value", "total_return", "annual_return", "sharpe",
                "max_drawdown", "win_rate", "profit_loss_ratio", "trades", "selected_stocks"]
        available = [c for c in cols if c in df.columns]
        df = df[available].copy()
        rename = {
            "strategy_name": "策略",
            "initial_cash": "初始资金",
            "final_value": "最终市值",
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
