from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class StockInfo:
    code: str          # 股票代码，如 000001
    name: str          # 股票名称
    market: str        # 市场：SH/SZ
    industry: str      # 行业
    list_date: Optional[date] = None


@dataclass
class DailyBar:
    code: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float      # 成交量（手）
    amount: float      # 成交额（元）
    pct_chg: float     # 涨跌幅（%）


@dataclass
class FinancialData:
    code: str
    report_date: date
    pe_ttm: Optional[float] = None    # 市盈率TTM
    pb: Optional[float] = None        # 市净率
    roe: Optional[float] = None       # 净资产收益率（%）
    revenue_yoy: Optional[float] = None   # 营收同比增长（%）
    profit_yoy: Optional[float] = None    # 净利润同比增长（%）
    total_mv: Optional[float] = None      # 总市值（万元）


@dataclass
class NewsItem:
    title: str
    content: str
    source: str
    publish_time: str
    related_codes: list = field(default_factory=list)
