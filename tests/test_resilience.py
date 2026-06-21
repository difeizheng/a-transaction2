"""韧性层单测：_run_timeout / _retry / canary / get_industry_list(retries) / 降级检测。

@pytest.mark.unit —— 全 monkeypatch，零网络/零 DB。防回归。
"""
import time
import threading

import pandas as pd
import pytest

from src.data import fetcher
from src.data.fetcher import _retry, _run_timeout

pytestmark = pytest.mark.unit


# ── _run_timeout ──────────────────────────────────────────────
class TestRunTimeout:
    def test_normal_completion_returns_value(self):
        assert _run_timeout(lambda: 42, timeout=2) == 42

    def test_timeout_raises_timeouterror_on_hang(self):
        t0 = time.time()
        with pytest.raises(TimeoutError, match="超时"):
            _run_timeout(lambda: time.sleep(5), timeout=0.3)
        # 严格在超时附近返回（不等挂起线程）
        assert time.time() - t0 < 1.5

    def test_propagates_inner_exception(self):
        # func 抛非超时异常 → 透传，不当 TimeoutError 处理
        with pytest.raises(ValueError, match="inner"):
            _run_timeout(lambda: (_ for _ in ()).throw(ValueError("inner")), timeout=2)

    def test_thread_is_daemon_so_does_not_block_shutdown(self):
        # 间接验证：daemon 线程的 .daemon 属性为 True。daemon 属性是这次从
        # ThreadPoolExecutor 改回 threading.Thread 的关键原因（非 daemon worker
        # 会卡住 interpreter shutdown）。
        captured = []

        def _capture():
            captured.append(threading.current_thread())

        def _runner():
            t = threading.Thread(target=_capture, daemon=True)
            t.start()
            t.join()

        _run_timeout(_runner, timeout=2)
        assert captured, "thread should have run"
        assert captured[0].daemon is True


