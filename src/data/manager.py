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
        # 保留 self.fetcher 兼容旧代码；_fetchers 供指数腾讯备源等按需取用
        self.fetcher = fetchers["akshare"]
        self._fetchers = fetchers

    # ── 股票列表 ──────────────────────────────────────────────────
    def get_stock_list(self, force_refresh: bool = False,
                       include_delisted: bool = False) -> pd.DataFrame:
        df = self.storage.get_stock_list()
        if df.empty or force_refresh:
            logger.info("拉取股票列表...")
            df = self.router.call("stock_list", include_delisted=include_delisted)
            self.storage.upsert_stock_list(df)
        return df

    def get_industry_list(self) -> pd.DataFrame:
        return self.router.call("industry_list")

    def get_industry_stocks(self, industry: str) -> pd.DataFrame:
        return self.router.call("industry_stocks", industry)

    # ── 市场快照（指数 + 板块，供情绪分析）──────────────────────
    def get_market_snapshot(
        self,
        index_codes: dict = None,
        lookback_days: int = 30,
    ) -> dict:
        """一次性拉取指数 + 行业板块快照，供 ``compute_market_temperature`` 与 UI。

        列名重命名 / 数字强转都在这层完成——Advisor 与纯函数永远不碰东财中文
        列名。**板块直接走 AKShareFetcher**（东财行业板块接口才有 涨跌幅 /
        上涨家数 / 下跌家数；tushare 的 industry_list 无这些列），与
        ``get_index_daily_bars`` 同样绕过路由。每个子取 try/except，**绝不抛**。

        Returns:
            ``{as_of, indices: {code: {name, pct_chg, trade_date, trend}},
            sectors: [{name, pct_chg, up_count, down_count}],
            sector_advances, sector_declines, source}``
        """
        from src.analysis.sentiment import INDEX_CODES
        index_codes = index_codes or INDEX_CODES

        end_date = _latest_possible_trading_day().isoformat()
        start_date = (date.today() - timedelta(days=int(lookback_days * 1.6))).isoformat()

        # 指数（东财日K；取空的用腾讯实时备源）
        indices: dict = {}
        as_of = None
        index_items = list(index_codes.items())

        def _parse_one(code, cname, df):
            """解析东财指数日K写入 indices；返回 trade_date 或 None。"""
            if df is None or df.empty:
                return None
            d = df.sort_values("trade_date")
            last = d.iloc[-1]
            pct = pd.to_numeric(last.get("pct_chg"), errors="coerce")
            pct = 0.0 if pd.isna(pct) else float(pct)
            td = last.get("trade_date")
            td_str = td.isoformat() if hasattr(td, "isoformat") else str(td)
            trend = pd.to_numeric(d["close"], errors="coerce").dropna().tolist()[-lookback_days:]
            indices[code] = {"name": cname, "pct_chg": pct, "trade_date": td_str, "trend": trend}
            return td_str

        # canary：先探活东财 push2（取第 1 个指数）。挂了就全部走腾讯，
        # 避免 4 个指数串行各等 socket 超时（4×10s）。socket.setdefaulttimeout 兜底挂起。
        missing_codes: list = []
        canary_code, canary_name = index_items[0]
        try:
            canary_td = _parse_one(canary_code, canary_name,
                                   self.get_index_daily_bars(canary_code, start_date, end_date))
        except Exception as e:
            logger.warning(f"市场快照：指数 {canary_code} 取数失败: {e}")
            canary_td = None

        if canary_td is not None:
            if as_of is None or canary_td > as_of:
                as_of = canary_td
            for code, cname in index_items[1:]:   # 东财可用 → 取其余
                try:
                    td_str = _parse_one(code, cname, self.get_index_daily_bars(code, start_date, end_date))
                except Exception as e:
                    logger.warning(f"市场快照：指数 {code} 取数失败: {e}")
                    td_str = None
                if td_str is None:
                    missing_codes.append(code)
                elif as_of is None or td_str > as_of:
                    as_of = td_str
        else:
            missing_codes = [c for c, _ in index_items]   # 东财不可用 → 全走腾讯

        # 东财日K取空的指数 → 腾讯实时备源（无 trend 线，当日涨跌仍可见）
        if missing_codes:
            self._fill_indices_from_tencent(indices, missing_codes, index_codes)

        # 板块（akshare 直连，东财行业板块带涨跌幅/上涨家数/下跌家数）
        sectors: list = []
        sector_advances = 0
        sector_declines = 0
        breadth_available = True
        try:
            ind_df = self.fetcher.get_industry_list(retries=1)  # 快照场景快速失败降级（板块无备源）
            if ind_df is not None and not ind_df.empty:
                ind_df = ind_df.copy()
                rename_map = {
                    "板块名称": "name",
                    "涨跌幅": "pct_chg",
                    "上涨家数": "up_count",
                    "下跌家数": "down_count",
                }
                ind_df = ind_df.rename(columns={k: v for k, v in rename_map.items() if k in ind_df.columns})
                if "name" in ind_df.columns:
                    for _, row in ind_df.iterrows():
                        pct = pd.to_numeric(row.get("pct_chg"), errors="coerce")
                        pct = 0.0 if pd.isna(pct) else float(pct)
                        up = pd.to_numeric(row.get("up_count"), errors="coerce")
                        dn = pd.to_numeric(row.get("down_count"), errors="coerce")
                        up_i = int(up) if not pd.isna(up) else None
                        dn_i = int(dn) if not pd.isna(dn) else None
                        sectors.append({"name": str(row["name"]), "pct_chg": pct,
                                        "up_count": up_i, "down_count": dn_i})
                        if up_i is not None:
                            sector_advances += up_i
                        else:
                            breadth_available = False
                        if dn_i is not None:
                            sector_declines += dn_i
                        else:
                            breadth_available = False
        except Exception as e:
            logger.warning(f"市场快照：行业板块取数失败: {e}")

        return {
            "as_of": as_of,
            "indices": indices,
            "sectors": sectors,
            "sector_advances": sector_advances if breadth_available and sector_advances + sector_declines > 0 else None,
            "sector_declines": sector_declines if breadth_available and sector_advances + sector_declines > 0 else None,
            "source": "akshare",
        }

    def _fill_indices_from_tencent(self, indices: dict, missing_codes: list, index_codes: dict) -> None:
        """东财日K取不到的指数，用腾讯实时接口补当日涨跌幅（无 trend 线）。

        腾讯 ``qt.gtimg.cn`` 不走东财 push2，限流时仍可用。用
        ``sentiment.INDEX_TENCENT_SYMBOLS`` 的正确交易所前缀（个股规则对指数是错的：
        000001 指数=上证 sh 而非平安银行 sz），复用 ``TencentFetcher.fetch_raw`` 解析。
        失败只 warning，不抛；不覆盖东财已取到的指数。
        """
        from src.analysis.sentiment import INDEX_TENCENT_SYMBOLS
        tencent = self._fetchers.get("tencent")
        if tencent is None:
            return
        symbols = [INDEX_TENCENT_SYMBOLS[c] for c in missing_codes if c in INDEX_TENCENT_SYMBOLS]
        if not symbols:
            return
        try:
            rows = tencent.fetch_raw(symbols)
            for row in rows:
                code = row.get("code")
                if code in index_codes and code not in indices:
                    indices[code] = {
                        "name": index_codes[code],
                        "pct_chg": float(row.get("pct_chg", 0.0)),
                        "trade_date": "实时",
                        "trend": [],
                        "source": "tencent",
                    }
                    logger.info(f"市场快照：指数 {code} 用腾讯实时备源填充")
        except Exception as e:
            logger.warning(f"市场快照：腾讯指数备源失败: {e}")


    # ── 宏观快照（四支柱，供 compute_macro_stance）─────────────
    def get_macro_snapshot(self, lookback_days: int = 120) -> dict:
        """一次性拉取四支柱宏观指标，供 ``compute_macro_stance`` 与 UI。

        **akshare 直连**（这些 macro/hsgt/margin 接口非行情推送，不经路由；
        与 ``get_market_snapshot`` 板块直连同理）。列名匹配 / 数字强转 / 显式
        升序排序 / 取最新非 NaN 行 / 历史基线(reference)计算 全在这层完成——
        Advisor 与纯函数永远不碰 akshare 中文列名。

        **各数据集排序方向不一致**（有 newest-first 有 oldest-first），故每个
        子取都**显式按日期列升序排序**后再取 latest/reference，绝不假设顺序。
        每个子取独立 try/except，**绝不抛**；某指标失败 → 该 key 省略，纯函数
        自动重分配权重。

        Returns:
            ``{as_of, indicators: {pillar: {key: {latest, reference, as_of}}},
            source}``。``reference`` 仅 use_neutral=False 的指标用到。
        """
        import akshare as ak
        from src.analysis.macro import INDICATOR_NAMES

        ind: dict = {"liquidity": {}, "capital": {}, "fundamental": {}, "external": {}}
        as_of_dates: list[str] = []

        def _col(cols, *needles):
            """第一个含任一 needle 子串的列名；无则 None。"""
            for c in cols:
                cs = str(c)
                if any(n in cs for n in needles):
                    return c
            return None

        def _series(df, date_col, val_col):
            """按 date_col 升序排序、val_col 强转数值、丢 NaN 的 [date,val] DataFrame。"""
            s = df[[date_col, val_col]].copy()
            s[val_col] = pd.to_numeric(s[val_col], errors="coerce")
            return s.dropna(subset=[val_col]).sort_values(date_col).reset_index(drop=True)

        def _track(as_of_str: str):
            """归一化为 ISO 日期后计入快照日期池（max 取最新）。

            月频指标 月份 列格式不一（"2026年05月份"/"202604"），日频为
            datetime.date→ISO。统一归一化，否则 max() 会把中文月串错排到日串之后。
            存入 ind 的 as_of 仍保留原始串（中文显示更友好），仅此池用归一化值。
            """
            import re
            if not as_of_str:
                return
            s = str(as_of_str)
            m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
            if m:
                iso = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            else:
                m = re.match(r"(\d{4})\D+(\d{1,2})", s)  # 2026年05月份
                if m:
                    iso = f"{m.group(1)}-{int(m.group(2)):02d}-01"
                else:
                    m = re.match(r"(\d{4})(\d{2})$", s)  # 202604
                    iso = f"{m.group(1)}-{m.group(2)}-01" if m else s
            as_of_dates.append(iso)

        # ── 流动性 ──
        # M1-M2 剪刀差（月频，固定中性 0）
        try:
            df = ak.macro_china_money_supply()
            mc = _col(df.columns, "月份")
            m2c = next((c for c in df.columns if "M2" in str(c) and "同比" in str(c)), None)
            m1c = next((c for c in df.columns if "M1" in str(c) and "同比" in str(c)), None)
            if mc and m2c and m1c:
                sub = df[[mc, m1c, m2c]].copy()
                sub[m1c] = pd.to_numeric(sub[m1c], errors="coerce")
                sub[m2c] = pd.to_numeric(sub[m2c], errors="coerce")
                sub = sub.dropna().sort_values(mc)
                if not sub.empty:
                    last = sub.iloc[-1]
                    gap = float(last[m1c]) - float(last[m2c])
                    ao = str(last[mc])
                    ind["liquidity"]["m1_m2_gap"] = {"latest": gap, "reference": 0.0, "as_of": ao}
                    _track(ao)
        except Exception as e:
            logger.warning(f"宏观快照：M1-M2 取数失败: {e}")

        # SHIBOR 隔夜（日频，reference=近60日中位）
        try:
            df = ak.macro_china_shibor_all()
            dc = _col(df.columns, "日期") or (df.columns[0] if len(df.columns) else None)
            # O/N 利率 列：该接口中文编码与 money_supply 不同（"利率"子串匹配不到），
            # 改用 ASCII "O/N" 匹配；列序上 O/N-利率 在 O/N-利差 之前，next() 取到利率列。
            onc = next((c for c in df.columns if "O/N" in str(c)), None)
            if dc and onc:
                s = _series(df, dc, onc)
                if not s.empty:
                    latest = float(s[onc].iloc[-1])
                    ref = float(s[onc].tail(60).median()) if len(s) >= 2 else latest
                    ao = str(s[dc].iloc[-1])
                    ind["liquidity"]["shibor_on"] = {"latest": latest, "reference": ref, "as_of": ao}
                    _track(ao)
        except Exception as e:
            logger.warning(f"宏观快照：SHIBOR 取数失败: {e}")

        # 社融同比（月频；从增量推 yoy=最新/去年同期-1）
        try:
            df = ak.macro_chrzgm() if hasattr(ak, "macro_chrzgm") else ak.macro_china_shrzgm()
            mc = _col(df.columns, "月份")
            ic = next((c for c in df.columns if "社会融资规模" in str(c) and "增量" in str(c)), None)
            if mc and ic:
                sub = df[[mc, ic]].copy()
                sub[ic] = pd.to_numeric(sub[ic], errors="coerce")
                sub = sub.dropna().sort_values(mc).reset_index(drop=True)
                if len(sub) >= 24:
                    # 滚动12月累计同比（远比单月增量同比稳定，单月受季节/口径扰动大）
                    recent_12 = float(sub[ic].tail(12).sum())
                    prior_12 = float(sub[ic].iloc[-24:-12].sum())
                    if abs(prior_12) > 1:
                        yoy = (recent_12 - prior_12) / abs(prior_12) * 100
                        ao = str(sub[mc].iloc[-1])
                        ind["liquidity"]["social_fin_yoy"] = {"latest": yoy, "reference": 0.0, "as_of": ao}
                        _track(ao)
        except Exception as e:
            logger.warning(f"宏观快照：社融 取数失败: {e}")

        # ── 资金面 ──
        # 注：北向资金日度净流入自 2024-08-19 起交易所不再公布（stock_hsgt_hist_em
        # 数据止于 2024-08），纳入会注入 2024 年陈旧数据，故不取。资金面以融资余额表征。
        # 融资余额近5日变化%（日频；固定中性 0）
        try:
            df = ak.stock_margin_account_info()
            dc = _col(df.columns, "日期")
            bc = _col(df.columns, "融资余额")
            if dc and bc:
                s = _series(df, dc, bc)
                if len(s) >= 6:
                    latest = float(s[bc].iloc[-1])
                    ago = float(s[bc].iloc[-6])
                    if latest > 0:
                        pct = (latest - ago) / latest
                        ao = str(s[dc].iloc[-1])
                        ind["capital"]["margin_5d_pct"] = {"latest": pct, "reference": 0.0, "as_of": ao}
                        _track(ao)
        except Exception as e:
            logger.warning(f"宏观快照：融资余额 取数失败: {e}")

        # ── 基本面 ──
        # 制造业 PMI（月频，固定中性 50）
        try:
            df = ak.macro_china_pmi()
            mc = _col(df.columns, "月份")
            pc = next((c for c in df.columns if "制造业" in str(c) and "指数" in str(c)), None)
            if mc and pc:
                sub = df[[mc, pc]].copy()
                sub[pc] = pd.to_numeric(sub[pc], errors="coerce")
                sub = sub.dropna().sort_values(mc)
                if not sub.empty:
                    ao = str(sub[mc].iloc[-1])
                    ind["fundamental"]["pmi"] = {"latest": float(sub[pc].iloc[-1]), "reference": 0.0, "as_of": ao}
                    _track(ao)
        except Exception as e:
            logger.warning(f"宏观快照：PMI 取数失败: {e}")

        # PPI-CPI 剪刀差（月频，固定中性 0；PPI同比 - CPI同比）
        try:
            pp = ak.macro_china_ppi()
            cp = ak.macro_china_cpi()
            pmc = _col(pp.columns, "月份")
            cmc = _col(cp.columns, "月份")
            pyc = next((c for c in pp.columns if "同比" in str(c)), None)
            cyc = next((c for c in cp.columns if "全国" in str(c) and "同比" in str(c)), None)
            if pmc and cmc and pyc and cyc:
                pps = _series(pp, pmc, pyc)
                cps = _series(cp, cmc, cyc)
                if not pps.empty and not cps.empty:
                    gap = float(pps[pyc].iloc[-1]) - float(cps[cyc].iloc[-1])
                    ao = str(pps[pmc].iloc[-1])
                    ind["fundamental"]["ppi_cpi_gap"] = {"latest": gap, "reference": 0.0, "as_of": ao}
                    _track(ao)
        except Exception as e:
            logger.warning(f"宏观快照：PPI/CPI 取数失败: {e}")

        # ── 外部 ──
        # 美10债收益率（日频，reference=近60日中位）
        try:
            start = (date.today() - timedelta(days=lookback_days)).strftime("%Y%m%d")
            df = ak.bond_zh_us_rate(start_date=start)
            dc = _col(df.columns, "日期")
            uc = next((c for c in df.columns if "美国" in str(c) and "10" in str(c)), None)
            if dc and uc:
                s = _series(df, dc, uc)
                if not s.empty:
                    latest = float(s[uc].iloc[-1])
                    ref = float(s[uc].tail(60).median()) if len(s) >= 2 else latest
                    ao = str(s[dc].iloc[-1])
                    ind["external"]["us_10y"] = {"latest": latest, "reference": ref, "as_of": ao}
                    _track(ao)
        except Exception as e:
            logger.warning(f"宏观快照：美债 取数失败: {e}")

        # USD/CNY 中间价（日频，近20日变化率，固定中性 0）
        try:
            df = ak.currency_boc_safe()
            dc = _col(df.columns, "日期")
            uc = next((c for c in df.columns if str(c) == "美元"), None)
            if dc and uc:
                s = _series(df, dc, uc)
                if len(s) >= 21:
                    latest_v = float(s[uc].iloc[-1])
                    ago = float(s[uc].iloc[-21])
                    if ago > 0:
                        chg = (latest_v - ago) / ago
                        ao = str(s[dc].iloc[-1])
                        ind["external"]["usd_cny"] = {"latest": chg, "reference": 0.0, "as_of": ao}
                        _track(ao)
        except Exception as e:
            logger.warning(f"宏观快照：USD/CNY 取数失败: {e}")

        as_of = max(as_of_dates) if as_of_dates else date.today().isoformat()
        return {
            "as_of": as_of,
            "indicators": ind,
            "indicator_names": INDICATOR_NAMES,
            "source": "akshare",
        }


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

    def get_index_daily_bars(
        self,
        code: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """获取指数日K线（回测基准用）。akshare 专用，不走路由。

        原因：路由层其他源（tushare 指数需 asset="I" 且代码映射有坑、腾讯无历史K线）
        不支持指数；指数基准直接走 AKShareFetcher。失败时返回空 DataFrame，
        由 BacktestComparator 优雅降级（不阻断主回测）。
        """
        try:
            return self.fetcher.get_index_daily_bars(code, start_date, end_date)
        except Exception as e:
            logger.warning(f"获取指数 {code} 日K线失败: {e}")
            return pd.DataFrame()

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

    def get_financial_as_of(self, code: str, as_of_date) -> "pd.DataFrame":
        """财务 point-in-time：返回决策日 ``as_of_date`` 当时**已披露**的全部财报。

        用 ``ann_date <= as_of_date`` 过滤（ann_date 缺失时按报告期 + 保守延迟兜底），
        避免回测/选股用「报告期当天即公告」的虚假假设（前视偏差）。

        实时/最新场景仍用 ``get_financial_data``（不过滤）；回测、历史决策务必用本方法。
        """
        from src.data.pit import filter_point_in_time

        df = self.get_financial_data(code)  # 含本地缓存 + 缺失时网络补充
        return filter_point_in_time(df, as_of_date)

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
