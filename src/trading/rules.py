"""A股交易规则"""


def get_price_limit(code: str, prev_close: float) -> tuple:
    """
    返回 (涨停价, 跌停价)
    主板: ±10%，创业板(30)/科创板(68/69): ±20%，北交所(8/9): ±30%
    ST股: ±5%
    """
    if code.startswith("30") or code.startswith("68") or code.startswith("69"):
        pct = 0.20
    elif code.startswith("8") or code.startswith("9"):
        pct = 0.30
    else:
        pct = 0.10
    limit_up = round(prev_close * (1 + pct), 2)
    limit_down = round(prev_close * (1 - pct), 2)
    return limit_up, limit_down


def calc_commission(amount: float, is_buy: bool, commission_rate: float = 0.0003) -> float:
    """计算手续费，最低5元"""
    fee = max(amount * commission_rate, 5.0)
    if not is_buy:
        fee += amount * 0.001  # 印花税（卖出）
    return round(fee, 2)


def round_to_lot(quantity: int) -> int:
    """取整到100股（1手）"""
    return (quantity // 100) * 100


def is_t1_available(buy_date: str, current_date: str) -> bool:
    """T+1规则：买入日期不等于当前日期才可卖出"""
    return buy_date != current_date
