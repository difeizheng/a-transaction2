"""数据源路由层：按数据类型路由到主/备源，自动故障切换"""
import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# 每种 data_type 对应的 fetcher 方法名
_METHOD_MAP = {
    "stock_list":      "get_stock_list",
    "daily_bars":      "get_daily_bars",
    "financial":       "get_financial_indicators",
    "realtime":        "get_realtime_quotes",
    "industry_list":   "get_industry_list",
    "industry_stocks": "get_industry_stocks",
    "announcements":   "get_stock_news",
    "news":            "get_news_em",
}


class SourceRouter:
    """
    按数据类型路由到主/备数据源，自动故障切换。

    切换策略：
    - 主源失败（网络/解析错误）→ 本次任务自动切换到备源
    - NotImplementedError → 该源不支持此类型，直接切备源（不记录失败）
    - 下次任务重新尝试主源（不跨任务持久化切换状态）
    """

    def __init__(self, storage, fetchers: dict):
        """
        storage: Storage 实例（用于记录成功/失败统计）
        fetchers: {"akshare": AKShareFetcher(), "tushare": TushareFetcher(), ...}
        """
        self.storage = storage
        self.fetchers = fetchers
        # 从 SQLite 加载路由配置，转为 {data_type: {primary, backup, enabled}}
        self._routes = {
            r["data_type"]: r
            for r in storage.get_source_routes()
        }
        # 本次任务内的临时切换记录 {data_type: "backup"}
        self._session_overrides: dict = {}

    def call(self, data_type: str, *args, **kwargs) -> pd.DataFrame:
        """
        调用指定 data_type 的数据获取方法。
        args/kwargs 透传给 fetcher 方法。
        """
        route = self._routes.get(data_type)
        if not route or not route.get("enabled", 1):
            raise ValueError(f"数据类型 {data_type!r} 未配置或已禁用")

        method_name = _METHOD_MAP.get(data_type)
        if not method_name:
            raise ValueError(f"未知数据类型: {data_type!r}")

        primary = self._session_overrides.get(data_type) or route["primary_source"]
        backup = route.get("backup_source")

        # 尝试主源（或本次任务已切换的备源）
        result = self._try_source(primary, method_name, data_type, args, kwargs)
        if result is not None:
            return result

        # 主源失败，尝试备源
        if backup and backup != primary and backup in self.fetchers:
            logger.warning(f"[路由] {data_type} 主源 {primary!r} 失败，切换到备源 {backup!r}")
            self._session_overrides[data_type] = backup
            result = self._try_source(backup, method_name, data_type, args, kwargs)
            if result is not None:
                return result

        raise RuntimeError(f"[路由] {data_type} 主备源均失败（主:{primary}, 备:{backup}）")

    def _try_source(self, source_name: str, method_name: str,
                    data_type: str, args: tuple, kwargs: dict):
        """尝试调用指定源，成功返回 DataFrame，失败返回 None"""
        fetcher = self.fetchers.get(source_name)
        if fetcher is None:
            logger.debug(f"[路由] 源 {source_name!r} 未初始化，跳过")
            return None

        method = getattr(fetcher, method_name, None)
        if method is None:
            return None

        try:
            result = method(*args, **kwargs)
            self.storage.record_source_success(source_name, data_type)
            return result
        except NotImplementedError:
            # 该源不支持此类型，静默跳过，不记录失败
            logger.debug(f"[路由] {source_name!r} 不支持 {data_type}，跳过")
            return None
        except Exception as e:
            self.storage.record_source_failure(source_name, data_type, str(e))
            logger.warning(f"[路由] {source_name!r}.{method_name} 失败: {e}")
            return None

    def get_routes(self) -> list:
        """返回所有路由配置（UI 展示用）"""
        return list(self._routes.values())

    def set_route(self, data_type: str, primary: str, backup: str = None):
        """用户手动切换路由，持久化到 SQLite"""
        self.storage.upsert_source_route(data_type, primary, backup)
        if data_type in self._routes:
            self._routes[data_type]["primary_source"] = primary
            self._routes[data_type]["backup_source"] = backup
        # 清除本次任务的临时切换
        self._session_overrides.pop(data_type, None)

    def get_available_sources(self) -> list:
        """返回当前已初始化的数据源名称列表"""
        return list(self.fetchers.keys())

    def reload_routes(self):
        """从 SQLite 重新加载路由配置"""
        self._routes = {
            r["data_type"]: r
            for r in self.storage.get_source_routes()
        }
        self._session_overrides.clear()
