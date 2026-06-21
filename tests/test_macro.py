"""compute_macro_stance 纯函数单测。

@pytest.mark.unit —— 纯逻辑，无 DB / 网络 / LLM。镜像 test_sentiment.py 范式。
"""
import pytest

from src.analysis.macro import compute_macro_stance

pytestmark = pytest.mark.unit


def _full(score_dir: float = 1.0) -> dict:
    """构造四支柱全指标输入。score_dir=1 全宽松，-1 全偏紧。

    各 latest/reference 设成让每指标信号 ≈ +1（宽松）或 -1（偏紧）。
    """
    s = score_dir
    return {
        "liquidity": {
            # m1_m2_gap: use_neutral, neutral 0 → latest=+5/-5 → value ±5, scale 5 → ±1
            "m1_m2_gap": {"latest": 5.0 * s, "reference": 0.0},
            # shibor_on: use_neutral=False → value=latest-ref。宽松=利率降 → latest<ref
            "shibor_on": {"latest": 1.5, "reference": 1.5 - 0.5 * s},
            "social_fin_yoy": {"latest": 15.0, "reference": 15.0 - 5.0 * s},
        },
        "capital": {
            "margin_5d_pct": {"latest": 0.01 * s, "reference": 0.0},
        },
        "fundamental": {
            "pmi": {"latest": 52.0 if s > 0 else 48.0, "reference": 0.0},   # ±2 vs 50
            "ppi_cpi_gap": {"latest": 3.0 * s, "reference": 0.0},
        },
        "external": {
            # us_10y: direction -1 → 宽松（利率降）latest<ref 给 +
            "us_10y": {"latest": 3.5, "reference": 3.5 + 0.5 * s},
            # usd_cny: direction -1 → 人民币升值（USD/CNY 跌）给 +
            "usd_cny": {"latest": -0.02 * s, "reference": 0.0},
        },
    }


class TestEmptyAndDegradation:
    def test_empty_returns_neutral_50(self):
        r = compute_macro_stance({})
        assert r["score"] == 50.0
        assert r["label"] == "neutral"
        assert r["stance"] == 0.0

    def test_none_returns_neutral(self):
        r = compute_macro_stance(None)
        assert r["score"] == 50.0

    def test_all_invalid_indicators_returns_neutral(self):
        # 全部 latest 是垃圾字符串 → 无有效指标 → 中性
        ind = {"liquidity": {"m1_m2_gap": {"latest": "abc", "reference": 0.0}}}
        r = compute_macro_stance(ind)
        assert r["score"] == 50.0
        assert r["debug"]["reason"] == "no_valid_indicators"


class TestMarketScenarios:
    def test_normal_all_at_neutral_is_around_50(self):
        # 各指标恰好在中性 → 全 signal 0 → score 50
        ind = {
            "liquidity": {
                "m1_m2_gap": {"latest": 0.0, "reference": 0.0},
                "shibor_on": {"latest": 1.5, "reference": 1.5},
                "social_fin_yoy": {"latest": 8.0, "reference": 8.0},
            },
            "capital": {
                "northbound_5d": {"latest": 0.0, "reference": 0.0},
                "margin_5d_pct": {"latest": 0.0, "reference": 0.0},
            },
            "fundamental": {
                "pmi": {"latest": 50.0, "reference": 0.0},
                "ppi_cpi_gap": {"latest": 0.0, "reference": 0.0},
            },
            "external": {
                "us_10y": {"latest": 4.0, "reference": 4.0},
                "usd_cny": {"latest": 0.0, "reference": 0.0},
            },
        }
        r = compute_macro_stance(ind)
        assert 48.0 <= r["score"] <= 52.0
        assert r["label"] == "neutral"

    def test_full_easing_is_bullish(self):
        r = compute_macro_stance(_full(1.0))
        assert r["score"] >= 65.0
        assert r["label"] == "bullish"

    def test_full_tightening_is_bearish(self):
        r = compute_macro_stance(_full(-1.0))
        assert r["score"] <= 35.0
        assert r["label"] == "bearish"

    def test_missing_pillar_weight_redistribution(self):
        # 只给外部支柱 → 权重全归外部，仍 sane
        ind = {"external": {
            "us_10y": {"latest": 3.5, "reference": 4.0},   # 利率降 → +
            "usd_cny": {"latest": -0.02, "reference": 0.0},  # 升值 → +
        }}
        r = compute_macro_stance(ind)
        assert r["score"] > 50.0
        # 外部权重应重分配到 1.0
        assert r["components"]["external"]["weight"] == pytest.approx(1.0)
        assert r["debug"]["pillar_count"] == 1