# ── _retry 退避算式 ───────────────────────────────────────────
class TestRetryBackoff:
    def test_success_on_first_try_no_sleep(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(fetcher.time, "sleep", lambda x: sleeps.append(x))
        monkeypatch.setattr(fetcher.random, "uniform", lambda lo, hi: 0)
        result = _retry(lambda: "ok", retries=3)
        assert result == "ok"
        assert sleeps == []

    def test_retries_1_calls_once_then_raises(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(fetcher.time, "sleep", lambda x: sleeps.append(x))
        calls = [0]

        def fail():
            calls[0] += 1
            raise ConnectionError("boom")

        with pytest.raises(ConnectionError):
            _retry(fail, retries=1)
        assert calls[0] == 1
        assert sleeps == []  # retries=1 不重试，也不 sleep

    def test_backoff_is_exponential_without_jitter(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(fetcher.time, "sleep", lambda x: sleeps.append(x))
        monkeypatch.setattr(fetcher.random, "uniform", lambda lo, hi: 0)
        with pytest.raises(ConnectionError):
            _retry(lambda: (_ for _ in ()).throw(ConnectionError("x")),
                   retries=3, delay=1)
        # 3 次尝试 → 失败 2 次（最后一次失败直接 raise）→ 2 次 sleep
        # 第 1 次后 sleep = delay*2^0 = 1；第 2 次后 sleep = delay*2^1 = 2
        assert sleeps == [1, 2]

    def test_backoff_includes_jitter(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(fetcher.time, "sleep", lambda x: sleeps.append(x))
        # jitter 固定 1.5
        monkeypatch.setattr(fetcher.random, "uniform", lambda lo, hi: 1.5)
        with pytest.raises(ConnectionError):
            _retry(lambda: (_ for _ in ()).throw(ConnectionError("x")),
                   retries=3, delay=2)
        # 第 1 次后: 2*1 + 1.5 = 3.5；第 2 次后: 2*2 + 1.5 = 5.5
        assert sleeps == [3.5, 5.5]

    def test_eventual_success_after_partial_failures(self, monkeypatch):
        monkeypatch.setattr(fetcher.time, "sleep", lambda x: None)
        monkeypatch.setattr(fetcher.random, "uniform", lambda lo, hi: 0)
        calls = [0]

        def fail_twice_then_ok():
            calls[0] += 1
            if calls[0] < 3:
                raise ConnectionError("blip")
            return "got it"

        assert _retry(fail_twice_then_ok, retries=5, delay=0) == "got it"
        assert calls[0] == 3


# ── canary + 腾讯备源 ────────────────────────────────────────
def _fake_index_df():
    """模拟东财日K返回（单行，4 列）。"""
    return pd.DataFrame({
        "trade_date": [pd.Timestamp("2026-06-15").date()],
        "open": [100.0], "high": [101.0], "low": [99.0], "close": [100.5],
        "volume": [1e6], "amount": [1e8], "pct_chg": [0.5],
    })


class _FakeTencent:
    """记录被请求的 symbols，返回合成实时报价。"""
    def __init__(self):
        self.calls = []

    def fetch_raw(self, symbols):
        self.calls.append(list(symbols))
        out = []
        for s in symbols:
            code = s[2:]  # strip sh/sz
            out.append({"code": code, "name": code, "price": 100.0,
                        "pct_chg": 0.5, "volume": 0.0, "amount": 0.0})
        return out


class TestCanaryAndTencentBackup:
    def test_canary_failure_falls_back_all_four_to_tencent(self, monkeypatch):
        from src.data.manager import DataManager
        dm = DataManager()
        # 第 1 个 + 后续 3 个都返回空（模拟东财 push2 全挂）
        monkeypatch.setattr(dm, "get_index_daily_bars", lambda code, *a, **kw: pd.DataFrame())
        ft = _FakeTencent()
        monkeypatch.setattr(dm, "_fetchers", {"tencent": ft})
        snap = dm.get_market_snapshot()
        assert len(snap["indices"]) == 4
        for code, info in snap["indices"].items():
            assert info["source"] == "tencent", f"{code} should be tencent"
            assert info["trade_date"] == "实时"
            assert info["trend"] == []
        # 腾讯被调一次（批量 4 个 symbol）
        assert len(ft.calls) == 1
        assert len(ft.calls[0]) == 4

    def test_canary_success_skips_tencent_for_successful_indices(self, monkeypatch):
        from src.data.manager import DataManager
        dm = DataManager()
        # 所有指数都返回东财正常数据
        monkeypatch.setattr(dm, "get_index_daily_bars", lambda code, *a, **kw: _fake_index_df())
        # 板块返回空（与指数无关）
        monkeypatch.setattr(dm, "fetcher",
                             type("F", (), {"get_industry_list": lambda self, retries=3: pd.DataFrame()})())
        ft = _FakeTencent()
        monkeypatch.setattr(dm, "_fetchers", {"tencent": ft})
        snap = dm.get_market_snapshot()
        assert len(snap["indices"]) == 4
        for code, info in snap["indices"].items():
            assert info.get("source") != "tencent", f"{code} should be akshare"
            assert info["trade_date"] != "实时"
        # 腾讯完全没被调
        assert ft.calls == []

    def test_partial_eastmoney_failure_mixed_sources(self, monkeypatch):
        """第 1 个指数成功(canary 通)→ 后续逐个取；第 3 个返回空 → 只它走腾讯。"""
        from src.data.manager import DataManager
        dm = DataManager()
        # 1、2、4 成功；3（399006）失败
        good_codes = {"000300", "000001", "000905"}
        def maybe(code, *a, **kw):
            return _fake_index_df() if code in good_codes else pd.DataFrame()
        monkeypatch.setattr(dm, "get_index_daily_bars", maybe)
        ft = _FakeTencent()
        monkeypatch.setattr(dm, "_fetchers", {"tencent": ft})
        monkeypatch.setattr(dm, "fetcher",
                             type("F", (), {"get_industry_list": lambda self, retries=3: pd.DataFrame()})())
        snap = dm.get_market_snapshot()
        tencent_codes = [c for c, i in snap["indices"].items() if i.get("source") == "tencent"]
        akshare_codes = [c for c, i in snap["indices"].items() if i.get("source") != "tencent"]
        assert tencent_codes == ["399006"]
        assert set(akshare_codes) == good_codes


# ── get_industry_list(retries) 参数接线 ──────────────────────────
class TestIndustryListRetriesParam:
    def test_retries_param_is_passed_to_retry(self, monkeypatch):
        captured = []

        def fake_retry(func, retries=3, **kw):
            captured.append(retries)
            return pd.DataFrame()

        monkeypatch.setattr(fetcher, "_retry", fake_retry)
        monkeypatch.setattr(fetcher.AKShareFetcher, "get_industry_list",
                           lambda self, retries=3: fetcher._retry(lambda: None, retries=retries))

        fet = fetcher.AKShareFetcher()
        fet.get_industry_list(retries=1)
        fet.get_industry_list()  # 默认 3
        assert captured == [1, 3]


# ── UI 降级检测 ────────────────────────────────────────────────
class TestDegradationLevel:
    def test_none_when_all_akshare_and_sectors_present(self):
        from src.ui.page_modules.analysis import _degradation_level
        display = {"indices": {"000300": {"source": "akshare"}}, "sectors": [{"name": "X"}]}
        assert _degradation_level(display) == "none"

    def test_partial_when_only_tencent_for_some_indices(self):
        from src.ui.page_modules.analysis import _degradation_level
        display = {"indices": {"000300": {"source": "akshare"},
                              "000001": {"source": "tencent"}},
                   "sectors": [{"name": "X"}]}
        assert _degradation_level(display) == "partial"

    def test_partial_when_sectors_empty_but_indices_akshare(self):
        from src.ui.page_modules.analysis import _degradation_level
        display = {"indices": {"000300": {"source": "akshare"}}, "sectors": []}
        assert _degradation_level(display) == "partial"

    def test_heavy_when_tencent_and_sectors_empty(self):
        from src.ui.page_modules.analysis import _degradation_level
        display = {"indices": {"000300": {"source": "tencent"}}, "sectors": []}
        assert _degradation_level(display) == "heavy"

    def test_handles_empty_indices_dict(self):
        from src.ui.page_modules.analysis import _degradation_level
        # 指数整组空 = 重度(连腾讯备源都拿不到,或根本没数据)
        assert _degradation_level({"indices": {}, "sectors": [{"n": "X"}]}) == "heavy"
        assert _degradation_level({"indices": {}, "sectors": []}) == "heavy"
        assert _degradation_level({"indices": {}, "sectors": None}) == "heavy"
        # display 整体空 = 无源信号 = "none"（UI 默认渲染）
        assert _degradation_level({}) == "none"
