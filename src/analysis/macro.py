"""宏观态势纯函数。

由真实宏观指标（流动性 / 资金面 / 基本面 / 外部 四支柱）确定性计算 0–100
宏观态势分，不依赖网络 / LLM，可单测、可复现。**LLM 不再凭空给分——态势分
来自这里；政策面（纯函数算不了）才交给 LLM 定性。**

镜像 ``sentiment.py`` 的范式：本模块只接受已清洗的 ``indicators``（每指标带
``latest`` + ``reference``，由 ``DataManager.get_macro_snapshot`` 从 akshare
中文列抽取），绝不触碰 akshare 列名 / DataFrame。

口径自检（手算）：
  - 常态：各指标落在历史中位附近 → ≈ 50（中性）
  - 全面宽松：M1>M2、SHIBOR 低位、社融高增、北向大流入、PMI>51、美债下行、
    人民币升值 → ≈ 95+（明显宽松）
  - 全面偏紧：反方向 → ≈ 5−（明显偏紧）
  - 全平 → 50.0
"""
from __future__ import annotations

import math
from typing import Optional

# 指标策略注册表：key -> (scale, direction_sign, use_neutral, neutral)
#   signal = direction_sign * clamp(value / scale, -1, 1)
#   value  = (latest - neutral) if use_neutral else (latest - reference)
# direction_sign：+1 = 该指标走高偏多；-1 = 走高偏空（如利率/美债/USD-CNY 升 → 偏空）。
# use_neutral=True：用固定中性点（PMI 50、M1-M2 0 等），忽略 reference。
# use_neutral=False：用 latest - reference（reference 由 manager 算历史基线，如 60 日中位）。
INDICATOR_SPEC: dict[str, tuple[float, int, bool, float]] = {
    # 流动性
    "m1_m2_gap":      (5.0,  +1, True,  0.0),   # M1同比 - M2同比；正值=资金活化=+
    "shibor_on":      (0.5,  -1, False, 0.0),   # 隔夜利率升于60日中位=收紧=-
    "social_fin_yoy": (5.0,  +1, False, 0.0),   # 社融同比走高=信用扩张=+
    # 资金面（注：北向资金日度净流入自 2024-08-19 起交易所不再公布，故不含）
    "margin_5d_pct":  (0.01, +1, True,  0.0),   # 融资余额近5日变化%；升=杠杆加=+
    # 基本面
    "pmi":            (2.0,  +1, True,  50.0),  # 50=荣枯线；每2点=+
    "ppi_cpi_gap":    (3.0,  +1, True,  0.0),   # PPI同比 - CPI同比；正=工业利润扩张=+
    # 外部
    "us_10y":         (0.5,  -1, False, 0.0),   # 美10债升于60日中位=估值压力/北向流出=-
    "usd_cny":        (0.02, -1, False, 0.0),   # USD/CNY升（人民币贬）=资本流出压力=-
}

# 支柱权重（和归一）。流动性与资金面是 A 股最直接的两大驱动 → 权重最高。
PILLAR_WEIGHTS: dict[str, float] = {
    "liquidity": 0.30,
    "capital": 0.30,
    "fundamental": 0.20,
    "external": 0.20,
}

INDICATOR_TO_PILLAR: dict[str, str] = {
    "m1_m2_gap": "liquidity", "shibor_on": "liquidity", "social_fin_yoy": "liquidity",
    "margin_5d_pct": "capital",
    "pmi": "fundamental", "ppi_cpi_gap": "fundamental",
    "us_10y": "external", "usd_cny": "external",
}

# 展示用中文名（UI 四支柱卡片 + as_of 标注用，纯静态注册数据）
INDICATOR_NAMES: dict[str, str] = {
    "m1_m2_gap": "M1-M2 剪刀差",
    "shibor_on": "SHIBOR 隔夜",
    "social_fin_yoy": "社融(滚动12月)同比",
    "margin_5d_pct": "融资余额5日变化",
    "pmi": "制造业 PMI",
    "ppi_cpi_gap": "PPI-CPI 剪刀差",
    "us_10y": "美10债收益率",
    "usd_cny": "USD/CNY 中间价",
}

