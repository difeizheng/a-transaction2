"""SourceRouter 单测 —— 数据源路由与故障切换。

覆盖：主源成功/失败回退/NotImplementedError 静默跳过/异常记录失败/
session 内切换/禁用路由报错/未知 data_type 报错。
"""
import pytest
import pandas as pd
from unittest.mock import MagicMock

from src.data.source_router import SourceRouter


# ── Fixtures ────────────────────────────────────────────────────
@pytest.fixture
def mock_storage():
    """Mock Storage，返回默认路由配置。"""
    storage = MagicMock()
    storage.get_source_routes.return_value = [
        {
            "data_type": "daily_bars",
            "primary_source": "tushare",
            "backup_source": "akshare",
            "enabled": 1,
        },
        {
            "data_type": "realtime",
            "primary_source": "tencent",
            "backup_source": "akshare",
            "enabled": 1,
        },
        {
            "data_type": "disabled_type",
            "primary_source": "tushare",
            "backup_source": None,
            "enabled": 0,  # 禁用
        },
    ]
    return storage


@pytest.fixture
def success_fetcher():
    """总是返回固定 DataFrame 的 fetcher。"""
    fetcher = MagicMock()
    fetcher.get_daily_bars.return_value = pd.DataFrame({"code": ["000001"]})
    fetcher.get_realtime_quotes.return_value = pd.DataFrame({"code": ["000001"]})
    return fetcher


@pytest.fixture
def failing_fetcher():
    """总是抛异常的 fetcher。"""
    fetcher = MagicMock()
    fetcher.get_daily_bars.side_effect = RuntimeError("网络超时")
    fetcher.get_realtime_quotes.side_effect = RuntimeError("网络超时")
    return fetcher


@pytest.fixture
def notimpl_fetcher():
    """总是抛 NotImplementedError 的 fetcher（不支持该数据类型）。"""
    fetcher = MagicMock()
    fetcher.get_daily_bars.side_effect = NotImplementedError("不支持")
    return fetcher


# ── 主源成功 ────────────────────────────────────────────────────
class TestPrimarySuccess:
    @pytest.mark.unit
    def test_primary_succeeds_returns_result(self, mock_storage, success_fetcher):
        router = SourceRouter(mock_storage, {"tushare": success_fetcher})
        result = router.call("daily_bars", "000001", "2024-01-01", "2024-01-31")

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 1
        success_fetcher.get_daily_bars.assert_called_once()
        mock_storage.record_source_success.assert_called_once_with("tushare", "daily_bars")

    @pytest.mark.unit
    def test_backup_not_called_when_primary_succeeds(self, mock_storage, success_fetcher):
        backup = MagicMock()
        router = SourceRouter(mock_storage, {"tushare": success_fetcher, "akshare": backup})
        router.call("daily_bars", "000001")

        backup.get_daily_bars.assert_not_called()


# ── 主源失败 → 回退备源 ────────────────────────────────────────
class TestFailover:
    @pytest.mark.unit
    def test_primary_fails_falls_back_to_backup(
        self, mock_storage, failing_fetcher, success_fetcher
    ):
        router = SourceRouter(mock_storage, {
            "tushare": failing_fetcher,
            "akshare": success_fetcher,
        })
        result = router.call("daily_bars", "000001")

        assert isinstance(result, pd.DataFrame)
        failing_fetcher.get_daily_bars.assert_called_once()
        success_fetcher.get_daily_bars.assert_called_once()
        mock_storage.record_source_failure.assert_called_once()

    @pytest.mark.unit
    def test_session_override_persists_within_task(
        self, mock_storage, failing_fetcher, success_fetcher
    ):
        # 第一次调用：主源失败 → 切换到备源
        router = SourceRouter(mock_storage, {
            "tushare": failing_fetcher,
            "akshare": success_fetcher,
        })
        router.call("daily_bars", "000001")

        # 第二次调用：应直接使用备源（session 内切换）
        success_fetcher.get_daily_bars.reset_mock()
        router.call("daily_bars", "000002")

        # 主源不应再被调用（session 已切换到备源）
        failing_fetcher.get_daily_bars.assert_called_once()  # 仅第一次
        success_fetcher.get_daily_bars.assert_called_once()  # 第二次

    @pytest.mark.unit
    def test_both_fail_raises_runtime_error(self, mock_storage, failing_fetcher):
        backup = MagicMock()
        backup.get_daily_bars.side_effect = RuntimeError("备源也失败")
        router = SourceRouter(mock_storage, {
            "tushare": failing_fetcher,
            "akshare": backup,
        })

        with pytest.raises(RuntimeError, match="主备源均失败"):
            router.call("daily_bars", "000001")


