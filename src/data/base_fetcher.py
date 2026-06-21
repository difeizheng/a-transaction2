"""数据源抽象基类"""
from abc import ABC, abstractmethod
import pandas as pd


class BaseFetcher(ABC):
    """
    所有数据源的统一接口。
    不支持的方法抛 NotImplementedError，调用方（SourceRouter）据此判断是否切换备源。
    """
    name: str  # "akshare" | "tushare" | "tencent" | "cninfo"

    @abstractmethod
    def get_stock_list(self) -> pd.DataFrame:
        """获取A股全量股票列表，返回列: code, name, market"""

    @abstractmethod
    def get_daily_bars(self, code: str, start_date: str, end_date: str,
                       adjust: str = "qfq") -> pd.DataFrame:
        """
        获取日K线数据。
        返回列: code, trade_date(date), open, high, low, close, volume, amount, pct_chg
        可选列: adj_factor（复权因子；tushare 可得，akshare 暂无，为 P1 raw+factor
        复权重构预留）。缺失该列时存储层按 NULL 处理，不影响当前 qfq 读取。
        """

    @abstractmethod
    def get_financial_indicators(self, code: str) -> pd.DataFrame:
        """
        获取个股基本面指标。
        返回列: code, report_date, pe_ttm, pb, roe
        """

    @abstractmethod
    def get_realtime_quotes(self, codes: list) -> pd.DataFrame:
        """
        获取实时行情（批量）。
        返回列: code, name, price, pct_chg, volume, amount
        """

    @abstractmethod
    def get_industry_list(self) -> pd.DataFrame:
        """获取行业板块列表"""

    @abstractmethod
    def get_industry_stocks(self, industry: str) -> pd.DataFrame:
        """获取某行业的股票列表，返回列: code, name"""

    @abstractmethod
    def get_stock_news(self, code: str) -> pd.DataFrame:
        """
        获取个股相关新闻。
        返回列: title, content, publish_time, source, related_codes
        """

    @abstractmethod
    def get_news_em(self, pages: int = 3) -> pd.DataFrame:
        """获取市场财经新闻，返回列同 get_stock_news"""
