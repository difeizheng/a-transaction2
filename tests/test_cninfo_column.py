"""巨潮公告 column 映射单测 —— 沪/京市公告可查性。

旧实现硬编码 "szse"，导致沪市(6xx)/北交所(4/8)查不到任何公告。
_column_for 按代码前缀映射到正确交易所板块。
"""
import pytest

from src.data.cninfo_fetcher import CninfoFetcher


class TestColumnFor:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "code,expected",
        [
            ("600519", "sse"),   # 贵州茅台 沪市
            ("688981", "sse"),   # 中芯国际 科创板（沪）
            ("000001", "szse"),  # 平安银行 深市主板
            ("300750", "szse"),  # 宁德时代 创业板
            ("002594", "szse"),  # 比亚迪 深市中小板
            ("830799", "bj"),    # 北交所 8xx
            ("430047", "bj"),    # 北交所 4xx
            # 注：920xxx(新北交所)/900xxx(沪B) 在 cninfo column 的归属无法
            # 离线核实，不纳入断言，避免臆测。
        ],
    )
    def test_maps_by_prefix(self, code, expected):
        assert CninfoFetcher._column_for(code) == expected

    @pytest.mark.unit
    def test_shanghai_never_returns_szse(self):
        # 回归核心 bug：6 开头绝不能再落到 szse
        for code in ("600000", "601318", "688001", "699999"):
            assert CninfoFetcher._column_for(code) == "sse"

    @pytest.mark.unit
    def test_staticmethod_callable_without_instance(self):
        # 确认无需实例化（无网络）即可调用
        assert CninfoFetcher._column_for("600519") == "sse"