PILLAR_NAMES: dict[str, str] = {
    "liquidity": "流动性",
    "capital": "资金面",
    "fundamental": "基本面",
    "external": "外部环境",
}

BASELINE = 50.0
BULL_THRESHOLD = 55.0       # score > 55 → 宽松积极
BEAR_THRESHOLD = 45.0       # score < 45 → 偏紧


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


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _label(score: float) -> str:
    if score >= BULL_THRESHOLD:
        return "bullish"
    if score <= BEAR_THRESHOLD:
        return "bearish"
    return "neutral"


def _neutral(reason: str = "no_macro_data") -> dict:
    return {
        "score": BASELINE,
        "label": _label(BASELINE),
        "stance": 0.0,
        "components": {},
        "indicators": {},
        "debug": {"reason": reason, "pillar_count": 0, "indicator_count": 0},
    }


def compute_macro_stance(indicators: Optional[dict]) -> dict:
    """计算四支柱宏观态势分。

    Args:
        indicators: ``{pillar: {key: {"latest": float, "reference": float}}}``，
            由 ``DataManager.get_macro_snapshot`` 抽取干净。``reference`` 仅对
            ``use_neutral=False`` 的指标生效（其余忽略）。

    Returns:
        ``{score[0,100], label, stance[-1,1], components:{pillar:{signal,weight}},
        indicators:{key:{signal,value}}, debug}``

    缺失支柱的权重**重分配**到存活支柱（同 sentiment 的归一化纪律），避免因
    某支柱取数失败丢信号。
    """
    if not indicators:
        return _neutral("no_macro_data")

    # 1. 每指标算 [-1,1] 信号
    ind_signals: dict[str, dict] = {}
    for pillar, kv in indicators.items():
        if not isinstance(kv, dict):
            continue
        for key, vals in kv.items():
            spec = INDICATOR_SPEC.get(key)
            if spec is None or not isinstance(vals, dict):
                continue
            scale, direction, use_neutral, neutral = spec
            latest = _safe_float(vals.get("latest"))
            if latest != latest:  # NaN
                continue
            if use_neutral:
                value = latest - neutral
            else:
                reference = _safe_float(vals.get("reference"))
                if reference != reference:  # NaN
                    continue
                value = latest - reference
            sig = direction * _clamp(value / scale, -1.0, 1.0)
            ind_signals[key] = {"signal": round(sig, 4), "value": round(value, 4)}

    if not ind_signals:
        return _neutral("no_valid_indicators")

    # 2. 支柱信号 = 其指标均值
    pillar_signals: dict[str, list[float]] = {}
    for key in ind_signals:
        pillar = INDICATOR_TO_PILLAR.get(key)
        if pillar:
            pillar_signals.setdefault(pillar, []).append(ind_signals[key]["signal"])

    surviving = {p: sum(sigs) / len(sigs) for p, sigs in pillar_signals.items() if sigs}
    if not surviving:
        return _neutral("no_pillar_data")

    # 3. 缺失支柱权重重分配
    total_w = sum(PILLAR_WEIGHTS.get(p, 0.0) for p in surviving)
    if total_w <= 0:
        return _neutral("zero_pillar_weight")

    stance = 0.0
    components: dict[str, dict] = {}
    for p, sig in surviving.items():
        w = PILLAR_WEIGHTS.get(p, 0.0) / total_w
        stance += sig * w
        components[p] = {"signal": round(sig, 4), "weight": round(w, 4)}

    stance = _clamp(stance, -1.0, 1.0)
    score = _clamp(round(BASELINE + stance * 50.0, 1), 0.0, 100.0)
    return {
        "score": score,
        "label": _label(score),
        "stance": round(stance, 4),
        "components": components,
        "indicators": ind_signals,
        "debug": {"pillar_count": len(surviving), "indicator_count": len(ind_signals)},
    }
