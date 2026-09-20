"""SQLite存储层，基于SQLAlchemy Core"""
import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Optional, List
import pandas as pd
from sqlalchemy import (
    create_engine, text, MetaData, Table, Column, event,
    String, Float, Date, DateTime, Integer, Text,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

logger = logging.getLogger(__name__)


def _insert_or_ignore(table, conn, keys, data_iter):
    """pandas to_sql 自定义方法：使用 INSERT OR IGNORE 跳过主键冲突"""
    stmt = sqlite_insert(table.table).prefix_with("OR IGNORE")
    conn.execute(stmt, [dict(zip(keys, row)) for row in data_iter])


def _make_upsert_method(pk_cols):
    """生成 pandas to_sql ``method``：主键冲突时**更新**所有非主键列（而非 IGNORE）。

    用于 daily_bars：前复权(qfq)历史价会随除权分红整体重算，INSERT OR IGNORE
    会保留陈旧价格 → 除权日前后虚假跳空、技术信号失真。改为 ON CONFLICT DO
    UPDATE，使全量重拉（``force_refresh_stock`` / ``batch_update_bars_v2(full)``）
    能刷新历史复权价。

    显式传 ``pk_cols``（列名字符串）而非依赖 pandas 反射 Table 的 primary_key
    ——反射在 append 模式下不可靠（拿不到 PK），会导致 ON CONFLICT 子句丢失、
    退化为普通 INSERT 在重复键上报 UNIQUE 冲突。
    """
    def _method(table, conn, keys, data_iter):
        data = [dict(zip(keys, row)) for row in data_iter]
        if not data:
            return
        sa_table = table.table
        stmt = sqlite_insert(sa_table).values(data)
        # excluded 命名空间引用「新值」（ON CONFLICT DO UPDATE 标准写法）
        update_cols = {c: getattr(stmt.excluded, c) for c in keys if c not in pk_cols}
        if update_cols:
            stmt = stmt.on_conflict_do_update(index_elements=pk_cols, set_=update_cols)
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=pk_cols)
        conn.execute(stmt)

    return _method


def _engine(db_path: str):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}", echo=False,
        connect_args={"timeout": 30},  # 防后台写锁冲突
    )
    # WAL 模式：读不阻塞写。后台批量更新（batch_update_bars_v2）长时间持写锁时，
    # 前端读不再被阻塞 30s 后抛 database is locked。WAL 为数据库级持久设置；
    # synchronous=NORMAL 为连接级，每个新连接都需设，故用 connect 事件监听器。
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, conn_record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    return engine


# 历史迁移：每条对应一次 schema 演进。新库 CREATE TABLE 已含这些列（迁移对新库
# 是 no-op，靠 _column_exists 幂等预检跳过）；旧库靠迁移补列。追加新列只需在
def _rebuild_positions_with_pk(conn) -> None:
    """v8 专用迁移：旧库 positions 表无 PRIMARY KEY（历史遗留建表），导致
    upsert_position 的 ON CONFLICT(code) 每次抛 OperationalError，持仓现价/
    T+1 解锁静默不落库。SQLite 不支持补加约束，只能建新表拷数据换名。
    幂等：已有 PK 直接返回。
    """
    cols = conn.execute(text("PRAGMA table_info(positions)")).fetchall()
    if not cols or any(c[5] for c in cols):  # c[5] 是 pk 序号
        return
    conn.execute(text("""
        CREATE TABLE positions_new (
            code TEXT PRIMARY KEY,
            name TEXT,
            quantity INTEGER,
            available INTEGER,
            cost_price REAL,
            current_price REAL,
            market_value REAL,
            profit_loss REAL,
            buy_date TEXT,
            updated_at TEXT
        )
    """))
    # 无约束旧表可能有重复 code：按 updated_at 升序写入，INSERT OR REPLACE 让最新行胜出
    conn.execute(text("""
        INSERT OR REPLACE INTO positions_new
        SELECT code, name, quantity, available, cost_price, current_price,
               market_value, profit_loss, buy_date, updated_at
        FROM positions ORDER BY updated_at
    """))
    conn.execute(text("DROP TABLE positions"))
    conn.execute(text("ALTER TABLE positions_new RENAME TO positions"))