class TestInputRobustness:
    def test_m1m2_extreme_clamped(self):
        # +30% 远超 scale 5 → 钳到 +1
        ind = {"liquidity": {"m1_m2_gap": {"latest": 30.0, "reference": 0.0}}}
        r = compute_macro_stance(ind)
        assert r["indicators"]["m1_m2_gap"]["signal"] == pytest.approx(1.0)

    def test_garbage_string_dropped(self):
        ind = {"fundamental": {
            "pmi": {"latest": "abc", "reference": 50.0},       # 无效 → 丢
            "ppi_cpi_gap": {"latest": 0.0, "reference": 0.0},  # 有效
        }}
        r = compute_macro_stance(ind)
        assert "pmi" not in r["indicators"]
        assert "ppi_cpi_gap" in r["indicators"]


class TestIndicatorSemantics:
    def test_shibor_uses_reference_not_neutral(self):
        # SHIBOR use_neutral=False：value = latest - reference
        # latest=2.0 高于 ref=1.5 → value +0.5，direction -1 → signal -1（升=偏空）
        ind = {"liquidity": {"shibor_on": {"latest": 2.0, "reference": 1.5}}}
        r = compute_macro_stance(ind)
        assert r["indicators"]["shibor_on"]["signal"] < 0

    def test_pmi_uses_neutral_50_ignores_reference(self):
        # PMI use_neutral=True：value = latest - 50，reference 被忽略
        ind = {"fundamental": {"pmi": {"latest": 52.0, "reference": 999.0}}}
        r = compute_macro_stance(ind)
        assert r["indicators"]["pmi"]["signal"] == pytest.approx(1.0)

    def test_margin_uses_neutral_zero(self):
        ind = {"capital": {"margin_5d_pct": {"latest": 0.01, "reference": 999.0}}}
        r = compute_macro_stance(ind)
        assert r["indicators"]["margin_5d_pct"]["signal"] == pytest.approx(1.0)

    def test_us10y_rising_is_bearish(self):
        # 美10债升于中位 → direction -1 → 偏空
        ind = {"external": {"us_10y": {"latest": 4.5, "reference": 4.0}}}
        r = compute_macro_stance(ind)
        assert r["indicators"]["us_10y"]["signal"] < 0

    def test_usd_cny_rising_is_bearish(self):
        # USD/CNY 升（人民币贬）→ direction -1 → 偏空
        ind = {"external": {"usd_cny": {"latest": 0.02, "reference": 0.0}}}
        r = compute_macro_stance(ind)
        assert r["indicators"]["usd_cny"]["signal"] < 0


class TestLabelThresholds:
    def test_just_above_bull_is_bullish(self):
        # 构造 score 略高于 55。单支柱 ppi_cpi_gap +0.9 → 重分配后 stance≈0.9
        ind = {"fundamental": {"ppi_cpi_gap": {"latest": 2.7, "reference": 0.0}}}
        r = compute_macro_stance(ind)
        # value 2.7, scale 3 → signal 0.9；单支柱重分配 weight 1.0 → stance 0.9 → score 95
        assert r["score"] > 55.0
        assert r["label"] == "bullish"


class TestDeterminism:
    def test_same_input_same_output(self):
        ind = _full(1.0)
        assert compute_macro_stance(ind) == compute_macro_stance(ind)

    def test_score_in_valid_range(self):
        for direction in (1.0, -1.0, 0.0):
            r = compute_macro_stance(_full(direction))
            assert 0.0 <= r["score"] <= 100.0
