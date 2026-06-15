"""Tushare数据源封装"""
import logging
import time
from datetime import date, datetime
import pandas as pd

from src.data.base_fetcher import BaseFetcher

logger = logging.getLogger(__name__)


def _ts_date(d) -> str:
    """将 date/str(YYYY-MM-DD) 转为 Tushare 格式 YYYYMMDD"""
    if isinstance(d, date):
        return d.strftime("%Y%m%d")
    return str(d).replace("-", "")


def _from_ts_date(s) -> date:
    """将 Tushare 日期字符串 YYYYMMDD 转为 date"""
    return datetime.strptime(str(s), "%Y%m%d").date()


class TushareFetcher(BaseFetcher):
    """
    Tushare Pro 数据源。
    主要用于：K线数据、财务数据、股票列表、公告。
    实时行情不使用（消耗积分），抛 NotImplementedError。
    """
    name = "tushare"

    def __init__(self, token: str):
        self._token = token
        self._pro = None  # 懒加载，避免初始化时发起网络请求

    @property
    def pro(self):
        if self._pro is None:
            import tushare as ts
            self._pro = ts.pro_api(self._token)
        return self._pro

    def get_stock_list(self) -> pd.DataFrame:
        """获取A股全量股票列表，返回列: code, name, market"""
        df = self.pro.stock_basic(
            exchange="",
            list_status="L",
            fields="ts_code,name,market,list_date"
        )
        if df is None or df.empty:
            return pd.DataFrame()
        # ts_code 格式: 000001.SZ → 转为纯数字代码
        df["code"] = df["ts_code"].apply(lambda x: x.split(".")[0])
        df["market"] = df["ts_code"].apply(lambda x: x.split(".")[1])
        return df[["code", "name", "market"]].reset_index(drop=True)

    def get_daily_bars(self, code: str, start_date: str, end_date: str,
                       adjust: str = "qfq") -> pd.DataFrame:
        """
        获取日K线数据（前复权）。
        返回列: code, trade_date(date), open, high, low, close, volume, amount, pct_chg
        """
        ts_code = self._to_ts_code(code)
        adj_map = {"qfq": "qfq", "hfq": "hfq", "": None}
        adj = adj_map.get(adjust, "qfq")

        try:
            import tushare as ts
            df = ts.pro_bar(
                ts_code=ts_code,
                adj=adj,
                start_date=_ts_date(start_date),
                end_date=_ts_date(end_date),
                freq="D",
            )
        except Exception as e:
            logger.warning(f"Tushare get_daily_bars {code} 失败: {e}")
            raise

        if df is None or df.empty:
            return pd.DataFrame()

        df = df.rename(columns={
            "trade_date": "trade_date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "vol": "volume",
            "amount": "amount",
            "pct_chg": "pct_chg",
        })
        df["code"] = code
        df["trade_date"] = df["trade_date"].apply(
            lambda x: _from_ts_date(x) if pd.notna(x) else None
        )
        # Tushare amount 单位是千元，转为元
        if "amount" in df.columns:
            df["amount"] = df["amount"] * 1000
        # Tushare vol 单位是手，转为股（与 akshare/tencent 一致，避免跨源100倍差异）
        if "volume" in df.columns:
            df["volume"] = df["volume"] * 100
        cols = ["code", "trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]
        return df[[c for c in cols if c in df.columns]].sort_values("trade_date").reset_index(drop=True)

    def get_financial_indicators(self, code: str) -> pd.DataFrame:
        """
        获取个股基本面指标。
        返回列: code, report_date, pe_ttm, pb, roe
        """
        ts_code = self._to_ts_code(code)
        try:
            df = self.pro.fina_indicator(
                ts_code=ts_code,
                fields="ts_code,end_date,roe,netprofit_yoy,or_yoy"
            )
        except Exception as e:
            logger.warning(f"Tushare fina_indicator {code} 失败: {e}")
            raise

        if df is None or df.empty:
            return pd.DataFrame()

        # PE/PB 从每日指标获取
        try:
            daily_basic = self.pro.daily_basic(
                ts_code=ts_code,
                fields="trade_date,pe_ttm,pb,total_mv"
            )
        except Exception:
            daily_basic = pd.DataFrame()

        df = df.rename(columns={
            "end_date": "report_date",
            "netprofit_yoy": "profit_yoy",
            "or_yoy": "revenue_yoy",
        })
        df["code"] = code
        df["report_date"] = df["report_date"].apply(
            lambda x: _from_ts_date(x) if pd.notna(x) else None
        )

        # pe_ttm/pb/total_mv 是当前交易日的时点估值，不属于历史报告期；
        # 只填到最新一条报告期行，避免广播成水平线（旧实现把最新估值抹平到所有季度，
        # 导致估值历史曲线是一条直线）。
        df["pe_ttm"] = None
        df["pb"] = None
        df["total_mv"] = None
        if not daily_basic.empty:
            latest_idx = df.sort_values("report_date").index[-1]
            basic = daily_basic.iloc[0]  # daily_basic 默认按 trade_date 降序，iloc[0]=最新
            df.at[latest_idx, "pe_ttm"] = basic.get("pe_ttm")
            df.at[latest_idx, "pb"] = basic.get("pb")
            df.at[latest_idx, "total_mv"] = basic.get("total_mv")

        cols = ["code", "report_date", "pe_ttm", "pb", "roe",
                "revenue_yoy", "profit_yoy", "total_mv"]
        return df[[c for c in cols if c in df.columns]].reset_index(drop=True)

    def get_realtime_quotes(self, codes: list) -> pd.DataFrame:
        raise NotImplementedError("Tushare实时行情消耗积分，请使用腾讯财经或AKShare")

    def get_industry_list(self) -> pd.DataFrame:
        """获取行业分类列表（使用申万行业）"""
        try:
            df = self.pro.index_classify(level="L1", src="SW2021")
            return df
        except Exception as e:
            logger.warning(f"Tushare get_industry_list 失败: {e}")
            raise

    def get_industry_stocks(self, industry: str) -> pd.DataFrame:
        """获取申万行业成分股（industry 为行业名称如 '农林牧渔' 或 index_code 如 '801010.SI'）"""
        try:
            # 如果传入的是 index_code 格式直接用，否则先查 index_code
            if '.' in industry:
                index_code = industry
            else:
                df_cls = self.pro.index_classify(level="L1", src="SW2021")
                match = df_cls[df_cls["industry_name"] == industry]
                if match.empty:
                    raise ValueError(f"未找到申万行业: {industry}")
                index_code = match.iloc[0]["index_code"]

            df = self.pro.index_member(index_code=index_code, fields="con_code,con_name")
            if df is None or df.empty:
                return pd.DataFrame()
            df = df.rename(columns={"con_code": "ts_code", "con_name": "name"})
            df["code"] = df["ts_code"].str[:6]
            return df[["code", "name"]]
        except Exception as e:
            logger.warning(f"Tushare get_industry_stocks {industry} 失败: {e}")
            raise

    def get_stock_news(self, code: str) -> pd.DataFrame:
        """获取个股公告（作为新闻使用）"""
        ts_code = self._to_ts_code(code)
        try:
            df = self.pro.anns(
                ts_code=ts_code,
                fields="ts_code,ann_date,title,content,source"
            )
        except Exception as e:
            logger.warning(f"Tushare get_stock_news {code} 失败: {e}")
            raise

        if df is None or df.empty:
            return pd.DataFrame()

        df = df.rename(columns={"ann_date": "publish_time"})
        df["related_codes"] = code
        if "content" not in df.columns:
            df["content"] = ""
        if "source" not in df.columns:
            df["source"] = "Tushare"

        cols = ["title", "content", "publish_time", "source", "related_codes"]
        return df[[c for c in cols if c in df.columns]].drop_duplicates(subset=["title"])

    def get_news_em(self, pages: int = 3) -> pd.DataFrame:
        """获取市场新闻（Tushare 新闻接口）"""
        try:
            df = self.pro.news(src="sina", fields="datetime,title,content,channels")
        except Exception as e:
            logger.warning(f"Tushare get_news_em 失败: {e}")
            raise

        if df is None or df.empty:
            return pd.DataFrame()

        df = df.rename(columns={"datetime": "publish_time", "channels": "source"})
        df["related_codes"] = ""
        if "content" not in df.columns:
            df["content"] = ""

        cols = ["title", "content", "publish_time", "source", "related_codes"]
        return df[[c for c in cols if c in df.columns]].drop_duplicates(subset=["title"])

    @staticmethod
    def _to_ts_code(code: str) -> str:
        """000001 → 000001.SZ，600519 → 600519.SH"""
        if "." in code:
            return code
        if code.startswith("6"):
            return f"{code}.SH"
        elif code.startswith(("4", "8")):
            return f"{code}.BJ"
        else:
            return f"{code}.SZ"
