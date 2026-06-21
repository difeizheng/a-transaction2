"""财务 point-in-time 过滤单测。

验证 PIT 纪律：决策日只能用已公告（ann_date <= as_of）的财报，未公告/报告期当天不可用。
"""
import pandas as pd
import pytest

from src.data.pit import filter_point_in_time, latest_as_of, latest_as_of_batch


# Q1 报告期 2024-03-31，4 月底公告 ann_date=2024-04-29
# Q2 报告期 2024-06-30，8 月底公告 ann_date=2024-08-30
DF_WITH_ANN = pd.DataFrame([
    {"code": "000001", "report_date": "2024-03-31", "ann_date": "2024-04-29", "pe_ttm": 8.0, "roe": 12.0},
    {"code": "000001", "report_date": "2024-06-30", "ann_date": "2024-08-30", "pe_ttm": 9.0, "roe": 13.0},
])


class TestFilterPointInTime:
    @pytest.mark.unit
    def test_undisclosed_report_excluded(self):
        # 决策日 2024-05-01：Q1 已公告(4-29<=5-01)保留，Q2 未公告(8-30>5-01)排除
        out = filter_point_in_time(DF_WITH_ANN, "2024-05-01")
        assert len(out) == 1
        assert out.iloc[0]["report_date"] == "2024-03-31"

    @pytest.mark.unit
    def test_both_disclosed_after_aug(self):
        # 决策日 2024-09-01：两份都公告了 → 返回最新 Q2
        out = filter_point_in_time(DF_WITH_ANN, "2024-09-01")
        assert len(out) == 2
        assert out.iloc[0]["report_date"] == "2024-06-30"  # 最新在前

    @pytest.mark.unit
    def test_before_any_disclosure_returns_empty(self):
        # 决策日 2024-04-01：Q1 还没公告 → 空
        out = filter_point_in_time(DF_WITH_ANN, "2024-04-01")
        assert out.empty

    @pytest.mark.unit
    def test_no_lookahead_on_report_date(self):
        # 【关键】报告期当天（3-31）不可用 —— 这是前视偏差防护的核心
        out = filter_point_in_time(DF_WITH_ANN, "2024-03-31")
        assert out.empty


class TestMissingAnnDateFallback:
    @pytest.mark.unit
    def test_lag_fallback_keeps_old_reports(self):
        # 无 ann_date：Q1(3-31)+90d=6-29 <= 决策日 7-01 → 保留；Q2+90d=9-28>7-01 → 排除
        df = pd.DataFrame([
            {"code": "c", "report_date": "2024-03-31", "pe_ttm": 8.0},
            {"code": "c", "report_date": "2024-06-30", "pe_ttm": 9.0},
        ])
        out = filter_point_in_time(df, "2024-07-01")
        assert len(out) == 1
        assert out.iloc[0]["report_date"] == "2024-03-31"

    @pytest.mark.unit
    def test_lag_fallback_still_prevents_lookahead(self):
        # 无 ann_date：报告期当天 +90 天还在未来 → 即使报告期已过也不可用
        df = pd.DataFrame([{"code": "c", "report_date": "2024-03-31", "pe_ttm": 8.0}])
        out = filter_point_in_time(df, "2024-04-15")  # 距 report_date 仅 15 天
        assert out.empty  # 90 天兜底窗口未到


class TestLatestAsOf:
    @pytest.mark.unit
    def test_returns_latest_disclosed_row(self):
        row = latest_as_of(DF_WITH_ANN, "2024-09-01")
        assert row is not None
        assert row["report_date"] == "2024-06-30"

    @pytest.mark.unit
    def test_none_when_nothing_disclosed(self):
        assert latest_as_of(DF_WITH_ANN, "2024-03-31") is None

    @pytest.mark.unit
    def test_batch(self):
        fin = {"000001": DF_WITH_ANN}
        out = latest_as_of_batch(fin, "2024-05-01")
        assert "000001" in out
        assert out["000001"]["report_date"] == "2024-03-31"
        # 决策日太早 → 该 code 不在结果
        out_empty = latest_as_of_batch(fin, "2024-03-31")
        assert "000001" not in out_empty

    @pytest.mark.unit
    def test_empty_df_inputs(self):
        assert filter_point_in_time(pd.DataFrame(), "2024-01-01").empty
        assert latest_as_of(pd.DataFrame(), "2024-01-01") is None

    @pytest.mark.unit
    def test_invalid_as_of_returns_empty(self):
        # 决策日无效 → 保守返回空（不做 PIT 判定）
        assert filter_point_in_time(DF_WITH_ANN, None).empty
