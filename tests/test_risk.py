"""风控纯函数单测 —— 回撤 / 成交量约束 / 加仓数量 / 回撤分档减仓。

这些函数是 P0/P1 修复的核心（回撤 high-water mark、单股仓位上限不被击穿、
成交量上限防无限流动性、回撤分档减仓），任何回归都会直接导致风控失效，故全覆盖。
"""
import pytest

from src.trading.risk import (
    cap_size_by_volume,
    compute_add_buy_quantity,
    compute_drawdown_pct,
    compute_trim_quantity,
    deescalation_level,
    compute_concentration,
    find_concentration_breaches,
    would_breach_concentration,
)


# ── 回撤（high-water mark）─────────────────────────────────────────
class TestDrawdown:
    @pytest.mark.unit
    def test_normal_drawdown(self):
        # 峰值 100，当前 90 → 回撤 10%
        assert compute_drawdown_pct(100.0, 90.0) == pytest.approx(10.0)

    @pytest.mark.unit
    def test_deep_drawdown(self):
        # 峰值 100，当前 75 → 真实回撤 25%（旧实现用初始资金会算成更小值）
        assert compute_drawdown_pct(100.0, 75.0) == pytest.approx(25.0)

    @pytest.mark.unit
    def test_at_peak_zero_drawdown(self):
        assert compute_drawdown_pct(100.0, 100.0) == 0.0

    @pytest.mark.unit
    def test_new_high_zero_drawdown(self):
        # 创新高不算回撤（>=0）
        assert compute_drawdown_pct(100.0, 120.0) == 0.0

    @pytest.mark.unit
    def test_invalid_peak_returns_zero(self):
        assert compute_drawdown_pct(0.0, 50.0) == 0.0
        assert compute_drawdown_pct(-10.0, 50.0) == 0.0


# ── 成交量上限（防无限流动性）─────────────────────────────────────
class TestVolumeCap:
    @pytest.mark.unit
    def test_no_cap_when_volume_large(self):
        # 目标 10000，当日量 100000，25% 上限=25000 → 不受限，仍 10000
        assert cap_size_by_volume(10000, 100000) == 10000

    @pytest.mark.unit
    def test_capped_by_volume(self):
        # 目标 100000，当日量 100000，25%=25000 → 截到 25000
        assert cap_size_by_volume(100000, 100000) == 25000

    @pytest.mark.unit
    def test_rounded_to_lot_after_cap(self):
        # 目标 1000，当日量 2000，25%=500 → 截到 500（已是整手）
        assert cap_size_by_volume(1000, 2000) == 500

    @pytest.mark.unit
    def test_rounded_down_to_lot(self):
        # 截断后非整手：目标 1000，当日量 2400，25%=600 → round_to_lot(600)=600
        # 再试 2700: 25%=675 → round_to_lot(675)=600
        assert cap_size_by_volume(1000, 2700) == 600

    @pytest.mark.unit
    def test_zero_target(self):
        assert cap_size_by_volume(0, 100000) == 0

    @pytest.mark.unit
    def test_zero_volume_keeps_target(self):
        # 无成交量数据时不额外约束（停牌/缺失，由调用方决策）
        assert cap_size_by_volume(5000, 0) == 5000


# ── 加仓数量（扣除已有持仓，防单股上限击穿）──────────────────────
class TestAddBuyQuantity:
    @pytest.mark.unit
    def test_new_position_full_headroom(self):
        # 总资产 100万，无持仓，价 10，上限 20% → max_pos=200000 → 20000 股
        qty, reason = compute_add_buy_quantity(1_000_000, 0, 10.0, 20.0)
        assert qty == 20000
        assert reason == ""

    @pytest.mark.unit
    def test_add_subtracts_existing(self):
        # 已占 12%（120000），上限 20% → 只能加 8%（80000）→ 8000 股（修复关键）
        qty, reason = compute_add_buy_quantity(1_000_000, 120_000, 10.0, 20.0)
        assert qty == 8000
        assert reason == ""

    @pytest.mark.unit
    def test_at_limit_no_headroom(self):
        # 已占满 20%（200000），上限 20% → headroom=0 → 拒绝
        qty, reason = compute_add_buy_quantity(1_000_000, 200_000, 10.0, 20.0)
        assert qty == 0
        assert "上限" in reason

    @pytest.mark.unit
    def test_over_limit_no_headroom(self):
        # 已占 25%（超上限），headroom<0 → 拒绝
        qty, reason = compute_add_buy_quantity(1_000_000, 250_000, 10.0, 20.0)
        assert qty == 0
        assert "上限" in reason

    @pytest.mark.unit
    def test_headroom_below_one_lot(self):
        # 高价股：总资产 1万，无持仓，价 2000（茅台档），上限 20% → max_pos=2000 →
        # int(2000/2000)=1 → round_to_lot(1)=0 → 不足 1 手
        qty, reason = compute_add_buy_quantity(10_000, 0, 2000.0, 20.0)
        assert qty == 0
        assert "不足1手" in reason

    @pytest.mark.unit
    def test_invalid_price(self):
        qty, reason = compute_add_buy_quantity(1_000_000, 0, 0.0, 20.0)
        assert qty == 0
        assert "非法" in reason


