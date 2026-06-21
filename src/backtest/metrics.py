"""回测结果计算与展示用的纯函数（不依赖 backtrader / 网络）。

由 ``BacktestEngine.run()`` 返回的每策略汇总行 + 基准指数收盘序列派生：
- 基准买入持有净值曲线 / 收益
- 多策略中的综合最优选择
- 给用户看的结论摘要文案
- 低交易次数警告

抽成纯函数的目的：可单测，且 UI/引擎只做编排。年化口径与 ``engine.py`` 保持一致。
"""
from __future__ import annotations

from typing import Optional, Sequence

# 可选的基准指数（代码 → 中文名），UI 侧边栏与 compare() 共用，单一来源。
BENCHMARK_NAMES = {
    "000300": "沪深300",
    "000001": "上证指数",
    "399006": "创业板指",
    "000905": "中证500",
}

# 交易次数低于该值时，胜率 / 盈亏比统计意义不足，触发警告。
LOW_TRADE_THRESHOLD = 5


def compute_buyhold_equity(
    dates: Sequence,
    close_prices: Sequence[float],
    initial_cash: float,
) -> dict:
    """买入持有基准净值曲线：首日按 ``initial_cash`` 满仓买入并持有至期末。

    返回 ``{"dates": [...], "values": [...]}``，``values`` 为每日组合净值（元），
    首值恒等于 ``initial_cash``，与各策略起跑资金一致，可直接同轴对比。
    """
    if len(close_prices) == 0:
        return {"dates": list(dates), "values": []}
    first = close_prices[0]
    if not first or first <= 0:
        # 首日收盘异常（停牌/数据缺失）——无法归一，回退为常量净值。
        return {"dates": list(dates), "values": [initial_cash] * len(dates)}
    values = [initial_cash * c / first for c in close_prices]
    return {"dates": list(dates), "values": values}


def compute_buyhold_return(
    close_prices: Sequence[float],
    days: int,
) -> tuple:
    """买入持有基准收益：返回 ``(总收益%, 年化收益%)``。

    年化口径与 ``BacktestEngine.run()``（engine.py:175-176）一致：
    ``((1 + total/100) ** (365/days) - 1) * 100``。
    """
    if len(close_prices) < 2:
        return (0.0, 0.0)
    first, last = close_prices[0], close_prices[-1]
    if not first or first <= 0:
        return (0.0, 0.0)
    total_return = (last - first) / first * 100
    annual_return = ((1 + total_return / 100) ** (365 / max(days, 1)) - 1) * 100
    return (round(total_return, 2), round(annual_return, 2))


def _is_valid_row(r: dict) -> bool:
    """有效汇总行：无 error 键，且交易次数 > 0。"""
    return "error" not in r and bool(r.get("trades")) and r["trades"] > 0


def pick_best_strategy(summary_rows: Sequence[dict]) -> Optional[str]:
    """从回测汇总行中选出综合最优策略名。

    规则：忽略含 ``error`` 或 ``trades==0`` 的行；按 ``sharpe`` 降序，
    ``sharpe`` 缺失（None）按 ``-inf`` 处理并以 ``total_return`` 兜底。
    全部不可用则返回 None。
    """
    candidates = [r for r in summary_rows if _is_valid_row(r)]
    if not candidates:
        return None

    def _key(r: dict):
        sharpe = r.get("sharpe")
        sharpe = sharpe if sharpe is not None else float("-inf")
        return (sharpe, r.get("total_return", float("-inf")))

    best = max(candidates, key=_key)
    return best.get("strategy_name")


def low_trade_warnings(
    summary_rows: Sequence[dict],
    threshold: int = LOW_TRADE_THRESHOLD,
) -> list:
    """返回交易次数过少（``0 < trades < threshold``）的策略描述列表。

    ``trades == 0`` 的行视为未触发（可能是选股为空被跳过），不计入警告。
    """
    out = []
    for r in summary_rows:
        if "error" in r:
            continue
        trades = r.get("trades", 0)
        if 0 < trades < threshold:
            out.append(f"「{r.get('strategy_name')}」({trades} 笔)")
    return out


def build_conclusion(
    summary_rows: Sequence[dict],
    benchmark: Optional[dict],
    start_date: str,
    end_date: str,
) -> str:
    """生成给用户看的结论摘要（一段文字）。

    包含：回测区间、有效策略数、最优策略关键指标、相对基准的年化超额、
    以及低交易次数提示。各段按可用信息拼接，缺信息则跳过该段。
    """
    valid = [r for r in summary_rows if _is_valid_row(r)]
    parts = [f"本次回测区间 **{start_date} ~ {end_date}**，共 {len(valid)} 个有效策略。"]

    best_name = pick_best_strategy(summary_rows)
    best = None
    if best_name is not None:
        best = next(r for r in valid if r.get("strategy_name") == best_name)
        parts.append(
            f"最优策略「{best_name}」：年化 **{best.get('annual_return', 0)}%**、"
            f"最大回撤 **{best.get('max_drawdown', 0)}%**、"
            f"夏普 **{best.get('sharpe', 0)}**、胜率 **{best.get('win_rate', 0)}%**。"
        )

    if benchmark is not None and best is not None:
        bench_annual = benchmark.get("annual_return", 0.0)
        diff = best.get("annual_return", 0) - bench_annual
        bench_label = benchmark.get("name") or benchmark.get("code")
        verb = "跑赢" if diff >= 0 else "跑输"
        sign = "+" if diff >= 0 else ""
        parts.append(
            f"同期基准「{bench_label}」年化 **{bench_annual}%**，"
            f"策略{verb}基准 **{sign}{diff:.2f}%**。"
        )

    return " ".join(parts)
