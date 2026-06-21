"""风控纯函数：回撤、成交量约束、加仓数量计算、回撤分档减仓、行业集中度。

抽成纯函数的目的：
1. 可单测（不依赖 Storage / Portfolio / AutoTrader 等重对象）；
2. 回测引擎（``backtest.engine``）与实盘自动交易（``trading.auto_trader``）
   共用同一套风控数学，避免「实盘严谨、回测放水」的两套口径。

A股 T+1 / 涨跌停 / 手数等规则见 ``trading.rules``；本模块只负责风控量纲计算。
"""
from __future__ import annotations

import math
from typing import List, Mapping, Sequence, Tuple

from src.trading.rules import round_to_lot


def compute_drawdown_pct(peak_value: float, current_value: float) -> float:
    """从历史峰值（high-water mark）计算回撤百分比，返回 >= 0 的值。

    真正的回撤 = (峰值 - 当前) / 峰值。旧实现用 ``(初始资金 - 当前)/初始资金``，
    那是「累计盈亏」不是回撤：先赚后亏时，累计盈亏远小于真实回撤，
    会导致 -15% 熔断线在真实回撤 25% 时仍不触发。

    Args:
        peak_value: 历史最高组合净值（持久化的 high-water mark）。
        current_value: 当前组合净值。

    Returns:
        回撤百分比（如 12.34 表示 12.34%）。peak<=0 或无回撤时返回 0.0。
    """
    if peak_value <= 0:
        return 0.0
    return max(0.0, (peak_value - current_value) / peak_value * 100.0)


def cap_size_by_volume(target_size: int, bar_volume: float,
                       max_pct: float = 0.25, lot: int = 100) -> int:
    """限制单笔下单不超过当日成交量一定比例，并取整到手。

    无限流动性是回测虚高收益的头号来源之一——50 万拍在小盘股上「安然成交」，
    实盘直接打飞价格。机构通常限制单笔 <= 当日成交量 10%~25%。

    Args:
        target_size: 目标下单股数。
        bar_volume: 当日成交量（股）。
        max_pct: 单笔占当日成交量上限（默认 25%）。
        lot: 手数单位（A股 100 股）。

    Returns:
        受约束并取整到手的目标股数（>=0）。
    """
    if target_size <= 0:
        return 0
    if bar_volume and bar_volume > 0:
        vol_cap = int(bar_volume * max_pct)
        capped = min(target_size, vol_cap)
    else:
        capped = target_size  # 无成交量数据时不额外约束（保守由调用方决定）
    return round_to_lot(capped)


def compute_add_buy_quantity(
    total_value: float,
    existing_pos_value: float,
    price: float,
    max_position_pct: float,
) -> Tuple[int, str]:
    """计算加仓数量：单股仓位上限**扣除已有持仓市值**后的剩余额度。

    旧实现 ``max_amount = total_value * max_position_pct / 100`` 没减已有持仓，
    导致某股已占 12%、上限 20% 时仍按 20% 加仓 → 实际冲到 32%，上限被击穿。

    Args:
        total_value: 当前组合总净值（现金+市值）。
        existing_pos_value: 该股当前持仓市值。
        price: 当前买入价。
        max_position_pct: 单股仓位上限（%，如 20 表示 20%）。

    Returns:
        (quantity, reason)。quantity 为手数整数（>=0）；quantity<=0 时 reason
        说明被拦原因，调用方可记入风控拦截日志。
    """
    if price <= 0 or total_value <= 0:
        return 0, "价格或总资产非法"
    max_pos_value = total_value * max_position_pct / 100.0
    headroom = max_pos_value - existing_pos_value
    if headroom <= 0:
        return 0, f"已达单股仓位上限 {max_position_pct}%（现有持仓已占满额度）"
    quantity = round_to_lot(int(headroom / price))
    if quantity <= 0:
        return 0, "可加仓额度不足1手（资金不足或价格过高）"
    return quantity, ""


# ── 回撤分档减仓（circuit-breaker deescalation）──────────────────
# 默认回撤减仓阶梯：(drawdown 阈值%, 目标保留仓位比例)。
# 回撤越深，强制把每只持仓 trim 到越低比例——真正的下行保护，而非仅「暂停开新仓」
# （审计报告 P1-B：旧实现熔断只暂停开仓，已深套持仓照旧裸奔，无减仓/清仓分档）。
DEFAULT_DEESCALATION_TIERS: List[Tuple[float, float]] = [
    (10.0, 1.00),    # < 10%   ：正常，保留 100%
    (15.0, 0.70),    # 10–15%  ：轻度减仓，trim 到 70%
    (20.0, 0.50),    # 15–20%  ：中度减仓，trim 到 50%
    (25.0, 0.30),    # 20–25%  ：重度减仓，trim 到 30%
    (math.inf, 0.20),  # ≥ 25%  ：深度减仓，trim 到 20%（保留底仓防踏空反弹）
]


