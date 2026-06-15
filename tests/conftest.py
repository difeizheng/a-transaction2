"""共享 fixtures。

每个测试用独立临时 SQLite 库（pytest tmp_path 自动清理），
互不污染、可并发，且不触碰真实 data/stock.db。
"""
import pytest


@pytest.fixture
def storage(tmp_path):
    """每测试一个全新临时库的 Storage。"""
    from src.data.storage import Storage
    return Storage(str(tmp_path / "test.db"))


@pytest.fixture
def portfolio(storage):
    """注入临时库的 Portfolio，初始资金 100 万。"""
    from src.trading.portfolio import Portfolio
    return Portfolio(storage, initial_cash=1_000_000.0)
