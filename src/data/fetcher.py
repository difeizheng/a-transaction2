"""AKShare数据获取封装"""
import time
import logging
from datetime import date, datetime, timedelta
from typing import Optional
import pandas as pd

from src.data.base_fetcher import BaseFetcher

logger = logging.getLogger(__name__)


def _retry(func, retries=3, delay=2):
    for i in range(retries):
        try:
            return func()
        except Exception as e:
            if i == retries - 1:
                raise
            logger.warning(f"{func.__name__} 第{i+1}次失败: {e}，{delay}s后重试")
            time.sleep(delay)


class AKShareFetcher(BaseFetcher):
    """封装AKShare接口，统一返回pandas DataFrame"""
    name = "akshare"

    def get_stock_list(self) -> pd.DataFrame:
        """获取A股全量股票列表，返回 code/name/market/industry/list_date"""
        import akshare as ak
        df = _retry(lambda: ak.stock_info_a_code_name())
        df = df.rename(columns={"code": "code", "name": "name"})
        # 补充市场标识
        df["market"] = df["code"].apply(
            lambda c: "SH" if c.startswith("6") else "SZ"
        )
        return df[["code", "name", "market"]]

    def get_daily_bars(
        self,
        code: str,
        start_date: str,
        end_date: str,
        adjust: str = "qfq",  # 前复权
    ) -> pd.DataFrame:
        """
        获取日K线数据
        返回列: trade_date/open/high/low/close/volume/amount/pct_chg
        """
        import akshare as ak
        symbol = self._to_akshare_symbol(code)
        df = _retry(
            lambda: ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
                adjust=adjust,
            )
        )
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(columns={
            "日期": "trade_date",
            "开盘": "open",
            "最高": "high",
            "最低": "low",
            "收盘": "close",
            "成交量": "volume",
            "成交额": "amount",
            "涨跌幅": "pct_chg",
        })
        df["code"] = code
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        return df[["code", "trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]]

    def get_financial_indicators(self, code: str) -> pd.DataFrame:
        """获取个股基本面指标（PE/PB/ROE/市值等）"""
        import akshare as ak
        symbol = self._to_akshare_symbol(code)
        try:
            df = _retry(lambda: ak.stock_a_lg_indicator(symbol=symbol))
        except Exception as e:
            logger.warning(f"获取{code}基本面数据失败: {e}")
            return pd.DataFrame()
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(columns={
            "trade_date": "report_date",
            "pe": "pe_ttm",
            "pb": "pb",
            "roe": "roe",
        })
        df["code"] = code
        return df

    def get_industry_stocks(self, industry: str) -> pd.DataFrame:
        """获取某行业的股票列表"""
        import akshare as ak
        df = _retry(lambda: ak.stock_board_industry_cons_em(symbol=industry))
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(columns={"代码": "code", "名称": "name"})
        return df[["code", "name"]]

    def get_industry_list(self) -> pd.DataFrame:
        """获取东方财富行业板块列表"""
        import akshare as ak
        df = _retry(lambda: ak.stock_board_industry_name_em())
        return df

    def get_realtime_quotes(self, codes: list) -> pd.DataFrame:
        """获取实时行情（批量）"""
        import akshare as ak
        df = _retry(lambda: ak.stock_zh_a_spot_em())
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(columns={"代码": "code", "名称": "name", "最新价": "price",
                                 "涨跌幅": "pct_chg", "成交量": "volume", "成交额": "amount"})
        return df[df["code"].isin(codes)][["code", "name", "price", "pct_chg", "volume", "amount"]]

    def get_news_em(self, pages: int = 3) -> pd.DataFrame:
        """获取财经新闻（用主要股票新闻代替市场新闻，因东方财富空symbol接口已失效）"""
        import akshare as ak
        market_codes = ["000001", "600519", "000858", "601318", "600036"]
        frames = []
        for code in market_codes:
            try:
                df = _retry(lambda c=code: ak.stock_news_em(symbol=c))
                if df is not None and not df.empty:
                    frames.append(df)
            except Exception as e:
                logger.warning(f"获取{code}新闻失败: {e}")
        if not frames:
            return pd.DataFrame()
        result = pd.concat(frames, ignore_index=True)
        return self._normalize_news(result)

    def get_stock_news(self, code: str) -> pd.DataFrame:
        """获取个股相关新闻"""
        import akshare as ak
        try:
            df = _retry(lambda: ak.stock_news_em(symbol=code))
            if df is None or df.empty:
                return pd.DataFrame()
            return self._normalize_news(df, related_code=code)
        except Exception as e:
            logger.warning(f"获取{code}新闻失败: {e}")
            return pd.DataFrame()

    @staticmethod
    def _normalize_news(df: pd.DataFrame, related_code: str = None) -> pd.DataFrame:
        """统一新闻列名为存储层期望的格式"""
        col_map = {
            "新闻标题": "title",
            "新闻内容": "content",
            "发布时间": "publish_time",
            "文章来源": "source",
            "关键词": "related_codes",
            "新闻链接": "url",
        }
        df = df.rename(columns=col_map)
        if "related_codes" not in df.columns:
            df["related_codes"] = related_code or ""
        elif related_code:
            df["related_codes"] = related_code
        # 只保留存储层需要的列
        keep = [c for c in ["title", "content", "publish_time", "source", "related_codes", "url"] if c in df.columns]
        return df[keep].drop_duplicates(subset=["title"])

    @staticmethod
    def _to_akshare_symbol(code: str) -> str:
        """000001 -> 000001（AKShare直接用纯数字代码）"""
        return code.lstrip("0") if False else code  # AKShare用原始代码
