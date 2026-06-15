"""数据管理器：协调 fetcher 和 storage，处理增量更新"""
import logging
import time
from datetime import date, datetime, timedelta
from typing import List, Optional, Callable
import pandas as pd

from src.data.fetcher import AKShareFetcher
from src.data.storage import Storage
from src.data.source_router import SourceRouter
from src.config import get_config

logger = logging.getLogger(__name__)


def _latest_possible_trading_day(d: date = None) -> date:
    """市场可能的最近交易日：周末（周六5/周日6）回退到周五。

    用于增量更新的 end_date 上界，避免 `latest(周五) < today(周六)` 恒真，
    导致周末对全市场发起无效重复拉取（消耗积分/触发限频）。
    节假日仍会触发一次空拉取（数据源返回空，无副作用）。
    """
    d = d or date.today()
    while d.weekday() >= 5:  # 5=周六, 6=周日
        d -= timedelta(days=1)
    return d


class DataManager:
    def __init__(self):
        cfg = get_config()
        self.storage = Storage(cfg["database"]["path"])
        self.cache_days = cfg["data"]["cache_days"]
        self.history_years = cfg["data"]["history_years"]

        # 初始化所有可用的 fetcher
        fetchers = {"akshare": AKShareFetcher()}
        ds_cfg = cfg.get("data_sources", {})

        if ds_cfg.get("tushare", {}).get("enabled") and ds_cfg["tushare"].get("token"):
            try:
                from src.data.tushare_fetcher import TushareFetcher
                fetchers["tushare"] = TushareFetcher(ds_cfg["tushare"]["token"])
                logger.info("Tushare 数据源已启用")
            except Exception as e:
                logger.warning(f"Tushare 初始化失败: {e}")

        if ds_cfg.get("tencent", {}).get("enabled", True):
            try:
                from src.data.tencent_fetcher import TencentFetcher
                fetchers["tencent"] = TencentFetcher()
                logger.info("腾讯财经数据源已启用")
            except Exception as e:
                logger.warning(f"腾讯财经初始化失败: {e}")

        if ds_cfg.get("cninfo", {}).get("enabled", True):
            try:
                from src.data.cninfo_fetcher import CninfoFetcher
                fetchers["cninfo"] = CninfoFetcher()
                logger.info("巨潮资讯数据源已启用")
            except Exception as e:
                logger.warning(f"巨潮资讯初始化失败: {e}")

        self.router = SourceRouter(self.storage, fetchers)
        # 保留 self.fetcher 兼容旧代码
        self.fetcher = fetchers["akshare"]

    # ── 股票列表 ──────────────────────────────────────────────────
    def get_stock_list(self, force_refresh: bool = False) -> pd.DataFrame:
        df = self.storage.get_stock_list()
        if df.empty or force_refresh:
            logger.info("拉取股票列表...")
            df = self.router.call("stock_list")
            self.storage.upsert_stock_list(df)
        return df

    def get_industry_list(self) -> pd.DataFrame:
        return self.router.call("industry_list")

    def get_industry_stocks(self, industry: str) -> pd.DataFrame:
        return self.router.call("industry_stocks", industry)

    # ── 日K线（增量更新）────────────────────────────────────────
    def get_daily_bars(
        self,
        code: str,
        start_date: str = None,
        end_date: str = None,
    ) -> pd.DataFrame:
        today = date.today().isoformat()
        if end_date is None:
            # 周末回退到周五，避免 latest(周五) < today(周末) 恒真触发全市场无效拉取
            end_date = _latest_possible_trading_day().isoformat()
        if start_date is None:
            start_date = (date.today() - timedelta(days=365 * self.history_years)).isoformat()

        # 检查本地最新日期，决定是否需要增量拉取
        latest = self.storage.get_latest_bar_date(code)
        if latest is None or latest < end_date:
            fetch_start = latest if latest else start_date
            logger.info(f"增量拉取 {code} 日K线: {fetch_start} ~ {end_date}")
            df_new = self.router.call("daily_bars", code, fetch_start, end_date)
            if not df_new.empty:
                self.storage.upsert_daily_bars(df_new)

        return self.storage.get_daily_bars(code, start_date, end_date)

    def batch_update_bars(self, codes: List[str], start_date: str = None):
        """批量更新多只股票的日K线"""
        for i, code in enumerate(codes):
            try:
                self.get_daily_bars(code, start_date=start_date)
                if (i + 1) % 20 == 0:
                    logger.info(f"已更新 {i+1}/{len(codes)} 只股票")
            except Exception as e:
                logger.warning(f"更新 {code} 失败: {e}")

    # ── 财务数据 ──────────────────────────────────────────────────
    def get_financial_data(self, code: str) -> pd.DataFrame:
        df = self.storage.get_financial_data(code)
        if df.empty:
            logger.info(f"拉取 {code} 财务数据...")
            df_new = self.router.call("financial", code)
            if not df_new.empty:
                self.storage.upsert_financial_data(df_new)
                df = self.storage.get_financial_data(code)
        return df

    def get_latest_financial_batch(self, codes: List[str]) -> pd.DataFrame:
        """获取多只股票最新财务数据，缺失的从网络补充"""
        existing = self.storage.get_latest_financial(codes)
        existing_codes = set(existing["code"].tolist()) if not existing.empty else set()
        missing = [c for c in codes if c not in existing_codes]
        for code in missing:
            try:
                self.get_financial_data(code)
            except Exception as e:
                logger.warning(f"获取{code}财务数据失败: {e}")
        return self.storage.get_latest_financial(codes)

    # ── 实时行情 ──────────────────────────────────────────────────
    def get_realtime_quotes(self, codes: List[str]) -> pd.DataFrame:
        return self.router.call("realtime", codes)

    # ── 新闻 ──────────────────────────────────────────────────────
    def fetch_and_save_news(self, code: str = None) -> pd.DataFrame:
        if code:
            df = self.router.call("announcements", code)
        else:
            df = self.router.call("news")
        if not df.empty:
            self.storage.insert_news(df)
        return df

    def get_news(self, code: str = None, limit: int = 50) -> pd.DataFrame:
        return self.storage.get_news(code, limit)

    def fetch_bars_range(self, code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """强制拉取指定时间段K线并入库，不受本地已有数据影响。"""
        df_new = self.router.call("daily_bars", code, start_date, end_date)
        if not df_new.empty:
            self.storage.upsert_daily_bars(df_new)
        return self.storage.get_daily_bars(code, start_date, end_date)

    # ── 增强批量更新 ──────────────────────────────────────────────
    def batch_update_bars_v2(
        self,
        codes: List[str],
        start_date: str = None,
        update_type: str = "incremental",
        progress_callback: Callable = None,
        cancel_flag: Callable = None,
        rate_limit: float = 0.3,
    ) -> dict:
        """增强版批量K线更新，支持进度回调、取消、速率限制。"""
        trading_today = _latest_possible_trading_day().isoformat()  # 周末回退，避免恒真触发拉取
        if start_date is None:
            start_date = (date.today() - timedelta(days=365 * self.history_years)).isoformat()
        success, failed, skipped, no_data = 0, 0, 0, 0
        failed_codes = []

        for i, code in enumerate(codes):
            if cancel_flag and cancel_flag():
                break
            try:
                if update_type == "incremental":
                    latest = self.storage.get_latest_bar_date(code)
                    if latest and latest >= trading_today:
                        skipped += 1
                        if progress_callback:
                            progress_callback(i + 1, len(codes), code, "跳过（已是最新）")
                        continue
                else:
                    self.storage.delete_stock_data(code, ["daily_bars"])

                before = self.storage.get_latest_bar_date(code)
                self.get_daily_bars(code, start_date=start_date)
                after = self.storage.get_latest_bar_date(code)

                if after and (before is None or after > before):
                    success += 1
                    if progress_callback:
                        progress_callback(i + 1, len(codes), code, "完成")
                else:
                    no_data += 1
                    if progress_callback:
                        progress_callback(i + 1, len(codes), code, "无数据（可能停牌）")
            except Exception as e:
                failed += 1
                failed_codes.append(code)
                logger.warning(f"更新 {code} K线失败: {e}")
                if progress_callback:
                    progress_callback(i + 1, len(codes), code, f"失败: {e}")

            if rate_limit > 0:
                time.sleep(rate_limit)

        return {"success": success, "failed": failed, "skipped": skipped,
                "no_data": no_data, "failed_codes": failed_codes}

    def batch_update_financial_v2(
        self,
        codes: List[str],
        update_type: str = "incremental",
        progress_callback: Callable = None,
        cancel_flag: Callable = None,
        rate_limit: float = 0.5,
    ) -> dict:
        """增强版批量财务数据更新，支持进度回调、取消、速率限制。"""
        success, failed, skipped = 0, 0, 0
        failed_codes = []

        for i, code in enumerate(codes):
            if cancel_flag and cancel_flag():
                break
            try:
                if update_type == "incremental":
                    existing = self.storage.get_financial_data(code)
                    if not existing.empty:
                        skipped += 1
                        if progress_callback:
                            progress_callback(i + 1, len(codes), code, "跳过（已有数据）")
                        continue
                else:
                    self.storage.delete_stock_data(code, ["financial_data"])

                self.get_financial_data(code)
                success += 1
                if progress_callback:
                    progress_callback(i + 1, len(codes), code, "完成")
            except Exception as e:
                failed += 1
                failed_codes.append(code)
                logger.warning(f"更新 {code} 财务数据失败: {e}")
                if progress_callback:
                    progress_callback(i + 1, len(codes), code, f"失败: {e}")

            if rate_limit > 0:
                time.sleep(rate_limit)

        return {"success": success, "failed": failed, "skipped": skipped, "failed_codes": failed_codes}

    def force_refresh_stock(self, code: str) -> dict:
        """删除并重新拉取单只股票的K线和财务数据。"""
        try:
            self.storage.delete_stock_data(code, ["daily_bars", "financial_data"])
            bars_df = self.get_daily_bars(code)
            fin_df = self.get_financial_data(code)
            return {
                "success": True,
                "bars_count": len(bars_df),
                "financial_count": len(fin_df),
                "error": None,
            }
        except Exception as e:
            return {"success": False, "bars_count": 0, "financial_count": 0, "error": str(e)}
