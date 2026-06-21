"""market_sentiment 表读写集成测试。

@pytest.mark.integration —— 用 conftest.py 的 storage fixture（每测试一个临时 SQLite 库），
不触碰真实 data/stock.db。
"""
from datetime import date

import pytest
from sqlalchemy import text


def _snap(snapshot_date="2026-06-16", temperature=62.5, label="bullish",
          index_moves=None, summary="市场情绪偏暖", key_events=None):
    return {
        "snapshot_date": snapshot_date,
        "temperature": temperature,
        "label": label,
        "index_moves_json": index_moves if index_moves is not None else {"000300": 0.8},
        "sector_summary_json": {"median": 0.5, "adv_ratio": 0.6},
        "summary": summary,
        "key_events_json": key_events if key_events is not None else ["央行降准"],
    }


class TestMarketSentimentStorage:
    @pytest.mark.integration
    def test_table_created_on_init(self, storage):
        # storage fixture 实例化时 _init_tables 应已建表
        with storage.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name='market_sentiment'")
            ).fetchall()
        assert len(rows) == 1

    @pytest.mark.integration
    def test_save_and_read_roundtrip(self, storage):
        storage.save_market_sentiment(_snap())
        hist = storage.get_market_sentiment_history(10)
        assert len(hist) == 1
        row = hist[0]
        assert row["snapshot_date"] == "2026-06-16"
        assert row["temperature"] == 62.5
        assert row["label"] == "bullish"
        # JSON 列反序列化为对象
        assert row["index_moves_json"] == {"000300": 0.8}
        assert row["key_events_json"] == ["央行降准"]
        # 自动填充的时间戳非空
        assert row["snapshot_time"]
        assert row["created_at"]

    @pytest.mark.integration
    def test_upsert_same_date_overwrites(self, storage):
        storage.save_market_sentiment(_snap(temperature=62.5, summary="早"))
        storage.save_market_sentiment(_snap(temperature=58.0, summary="晚"))
        hist = storage.get_market_sentiment_history(10)
        assert len(hist) == 1  # 同日 → 1 行
        assert hist[0]["temperature"] == 58.0  # 后写覆盖
        assert hist[0]["summary"] == "晚"

    @pytest.mark.integration
    def test_different_dates_kept_both_newest_first(self, storage):
        storage.save_market_sentiment(_snap("2026-06-15", temperature=40.0, label="bearish"))
        storage.save_market_sentiment(_snap("2026-06-16", temperature=62.5, label="bullish"))
        hist = storage.get_market_sentiment_history(10)
        assert len(hist) == 2
        assert hist[0]["snapshot_date"] == "2026-06-16"  # 新在前
        assert hist[1]["snapshot_date"] == "2026-06-15"

    @pytest.mark.integration
    def test_history_limit_respected(self, storage):
        for i in range(5):
            storage.save_market_sentiment(_snap(f"2026-06-1{i}", temperature=50.0 + i))
        hist = storage.get_market_sentiment_history(3)
        assert len(hist) == 3
        # 取最近 3 天
        assert hist[0]["snapshot_date"] == "2026-06-14"

    @pytest.mark.integration
    def test_date_object_snapshot_key(self, storage):
        # snapshot_date 传 date 对象也能存（内部 isoformat）
        storage.save_market_sentiment(_snap(snapshot_date=date(2026, 6, 16)))
        hist = storage.get_market_sentiment_history(10)
        assert len(hist) == 1
        assert hist[0]["snapshot_date"] == "2026-06-16"
