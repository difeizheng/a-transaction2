"""SQLite存储层，基于SQLAlchemy Core"""
import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Optional, List
import pandas as pd
from sqlalchemy import (
    create_engine, text, MetaData, Table, Column,
    String, Float, Date, DateTime, Integer, Text,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

logger = logging.getLogger(__name__)


def _insert_or_ignore(table, conn, keys, data_iter):
    """pandas to_sql 自定义方法：使用 INSERT OR IGNORE 跳过主键冲突"""
    stmt = sqlite_insert(table.table).prefix_with("OR IGNORE")
    conn.execute(stmt, [dict(zip(keys, row)) for row in data_iter])


def _engine(db_path: str):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(
        f"sqlite:///{db_path}", echo=False,
        connect_args={"timeout": 30},  # 防后台写锁冲突
    )


class Storage:
    def __init__(self, db_path: str):
        self.engine = _engine(db_path)
        self._init_tables()

    def _init_tables(self):
        with self.engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS stock_list (
                    code TEXT PRIMARY KEY,
                    name TEXT,
                    market TEXT,
                    industry TEXT,
                    updated_at TEXT
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS daily_bars (
                    code TEXT,
                    trade_date TEXT,
                    open REAL, high REAL, low REAL, close REAL,
                    volume REAL, amount REAL, pct_chg REAL,
                    PRIMARY KEY (code, trade_date)
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS financial_data (
                    code TEXT,
                    report_date TEXT,
                    pe_ttm REAL, pb REAL, roe REAL,
                    revenue_yoy REAL, profit_yoy REAL, total_mv REAL,
                    PRIMARY KEY (code, report_date)
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS news (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT,
                    content TEXT,
                    source TEXT,
                    publish_time TEXT,
                    related_codes TEXT,
                    sentiment TEXT,
                    analysis TEXT,
                    url TEXT DEFAULT '',
                    created_at TEXT
                )
            """))
            # Migration: add url column if not exists (for existing DBs)
            try:
                conn.execute(text("ALTER TABLE news ADD COLUMN url TEXT DEFAULT ''"))
            except Exception:
                pass  # Column already exists
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS backtest_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_name TEXT,
                    params TEXT,
                    start_date TEXT,
                    end_date TEXT,
                    total_return REAL,
                    annual_return REAL,
                    sharpe REAL,
                    max_drawdown REAL,
                    win_rate REAL,
                    profit_loss_ratio REAL,
                    trades INTEGER,
                    created_at TEXT
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS trade_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT,
                    name TEXT,
                    direction TEXT,  -- BUY/SELL
                    price REAL,
                    quantity INTEGER,
                    amount REAL,
                    commission REAL,
                    status TEXT,     -- PENDING/FILLED/CANCELLED
                    order_date TEXT,
                    fill_date TEXT,
                    note TEXT
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS positions (
                    code TEXT PRIMARY KEY,
                    name TEXT,
                    quantity INTEGER,
                    available INTEGER,  -- T+1后可卖数量
                    cost_price REAL,
                    current_price REAL,
                    market_value REAL,
                    profit_loss REAL,
                    buy_date TEXT,      -- 首次买入日期，用于T+1判定
                    updated_at TEXT
                )
            """))
            # Migration: 为旧库补 buy_date 列（旧表无此列，导致T+1状态无法持久化）
            try:
                conn.execute(text("ALTER TABLE positions ADD COLUMN buy_date TEXT"))
            except Exception:
                pass  # Column already exists
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    cash REAL NOT NULL,
                    initial_cash REAL NOT NULL,
                    updated_at TEXT
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS update_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    update_type TEXT,
                    scope TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    duration_seconds REAL,
                    total_count INTEGER,
                    success_count INTEGER,
                    failure_count INTEGER,
                    failed_codes TEXT,
                    status TEXT
                )
            """))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_daily_bars_trade_date ON daily_bars(trade_date)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_financial_data_code ON financial_data(code)"))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS data_source_routes (
                    data_type TEXT PRIMARY KEY,
                    primary_source TEXT NOT NULL,
                    backup_source TEXT,
                    enabled INTEGER DEFAULT 1
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS data_source_status (
                    source_name TEXT,
                    data_type TEXT,
                    success_count INTEGER DEFAULT 0,
                    failure_count INTEGER DEFAULT 0,
                    last_success_at TEXT,
                    last_failure_at TEXT,
                    last_error TEXT,
                    PRIMARY KEY (source_name, data_type)
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS screening_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL DEFAULT 'running',
                    strategy_keys TEXT NOT NULL,
                    industry TEXT,
                    mode TEXT DEFAULT 'union',
                    top_n INTEGER DEFAULT 20,
                    pool_size INTEGER,
                    processed_count INTEGER DEFAULT 0,
                    selected_count INTEGER DEFAULT 0
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS screening_evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    strategy_key TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT,
                    selected INTEGER NOT NULL DEFAULT 0,
                    score REAL DEFAULT 0,
                    indicators TEXT,
                    conditions TEXT,
                    reason TEXT,
                    trace_log TEXT,
                    UNIQUE(session_id, strategy_key, code),
                    FOREIGN KEY (session_id) REFERENCES screening_sessions(id)
                )
            """))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_eval_session ON screening_evaluations(session_id)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_eval_session_strategy "
                "ON screening_evaluations(session_id, strategy_key)"
            ))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS watchlist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    name TEXT,
                    score REAL DEFAULT 0,
                    signals TEXT,
                    reason TEXT,
                    source TEXT,
                    note TEXT DEFAULT '',
                    added_at TEXT NOT NULL,
                    UNIQUE(code)
                )
            """))
            conn.commit()
            self._init_default_routes(conn)

    # ── stock_list ──────────────────────────────────────────────
    def upsert_stock_list(self, df: pd.DataFrame):
        df["updated_at"] = datetime.now().isoformat()
        df.to_sql("stock_list", self.engine, if_exists="replace", index=False)

    def get_stock_list(self) -> pd.DataFrame:
        return pd.read_sql("SELECT * FROM stock_list", self.engine)

    # ── daily_bars ───────────────────────────────────────────────
    def upsert_daily_bars(self, df: pd.DataFrame):
        if df.empty:
            return
        df["trade_date"] = df["trade_date"].astype(str)
        df.to_sql("daily_bars", self.engine, if_exists="append", index=False,
                  method=_insert_or_ignore)

    def get_daily_bars(self, code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
        sql = "SELECT * FROM daily_bars WHERE code = :code"
        params = {"code": code}
        if start_date:
            sql += " AND trade_date >= :start"
            params["start"] = start_date
        if end_date:
            sql += " AND trade_date <= :end"
            params["end"] = end_date
        sql += " ORDER BY trade_date"
        return pd.read_sql(text(sql), self.engine, params=params)

    def get_latest_bar_date(self, code: str) -> Optional[str]:
        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT MAX(trade_date) FROM daily_bars WHERE code = :code"),
                {"code": code}
            ).fetchone()
        return row[0] if row else None

    # ── financial_data ───────────────────────────────────────────
    def upsert_financial_data(self, df: pd.DataFrame):
        if df.empty:
            return
        df["report_date"] = df["report_date"].astype(str)
        df.to_sql("financial_data", self.engine, if_exists="append", index=False,
                  method=_insert_or_ignore)

    def get_financial_data(self, code: str) -> pd.DataFrame:
        return pd.read_sql(
            text("SELECT * FROM financial_data WHERE code = :code ORDER BY report_date DESC"),
            self.engine, params={"code": code}
        )

    def get_latest_financial(self, codes: list) -> pd.DataFrame:
        """获取多只股票最新一条财务数据"""
        placeholders = ",".join([f":c{i}" for i in range(len(codes))])
        params = {f"c{i}": c for i, c in enumerate(codes)}
        sql = f"""
            SELECT f.* FROM financial_data f
            INNER JOIN (
                SELECT code, MAX(report_date) AS max_date
                FROM financial_data WHERE code IN ({placeholders})
                GROUP BY code
            ) latest ON f.code = latest.code AND f.report_date = latest.max_date
        """
        return pd.read_sql(text(sql), self.engine, params=params)

    # ── news ─────────────────────────────────────────────────────
    def insert_news(self, df: pd.DataFrame):
        """插入新闻，按标题去重（已存在的标题跳过）。"""
        if df.empty:
            return
        df = df.copy()
        df["created_at"] = datetime.now().isoformat()
        if "url" not in df.columns:
            df["url"] = ""
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT title FROM news ORDER BY created_at DESC LIMIT 500"
            )).fetchall()
            existing_titles = {r[0] for r in rows}
            new_rows = df[~df["title"].isin(existing_titles)]
            if not new_rows.empty:
                new_rows.to_sql("news", conn, if_exists="append", index=False)
            conn.commit()

    def update_news_sentiment(self, news_id: int, sentiment: str):
        """更新单条新闻的情绪标签。"""
        with self.engine.connect() as conn:
            conn.execute(text(
                "UPDATE news SET sentiment = :sentiment WHERE id = :id"
            ), {"sentiment": sentiment, "id": news_id})
            conn.commit()

    def delete_all_news(self):
        """清空新闻表。"""
        with self.engine.connect() as conn:
            conn.execute(text("DELETE FROM news"))
            conn.commit()

    def get_news(self, code: str = None, limit: int = 50) -> pd.DataFrame:
        if code:
            sql = text("SELECT * FROM news WHERE related_codes LIKE :code ORDER BY publish_time DESC LIMIT :limit")
            return pd.read_sql(sql, self.engine, params={"code": f"%{code}%", "limit": limit})
        return pd.read_sql(
            text("SELECT * FROM news ORDER BY publish_time DESC LIMIT :limit"),
            self.engine, params={"limit": limit}
        )

    # ── backtest_results ─────────────────────────────────────────
    def save_backtest_result(self, result: dict):
        result["created_at"] = datetime.now().isoformat()
        pd.DataFrame([result]).to_sql("backtest_results", self.engine, if_exists="append", index=False)

    def get_backtest_results(self) -> pd.DataFrame:
        return pd.read_sql("SELECT * FROM backtest_results ORDER BY created_at DESC", self.engine)

    # ── trade_orders / positions ──────────────────────────────────
    def save_order(self, order: dict):
        pd.DataFrame([order]).to_sql("trade_orders", self.engine, if_exists="append", index=False)

    def get_orders(self, status: str = None) -> pd.DataFrame:
        if status:
            return pd.read_sql(
                text("SELECT * FROM trade_orders WHERE status = :s ORDER BY order_date DESC"),
                self.engine, params={"s": status}
            )
        return pd.read_sql("SELECT * FROM trade_orders ORDER BY order_date DESC", self.engine)

    def upsert_position(self, pos: dict):
        """按 code 主键 upsert 单条持仓。
        注意：绝不能用 pandas to_sql if_exists="replace"——那会 DROP 整张表只留最后一行。
        """
        pos = dict(pos)  # 不修改调用方传入的 dict
        pos.setdefault("buy_date", None)
        pos["updated_at"] = datetime.now().isoformat()
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO positions
                    (code, name, quantity, available, cost_price, current_price,
                     market_value, profit_loss, buy_date, updated_at)
                VALUES
                    (:code, :name, :quantity, :available, :cost_price, :current_price,
                     :market_value, :profit_loss, :buy_date, :updated_at)
                ON CONFLICT(code) DO UPDATE SET
                    name          = excluded.name,
                    quantity      = excluded.quantity,
                    available     = excluded.available,
                    cost_price    = excluded.cost_price,
                    current_price = excluded.current_price,
                    market_value  = excluded.market_value,
                    profit_loss   = excluded.profit_loss,
                    buy_date      = excluded.buy_date,
                    updated_at    = excluded.updated_at
            """), pos)
            conn.commit()

    # ── account（模拟账户资金，单行表 id 固定为 1） ──────────────
    def get_account(self) -> dict:
        """读取账户资金。首次启动（无记录）返回空 dict。"""
        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT cash, initial_cash FROM account WHERE id = 1")
            ).fetchone()
        return dict(row._mapping) if row else {}

    def upsert_account(self, cash: float, initial_cash: float):
        now = datetime.now().isoformat()
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO account (id, cash, initial_cash, updated_at)
                VALUES (1, :cash, :initial_cash, :now)
                ON CONFLICT(id) DO UPDATE SET
                    cash          = excluded.cash,
                    initial_cash  = excluded.initial_cash,
                    updated_at    = excluded.updated_at
            """), {"cash": cash, "initial_cash": initial_cash, "now": now})
            conn.commit()

    def get_positions(self) -> pd.DataFrame:
        return pd.read_sql("SELECT * FROM positions", self.engine)

    def delete_position(self, code: str):
        with self.engine.connect() as conn:
            conn.execute(text("DELETE FROM positions WHERE code = :code"), {"code": code})
            conn.commit()

    # ── update_log ───────────────────────────────────────────────
    def insert_update_log(self, log: dict) -> int:
        log["started_at"] = log.get("started_at", datetime.now().isoformat())
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO update_log (update_type, scope, started_at, status, total_count)
                VALUES (:update_type, :scope, :started_at, :status, :total_count)
            """), log)
            row = conn.execute(text("SELECT last_insert_rowid()")).fetchone()
            conn.commit()
        return row[0]

    def update_update_log(self, log_id: int, updates: dict):
        sets = ", ".join(f"{k} = :{k}" for k in updates)
        updates["log_id"] = log_id
        with self.engine.connect() as conn:
            conn.execute(text(f"UPDATE update_log SET {sets} WHERE id = :log_id"), updates)
            conn.commit()

    def get_update_logs(self, limit: int = 50) -> pd.DataFrame:
        return pd.read_sql(
            text("SELECT * FROM update_log ORDER BY started_at DESC LIMIT :limit"),
            self.engine, params={"limit": limit}
        )

    # ── 统计 / 覆盖率 ─────────────────────────────────────────────
    def get_data_overview(self) -> dict:
        with self.engine.connect() as conn:
            total = conn.execute(text("SELECT COUNT(*) FROM stock_list")).scalar() or 0
            bars_stocks = conn.execute(text("SELECT COUNT(DISTINCT code) FROM daily_bars")).scalar() or 0
            fin_stocks = conn.execute(text("SELECT COUNT(DISTINCT code) FROM financial_data")).scalar() or 0
            total_bars = conn.execute(text("SELECT COUNT(*) FROM daily_bars")).scalar() or 0
            total_fin = conn.execute(text("SELECT COUNT(*) FROM financial_data")).scalar() or 0
            total_news = conn.execute(text("SELECT COUNT(*) FROM news")).scalar() or 0
            latest_bar = conn.execute(text("SELECT MAX(trade_date) FROM daily_bars")).scalar()
            latest_fin = conn.execute(text("SELECT MAX(report_date) FROM financial_data")).scalar()
        db_path = str(self.engine.url).replace("sqlite:///", "")
        db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
        return {
            "total_stocks": total,
            "stocks_with_bars": bars_stocks,
            "stocks_with_financial": fin_stocks,
            "bars_coverage_pct": round(bars_stocks / total * 100, 1) if total else 0,
            "financial_coverage_pct": round(fin_stocks / total * 100, 1) if total else 0,
            "total_bars": total_bars,
            "total_financial": total_fin,
            "total_news": total_news,
            "latest_bar_date": latest_bar,
            "latest_financial_date": latest_fin,
            "db_size_bytes": db_size,
        }

    def get_bars_coverage(self) -> pd.DataFrame:
        sql = """
            SELECT s.code, s.name, s.market,
                   MIN(d.trade_date) AS first_date,
                   MAX(d.trade_date) AS last_date,
                   COUNT(d.trade_date) AS bar_count
            FROM stock_list s
            LEFT JOIN daily_bars d ON s.code = d.code
            GROUP BY s.code, s.name, s.market
        """
        return pd.read_sql(sql, self.engine)

    def get_financial_coverage(self) -> pd.DataFrame:
        sql = """
            SELECT s.code, s.name,
                   COUNT(f.report_date) AS fin_count,
                   MAX(f.report_date) AS latest_report
            FROM stock_list s
            LEFT JOIN financial_data f ON s.code = f.code
            GROUP BY s.code, s.name
        """
        return pd.read_sql(sql, self.engine)

    def get_stale_stocks(self, threshold_date: str) -> pd.DataFrame:
        sql = """
            SELECT s.code, s.name, MAX(d.trade_date) AS last_date
            FROM stock_list s
            LEFT JOIN daily_bars d ON s.code = d.code
            GROUP BY s.code, s.name
            HAVING last_date < :threshold OR last_date IS NULL
        """
        return pd.read_sql(text(sql), self.engine, params={"threshold": threshold_date})

    def get_stock_detail(self, code: str) -> dict:
        with self.engine.connect() as conn:
            info = conn.execute(
                text("SELECT * FROM stock_list WHERE code = :code"), {"code": code}
            ).fetchone()
            bar_stats = conn.execute(text("""
                SELECT COUNT(*) AS bar_count, MIN(trade_date) AS first_date, MAX(trade_date) AS last_date
                FROM daily_bars WHERE code = :code
            """), {"code": code}).fetchone()
            fin_stats = conn.execute(text("""
                SELECT COUNT(*) AS fin_count, MAX(report_date) AS latest_report
                FROM financial_data WHERE code = :code
            """), {"code": code}).fetchone()
            news_count = conn.execute(text("""
                SELECT COUNT(*) FROM news WHERE related_codes LIKE :code
            """), {"code": f"%{code}%"}).scalar() or 0
        return {
            "info": dict(info._mapping) if info else {},
            "bar_count": bar_stats[0] if bar_stats else 0,
            "first_date": bar_stats[1] if bar_stats else None,
            "last_date": bar_stats[2] if bar_stats else None,
            "fin_count": fin_stats[0] if fin_stats else 0,
            "latest_report": fin_stats[1] if fin_stats else None,
            "news_count": news_count,
        }

    def delete_stock_data(self, code: str, tables: List[str] = None):
        if tables is None:
            tables = ["daily_bars", "financial_data"]
        with self.engine.connect() as conn:
            for table in tables:
                if table in ("daily_bars", "financial_data"):
                    conn.execute(text(f"DELETE FROM {table} WHERE code = :code"), {"code": code})
            conn.commit()

    # ── data_source_routes ────────────────────────────────────────
    _DEFAULT_ROUTES = [
        {"data_type": "stock_list",      "primary_source": "tushare",  "backup_source": "akshare"},
        {"data_type": "daily_bars",      "primary_source": "tushare",  "backup_source": "akshare"},
        {"data_type": "financial",       "primary_source": "tushare",  "backup_source": "akshare"},
        {"data_type": "realtime",        "primary_source": "tencent",  "backup_source": "akshare"},
        {"data_type": "industry_list",   "primary_source": "tushare",  "backup_source": "akshare"},
        {"data_type": "industry_stocks", "primary_source": "tushare",  "backup_source": "akshare"},
        {"data_type": "announcements",   "primary_source": "tushare",  "backup_source": "cninfo"},
        {"data_type": "news",            "primary_source": "akshare",  "backup_source": "tushare"},
    ]

    def _init_default_routes(self, conn):
        """首次启动时写入默认路由（已存在则跳过）"""
        for route in self._DEFAULT_ROUTES:
            conn.execute(text("""
                INSERT OR IGNORE INTO data_source_routes (data_type, primary_source, backup_source, enabled)
                VALUES (:data_type, :primary_source, :backup_source, 1)
            """), route)
        conn.commit()

    def get_source_routes(self) -> list:
        with self.engine.connect() as conn:
            rows = conn.execute(text("SELECT * FROM data_source_routes ORDER BY data_type")).fetchall()
        return [dict(r._mapping) for r in rows]

    def upsert_source_route(self, data_type: str, primary_source: str,
                            backup_source: str = None, enabled: int = 1):
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO data_source_routes (data_type, primary_source, backup_source, enabled)
                VALUES (:data_type, :primary_source, :backup_source, :enabled)
                ON CONFLICT(data_type) DO UPDATE SET
                    primary_source = excluded.primary_source,
                    backup_source  = excluded.backup_source,
                    enabled        = excluded.enabled
            """), {"data_type": data_type, "primary_source": primary_source,
                   "backup_source": backup_source, "enabled": enabled})
            conn.commit()

    # ── data_source_status ────────────────────────────────────────
    def get_source_status(self) -> list:
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT * FROM data_source_status ORDER BY source_name, data_type"
            )).fetchall()
        return [dict(r._mapping) for r in rows]

    def record_source_success(self, source_name: str, data_type: str):
        now = datetime.now().isoformat()
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO data_source_status (source_name, data_type, success_count, last_success_at)
                VALUES (:src, :dt, 1, :now)
                ON CONFLICT(source_name, data_type) DO UPDATE SET
                    success_count   = success_count + 1,
                    last_success_at = :now
            """), {"src": source_name, "dt": data_type, "now": now})
            conn.commit()

    def record_source_failure(self, source_name: str, data_type: str, error: str = ""):
        now = datetime.now().isoformat()
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO data_source_status (source_name, data_type, failure_count, last_failure_at, last_error)
                VALUES (:src, :dt, 1, :now, :err)
                ON CONFLICT(source_name, data_type) DO UPDATE SET
                    failure_count   = failure_count + 1,
                    last_failure_at = :now,
                    last_error      = :err
            """), {"src": source_name, "dt": data_type, "now": now, "err": error[:500]})
            conn.commit()

    def reset_source_status(self, source_name: str = None, data_type: str = None):
        with self.engine.connect() as conn:
            if source_name and data_type:
                conn.execute(text(
                    "DELETE FROM data_source_status WHERE source_name=:src AND data_type=:dt"
                ), {"src": source_name, "dt": data_type})
            elif source_name:
                conn.execute(text(
                    "DELETE FROM data_source_status WHERE source_name=:src"
                ), {"src": source_name})
            else:
                conn.execute(text("DELETE FROM data_source_status"))
            conn.commit()

    # ── screening_sessions / screening_evaluations ────────────────

    def create_screening_session(self, params: dict) -> int:
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                INSERT INTO screening_sessions
                    (started_at, status, strategy_keys, industry, mode, top_n, pool_size)
                VALUES (:started_at, 'running', :strategy_keys, :industry, :mode, :top_n, :pool_size)
            """), {
                "started_at": datetime.now().isoformat(),
                "strategy_keys": json.dumps(params.get("strategy_keys", [])),
                "industry": params.get("industry"),
                "mode": params.get("mode", "union"),
                "top_n": params.get("top_n", 20),
                "pool_size": params.get("pool_size"),
            })
            conn.commit()
            return result.lastrowid

    def update_screening_session(self, session_id: int, updates: dict):
        if not updates:
            return
        sets = ", ".join(f"{k} = :{k}" for k in updates)
        updates["session_id"] = session_id
        with self.engine.connect() as conn:
            conn.execute(text(f"UPDATE screening_sessions SET {sets} WHERE id = :session_id"), updates)
            conn.commit()

    def insert_evaluation(self, ev: dict):
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT OR IGNORE INTO screening_evaluations
                    (session_id, strategy_key, code, name, selected, score,
                     indicators, conditions, reason, trace_log)
                VALUES
                    (:session_id, :strategy_key, :code, :name, :selected, :score,
                     :indicators, :conditions, :reason, :trace_log)
            """), ev)
            conn.commit()

    def insert_evaluations_batch(self, ev_list: list):
        if not ev_list:
            return
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT OR IGNORE INTO screening_evaluations
                    (session_id, strategy_key, code, name, selected, score,
                     indicators, conditions, reason, trace_log)
                VALUES
                    (:session_id, :strategy_key, :code, :name, :selected, :score,
                     :indicators, :conditions, :reason, :trace_log)
            """), ev_list)
            conn.commit()

    def get_session_evaluations(self, session_id: int, strategy_key: str = None,
                                selected_only: bool = False) -> list:
        sql = "SELECT * FROM screening_evaluations WHERE session_id = :sid"
        params: dict = {"sid": session_id}
        if strategy_key:
            sql += " AND strategy_key = :sk"
            params["sk"] = strategy_key
        if selected_only:
            sql += " AND selected = 1"
        sql += " ORDER BY selected DESC, score DESC"
        with self.engine.connect() as conn:
            rows = conn.execute(text(sql), params).fetchall()
        return [dict(r._mapping) for r in rows]

    def get_session_resume_point(self, session_id: int, strategy_key: str) -> set:
        """返回该 session + strategy 已处理的 code 集合，用于断点续跑。"""
        with self.engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT code FROM screening_evaluations
                WHERE session_id = :sid AND strategy_key = :sk
            """), {"sid": session_id, "sk": strategy_key}).fetchall()
        return {r[0] for r in rows}

    def get_fully_processed_codes(self, session_id: int, num_strategies: int) -> set:
        """返回已完成所有策略评估的 code 集合（用于股票优先模式的断点续跑）。"""
        with self.engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT code FROM screening_evaluations
                WHERE session_id = :sid
                GROUP BY code
                HAVING COUNT(DISTINCT strategy_key) >= :n
            """), {"sid": session_id, "n": num_strategies}).fetchall()
        return {r[0] for r in rows}

    def get_recent_sessions(self, limit: int = 20) -> list:
        with self.engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT s.id, s.started_at, s.finished_at, s.status, s.strategy_keys,
                       s.industry, s.mode, s.top_n, s.pool_size, s.processed_count,
                       COALESCE(NULLIF(s.selected_count, 0),
                           CASE s.mode
                               WHEN 'intersect' THEN (
                                   SELECT COUNT(*) FROM (
                                       SELECT e.code
                                       FROM screening_evaluations e
                                       WHERE e.session_id = s.id AND e.selected = 1
                                       GROUP BY e.code
                                       HAVING COUNT(DISTINCT e.strategy_key) = json_array_length(s.strategy_keys)
                                   )
                               )
                               ELSE (
                                   SELECT COUNT(DISTINCT e.code)
                                   FROM screening_evaluations e
                                   WHERE e.session_id = s.id AND e.selected = 1
                               )
                           END
                       ) AS selected_count
                FROM screening_sessions s
                ORDER BY s.started_at DESC LIMIT :limit
            """), {"limit": limit}).fetchall()
        return [dict(r._mapping) for r in rows]

    def get_session(self, session_id: int) -> dict:
        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM screening_sessions WHERE id = :sid"),
                {"sid": session_id}
            ).fetchone()
        return dict(row._mapping) if row else {}

    def delete_screening_session(self, session_id: int):
        """删除筛选会话及其所有评估记录。"""
        with self.engine.connect() as conn:
            conn.execute(text(
                "DELETE FROM screening_evaluations WHERE session_id = :sid"
            ), {"sid": session_id})
            conn.execute(text(
                "DELETE FROM screening_sessions WHERE id = :sid"
            ), {"sid": session_id})
            conn.commit()

    # ── watchlist ────────────────────────────────────────────────

    def add_to_watchlist(self, item: dict):
        """添加或更新自选股（UNIQUE(code)，重复添加更新数据）。"""
        item.setdefault("added_at", datetime.now().isoformat())
        item.setdefault("note", "")
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO watchlist (code, name, score, signals, reason, source, note, added_at)
                VALUES (:code, :name, :score, :signals, :reason, :source, :note, :added_at)
                ON CONFLICT(code) DO UPDATE SET
                    name     = excluded.name,
                    score    = excluded.score,
                    signals  = excluded.signals,
                    reason   = excluded.reason,
                    source   = excluded.source,
                    added_at = excluded.added_at
            """), item)
            conn.commit()

    def batch_add_to_watchlist(self, items: list):
        if not items:
            return
        now = datetime.now().isoformat()
        with self.engine.connect() as conn:
            for item in items:
                item.setdefault("added_at", now)
                item.setdefault("note", "")
                conn.execute(text("""
                    INSERT INTO watchlist (code, name, score, signals, reason, source, note, added_at)
                    VALUES (:code, :name, :score, :signals, :reason, :source, :note, :added_at)
                    ON CONFLICT(code) DO UPDATE SET
                        name     = excluded.name,
                        score    = excluded.score,
                        signals  = excluded.signals,
                        reason   = excluded.reason,
                        source   = excluded.source,
                        added_at = excluded.added_at
                """), item)
            conn.commit()

    def get_watchlist(self) -> list:
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT * FROM watchlist ORDER BY added_at DESC"
            )).fetchall()
        return [dict(r._mapping) for r in rows]

    def update_watchlist_note(self, code: str, note: str):
        with self.engine.connect() as conn:
            conn.execute(text(
                "UPDATE watchlist SET note = :note WHERE code = :code"
            ), {"code": code, "note": note})
            conn.commit()

    def remove_from_watchlist(self, code: str):
        with self.engine.connect() as conn:
            conn.execute(text("DELETE FROM watchlist WHERE code = :code"), {"code": code})
            conn.commit()

    def clear_watchlist(self):
        """清空全部自选股。"""
        with self.engine.connect() as conn:
            conn.execute(text("DELETE FROM watchlist"))
            conn.commit()
