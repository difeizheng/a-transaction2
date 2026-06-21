"""AKShare数据获取封装"""
import os
import time
import socket
import random
import logging
import threading
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional
import pandas as pd

from src.data.base_fetcher import BaseFetcher

logger = logging.getLogger(__name__)


# ── 国内行情域名直连（绕过系统代理）─────────────────────────────────
# akshare 打的东方财富/新浪/交易所都是国内站，直连本就可达；但用户的系统代理
# （如 Clash 默认 7890）常把这些请求也绕进去，导致 ProxyError 全挂（选股池、
# 基准指数、备份 K 线全部失败）。对这些域名设置 NO_PROXY 让 requests 直连；
# 无代理环境下是 no-op，安全。
_DOMESTIC_DIRECT_DOMAINS = (
    "eastmoney.com",   # 东方财富：个股/指数/财务，akshare 主要源
    "sinajs.cn",       # 新浪实时行情
    "sina.com.cn",
    "sse.com.cn",      # 上交所
    "szse.cn",         # 深交所
)


def _ensure_domestic_direct() -> None:
    """把这些国内行情域名加入 NO_PROXY，使其绕过系统代理直连。保留用户已有设置。"""
    existing = os.environ.get("NO_PROXY", "") or os.environ.get("no_proxy", "")
    parts = [h.strip() for h in existing.split(",") if h.strip()]
    for d in _DOMESTIC_DIRECT_DOMAINS:
        if d not in parts:
            parts.append(d)
    merged = ",".join(parts)
    os.environ["NO_PROXY"] = merged
    os.environ["no_proxy"] = merged  # 兼容大小写读取


_ensure_domestic_direct()

# 全局 socket 超时：akshare/requests 调用默认无 timeout，东财反爬升级到「接受连接
# 不响应」时会无限挂起（_retry/备源都触发不了——首参永不返回）。设 10s 兜底，
# 让挂起的调用在 10s 内抛 socket.timeout → 走重试/腾讯备源。
# 仅影响「未显式设 timeout」的 requests 调用（akshare/cninfo）；腾讯(显式 timeout=10)
# 与 LLM SDK(httpx 自管 timeout)不受影响。
socket.setdefaulttimeout(10)


def _run_timeout(func: Callable[[], Any], timeout: float = 8) -> Any:
    """在线程里跑 func，超时抛 TimeoutError。

    akshare/urllib3 实测**不受** socket.setdefaulttimeout 约束（东财「接受连接不
    响应」时无限阻塞），用线程级硬超时兜底，让 ``_retry`` / 腾讯备源得以触发。
    用 **daemon 线程**——超时后 hung 线程残留但不阻塞解释器退出（ThreadPoolExecutor
    的非 daemon worker 会卡住 shutdown）。每次调用起一个 daemon 线程，开销可忽略。
    """
    box: dict = {}

    def _worker():
        try:
            box["result"] = func()
        except BaseException as e:  # 含 socket.timeout / RemoteDisconnected 等
            box["error"] = e

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        raise TimeoutError(f"akshare 调用 {timeout}s 超时（东财可能限流/挂起）")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _retry(func: Callable[[], Any], retries: int = 3, delay: float = 2, timeout: float = 8) -> Any:
    """指数退避 + 抖动重试，每次尝试有线程级硬超时兜底。

    - 退避：``delay*2^attempt + 抖动[0,delay]``，避免冷却期密集重试加剧东财限流。
    - 超时：akshare 挂起不受 socket 超时约束 → 每次尝试 ``_run_timeout`` 兜底（超时
      抛 TimeoutError，而非无限挂起），让重试 / 腾讯备源得以触发。
    指数日K有腾讯备源，调用方传 ``retries=1`` 快速失败让备源接管。
    """
    for i in range(retries):
        try:
            return _run_timeout(func, timeout)
        except Exception as e:
            if i == retries - 1:
                raise
            backoff = delay * (2 ** i) + random.uniform(0, delay)
            name = getattr(func, "__name__", "<lambda>")
            logger.warning(f"{name} 第{i+1}次失败: {e}，{backoff:.1f}s后重试")
            time.sleep(backoff)


class AKShareFetcher(BaseFetcher):
    """封装AKShare接口，统一返回pandas DataFrame"""
    name = "akshare"

    def get_stock_list(self, include_delisted: bool = False) -> pd.DataFrame:
        """获取A股股票列表，返回 code/name/market。

        ``include_delisted`` 为兼容路由签名而保留——**akshare 的 ``stock_info_a_code_name``
        仅返回在市股，无法获取退市股**。消除幸存者偏差需切换主源为 tushare
        （见 TushareFetcher.get_stock_list）。此处忽略该参数并记录。
        """
        import akshare as ak
        if include_delisted:
            logger.warning("akshare 源无法获取退市股（幸存者偏差无法消除）；"
                           "请在数据管理页把 stock_list 主源切到 tushare 后重拉。")
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

    def get_index_daily_bars(
        self,
        code: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """获取指数日K线（沪深300/上证指数/创业板指/中证500 等），用于回测基准。

        akshare 的 ``stock_zh_a_hist`` 不支持指数代码，故改用 ``index_zh_a_hist``。
        返回与 ``get_daily_bars`` 完全一致的 schema（code/trade_date/open/high/low/
        close/volume/amount/pct_chg），便于复用存储与下游。
        """
        import akshare as ak
        # 指数日K有腾讯实时备源（见 manager._fill_indices_from_tencent），故
        # retries=1 快速失败让备源接管——限流时不必 3 次指数退避拖慢页面（4 指数
        # 串行 ×3×退避会到 ~30s）。回测基准也走此方法，偶发抖动丢失基准可优雅降级。
        df = _retry(
            lambda: ak.index_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
            ),
            retries=1,
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
        })
        df["code"] = code
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        if "pct_chg" not in df.columns:
            df["pct_chg"] = df["close"].pct_change().fillna(0.0) * 100
        cols = [c for c in ["code", "trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]
                if c in df.columns]
        return df[cols]

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

    def get_industry_list(self, retries: int = 3) -> pd.DataFrame:
        """获取东方财富行业板块列表（retries 可调；市场快照传 1 快速失败降级）。"""
        import akshare as ak
        df = _retry(lambda: ak.stock_board_industry_name_em(), retries=retries)
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