# 列表末尾加一条 (version, 描述, ALTER SQL 或可调用迁移)，_run_migrations 会自动按序应用。
# 字符串 SQL 走「列存在预检 + ALTER」路径（幂等）；可调用对象用于 ALTER 表达不了的
# 结构性迁移（如 v8 表重建），需自行保证幂等。
_MIGRATIONS = [
    (1, "daily_bars 复权因子(adj_factor)",     "ALTER TABLE daily_bars ADD COLUMN adj_factor REAL"),
    (2, "financial_data 披露日(ann_date,PIT)", "ALTER TABLE financial_data ADD COLUMN ann_date TEXT"),
    (3, "news URL",                             "ALTER TABLE news ADD COLUMN url TEXT DEFAULT ''"),
    (4, "positions 买入日(buy_date,T+1)",       "ALTER TABLE positions ADD COLUMN buy_date TEXT"),
    (5, "account 峰值(peak_value,回撤)",        "ALTER TABLE account ADD COLUMN peak_value REAL"),
    (6, "backtest_results 逐笔交易(trades_detail)", "ALTER TABLE backtest_results ADD COLUMN trades_detail TEXT"),
    (7, "watchlist 标签(tags,分组)",            "ALTER TABLE watchlist ADD COLUMN tags TEXT DEFAULT ''"),
    (8, "positions 主键重建(code PRIMARY KEY)", _rebuild_positions_with_pk),
    (9, "价格提醒(price_alerts)", """
        CREATE TABLE IF NOT EXISTS price_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            name TEXT,
            direction TEXT NOT NULL,       -- above / below
            target_price REAL NOT NULL,
            note TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            triggered_at TEXT,             -- 触发时刻（本地最新收盘越线）
            created_at TEXT
        )"""),
]


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
                    adj_factor REAL,     -- 复权因子（tushare 可得，akshare 暂为 NULL）；为 raw+factor 复权重构预留
                    PRIMARY KEY (code, trade_date)
                )
            """))
            # adj_factor 列：新库由 CREATE TABLE 定义；旧库由 _run_migrations v1 补。
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS financial_data (
                    code TEXT,
                    report_date TEXT,
                    ann_date TEXT,                       -- 披露日（PIT：决策日只能用 ann_date<=决策日 的财报）；akshare 无则 NULL
                    pe_ttm REAL, pb REAL, roe REAL,
                    revenue_yoy REAL, profit_yoy REAL, total_mv REAL,
                    PRIMARY KEY (code, report_date)
                )
            """))
            # ann_date 列：新库由 CREATE TABLE 定义；旧库由 _run_migrations v2 补。
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
            # url 列：新库由 CREATE TABLE 定义；旧库由 _run_migrations v3 补。
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
            # buy_date 列：新库由 CREATE TABLE 定义；旧库由 _run_migrations v4 补。
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    cash REAL NOT NULL,
                    initial_cash REAL NOT NULL,
                    peak_value REAL,                       -- 历史最高组合净值（high-water mark），用于回撤计算
                    updated_at TEXT
                )
            """))
            # peak_value 列：新库由 CREATE TABLE 定义；旧库由 _run_migrations v5 补。
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
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS market_sentiment (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_date TEXT NOT NULL UNIQUE,   -- 末日 trade_date 的 ISO 日期，按交易日去重
                    snapshot_time TEXT NOT NULL,          -- 运行时刻 ISO 时间戳（信息性）
                    temperature REAL NOT NULL,
                    label TEXT NOT NULL,                  -- bullish/neutral/bearish
                    index_moves_json TEXT,
                    sector_summary_json TEXT,
                    summary TEXT,                         -- LLM 定性总结
                    key_events_json TEXT,
                    created_at TEXT NOT NULL
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS macro_snapshot (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_date TEXT NOT NULL UNIQUE,   -- 各指标最新 as_of 的 ISO 日期，按日去重
                    snapshot_time TEXT NOT NULL,          -- 运行时刻 ISO 时间戳（信息性）
                    score REAL NOT NULL,                  -- 0-100 宏观态势分（纯函数算出）
                    label TEXT NOT NULL,                  -- bullish/neutral/bearish
                    stance REAL,                          -- [-1,1] 原始态势
                    components_json TEXT,                 -- 四支柱 {signal, weight}
                    indicators_json TEXT,                 -- 各指标 {signal, value}
                    summary TEXT,                         -- LLM 综合定性
                    key_risks_json TEXT,                  -- LLM 关键风险列表
                    created_at TEXT NOT NULL
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS execution_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,             -- 运行时刻（= report.timestamp）
                    mode TEXT,                            -- signal / auto
                    drawdown_pct REAL,
                    deescalation_tier INTEGER,
                    paused_reason TEXT,
                    n_candidates INTEGER,
                    n_buy_suggestions INTEGER,
                    n_sell_suggestions INTEGER,
                    n_executed INTEGER,
                    n_blocked INTEGER,
                    n_errors INTEGER,
                    strategy_keys_json TEXT,              -- 输入策略键
                    report_json TEXT NOT NULL             -- 完整 ExecutionReport JSON（可审计回放）
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS llm_call_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    provider TEXT,                          -- claude / openai
                    model TEXT,
                    endpoint TEXT,                          -- call / chat / summarize_market ...
                    prompt_excerpt TEXT,                    -- 输入摘要（审计用，不存全文省空间）
                    raw_response TEXT,                      -- 原始输出（复盘幻觉用）
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    latency_ms INTEGER,
                    success INTEGER DEFAULT 1,
                    error TEXT
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    date TEXT PRIMARY KEY,   -- YYYY-MM-DD，每日一条
                    total_value REAL,
                    cash REAL,
                    market_value REAL,
                    updated_at TEXT
                )
            """))
            self._run_migrations(conn)
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
        """upsert 日K线：主键冲突时**更新**（刷新陈旧复权价），不再 IGNORE。

        见 ``_make_upsert_method`` 说明——这是修复 qfq 复权漂移的关键。
        """
        if df.empty:
            return
        df["trade_date"] = df["trade_date"].astype(str)
        df.to_sql("daily_bars", self.engine, if_exists="append", index=False,
                  method=_make_upsert_method(["code", "trade_date"]))

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

    def get_global_latest_bar_date(self) -> Optional[str]:
        """全库最新K线日期（新鲜度徽标用，比 get_data_overview 轻量）。"""
        with self.engine.connect() as conn:
            return conn.execute(
                text("SELECT MAX(trade_date) FROM daily_bars")
            ).scalar()

    # ── financial_data ───────────────────────────────────────────
    def upsert_financial_data(self, df: pd.DataFrame):
        """upsert 财务数据：主键冲突时**更新**（刷新 ann_date 等字段），不再 IGNORE。

        用 update-existing 而非 IGNORE：ann_date（披露日）可能在重拉时被补全/修正，
        IGNORE 会保留旧 NULL → 财务 point-in-time 过滤失效。pe_ttm/pb/total_mv 仍由
        fetcher 只填在最新 report_date 行（历史行 None），update-existing 不破坏该语义。
        """
        if df.empty:
            return
        df["report_date"] = df["report_date"].astype(str)
        df.to_sql("financial_data", self.engine, if_exists="append", index=False,
                  method=_make_upsert_method(["code", "report_date"]))

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

    # ── market_sentiment（市场情绪温度历史）──────────────────────
    def save_market_sentiment(self, snapshot: dict) -> None:
        """按 snapshot_date upsert（同交易日多次运行，后写覆盖前写）。

        JSON 列用 json.dumps(ensure_ascii=False) 序列化；snapshot_date 若是
        date/datetime 先 isoformat()。镜像 upsert_position 的写法（不改调用方 dict）。
        """
        rec = dict(snapshot)
        sd = rec.get("snapshot_date")
        if isinstance(sd, (date, datetime)):
            rec["snapshot_date"] = sd.isoformat()
        rec["snapshot_time"] = rec.get("snapshot_time") or datetime.now().isoformat()
        rec["created_at"] = rec.get("created_at") or datetime.now().isoformat()
        for k in ("index_moves_json", "sector_summary_json", "key_events_json"):
            v = rec.get(k)
            if v is not None and not isinstance(v, str):
                rec[k] = json.dumps(v, ensure_ascii=False)
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO market_sentiment
                    (snapshot_date, snapshot_time, temperature, label,
                     index_moves_json, sector_summary_json, summary, key_events_json, created_at)
                VALUES
                    (:snapshot_date, :snapshot_time, :temperature, :label,
                     :index_moves_json, :sector_summary_json, :summary, :key_events_json, :created_at)
                ON CONFLICT(snapshot_date) DO UPDATE SET
                    snapshot_time       = excluded.snapshot_time,
                    temperature         = excluded.temperature,
                    label               = excluded.label,
                    index_moves_json    = excluded.index_moves_json,
                    sector_summary_json = excluded.sector_summary_json,
                    summary             = excluded.summary,
                    key_events_json     = excluded.key_events_json,
                    created_at          = excluded.created_at
            """), rec)
            conn.commit()

    def get_market_sentiment_history(self, limit: int = 30) -> list:
        """返回最近 N 条情绪快照（新在前），JSON 列已反序列化为对象。"""
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT * FROM market_sentiment ORDER BY snapshot_date DESC LIMIT :limit"
            ), {"limit": limit}).fetchall()
        result = []
        for r in rows:
            d = dict(r._mapping)
            for k in ("index_moves_json", "sector_summary_json", "key_events_json"):
                raw = d.get(k)
                if isinstance(raw, str) and raw:
                    try:
                        d[k] = json.loads(raw)
                    except (ValueError, TypeError):
                        pass
            result.append(d)
        return result


    # ── macro_snapshot（宏观态势历史）────────────────────────────
    def save_macro_snapshot(self, snapshot: dict) -> None:
        """按 snapshot_date upsert（同日多次运行，后写覆盖前写）。

        JSON 列用 json.dumps(ensure_ascii=False) 序列化；snapshot_date 若是
        date/datetime 先 isoformat()。镜像 save_market_sentiment 的写法（不改调用方 dict）。
        """
        rec = dict(snapshot)
        sd = rec.get("snapshot_date")
        if isinstance(sd, (date, datetime)):
            rec["snapshot_date"] = sd.isoformat()
        rec["snapshot_time"] = rec.get("snapshot_time") or datetime.now().isoformat()
        rec["created_at"] = rec.get("created_at") or datetime.now().isoformat()
        for k in ("components_json", "indicators_json", "key_risks_json"):
            v = rec.get(k)
            if v is not None and not isinstance(v, str):
                rec[k] = json.dumps(v, ensure_ascii=False)
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO macro_snapshot
                    (snapshot_date, snapshot_time, score, label, stance,
                     components_json, indicators_json, summary, key_risks_json, created_at)
                VALUES
                    (:snapshot_date, :snapshot_time, :score, :label, :stance,
                     :components_json, :indicators_json, :summary, :key_risks_json, :created_at)
                ON CONFLICT(snapshot_date) DO UPDATE SET
                    snapshot_time     = excluded.snapshot_time,
                    score             = excluded.score,
                    label             = excluded.label,
                    stance            = excluded.stance,
                    components_json   = excluded.components_json,
                    indicators_json   = excluded.indicators_json,
                    summary           = excluded.summary,
                    key_risks_json    = excluded.key_risks_json,
                    created_at        = excluded.created_at
            """), rec)
            conn.commit()

    def get_macro_history(self, limit: int = 60) -> list:
        """返回最近 N 条宏观态势快照（新在前），JSON 列已反序列化为对象。"""
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT * FROM macro_snapshot ORDER BY snapshot_date DESC LIMIT :limit"
            ), {"limit": limit}).fetchall()
        result = []
        for r in rows:
            d = dict(r._mapping)
            for k in ("components_json", "indicators_json", "key_risks_json"):
                raw = d.get(k)
                if isinstance(raw, str) and raw:
                    try:
                        d[k] = json.loads(raw)
                    except (ValueError, TypeError):
                        pass
            result.append(d)
        return result


    # ── execution_reports（信号生成器运行报告，可审计）──────────────
    def save_execution_report(self, report: dict) -> int:
        """落库一份 ExecutionReport（可审计回放）。

        ``report`` 是 ``ExecutionReport`` 的 dict 形态（含 timestamp/mode/各 list 字段）。
        list/dict 字段整体序列化为 ``report_json``；同时抽取关键计数为独立列，便于
        不解析 JSON 也能快速筛选/统计。返回自增 id。
        """
        rec = {
            "created_at": report.get("timestamp") or datetime.now().isoformat(),
            "mode": report.get("mode"),
            "drawdown_pct": report.get("drawdown_pct"),
            "deescalation_tier": report.get("deescalation_tier"),
            "paused_reason": report.get("paused_reason") or "",
            "n_candidates": len(report.get("strategy_candidates", [])),
            "n_buy_suggestions": len(report.get("buy_suggestions", [])),
            "n_sell_suggestions": len(report.get("sell_suggestions", [])),
            "n_executed": len(report.get("executed_orders", []))
                          + len(report.get("stop_loss_sells", []))
                          + len(report.get("take_profit_sells", []))
                          + len(report.get("deescalation_sells", [])),
            "n_blocked": len(report.get("risk_blocked", [])),
            "n_errors": len(report.get("errors", [])),
            "strategy_keys_json": json.dumps(report.get("strategy_keys", []),
                                              ensure_ascii=False) if isinstance(report.get("strategy_keys"), list)
                                  else (report.get("strategy_keys") or ""),
            "report_json": json.dumps(report, ensure_ascii=False, default=str),
        }
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                INSERT INTO execution_reports
                    (created_at, mode, drawdown_pct, deescalation_tier, paused_reason,
                     n_candidates, n_buy_suggestions, n_sell_suggestions, n_executed,
                     n_blocked, n_errors, strategy_keys_json, report_json)
                VALUES
                    (:created_at, :mode, :drawdown_pct, :deescalation_tier, :paused_reason,
                     :n_candidates, :n_buy_suggestions, :n_sell_suggestions, :n_executed,
                     :n_blocked, :n_errors, :strategy_keys_json, :report_json)
            """), rec)
            conn.commit()
            return result.lastrowid

    def get_execution_reports(self, limit: int = 20) -> list:
        """返回最近 N 条运行报告（新在前），``report_json`` 已反序列化回 dict。"""
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT * FROM execution_reports ORDER BY id DESC LIMIT :limit"
            ), {"limit": limit}).fetchall()
        out = []
        for r in rows:
            d = dict(r._mapping)
            raw = d.get("report_json")
            if isinstance(raw, str) and raw:
                try:
                    d["report"] = json.loads(raw)
                except (ValueError, TypeError):
                    d["report"] = None
            out.append(d)
        return out

    # ── backtest_results ─────────────────────────────────────────
    # save_backtest_result 只落库这些列（引擎 result 还含 equity_curve/initial_cash/
    # final_value/selected_stocks 等运行期字段，不过滤会导致 to_sql 报「无此列」）。
    _BT_RESULT_COLS = (
        "strategy_name", "params", "start_date", "end_date", "total_return",
        "annual_return", "sharpe", "max_drawdown", "win_rate", "profit_loss_ratio",
        "trades", "trades_detail",
    )

    def save_backtest_result(self, result: dict):
        row = {k: result[k] for k in self._BT_RESULT_COLS if k in result}
        row["created_at"] = datetime.now().isoformat()
        pd.DataFrame([row]).to_sql("backtest_results", self.engine, if_exists="append", index=False)

    def get_backtest_results(self) -> pd.DataFrame:
        return pd.read_sql("SELECT * FROM backtest_results ORDER BY created_at DESC", self.engine)

    # ── equity_snapshots（净值快照：仪表盘/模拟交易的净值曲线数据底座）──
    def upsert_equity_snapshot(self, snap: dict) -> None:
        """按 date 主键 upsert 当日净值快照（同日重复记录覆盖，幂等）。"""
        snap = dict(snap)
        snap.setdefault("updated_at", datetime.now().isoformat())
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT OR REPLACE INTO equity_snapshots
                    (date, total_value, cash, market_value, updated_at)
                VALUES (:date, :total_value, :cash, :market_value, :updated_at)
            """), snap)
            conn.commit()

    def get_equity_snapshots(self, start_date: str = None) -> pd.DataFrame:
        """按日期升序返回净值快照；``start_date``（YYYY-MM-DD）可选下界。"""
        if start_date:
            return pd.read_sql(
                text("SELECT * FROM equity_snapshots WHERE date >= :d ORDER BY date"),
                self.engine, params={"d": start_date})
        return pd.read_sql("SELECT * FROM equity_snapshots ORDER BY date", self.engine)

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
                text("SELECT cash, initial_cash, peak_value FROM account WHERE id = 1")
            ).fetchone()
        return dict(row._mapping) if row else {}

    def upsert_account(self, cash: float, initial_cash: float):
        """upsert 账户资金。首次插入时 peak_value=initial_cash（回撤起点峰值）；
        已存在记录时**不覆盖 peak_value**（保护历史 high-water mark）。"""
        now = datetime.now().isoformat()
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO account (id, cash, initial_cash, peak_value, updated_at)
                VALUES (1, :cash, :initial_cash, :initial_cash, :now)
                ON CONFLICT(id) DO UPDATE SET
                    cash          = excluded.cash,
                    initial_cash  = excluded.initial_cash,
                    updated_at    = excluded.updated_at
            """), {"cash": cash, "initial_cash": initial_cash, "now": now})
            conn.commit()

    def update_peak_value(self, peak_value: float):
        """更新历史最高组合净值（high-water mark）。

        仅在 account 行已存在时生效（Portfolio 初始化必然先 upsert_account）。
        缺失行时静默 no-op，避免构造 cash=0 的脏行。
        """
        now = datetime.now().isoformat()
        with self.engine.connect() as conn:
            conn.execute(text(
                "UPDATE account SET peak_value = :peak, updated_at = :now WHERE id = 1"
            ), {"peak": peak_value, "now": now})
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

    def _run_migrations(self, conn):
        """按序执行版本化 schema 迁移，替代裸 try/except ALTER（可审计、失败必抛）。

        幂等：用 PRAGMA table_info 预检列是否已存在（新库 CREATE 已含、旧库可能已
        通过历史 try/except 加过），存在则登记 schema_version 跳过；不存在则执行
        ALTER 并登记。真正失败（非「列已存在」）抛 RuntimeError，不静默吞错。
        """
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                description TEXT,
                applied_at TEXT NOT NULL
            )
        """))
        applied = {
            r[0] for r in conn.execute(text("SELECT version FROM schema_version")).fetchall()
        }
        now = datetime.now().isoformat()
        for version, desc, sql in _MIGRATIONS:
            if version in applied:
                continue
            if callable(sql):
                # 结构性迁移（表重建等），函数自行幂等
                try:
                    sql(conn)
                except Exception as e:
                    raise RuntimeError(f"schema 迁移 v{version}（{desc}）失败: {e}") from e
                conn.execute(text(
                    "INSERT OR IGNORE INTO schema_version (version, description, applied_at) "
                    "VALUES (:v, :d, :t)"
                ), {"v": version, "d": desc, "t": now})
                continue
            if self._column_exists(conn, sql):
                # 旧库已通过历史 try/except 加过列，登记后跳过（幂等）
                conn.execute(text(
                    "INSERT OR IGNORE INTO schema_version (version, description, applied_at) "
                    "VALUES (:v, :d, :t)"
                ), {"v": version, "d": desc, "t": now})
                continue
            try:
                conn.execute(text(sql))
            except Exception as e:
                raise RuntimeError(f"schema 迁移 v{version}（{desc}）失败: {e}") from e
            conn.execute(text(
                "INSERT INTO schema_version (version, description, applied_at) "
                "VALUES (:v, :d, :t)"
            ), {"v": version, "d": desc, "t": now})

    @staticmethod
    def _column_exists(conn, alter_sql: str) -> bool:
        """从 ``ALTER TABLE t ADD COLUMN c`` 解析表/列名，预检是否已存在。"""
        import re
        m = re.match(
            r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)", alter_sql, re.IGNORECASE,
        )
        if not m:
            return False
        table, col = m.group(1), m.group(2)
        cols = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        return any(c[1] == col for c in cols)

    # ── llm_call_log（LLM 调用审计 / 成本核算 / 幻觉追溯）─────────
    def save_llm_call_log(self, rec: dict) -> None:
        """落库一次 LLM 调用。``rec`` 缺省字段自动补 NULL/默认。"""
        rec = dict(rec)
        rec.setdefault("created_at", datetime.now().isoformat())
        for k in ("provider", "model", "endpoint", "prompt_excerpt", "raw_response", "error"):
            rec.setdefault(k, None)
        for k in ("input_tokens", "output_tokens", "latency_ms"):
            rec.setdefault(k, None)
        rec.setdefault("success", 1)
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO llm_call_log
                    (created_at, provider, model, endpoint, prompt_excerpt,
                     raw_response, input_tokens, output_tokens, latency_ms, success, error)
                VALUES
                    (:created_at, :provider, :model, :endpoint, :prompt_excerpt,
                     :raw_response, :input_tokens, :output_tokens, :latency_ms, :success, :error)
            """), rec)
            conn.commit()

    def get_llm_token_usage(self, since: str) -> dict:
        """统计 ``since``（ISO 时间串）以来的 LLM token 消耗，供 UI 成本展示。"""
        with self.engine.connect() as conn:
            row = conn.execute(text("""
                SELECT COUNT(*) AS calls,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens
                FROM llm_call_log WHERE created_at >= :since
            """), {"since": since}).fetchone()
        return {"calls": row[0], "input_tokens": int(row[1]), "output_tokens": int(row[2])}

    def get_llm_call_health(self, limit: int = 5) -> list:
        """最近 ``limit`` 次 LLM 调用的健康状态（created_at/success/error），
        供 UI 连续失败横幅判定。"""
        with self.engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT created_at, provider, model, endpoint, success,
                       substr(COALESCE(error, ''), 1, 160) AS error
                FROM llm_call_log ORDER BY created_at DESC, id DESC LIMIT :limit
            """), {"limit": limit}).fetchall()
        return [dict(r._mapping) for r in rows]

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
        item.setdefault("tags", "")
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO watchlist (code, name, score, signals, reason, source, note, tags, added_at)
                VALUES (:code, :name, :score, :signals, :reason, :source, :note, :tags, :added_at)
                ON CONFLICT(code) DO UPDATE SET
                    name     = excluded.name,
                    score    = excluded.score,
                    signals  = excluded.signals,
                    reason   = excluded.reason,
                    source   = excluded.source,
                    added_at = excluded.added_at
                    -- tags/note 是用户手工编辑的，重复添加不覆盖
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
                item.setdefault("tags", "")
                conn.execute(text("""
                    INSERT INTO watchlist (code, name, score, signals, reason, source, note, tags, added_at)
                    VALUES (:code, :name, :score, :signals, :reason, :source, :note, :tags, :added_at)
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

    def update_watchlist_tags(self, code: str, tags: str):
        """更新自选股标签（逗号分隔字符串，如 '白马,观察'）。"""
        with self.engine.connect() as conn:
            conn.execute(text(
                "UPDATE watchlist SET tags = :tags WHERE code = :code"
            ), {"tags": tags, "code": code})
            conn.commit()

    # ── price_alerts（价格提醒，本地最新收盘越线即触发）─────────────
    def add_price_alert(self, code: str, name: str, direction: str,
                        target_price: float, note: str = "") -> int:
        assert direction in ("above", "below")
        with self.engine.connect() as conn:
            res = conn.execute(text("""
                INSERT INTO price_alerts (code, name, direction, target_price, note, active, created_at)
                VALUES (:code, :name, :direction, :target, :note, 1, :created)
            """), {"code": code, "name": name, "direction": direction,
                    "target": target_price, "note": note,
                    "created": datetime.now().isoformat(timespec="seconds")})
            conn.commit()
            return res.lastrowid

    def get_price_alerts(self, active_only: bool = True) -> list:
        sql = "SELECT * FROM price_alerts"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY id DESC"
        with self.engine.connect() as conn:
            rows = conn.execute(text(sql)).fetchall()
        return [dict(r._mapping) for r in rows]

    def deactivate_price_alert(self, alert_id: int):
        with self.engine.connect() as conn:
            conn.execute(text("UPDATE price_alerts SET active = 0 WHERE id = :id"),
                         {"id": alert_id})
            conn.commit()

    def check_price_alerts(self) -> list:
        """用本地最新收盘价检查生效中的提醒，越线的标记触发并返回列表。

        刻意用本地K线而非实时行情：提醒是「粗粒度哨兵」，不追求盘中秒级；
        无网络时也能工作。返回 [{alert..., last_close}]。
        """
        fired = []
        for a in self.get_price_alerts(active_only=True):
            bars = self.get_daily_bars(a["code"])
            if bars.empty:
                continue
            last_close = float(bars.iloc[-1]["close"])
            last_date = str(bars.iloc[-1]["trade_date"])[:10]
            hit = ((a["direction"] == "above" and last_close >= a["target_price"]) or
                   (a["direction"] == "below" and last_close <= a["target_price"]))
            if hit:
                with self.engine.connect() as conn:
                    conn.execute(text(
                        "UPDATE price_alerts SET active = 0, triggered_at = :t WHERE id = :id"
                    ), {"t": datetime.now().isoformat(timespec="seconds"), "id": a["id"]})
                    conn.commit()
                fired.append({**a, "last_close": last_close, "last_date": last_date})
        return fired

    def remove_from_watchlist(self, code: str):
        with self.engine.connect() as conn:
            conn.execute(text("DELETE FROM watchlist WHERE code = :code"), {"code": code})
            conn.commit()

    def clear_watchlist(self):
        """清空全部自选股。"""
        with self.engine.connect() as conn:
            conn.execute(text("DELETE FROM watchlist"))
            conn.commit()