def deescalation_level(
    drawdown_pct: float,
    tiers: Sequence[Tuple[float, float]] = DEFAULT_DEESCALATION_TIERS,
) -> Tuple[float, int]:
    """根据回撤幅度返回 ``(keep_ratio, tier_index)``。

    ``keep_ratio`` 是目标保留仓位比例（0~1）：回撤达到某档阈值即把每只持仓 trim 到
    对应比例。回撤低于第一档阈值时返回 ``(1.0, 0)``（不强制减仓）。``tier_index`` 标识
    命中的档位（0=最轻…N-1=最深），供 UI 展示「当前减仓级别」。
    """
    if drawdown_pct < 0:
        drawdown_pct = 0.0
    for idx, (threshold, keep) in enumerate(tiers):
        if drawdown_pct < threshold:
            return float(keep), idx
    # drawdown 超过所有有限阈值（含 inf 档）→ 命中最后一档
    last_keep = tiers[-1][1]
    return float(last_keep), len(tiers) - 1


def compute_trim_quantity(current_qty: int, keep_ratio: float, lot: int = 100) -> int:
    """计算单只持仓需**减仓**的股数（>=0）。

    减仓后保留 ``round_to_lot(current_qty * keep_ratio)``（向下取整到整手，保证剩余持仓
    是干净的手数），故 trim 量 = ``current_qty - 保留量``。

    注意：trim 量本身**可能是零股（< 1 手）**——A 股只要求**买入**整手，**卖出**允许零股
    （清理零头）。所以不对 trim 再取整；保证「剩余持仓」是整手即可（审计/回测口径一致）。
    """
    if current_qty <= 0 or keep_ratio >= 1.0:
        return 0
    keep_qty = round_to_lot(int(current_qty * keep_ratio))
    trim = current_qty - keep_qty
    return trim if trim > 0 else 0


# ── 行业/风格集中度（concentration cap）──────────────────────────
def compute_concentration(weights: Mapping[str, float]) -> dict:
    """计算分组权重的集中度指标。

    Args:
        weights: {分组: 权重（市值/金额，任意同纲量）}。

    Returns:
        ``{total, n_groups, max_group, max_pct, hhi}``：
        - ``max_pct``：最大单一分组占比（%）；
        - ``hhi``：赫芬达尔指数 = Σ(占比%)²，越大越集中（单一分组独占≈10000）。
    """
    total = float(sum(weights.values())) if weights else 0.0
    if total <= 0:
        return {"total": 0.0, "n_groups": 0, "max_group": None,
                "max_pct": 0.0, "hhi": 0.0}
    pcts = {g: w / total * 100 for g, w in weights.items()}
    max_group = max(pcts, key=pcts.get)
    hhi = sum(p * p for p in pcts.values())
    return {
        "total": round(total, 4),
        "n_groups": len(weights),
        "max_group": max_group,
        "max_pct": round(pcts[max_group], 2),
        "hhi": round(hhi, 2),
    }


def find_concentration_breaches(
    weights: Mapping[str, float], cap_pct: float
) -> List[Tuple[str, float]]:
    """返回占比超过 ``cap_pct``% 的 ``[(分组, 占比%)]``，按占比降序。"""
    total = float(sum(weights.values())) if weights else 0.0
    if total <= 0:
        return []
    breaches = [(g, round(w / total * 100, 2)) for g, w in weights.items()
                if w / total * 100 > cap_pct]
    return sorted(breaches, key=lambda x: -x[1])


def would_breach_concentration(
    weights: Mapping[str, float],
    group: str,
    add_amount: float,
    cap_pct: float,
) -> bool:
    """拟在 ``group`` 增配 ``add_amount`` 后，该分组占比是否会超过 ``cap_pct``%。

    用于买入前预检：把拟买入金额加到该行业，若会令该行业突破集中度上限则拦截。
    ``weights`` 不必归一化（按总额算占比）；``group`` 不在 weights 中视为 0。
    """
    new_total = float(sum(weights.values())) + add_amount
    if new_total <= 0:
        return False
    new_group_weight = float(weights.get(group, 0.0)) + add_amount
    return new_group_weight / new_total * 100 > cap_pct
