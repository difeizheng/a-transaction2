"""市场情绪温度纯函数单测（``src/analysis/sentiment.py``）。

全部 @pytest.mark.unit —— 纯数字进、纯数字出，无网络 / LLM / DataFrame。
"""
import pytest

from src.analysis.sentiment import (
    compute_market_temperature,
    _label,
    BASELINE,
    BULLISH_THRESHOLD,
    BEARISH_THRESHOLD,
)


# ── 空输入 / 单边退化 ────────────────────────────────────────────
class TestEmptyAndDegradation:
    @pytest.mark.unit
    def test_both_empty_returns_neutral(self):
        r = compute_market_temperature({}, [])
        assert r["temperature"] == BASELINE
        assert r["label"] == "neutral"
        assert r["debug"]["reason"] == "no_market_data"

    @pytest.mark.unit
    def test_none_inputs_returns_neutral(self):
        r = compute_market_temperature(None, None)
        assert r["temperature"] == BASELINE

    @pytest.mark.unit
    def test_only_sectors_drives_score(self):
        # 仅板块、全涨 → 温度 > 50
        r = compute_market_temperature({}, [1.0, 2.0, 3.0])
        assert r["temperature"] > BASELINE
        assert r["debug"]["index_count"] == 0
        assert r["debug"]["sector_count"] == 3

    @pytest.mark.unit
    def test_only_index_drives_score(self):
        # 仅指数 沪深300 +1% → 权重重分配为 1.0，贡献 8 → 58.0
        r = compute_market_temperature({"000300": 1.0}, [])
        assert r["temperature"] == pytest.approx(58.0, abs=0.1)
        assert BASELINE < r["temperature"]  # 信号没丢（权重重分配生效）


# ── 常态 / 普涨 / 普跌 ────────────────────────────────────────────
class TestMarketScenarios:
    @pytest.mark.unit
    def test_normal_day_lands_near_50(self):
        idx = {"000300": 0.5, "000001": 0.3, "399006": 0.3, "000905": 0.3}
        secs = [0.2] * 10  # 中位 +0.2%
        r = compute_market_temperature(idx, secs, sector_advances=55, sector_declines=45)
        assert BASELINE < r["temperature"] < 58.0
        assert r["label"] == "neutral"

    @pytest.mark.unit
    def test_broad_rally_is_bullish(self):
        idx = {"000300": 2.0, "000001": 2.0, "399006": 2.0, "000905": 2.0}
        secs = [2.0] * 10
        r = compute_market_temperature(idx, secs, sector_advances=100, sector_declines=0)
        assert r["temperature"] >= 65.0
        assert r["label"] == "bullish"

    @pytest.mark.unit
    def test_broad_selloff_is_bearish(self):
        idx = {"000300": -2.0, "000001": -2.0, "399006": -2.0, "000905": -2.0}
        secs = [-2.0] * 10
        r = compute_market_temperature(idx, secs, sector_advances=0, sector_declines=100)
        assert r["temperature"] <= 35.0
        assert r["label"] == "bearish"


# ── 输入钳制 / 缺失重分配 / 脏数据 ───────────────────────────────
class TestInputRobustness:
    @pytest.mark.unit
    def test_sector_outlier_clamped(self):
        # +50% 被钳为 +10% → 与直接给 +10% 结果一致（且不超 100）
        a = compute_market_temperature({}, [50.0])
        b = compute_market_temperature({}, [10.0])
        assert a["temperature"] == b["temperature"]
        assert a["temperature"] <= 100.0

    @pytest.mark.unit
    def test_index_outlier_clamped(self):
        # 指数 -50% 视为 -10%
        a = compute_market_temperature({"000300": -50.0}, [])
        b = compute_market_temperature({"000300": -10.0}, [])
        assert a["temperature"] == b["temperature"]

    @pytest.mark.unit
    def test_missing_index_redistributes_weight(self):
        # 只剩沪深300 +1%，权重重分配为 1.0 → 58.0（与 4 指数全 +1% 的指数贡献一致）
        only300 = compute_market_temperature({"000300": 1.0}, [])
        allfour = compute_market_temperature(
            {"000300": 1.0, "000001": 1.0, "399006": 1.0, "000905": 1.0}, []
        )
        # 单指数加权=1.0 vs 四指数加权=1.0 → 指数贡献相同 → 温度相同
        assert only300["temperature"] == pytest.approx(allfour["temperature"], abs=0.1)

    @pytest.mark.unit
    def test_string_nan_input_treated_as_missing(self):
        # 非数字输入被当缺失丢弃 → 行同空
        r = compute_market_temperature({"000300": "abc"}, [])
        assert r["temperature"] == BASELINE
        assert r["debug"]["index_count"] == 0


# ── 标签阈值 ──────────────────────────────────────────────────────
class TestLabelThresholds:
    @pytest.mark.unit
    def test_boundary_just_below_bullish(self):
        assert _label(BULLISH_THRESHOLD - 0.1) == "neutral"

    @pytest.mark.unit
    def test_boundary_at_bullish(self):
        assert _label(BULLISH_THRESHOLD) == "bullish"

    @pytest.mark.unit
    def test_boundary_at_bearish(self):
        assert _label(BEARISH_THRESHOLD) == "bearish"

    @pytest.mark.unit
    def test_boundary_just_below_bearish(self):
        assert _label(BEARISH_THRESHOLD - 0.1) == "bearish"


# ── 确定性 / 广度来源 ─────────────────────────────────────────────
class TestDeterminismAndBreadth:
    @pytest.mark.unit
    def test_deterministic_same_inputs(self):
        idx = {"000300": 0.5, "399006": -0.3}
        secs = [1.0, -0.5, 0.2]
        a = compute_market_temperature(idx, secs)
        b = compute_market_temperature(idx, secs)
        assert a == b

    @pytest.mark.unit
    def test_advance_decline_overrides_sign_breadth(self):
        # 板块涨跌参半（符号广度=0.5 → 广度贡献 0），但显式 8:2 家数 → 广度强正
        secs = [1.0, -1.0]
        sign_based = compute_market_temperature({}, secs)
        counted = compute_market_temperature({}, secs, sector_advances=8, sector_declines=2)
        assert counted["temperature"] > sign_based["temperature"]
        # 中位=0 → 仅广度贡献：(0.8-0.5)*30 = 9 → 59.0
        assert counted["temperature"] == pytest.approx(59.0, abs=0.1)
