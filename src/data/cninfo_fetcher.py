"""巨潮资讯公告数据源"""
import logging
import requests
import pandas as pd
from datetime import datetime

from src.data.base_fetcher import BaseFetcher

logger = logging.getLogger(__name__)

_CNINFO_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"


class CninfoFetcher(BaseFetcher):
    """
    巨潮资讯公告数据源。
    仅实现 get_stock_news（公告作为新闻），其他方法抛 NotImplementedError。
    免费，无需账号，作为 Tushare 公告的备源。
    """
    name = "cninfo"

    def __init__(self, timeout: int = 15):
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "http://www.cninfo.com.cn",
            "Content-Type": "application/x-www-form-urlencoded",
        })

    @staticmethod
    def _column_for(code: str) -> str:
        """按代码前缀映射巨潮 column（交易所板块）。
        沪市(6xx)→sse、北交所(4/8)→bj、深市(0/3)→szse。
        旧实现硬编码 szse，导致沪市/北交所股票查不到公告。
        """
        if code.startswith("6"):
            return "sse"
        if code.startswith(("4", "8")):
            return "bj"
        return "szse"  # 0/3 开头（深市主板/创业板）

    def get_stock_news(self, code: str) -> pd.DataFrame:
        """
        获取个股公告列表（作为新闻使用）。
        返回列: title, content, publish_time, source, related_codes
        """
        # 巨潮需要 orgId，先查询
        org_id = self._get_org_id(code)
        if not org_id:
            return pd.DataFrame()

        try:
            payload = {
                "stock": f"{code},{org_id}",
                "tabName": "fulltext",
                "pageSize": 30,
                "pageNum": 1,
                "column": self._column_for(code),  # 按交易所映射，沪/京市公告才能查到
                "category": "",
                "plate": "",
                "seDate": "",
                "searchkey": "",
                "secid": "",
                "sortName": "",
                "sortType": "",
                "isHLtitle": "true",
            }
            resp = self._session.post(_CNINFO_URL, data=payload, timeout=self._timeout)
            data = resp.json()
        except Exception as e:
            logger.warning(f"巨潮资讯 get_stock_news {code} 失败: {e}")
            raise

        announcements = data.get("announcements") or []
        if not announcements:
            return pd.DataFrame()

        rows = []
        for ann in announcements:
            # 巨潮无摘要字段，公告正文需 adjunctUrl 单独请求（每条一个请求，成本高，
            # 属后续优化）。用标题兜底避免 content 全空——路由 announcements 在 tushare
            # 不可用切到 cninfo 时，下游 LLM 至少有文本可读（而非空串退化为只看标题）。
            title = ann.get("announcementTitle", "")
            rows.append({
                "title": title,
                "content": title,
                "publish_time": ann.get("announcementTime", ""),
                "source": "巨潮资讯",
                "related_codes": code,
            })

        df = pd.DataFrame(rows)
        df["publish_time"] = pd.to_datetime(df["publish_time"], unit="ms", errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
        return df.drop_duplicates(subset=["title"])

    def _get_org_id(self, code: str) -> str:
        """查询股票对应的巨潮 orgId"""
        try:
            resp = self._session.get(
                f"http://www.cninfo.com.cn/new/information/topSearch/query?keyWord={code}&maxNum=5",
                timeout=self._timeout,
            )
            data = resp.json()
            for item in data:
                if item.get("code") == code:
                    return item.get("orgId", "")
        except Exception as e:
            logger.warning(f"巨潮资讯查询 orgId {code} 失败: {e}")
        return ""

    def get_stock_list(self) -> pd.DataFrame:
        raise NotImplementedError("巨潮资讯不提供股票列表接口")

    def get_daily_bars(self, code, start_date, end_date, adjust="qfq") -> pd.DataFrame:
        raise NotImplementedError("巨潮资讯不提供K线数据接口")

    def get_financial_indicators(self, code) -> pd.DataFrame:
        raise NotImplementedError("巨潮资讯不提供财务指标接口")

    def get_realtime_quotes(self, codes) -> pd.DataFrame:
        raise NotImplementedError("巨潮资讯不提供实时行情接口")

    def get_industry_list(self) -> pd.DataFrame:
        raise NotImplementedError("巨潮资讯不提供行业列表接口")

    def get_industry_stocks(self, industry) -> pd.DataFrame:
        raise NotImplementedError("巨潮资讯不提供行业成分股接口")

    def get_news_em(self, pages=3) -> pd.DataFrame:
        raise NotImplementedError("巨潮资讯不提供市场新闻接口")
