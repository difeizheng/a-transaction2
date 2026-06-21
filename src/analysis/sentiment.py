"""市场情绪温度纯函数。

由真实市场数据（指数涨跌 + 板块轮动广度）确定性计算 0–100 情绪温度，
不依赖网络 / LLM，可单测、可复现。**LLM 不再凭空给分——温度来自这里。**

镜像 ``test_screener_normalize.py`` 的纯函数测试范式：本模块只接受已清洗的
数字输入，绝不触碰东财中文列名 / DataFrame（列名重命名在
``DataManager.get_market_snapshot`` 完成）。

口径自检（手算）：
  - 常态：沪深300 +0.5%、其余 +0.3%、板块中位 +0.2%、55% 上涨 → ≈ 52.9
  - 普涨：全部指数 +2%、板块中位 +2%、全涨 → ≈ 70.4（明显看涨）
  - 普跌：全部指数 -2%、板块中位 -2%、全跌 → ≈ 29.6（明显看跌）
  - 全平 → 50.0（中性）
"""
import math
import statistics
from typing import Optional

# 指数代码 / 腾讯备源 symbol 下沉到 data 层（src/data/market_indices.py），消除
# data→analysis 反向依赖；此处 re-export 保持向后兼容（旧代码仍可
# `from src.analysis.sentiment import INDEX_CODES`）。
from src.data.market_indices import INDEX_CODES, INDEX_TENCENT_SYMBOLS

# 指数权重（和归一）。沪深300 最能代表大盘 → 权重最高。
INDEX_WEIGHTS = {
    "000300": 0.40,
    "000001": 0.20,
    "399006": 0.20,
    "000905": 0.20,
}

# （INDEX_TENCENT_SYMBOLS 已由文件顶部 re-export 从 src.data.market_indices 引入）

INDEX_PCT_SCALE = 8.0        # 1 个指数 +2% → 该指数贡献约 +16 原始分
SECTOR_MEDIAN_SCALE = 6.0
SECTOR_BREADTH_SCALE = 30.0  # 广度最大摆幅 ±15（全涨 vs 全跌）
INPUT_CLAMP_PCT = 10.0       # 单输入钳到 [-10,10]（A 股涨跌停边界，超出视为噪声）

BASELINE = 50.0
BULLISH_THRESHOLD = 60.0
BEARISH_THRESHOLD = 40.0

# 指数 / 板块合成权重（指数信号更干净，权重更高）
INDEX_BLEND = 0.6
SECTOR_BLEND = 0.4


def _safe_float(v) -> float:
    """转 float；None / 非数字 / inf / nan → nan。"""
    if v is None:
        return float("nan")
    try:
        f = float(v)
    except (TypeError, ValueError):
        return float("nan")
    if math.isnan(f) or math.isinf(f):
        return float("nan")
    return f


def _clamp_pct(x: float) -> float:
    """钳到 [-INPUT_CLAMP_PCT, INPUT_CLAMP_PCT]。"""
    if x > INPUT_CLAMP_PCT:
        return INPUT_CLAMP_PCT
    if x < -INPUT_CLAMP_PCT:
        return -INPUT_CLAMP_PCT
    return x


def _label(temperature: float) -> str:
    """温度 → bullish / neutral / bearish（边界值归入更积极的一侧）。"""
    if temperature >= BULLISH_THRESHOLD:
        return "bullish"
    if temperature <= BEARISH_THRESHOLD:
        return "bearish"
    return "neutral"


def _neutral(reason: str = "no_market_data") -> dict:
    return {
        "temperature": BASELINE,
        "label": _label(BASELINE),
        "components": {"index": 0.0, "sector_median": 0.0, "sector_breadth": 0.0},
        "debug": {"reason": reason, "index_count": 0, "sector_count": 0},
    }


def compute_market_temperature(
    index_moves: Optional[dict],
    sector_returns: Optional[list],
    sector_advances: Optional[int] = None,
    sector_declines: Optional[int] = None,
) -> dict:
    """计算市场情绪温度。

    Args:
        index_moves: {指数代码: 当日涨跌幅%}，如 ``{"000300": 0.52}``。
        sector_returns: 各行业板块当日涨跌幅% 列表（顺序无关，取中位数）。
        sector_advances / sector_declines: 板块层面的上涨 / 下跌家数（可选）。
            缺省时用 ``sector_returns`` 中正数占比估算广度。

    Returns:
        ``{temperature: float[0,100], label, components, debug}``

    权重：指数信号 INDEX_BLEND、板块信号 SECTOR_BLEND（指数 0.6 更干净）。
    **缺失指数的权重重分配**到存活指数（同 screener 的归一化纪律），避免因某
    指数取数失败丢信号。指数 / 板块任一为空则退化为单边合成。
    """
    idx_raw = {k: _safe_float(v) for k, v in (index_moves or {}).items()}
    idx = {k: _clamp_pct(v) for k, v in idx_raw.items() if v == v}  # 丢 NaN

    secs = [_clamp_pct(x) for x in (_safe_float(v) for v in (sector_returns or [])) if x == x]

    if not idx and not secs:
        return _neutral("no_market_data")

    # 指数贡献（缺失指数权重重分配）
    index_contrib = 0.0
    if idx:
        present = {k: idx[k] for k in idx if k in INDEX_WEIGHTS}
        if present:
            w_sum = sum(INDEX_WEIGHTS[k] for k in present)
            present_weighted = sum(INDEX_WEIGHTS[k] / w_sum * present[k] for k in present)
            index_contrib = present_weighted * INDEX_PCT_SCALE

    # 板块贡献（中位涨幅 + 广度）
    sector_median_contrib = 0.0
    sector_breadth_contrib = 0.0
    if secs:
        median_ret = statistics.median(secs)
        sector_median_contrib = median_ret * SECTOR_MEDIAN_SCALE
        if (sector_advances is not None and sector_declines is not None
                and (sector_advances + sector_declines) > 0):
            adv_ratio = sector_advances / (sector_advances + sector_declines)
        else:
            adv_ratio = sum(1 for s in secs if s > 0) / len(secs)
        sector_breadth_contrib = (adv_ratio - 0.5) * SECTOR_BREADTH_SCALE

    # 合成（指数 / 板块任一为空 → 单边退化）
    if idx and secs:
        temperature = (
            BASELINE
            + INDEX_BLEND * index_contrib
            + SECTOR_BLEND * (sector_median_contrib + sector_breadth_contrib)
        )
    elif idx:
        temperature = BASELINE + index_contrib
    else:
        temperature = BASELINE + sector_median_contrib + sector_breadth_contrib

    temperature = max(0.0, min(100.0, round(temperature, 1)))
    return {
        "temperature": temperature,
        "label": _label(temperature),
        "components": {
            "index": round(index_contrib, 2),
            "sector_median": round(sector_median_contrib, 2),
            "sector_breadth": round(sector_breadth_contrib, 2),
        },
        "debug": {"index_count": len(idx), "sector_count": len(secs)},
    }
