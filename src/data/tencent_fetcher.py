"""腾讯财经实时行情数据源"""
import logging
import re
import requests
import pandas as pd

from src.data.base_fetcher import BaseFetcher

logger = logging.getLogger(__name__)

# 腾讯财经行情接口
_TENCENT_URL = "http://qt.gtimg.cn/q={symbols}"
_BATCH_SIZE = 100  # 每次请求最多100只


class TencentFetcher(BaseFetcher):
    """
    腾讯财经实时行情数据源。
    仅实现 get_realtime_quotes，其他方法抛 NotImplementedError。
    免费，无需账号，适合实时行情场景。
    """
    name = "tencent"

    def __init__(self, timeout: int = 10):
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://finance.qq.com",
        })

    def get_realtime_quotes(self, codes: list) -> pd.DataFrame:
        """
        批量获取实时行情。
        返回列: code, name, price, pct_chg, volume, amount
        """
        if not codes:
            return pd.DataFrame()

        results = []
        # 分批请求
        for i in range(0, len(codes), _BATCH_SIZE):
            batch = codes[i: i + _BATCH_SIZE]
            symbols = ",".join(self._to_tencent_symbol(c) for c in batch)
            try:
                resp = self._session.get(
                    _TENCENT_URL.format(symbols=symbols),
                    timeout=self._timeout,
                )
                resp.encoding = "gbk"
                rows = self._parse_response(resp.text)
                results.extend(rows)
            except Exception as e:
                logger.warning(f"腾讯财经请求失败 batch {i//100}: {e}")
                raise

        if not results:
            return pd.DataFrame()

        df = pd.DataFrame(results)
        return df[["code", "name", "price", "pct_chg", "volume", "amount"]]

    def fetch_raw(self, symbols: list) -> list:
        """取**已带交易所前缀**(sh/sz/bj)的 symbol 实时行情，返回解析后的行列表。

        供指数备源用：指数 symbol 不能套用 ``_to_tencent_symbol`` 个股规则
        （000001 在指数里是上证指数 sh，而非平安银行 sz），调用方负责传正确前缀。
        返回行含 code(纯数字)/name/price/pct_chg/volume/amount。
        """
        if not symbols:
            return []
        resp = self._session.get(
            _TENCENT_URL.format(symbols=",".join(symbols)),
            timeout=self._timeout,
        )
        resp.encoding = "gbk"
        return self._parse_response(resp.text)

    def get_stock_list(self) -> pd.DataFrame:
        raise NotImplementedError("腾讯财经不提供股票列表接口")

    def get_daily_bars(self, code, start_date, end_date, adjust="qfq") -> pd.DataFrame:
        raise NotImplementedError("腾讯财经不提供历史K线接口")

    def get_financial_indicators(self, code) -> pd.DataFrame:
        raise NotImplementedError("腾讯财经不提供财务数据接口")

    def get_industry_list(self) -> pd.DataFrame:
        raise NotImplementedError("腾讯财经不提供行业列表接口")

    def get_industry_stocks(self, industry) -> pd.DataFrame:
        raise NotImplementedError("腾讯财经不提供行业成分股接口")

    def get_stock_news(self, code) -> pd.DataFrame:
        raise NotImplementedError("腾讯财经不提供新闻接口")

    def get_news_em(self, pages=3) -> pd.DataFrame:
        raise NotImplementedError("腾讯财经不提供新闻接口")

    @staticmethod
    def _to_tencent_symbol(code: str) -> str:
        """000001 → sz000001，600519 → sh600519，北交所 → bj"""
        if code.startswith("6"):
            return f"sh{code}"
        elif code.startswith(("4", "8")):
            return f"bj{code}"
        else:
            return f"sz{code}"

    @staticmethod
    def _parse_response(text: str) -> list:
        """
        解析腾讯财经返回的文本格式：
        v_sh600519="1~贵州茅台~600519~1800.00~1750.00~1810.00~..."
        字段为 split('~') 后的 0-based 索引：
          fields[1]=名称, fields[3]=昨收, fields[5]=现价,
          fields[32]=涨跌幅(%), fields[36]=成交量(手), fields[37]=成交额(万元)
        """
        rows = []
        # 匹配每行数据
        pattern = re.compile(r'v_[a-z]{2}(\d+)="([^"]*)"')
        for match in pattern.finditer(text):
            code = match.group(1)
            fields = match.group(2).split("~")
            if len(fields) < 37:
                continue
            try:
                # fields[3]=昨收, fields[5]=现价
                # 盘前/停牌时现价可能为0，回退昨收，避免0价污染市值计算
                current = float(fields[5]) if fields[5] else 0.0
                prev_close = float(fields[3]) if fields[3] else 0.0
                price = current if current > 0 else prev_close
                rows.append({
                    "code": code,
                    "name": fields[1],
                    "price": price,
                    "pct_chg": float(fields[32]) if fields[32] else 0.0,
                    "volume": float(fields[36]) * 100 if fields[36] else 0.0,  # 手→股
                    # 腾讯 amount 单位为「万元」，akshare/tushare 均为「元」——
                    # 跨源切换（路由 realtime: tencent↔akshare）必须统一到元，否则
                    # amount 差 10000 倍，污染市值估算与 LLM 输入。
                    "amount": float(fields[37]) * 10000 if fields[37] else 0.0,
                })
            except (ValueError, IndexError):
                continue
        return rows