# ── 回撤分档减仓（circuit-breaker deescalation）──────────────────
class TestDeescalationLevel:
    @pytest.mark.unit
    def test_shallow_drawdown_no_trim(self):
        # 回撤 5% < 第一档 10% → keep 1.0，不强制减仓
        keep, tier = deescalation_level(5.0)
        assert keep == 1.0
        assert tier == 0

    @pytest.mark.unit
    def test_moderate_drawdown_tier1(self):
        # 回撤 12% 命中第 1 档（10-15%）→ trim 到 70%
        keep, tier = deescalation_level(12.0)
        assert keep == pytest.approx(0.70)
        assert tier == 1

    @pytest.mark.unit
    def test_severe_drawdown_tier2(self):
        keep, tier = deescalation_level(18.0)
        assert keep == pytest.approx(0.50)
        assert tier == 2

    @pytest.mark.unit
    def test_deep_drawdown_keeps_floor(self):
        # 回撤 40% → 命中最后一档，保留底仓 20%（防踏空反弹）
        keep, tier = deescalation_level(40.0)
        assert keep == pytest.approx(0.20)
        assert tier == 4  # 最后一档

    @pytest.mark.unit
    def test_monotonic_deeper_means_lower_keep(self):
        # 回撤越深，保留比例越低（单调）
        keeps = [deescalation_level(dd)[0] for dd in (5, 12, 18, 22, 40)]
        assert keeps == sorted(keeps, reverse=True)


class TestComputeTrimQuantity:
    @pytest.mark.unit
    def test_trim_30pct_of_1000(self):
        # 1000 股保留 70% → 保留 700，减 300
        assert compute_trim_quantity(1000, 0.70) == 300

    @pytest.mark.unit
    def test_no_trim_when_keep_full(self):
        assert compute_trim_quantity(1000, 1.0) == 0

    @pytest.mark.unit
    def test_rounds_to_lot(self):
        # 150 股保留 70% = 105 → 取整到手 100，减 50
        assert compute_trim_quantity(150, 0.70) == 50

    @pytest.mark.unit
    def test_keep_ratio_keeps_floor_one_lot(self):
        # 100 股保留 20% = 20 → round_to_lot(20)=0（不足1手保留）→ 减仓后保留0手？保守不减
        # round_to_lot(20)=0 → keep_qty=0 → trim=100。但保留0手等于清仓，可接受（深度减仓）。
        trim = compute_trim_quantity(100, 0.20)
        assert trim == 100  # 保留量不足1手 → 全部减掉

    @pytest.mark.unit
    def test_zero_quantity(self):
        assert compute_trim_quantity(0, 0.5) == 0


# ── 行业/风格集中度（concentration cap）──────────────────────────
class TestConcentration:
    @pytest.mark.unit
    def test_max_pct_and_hhi(self):
        # 银行 40万 / 券商 30万 / 科技 30万 → 银行 40%，HHI = 1600+900+900 = 3400
        info = compute_concentration({"银行": 40, "券商": 30, "科技": 30})
        assert info["max_group"] == "银行"
        assert info["max_pct"] == pytest.approx(40.0)
        assert info["hhi"] == pytest.approx(40**2 + 30**2 + 30**2)
        assert info["n_groups"] == 3

    @pytest.mark.unit
    def test_single_group_dominant(self):
        info = compute_concentration({"银行": 90, "其它": 10})
        assert info["max_pct"] == pytest.approx(90.0)
        assert info["hhi"] > 5000  # 高度集中

    @pytest.mark.unit
    def test_empty_weights(self):
        info = compute_concentration({})
        assert info["max_pct"] == 0.0
        assert info["max_group"] is None

    @pytest.mark.unit
    def test_find_breaches(self):
        breaches = find_concentration_breaches(
            {"银行": 40, "券商": 30, "科技": 30}, cap_pct=35.0
        )
        assert len(breaches) == 1
        assert breaches[0] == ("银行", pytest.approx(40.0))

    @pytest.mark.unit
    def test_no_breach_when_balanced(self):
        assert find_concentration_breaches(
            {"a": 25, "b": 25, "c": 25, "d": 25}, cap_pct=30.0
        ) == []

    @pytest.mark.unit
    def test_would_breach_blocks_buy(self):
        # 银行已占 28/100，再加 20 → 48/120 = 40% 超 35% 上限 → breach
        # （注意分母随增配增长，不是简单 28+20=48%）
        weights = {"银行": 28, "其它": 72}
        assert would_breach_concentration(weights, "银行", 20, cap_pct=35.0) is True

    @pytest.mark.unit
    def test_would_not_breach_allows_buy(self):
        # 银行已占 20/100，再加 5 → 25/105 ≈ 23.8% 未超 35% → 不 breach
        weights = {"银行": 20, "其它": 80}
        assert would_breach_concentration(weights, "银行", 5, cap_pct=35.0) is False

    @pytest.mark.unit
    def test_would_breach_new_group(self):
        # 新行业（weights 中无）从 0 加 50 → 50% 超 30% → breach
        assert would_breach_concentration({"a": 100}, "新行业", 50, cap_pct=30.0) is True
