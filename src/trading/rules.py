"""A股交易规则"""

# 费率常量（集中管理，避免散落硬编码导致账目失真）。
# 印花税：2023.8.28 起由 0.1% 降至 0.05%，**卖出单边**征收。
STAMP_DUTY_RATE = 0.0005
# 过户费：2022.4.29 起沪深统一为 0.001%，**双边**征收（原沪市 0.002%）。
TRANSFER_FEE_RATE = 0.0001
# 券商佣金下限：不足 5 元按 5 元收。
MIN_COMMISSION = 5.0


def get_price_limit(code: str, prev_close: float) -> tuple:
    """
    返回 (涨停价, 跌停价)
    主板: ±10%，创业板(30)/科创板(68/69): ±20%，北交所(8/9): ±30%
    ST股: ±5%（注：当前未根据名称/标记判定 ST，调用方需自行规避 ST 股按本档）
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


def is_at_limit_up(code: str, prev_close: float, price: float) -> bool:
    """价格是否触及/超过涨停价（买不进）。prev_close<=0 时按未触及处理。"""
    if prev_close <= 0:
        return False
    limit_up, _ = get_price_limit(code, prev_close)
    return price >= limit_up


def is_at_limit_down(code: str, prev_close: float, price: float) -> bool:
    """价格是否触及/跌破跌停价（卖不出）。prev_close<=0 时按未触及处理。"""
    if prev_close <= 0:
        return False
    _, limit_down = get_price_limit(code, prev_close)
    return price <= limit_down


def calc_commission(
    amount: float,
    is_buy: bool,
    commission_rate: float = 0.0003,
    stamp_duty_rate: float = STAMP_DUTY_RATE,
    transfer_fee_rate: float = TRANSFER_FEE_RATE,
    min_commission: float = MIN_COMMISSION,
) -> float:
    """计算交易费用。

    构成：
    - 券商佣金：双边，``max(amount*commission_rate, min_commission)``；
    - 过户费：双边 0.001%（2022.4.29 起沪深统一）；
    - 印花税：**仅卖出** 0.05%（2023.8.28 起；旧值 0.1% 已作废）。

    旧实现漏过户费、印花税写死 0.001（多收一倍），长期回测/模拟系统性失真。
    """
    fee = max(amount * commission_rate, min_commission)
    fee += amount * transfer_fee_rate           # 过户费双边
    if not is_buy:
        fee += amount * stamp_duty_rate         # 印花税卖出单边
    return round(fee, 2)


def round_to_lot(quantity: int) -> int:
    """取整到100股（1手）"""
    return (quantity // 100) * 100


def is_t1_available(buy_date: str, current_date: str) -> bool:
    """T+1规则：买入日期不等于当前日期才可卖出"""
    return buy_date != current_date
