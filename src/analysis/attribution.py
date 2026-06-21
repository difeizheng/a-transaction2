"""绩效归因 —— Brinson 多期/多行业超额收益分解（纯函数）。

知道「组合赚了多少」只是总账；绩效归因回答「赚的是什么钱」：相对基准的超额收益
（active return）拆成三块：

- **配置效应（allocation）** = Σ (w_p,i − w_b,i)·(R_b,i − R_b)
  —— 超配/低配了哪些行业；超配好行业为正。
- **选股效应（selection）** = Σ w_b,i·(R_p,i − R_b,i)
  —— 在同一行业内选股是否跑赢行业基准。
- **交互效应（interaction）** = Σ (w_p,i − w_b,i)·(R_p,i − R_b,i)
  —— 配置与选股的联合残差（既改了权重又改了选股）。

三者之和 = 组合相对基准的总超额收益（Brinson-Fachler 恒等式，可用作正确性校验）。

纯函数、零外部依赖；输入用 dict {行业/资产: 权重}、{行业/资产: 收益率}，
便于用已知数值单测（见 tests/test_attribution.py）。
"""
from __future__ import annotations

from typing import Dict, List

import pandas as pd


def _total_return(weights: Dict[str, float], returns: Dict[str, float]) -> float:
    """加权平均总收益 = Σ w_i · R_i（行业全集取并集，缺失视为 0）。"""
    sectors = set(weights) | set(returns)
    return sum(weights.get(s, 0.0) * returns.get(s, 0.0) for s in sectors)


def brinson_attribution(
    portfolio_weights: Dict[str, float],
    portfolio_returns: Dict[str, float],
    benchmark_weights: Dict[str, float],
    benchmark_returns: Dict[str, float],
) -> dict:
    """Brinson-Fachler 行业归因。

    Args:
        portfolio_weights: {行业: 组合权重}（建议归一化，但函数不强制）。
        portfolio_returns: {行业: 组合在该行业的收益率}。
        benchmark_weights / benchmark_returns: 基准对应项。

    Returns:
        ``{
            portfolio_return, benchmark_return, active_return,
            allocation, selection, interaction,   # 三大效应合计
            by_sector: [{sector, w_p, w_b, r_p, r_b,
                         allocation, selection, interaction, active}, ...],
        }``
        其中 ``active_return ≈ allocation + selection + interaction``（恒等校验）。
    """
    sectors = sorted(set(portfolio_weights) | set(portfolio_returns)
                     | set(benchmark_weights) | set(benchmark_returns))
    bench_total = _total_return(benchmark_weights, benchmark_returns)
    port_total = _total_return(portfolio_weights, portfolio_returns)

    rows: List[dict] = []
    tot_alloc = tot_sel = tot_inter = 0.0
    for s in sectors:
        wp = float(portfolio_weights.get(s, 0.0))
        wb = float(benchmark_weights.get(s, 0.0))
        rp = float(portfolio_returns.get(s, 0.0))
        rb = float(benchmark_returns.get(s, 0.0))
        alloc = (wp - wb) * (rb - bench_total)
        sel = wb * (rp - rb)
        inter = (wp - wb) * (rp - rb)
        tot_alloc += alloc
        tot_sel += sel
        tot_inter += inter
        rows.append({
            "sector": s, "w_p": round(wp, 6), "w_b": round(wb, 6),
            "r_p": round(rp, 6), "r_b": round(rb, 6),
            "allocation": round(alloc, 6),
            "selection": round(sel, 6),
            "interaction": round(inter, 6),
            "active": round(alloc + sel + inter, 6),
        })

    active = port_total - bench_total
    return {
        "portfolio_return": round(port_total, 6),
        "benchmark_return": round(bench_total, 6),
        "active_return": round(active, 6),
        "allocation": round(tot_alloc, 6),
        "selection": round(tot_sel, 6),
        "interaction": round(tot_inter, 6),
        "by_sector": rows,
    }


def aggregate_to_sectors(
    stock_weights: Dict[str, float],
    stock_returns: Dict[str, float],
    stock_sector: Dict[str, str],
) -> tuple:
    """从个股级权重/收益聚合到行业级，供 ``brinson_attribution`` 使用。

    Args:
        stock_weights: {股票: 权重}。
        stock_returns: {股票: 收益率}。
        stock_sector: {股票: 所属行业}。

    Returns:
        ``(sector_weights, sector_returns)``：行业权重 = 该行业个股权重和；
        行业收益 = 该行业内个股的**权重加权平均**收益率（不是简单平均——大盘股
        不应与小盘股等权）。无持仓行业收益按 0。
    """
    sec_w: Dict[str, float] = {}
    sec_wr: Dict[str, float] = {}  # 行业内 Σ w·r
    for code, w in stock_weights.items():
        sector = stock_sector.get(code, "未知")
        sec_w[sector] = sec_w.get(sector, 0.0) + float(w)
        sec_wr[sector] = sec_wr.get(sector, 0.0) + float(w) * float(stock_returns.get(code, 0.0))
    sec_r: Dict[str, float] = {}
    for sector, w in sec_w.items():
        sec_r[sector] = sec_wr[sector] / w if w != 0 else 0.0
    return sec_w, sec_r


def attribution_from_stocks(
    port_weights: Dict[str, float],
    port_returns: Dict[str, float],
    bench_weights: Dict[str, float],
    bench_returns: Dict[str, float],
    stock_sector: Dict[str, str],
) -> dict:
    """端到端：从个股权重/收益 + 股票→行业映射，直接做 Brinson 归因（最常用入口）。"""
    pw, pr = aggregate_to_sectors(port_weights, port_returns, stock_sector)
    bw, br = aggregate_to_sectors(bench_weights, bench_returns, stock_sector)
    return brinson_attribution(pw, pr, bw, br)


def to_dataframe(result: dict) -> pd.DataFrame:
    """把归因结果 by_sector 渲染为 DataFrame，便于 UI 表格展示。"""
    return pd.DataFrame(result["by_sector"])
