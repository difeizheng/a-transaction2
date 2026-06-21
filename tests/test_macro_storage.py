"""macro_snapshot 表读写集成测试。

@pytest.mark.integration —— 用 conftest.py 的 storage fixture（每测试一个临时 SQLite 库），
不触碰真实 data/stock.db。镜像 test_market_storage.py 范式。
"""
from datetime import date

import pytest
from sqlalchemy import text


def _snap(snapshot_date="2026-06-16", score=72.0, label="bullish",
          components=None, indicators=None, summary="流动性宽松", key_risks=None):
    return {
        "snapshot_date": snapshot_date,
        "score": score,
        "label": label,
        "stance": 0.44,
        "components_json": components if components is not None
            else {"liquidity": {"signal": 0.8, "weight": 0.3}},
        "indicators_json": indicators if indicators is not None
            else {"m1_m2_gap": {"signal": 1.0, "value": 5.0}},
        "summary": summary,
        "key_risks_json": key_risks if key_risks is not None else ["外部加息"],
    }


class TestMacroSnapshotStorage:
    @pytest.mark.integration
    def test_table_created_on_init(self, storage):
        with storage.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name='macro_snapshot'")
            ).fetchall()
        assert len(rows) == 1

    @pytest.mark.integration
    def test_save_and_read_roundtrip(self, storage):
        storage.save_macro_snapshot(_snap())
        hist = storage.get_macro_history(10)
        assert len(hist) == 1
        row = hist[0]
        assert row["snapshot_date"] == "2026-06-16"
        assert row["score"] == 72.0
        assert row["label"] == "bullish"
        # JSON 列反序列化为对象
        assert row["components_json"] == {"liquidity": {"signal": 0.8, "weight": 0.3}}
        assert row["indicators_json"] == {"m1_m2_gap": {"signal": 1.0, "value": 5.0}}
        assert row["key_risks_json"] == ["外部加息"]
        # 自动填充的时间戳非空
        assert row["snapshot_time"]
        assert row["created_at"]

    @pytest.mark.integration
    def test_upsert_same_date_overwrites(self, storage):
        storage.save_macro_snapshot(_snap(score=72.0, summary="早"))
        storage.save_macro_snapshot(_snap(score=68.0, summary="晚"))
        hist = storage.get_macro_history(10)
        assert len(hist) == 1  # 同日 → 1 行
        assert hist[0]["score"] == 68.0  # 后写覆盖
        assert hist[0]["summary"] == "晚"

    @pytest.mark.integration
    def test_different_dates_kept_both_newest_first(self, storage):
        storage.save_macro_snapshot(_snap("2026-06-15", score=42.0, label="bearish"))
        storage.save_macro_snapshot(_snap("2026-06-16", score=72.0, label="bullish"))
        hist = storage.get_macro_history(10)
        assert len(hist) == 2
        assert hist[0]["snapshot_date"] == "2026-06-16"  # 新在前
        assert hist[1]["snapshot_date"] == "2026-06-15"

    @pytest.mark.integration
    def test_history_limit_respected(self, storage):
        for i in range(5):
            storage.save_macro_snapshot(_snap(f"2026-06-1{i}", score=50.0 + i))
        hist = storage.get_macro_history(3)
        assert len(hist) == 3
        assert hist[0]["snapshot_date"] == "2026-06-14"  # 取最近 3 天

    @pytest.mark.integration
    def test_date_object_snapshot_key(self, storage):
        # snapshot_date 传 date 对象也能存（内部 isoformat）
        storage.save_macro_snapshot(_snap(snapshot_date=date(2026, 6, 16)))
        hist = storage.get_macro_history(10)
        assert len(hist) == 1
        assert hist[0]["snapshot_date"] == "2026-06-16"
