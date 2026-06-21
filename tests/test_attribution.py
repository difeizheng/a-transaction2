"""绩效归因单测 —— Brinson-Fachler 超额收益分解。

用经典 2 行业（权益/债券）已知数值例验证：
- 三大效应（配置/选股/交互）数值正确；
- 配置+选股+交互 == 组合超额收益（Brinson 恒等式）；
- 个股→行业聚合的权重加权平均口径正确。
"""
import pytest

from src.analysis.attribution import (
    aggregate_to_sectors,
    attribution_from_stocks,
    brinson_attribution,
    to_dataframe,
)


# 经典例：2 行业，已知各效应数值（手算见模块 docstring）
PORT_W = {"equity": 0.6, "bonds": 0.4}
PORT_R = {"equity": 0.12, "bonds": 0.04}
BENCH_W = {"equity": 0.5, "bonds": 0.5}
BENCH_R = {"equity": 0.10, "bonds": 0.05}
# 组合收益 = 0.6*0.12 + 0.4*0.04 = 0.088；基准 = 0.5*0.10 + 0.5*0.05 = 0.075
# active = 0.013；allocation = 0.005；selection = 0.005；interaction = 0.003


class TestBrinson:
    @pytest.mark.unit
    def test_total_returns(self):
        r = brinson_attribution(PORT_W, PORT_R, BENCH_W, BENCH_R)
        assert r["portfolio_return"] == pytest.approx(0.088)
        assert r["benchmark_return"] == pytest.approx(0.075)
        assert r["active_return"] == pytest.approx(0.013)

    @pytest.mark.unit
    def test_three_effects_match_known_values(self):
        r = brinson_attribution(PORT_W, PORT_R, BENCH_W, BENCH_R)
        assert r["allocation"] == pytest.approx(0.005, abs=1e-6)
        assert r["selection"] == pytest.approx(0.005, abs=1e-6)
        assert r["interaction"] == pytest.approx(0.003, abs=1e-6)

    @pytest.mark.unit
    def test_brinson_identity_holds(self):
        # 配置+选股+交互 必须等于超额收益（核心正确性校验）
        r = brinson_attribution(PORT_W, PORT_R, BENCH_W, BENCH_R)
        assert r["allocation"] + r["selection"] + r["interaction"] == pytest.approx(
            r["active_return"], abs=1e-6
        )

    @pytest.mark.unit
    def test_overweight_good_sector_positive_allocation(self):
        # 超配跑赢基准的好行业 → 配置效应为正
        port_w = {"good": 0.8, "bad": 0.2}
        bench_w = {"good": 0.5, "bad": 0.5}
        ret = {"good": 0.10, "bad": 0.02}
        r = brinson_attribution(port_w, ret, bench_w, ret)
        assert r["allocation"] > 0

    @pytest.mark.unit
    def test_outperformance_positive_selection(self):
        # 组合在各行业都跑赢基准（同权重）→ 选股效应为正、配置为 0
        ret_p = {"a": 0.10, "b": 0.08}
        ret_b = {"a": 0.05, "b": 0.03}
        w = {"a": 0.5, "b": 0.5}
        r = brinson_attribution(w, ret_p, w, ret_b)
        assert r["allocation"] == pytest.approx(0.0, abs=1e-9)
        assert r["selection"] > 0

    @pytest.mark.unit
    def test_by_sector_rows(self):
        r = brinson_attribution(PORT_W, PORT_R, BENCH_W, BENCH_R)
        assert len(r["by_sector"]) == 2
        sectors = {row["sector"] for row in r["by_sector"]}
        assert sectors == {"equity", "bonds"}
        # 每行 allocation+selection+interaction == 该行 active
        for row in r["by_sector"]:
            assert row["allocation"] + row["selection"] + row["interaction"] == pytest.approx(
                row["active"], abs=1e-6
            )

    @pytest.mark.unit
    def test_identical_portfolio_zero_active(self):
        # 组合 == 基准 → 所有效应为 0
        r = brinson_attribution(BENCH_W, BENCH_R, BENCH_W, BENCH_R)
        assert r["active_return"] == pytest.approx(0.0)
        assert r["allocation"] == pytest.approx(0.0)
        assert r["selection"] == pytest.approx(0.0)
        assert r["interaction"] == pytest.approx(0.0)


class TestAggregate:
    @pytest.mark.unit
    def test_weight_weighted_average(self):
        # 行业内按个股权重加权平均（不是简单平均）
        sw = {"a1": 0.6, "a2": 0.4}      # 行业 X 占 1.0
        sr = {"a1": 0.10, "a2": 0.20}
        sector = {"a1": "X", "a2": "X"}
        sw_out, sr_out = aggregate_to_sectors(sw, sr, sector)
        assert sw_out["X"] == pytest.approx(1.0)
        assert sr_out["X"] == pytest.approx(0.6 * 0.10 + 0.4 * 0.20)  # 0.14

    @pytest.mark.unit
    def test_multiple_sectors(self):
        sw = {"a1": 0.5, "b1": 0.5}
        sr = {"a1": 0.10, "b1": 0.04}
        sector = {"a1": "X", "b1": "Y"}
        sw_out, sr_out = aggregate_to_sectors(sw, sr, sector)
        assert set(sw_out) == {"X", "Y"}
        assert sr_out["X"] == pytest.approx(0.10)
        assert sr_out["Y"] == pytest.approx(0.04)


class TestEndToEndAndRender:
    @pytest.mark.unit
    def test_attribution_from_stocks(self):
        # 同行业内：组合超配低收益股 a1(0.10)、低配高收益股 a2(0.20) → 组合跑输。
        # 组合行业收益 = 0.6*0.10+0.4*0.20 = 0.14；基准 = 0.5*0.10+0.5*0.20 = 0.15
        # → active = -0.01（验证聚合+归因端到端路径正确）。
        port_w = {"a1": 0.6, "a2": 0.4}
        port_r = {"a1": 0.10, "a2": 0.20}
        bench_w = {"a1": 0.5, "a2": 0.5}
        bench_r = {"a1": 0.10, "a2": 0.20}
        sector = {"a1": "X", "a2": "X"}
        r = attribution_from_stocks(port_w, port_r, bench_w, bench_r, sector)
        assert r["portfolio_return"] == pytest.approx(0.14)
        assert r["benchmark_return"] == pytest.approx(0.15)
        assert r["active_return"] == pytest.approx(-0.01)

    @pytest.mark.unit
    def test_to_dataframe(self):
        r = brinson_attribution(PORT_W, PORT_R, BENCH_W, BENCH_R)
        df = to_dataframe(r)
        assert list(df.columns) == ["sector", "w_p", "w_b", "r_p", "r_b",
                                    "allocation", "selection", "interaction", "active"]
        assert len(df) == 2