# ── NotImplementedError 静默跳过 ───────────────────────────────
class TestNotImplemented:
    @pytest.mark.unit
    def test_not_implemented_silently_skips_no_failure_recorded(
        self, mock_storage, notimpl_fetcher, success_fetcher
    ):
        # 主源不支持 → 静默切备源，不记录失败
        router = SourceRouter(mock_storage, {
            "tushare": notimpl_fetcher,
            "akshare": success_fetcher,
        })
        result = router.call("daily_bars", "000001")

        assert isinstance(result, pd.DataFrame)
        mock_storage.record_source_failure.assert_not_called()  # 不记录失败
        mock_storage.record_source_success.assert_called_once_with("akshare", "daily_bars")


# ── 路由配置错误 ────────────────────────────────────────────────
class TestRouteErrors:
    @pytest.mark.unit
    def test_disabled_route_raises_value_error(self, mock_storage):
        router = SourceRouter(mock_storage, {})
        with pytest.raises(ValueError, match="未配置或已禁用"):
            router.call("disabled_type")

    @pytest.mark.unit
    def test_unknown_data_type_raises_value_error(self, mock_storage):
        router = SourceRouter(mock_storage, {})
        with pytest.raises(ValueError):
            router.call("nonexistent_type")


# ── 手动切换路由 ────────────────────────────────────────────────
class TestManualSwitch:
    @pytest.mark.unit
    def test_set_route_updates_config(self, mock_storage, success_fetcher):
        router = SourceRouter(mock_storage, {"tushare": success_fetcher})
        router.set_route("daily_bars", "akshare", "tushare")

        mock_storage.upsert_source_route.assert_called_once_with(
            "daily_bars", "akshare", "tushare"
        )
        assert router._routes["daily_bars"]["primary_source"] == "akshare"
        assert router._routes["daily_bars"]["backup_source"] == "tushare"

    @pytest.mark.unit
    def test_set_route_clears_session_override(
        self, mock_storage, failing_fetcher, success_fetcher
    ):
        # 先触发 session 切换
        router = SourceRouter(mock_storage, {
            "tushare": failing_fetcher,
            "akshare": success_fetcher,
        })
        router.call("daily_bars", "000001")
        assert "daily_bars" in router._session_overrides

        # 手动切换路由 → 清除 session override
        router.set_route("daily_bars", "tushare", "akshare")
        assert "daily_bars" not in router._session_overrides


# ── 辅助方法 ────────────────────────────────────────────────────
class TestHelpers:
    @pytest.mark.unit
    def test_get_routes_returns_list(self, mock_storage):
        router = SourceRouter(mock_storage, {})
        routes = router.get_routes()
        assert len(routes) == 3

    @pytest.mark.unit
    def test_get_available_sources_returns_keys(self, mock_storage):
        router = SourceRouter(mock_storage, {"tushare": MagicMock(), "akshare": MagicMock()})
        sources = router.get_available_sources()
        assert set(sources) == {"tushare", "akshare"}

    @pytest.mark.unit
    def test_reload_routes_clears_session_overrides(
        self, mock_storage, failing_fetcher, success_fetcher
    ):
        router = SourceRouter(mock_storage, {
            "tushare": failing_fetcher,
            "akshare": success_fetcher,
        })
        router.call("daily_bars", "000001")
        assert "daily_bars" in router._session_overrides

        router.reload_routes()
        assert len(router._session_overrides) == 0
