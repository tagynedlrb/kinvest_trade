from __future__ import annotations

import json
import sqlite3
from pathlib import Path


class SqliteRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS risk_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    raw_payload TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS heartbeats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS paper_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    watchlist_json TEXT NOT NULL,
                    starting_cash_krw INTEGER NOT NULL,
                    ending_cash_krw INTEGER,
                    realized_pnl_krw INTEGER,
                    notes TEXT,
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    ended_at TEXT
                );

                CREATE TABLE IF NOT EXISTS quote_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    captured_at TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    best_ask INTEGER NOT NULL,
                    best_bid INTEGER NOT NULL,
                    ask_size INTEGER NOT NULL,
                    bid_size INTEGER NOT NULL,
                    mid_price REAL NOT NULL,
                    spread_pct REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS paper_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    requested_price INTEGER NOT NULL,
                    fill_price INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    realized_pnl_krw INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS paper_positions (
                    run_id INTEGER NOT NULL,
                    stock_code TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    avg_price INTEGER NOT NULL,
                    peak_price INTEGER NOT NULL,
                    opened_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, stock_code)
                );

                CREATE TABLE IF NOT EXISTS virtual_positions (
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    exchange_code TEXT,
                    qty INTEGER NOT NULL,
                    avg_price REAL NOT NULL,
                    currency TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (market, symbol)
                );

                CREATE TABLE IF NOT EXISTS virtual_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    exchange_code TEXT,
                    side TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    fill_price REAL NOT NULL,
                    currency TEXT NOT NULL,
                    session TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    realized_pnl_pct REAL NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS virtual_sell_pending (
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    exchange_code TEXT,
                    qty INTEGER NOT NULL,
                    avg_sell_price REAL NOT NULL,
                    currency TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (market, symbol)
                );

                CREATE TABLE IF NOT EXISTS indicator_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    stock_code TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    bar_count INTEGER NOT NULL,
                    last_close INTEGER,
                    rsi14 REAL,
                    sma5 REAL,
                    sma20 REAL,
                    volume_sum INTEGER NOT NULL,
                    change_pct_from_oldest REAL,
                    raw_payload TEXT
                );

                CREATE TABLE IF NOT EXISTS auto_trade_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mode TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    exchange_code TEXT NOT NULL,
                    status TEXT NOT NULL,
                    max_actions INTEGER NOT NULL,
                    realized_pnl_usd REAL NOT NULL DEFAULT 0,
                    realized_pnl_net_usd REAL NOT NULL DEFAULT 0,
                    realized_pnl_net_krw REAL NOT NULL DEFAULT 0,
                    fees_total_usd REAL NOT NULL DEFAULT 0,
                    fx_pnl_krw REAL NOT NULL DEFAULT 0,
                    estimated_tax_krw REAL NOT NULL DEFAULT 0,
                    notes TEXT,
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    ended_at TEXT
                );

                CREATE TABLE IF NOT EXISTS auto_trade_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    action_no INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    side TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    price REAL NOT NULL,
                    reason TEXT NOT NULL,
                    broker_order_no TEXT,
                    status TEXT NOT NULL,
                    realized_pnl_usd REAL NOT NULL DEFAULT 0,
                    realized_pnl_net_usd REAL NOT NULL DEFAULT 0,
                    realized_pnl_net_krw REAL NOT NULL DEFAULT 0,
                    fees_usd REAL NOT NULL DEFAULT 0,
                    fx_rate_krw REAL NOT NULL DEFAULT 0,
                    fx_pnl_krw REAL NOT NULL DEFAULT 0,
                    estimated_tax_delta_krw REAL NOT NULL DEFAULT 0,
                    raw_payload TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS telegram_control_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    started_at TEXT,
                    ended_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    cycles_completed INTEGER NOT NULL DEFAULT 0,
                    domestic_paper_runs INTEGER NOT NULL DEFAULT 0,
                    domestic_paper_realized_pnl_krw INTEGER NOT NULL DEFAULT 0,
                    domestic_orders_submitted INTEGER NOT NULL DEFAULT 0,
                    overseas_orders_submitted INTEGER NOT NULL DEFAULT 0,
                    domestic_orders_failed INTEGER NOT NULL DEFAULT 0,
                    overseas_orders_failed INTEGER NOT NULL DEFAULT 0,
                    summary_json TEXT NOT NULL
                );
                """
            )
            self._ensure_column(conn, "auto_trade_runs", "realized_pnl_net_usd", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(conn, "auto_trade_runs", "realized_pnl_net_krw", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(conn, "auto_trade_runs", "fees_total_usd", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(conn, "auto_trade_runs", "fx_pnl_krw", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(conn, "auto_trade_runs", "estimated_tax_krw", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(conn, "auto_trade_actions", "realized_pnl_net_usd", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(conn, "auto_trade_actions", "realized_pnl_net_krw", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(conn, "auto_trade_actions", "fees_usd", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(conn, "auto_trade_actions", "fx_rate_krw", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(conn, "auto_trade_actions", "fx_pnl_krw", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(conn, "auto_trade_actions", "estimated_tax_delta_krw", "REAL NOT NULL DEFAULT 0")

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection,
        table_name: str,
        column_name: str,
        column_type: str,
    ) -> None:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        existing_columns = {str(row[1]) for row in rows}
        if column_name in existing_columns:
            return
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")

    def save_risk_event(
        self,
        event_type: str,
        severity: str,
        message: str,
        raw_payload: dict | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO risk_events (event_type, severity, message, raw_payload)
                VALUES (?, ?, ?, ?)
                """,
                (
                    event_type,
                    severity,
                    message,
                    None
                    if raw_payload is None
                    else json.dumps(raw_payload, ensure_ascii=False, default=str),
                ),
            )

    def save_heartbeat(self, status: str, message: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO heartbeats (status, message) VALUES (?, ?)",
                (status, message),
            )

    def save_telegram_control_session(
        self,
        *,
        command: str,
        profile: str,
        started_at: str | None,
        cycles_completed: int,
        domestic_paper_runs: int,
        domestic_paper_realized_pnl_krw: int,
        domestic_orders_submitted: int,
        overseas_orders_submitted: int,
        domestic_orders_failed: int,
        overseas_orders_failed: int,
        summary_json: dict,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO telegram_control_sessions (
                    command, profile, started_at, cycles_completed,
                    domestic_paper_runs, domestic_paper_realized_pnl_krw,
                    domestic_orders_submitted, overseas_orders_submitted,
                    domestic_orders_failed, overseas_orders_failed, summary_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command,
                    profile,
                    started_at,
                    cycles_completed,
                    domestic_paper_runs,
                    domestic_paper_realized_pnl_krw,
                    domestic_orders_submitted,
                    overseas_orders_submitted,
                    domestic_orders_failed,
                    overseas_orders_failed,
                    json.dumps(summary_json, ensure_ascii=False, default=str),
                ),
            )
            return int(cursor.lastrowid)

    def create_paper_run(
        self,
        mode: str,
        watchlist: list[str],
        starting_cash_krw: int,
        notes: str = "",
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO paper_runs (mode, status, watchlist_json, starting_cash_krw, notes)
                VALUES (?, 'RUNNING', ?, ?, ?)
                """,
                (mode, json.dumps(watchlist, ensure_ascii=False), starting_cash_krw, notes),
            )
            return int(cursor.lastrowid)

    def finish_paper_run(
        self,
        run_id: int,
        status: str,
        ending_cash_krw: int,
        realized_pnl_krw: int,
        notes: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE paper_runs
                SET status = ?, ending_cash_krw = ?, realized_pnl_krw = ?, notes = ?, ended_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, ending_cash_krw, realized_pnl_krw, notes, run_id),
            )

    def save_quote_snapshot(
        self,
        run_id: int,
        captured_at: str,
        stock_code: str,
        best_ask: int,
        best_bid: int,
        ask_size: int,
        bid_size: int,
        mid_price: float,
        spread_pct: float,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO quote_snapshots (
                    run_id, captured_at, stock_code, best_ask, best_bid, ask_size, bid_size, mid_price, spread_pct
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    captured_at,
                    stock_code,
                    best_ask,
                    best_bid,
                    ask_size,
                    bid_size,
                    mid_price,
                    spread_pct,
                ),
            )

    def save_paper_order(
        self,
        run_id: int,
        created_at: str,
        stock_code: str,
        side: str,
        qty: int,
        requested_price: int,
        fill_price: int,
        status: str,
        reason: str,
        realized_pnl_krw: int = 0,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO paper_orders (
                    run_id, created_at, stock_code, side, qty, requested_price, fill_price, status, reason, realized_pnl_krw
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    created_at,
                    stock_code,
                    side,
                    qty,
                    requested_price,
                    fill_price,
                    status,
                    reason,
                    realized_pnl_krw,
                ),
            )

    def upsert_paper_position(
        self,
        run_id: int,
        stock_code: str,
        qty: int,
        avg_price: int,
        peak_price: int,
        opened_at: str,
        updated_at: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO paper_positions (
                    run_id, stock_code, qty, avg_price, peak_price, opened_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, stock_code) DO UPDATE SET
                    qty = excluded.qty,
                    avg_price = excluded.avg_price,
                    peak_price = excluded.peak_price,
                    opened_at = excluded.opened_at,
                    updated_at = excluded.updated_at
                """,
                (run_id, stock_code, qty, avg_price, peak_price, opened_at, updated_at),
            )

    def delete_paper_position(self, run_id: int, stock_code: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM paper_positions WHERE run_id = ? AND stock_code = ?",
                (run_id, stock_code),
            )

    def get_latest_paper_run_id(self) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM paper_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return int(row[0])

    def get_paper_run(self, run_id: int) -> dict | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM paper_runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row is not None else None

    def get_paper_orders(self, run_id: int) -> list[dict]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM paper_orders WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_paper_positions(self, run_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_positions WHERE run_id = ? ORDER BY stock_code",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_virtual_position(
        self,
        market: str,
        symbol: str,
        exchange_code: str | None,
        qty: int,
        avg_price: float,
        currency: str,
        opened_at: str,
        updated_at: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO virtual_positions
                    (market, symbol, exchange_code, qty, avg_price, currency, opened_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market, symbol) DO UPDATE SET
                    qty = excluded.qty,
                    avg_price = excluded.avg_price,
                    exchange_code = excluded.exchange_code,
                    updated_at = excluded.updated_at
                """,
                (market, symbol, exchange_code, qty, avg_price, currency, opened_at, updated_at),
            )

    def delete_virtual_position(self, market: str, symbol: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM virtual_positions WHERE market = ? AND symbol = ?",
                (market, symbol),
            )

    def get_virtual_position(self, market: str, symbol: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM virtual_positions WHERE market = ? AND symbol = ?",
                (market, symbol),
            ).fetchone()
        return dict(row) if row else None

    def list_virtual_positions(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM virtual_positions ORDER BY opened_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def save_virtual_order(
        self,
        *,
        created_at: str,
        market: str,
        symbol: str,
        exchange_code: str | None,
        side: str,
        qty: int,
        fill_price: float,
        currency: str,
        session: str,
        reason: str,
        realized_pnl: float = 0.0,
        realized_pnl_pct: float = 0.0,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO virtual_orders
                    (created_at, market, symbol, exchange_code, side, qty,
                     fill_price, currency, session, reason, realized_pnl, realized_pnl_pct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    market,
                    symbol,
                    exchange_code,
                    side,
                    qty,
                    fill_price,
                    currency,
                    session,
                    reason,
                    realized_pnl,
                    realized_pnl_pct,
                ),
            )
            return int(cursor.lastrowid)

    def list_virtual_orders(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM virtual_orders ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_virtual_performance_summary(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT market, currency,
                       COUNT(*) AS trade_count,
                       SUM(realized_pnl) AS total_pnl,
                       SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS win_count
                FROM virtual_orders
                WHERE side = 'sell'
                GROUP BY market, currency
                """
            ).fetchall()
        return {f"{row['market']}_{row['currency']}": dict(row) for row in rows}

    def upsert_virtual_sell_pending(
        self,
        market: str,
        symbol: str,
        exchange_code: str | None,
        qty: int,
        avg_sell_price: float,
        currency: str,
        updated_at: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO virtual_sell_pending
                    (market, symbol, exchange_code, qty, avg_sell_price, currency, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market, symbol) DO UPDATE SET
                    qty = excluded.qty,
                    avg_sell_price = excluded.avg_sell_price,
                    exchange_code = excluded.exchange_code,
                    currency = excluded.currency,
                    updated_at = excluded.updated_at
                """,
                (market, symbol, exchange_code, qty, avg_sell_price, currency, updated_at),
            )

    def get_virtual_sell_pending(self, market: str, symbol: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM virtual_sell_pending WHERE market = ? AND symbol = ?",
                (market, symbol),
            ).fetchone()
        return dict(row) if row else None

    def list_virtual_sell_pending(self, market: str | None = None) -> list[dict]:
        with self._connect() as conn:
            if market is not None:
                rows = conn.execute(
                    "SELECT * FROM virtual_sell_pending WHERE market = ? ORDER BY symbol",
                    (market,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM virtual_sell_pending ORDER BY market, symbol"
                ).fetchall()
        return [dict(row) for row in rows]

    def delete_virtual_sell_pending(self, market: str, symbol: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM virtual_sell_pending WHERE market = ? AND symbol = ?",
                (market, symbol),
            )

    def get_latest_quotes_for_run(self, run_id: int) -> dict[str, dict]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT q.*
                FROM quote_snapshots q
                JOIN (
                    SELECT stock_code, MAX(id) AS max_id
                    FROM quote_snapshots
                    WHERE run_id = ?
                    GROUP BY stock_code
                ) latest
                ON q.id = latest.max_id
                """,
                (run_id,),
            ).fetchall()
        return {row["stock_code"]: dict(row) for row in rows}

    def save_indicator_check(
        self,
        stock_code: str,
        timeframe: str,
        bar_count: int,
        last_close: int | None,
        rsi14: float | None,
        sma5: float | None,
        sma20: float | None,
        volume_sum: int,
        change_pct_from_oldest: float | None,
        raw_payload: list[dict],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO indicator_checks (
                    stock_code, timeframe, bar_count, last_close, rsi14, sma5, sma20,
                    volume_sum, change_pct_from_oldest, raw_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stock_code,
                    timeframe,
                    bar_count,
                    last_close,
                    rsi14,
                    sma5,
                    sma20,
                    volume_sum,
                    change_pct_from_oldest,
                    json.dumps(raw_payload, ensure_ascii=False, default=str),
                ),
            )

    def create_auto_trade_run(
        self,
        mode: str,
        profile: str,
        symbol: str,
        exchange_code: str,
        max_actions: int,
        notes: str = "",
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO auto_trade_runs (
                    mode, profile, symbol, exchange_code, status, max_actions, notes
                ) VALUES (?, ?, ?, ?, 'RUNNING', ?, ?)
                """,
                (mode, profile, symbol, exchange_code, max_actions, notes),
            )
            return int(cursor.lastrowid)

    def abort_stale_auto_trade_runs(
        self,
        *,
        older_than_minutes: int,
        reason: str,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE auto_trade_runs
                SET status = 'ABORTED',
                    notes = ?,
                    ended_at = CURRENT_TIMESTAMP
                WHERE status = 'RUNNING'
                  AND ended_at IS NULL
                  AND started_at <= datetime('now', ?)
                """,
                (
                    reason,
                    f"-{max(int(older_than_minutes), 1)} minutes",
                ),
            )
            return int(cursor.rowcount or 0)

    def finish_auto_trade_run(
        self,
        run_id: int,
        status: str,
        realized_pnl_usd: float,
        realized_pnl_net_usd: float,
        realized_pnl_net_krw: float,
        fees_total_usd: float,
        fx_pnl_krw: float,
        estimated_tax_krw: float,
        notes: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE auto_trade_runs
                SET status = ?,
                    realized_pnl_usd = ?,
                    realized_pnl_net_usd = ?,
                    realized_pnl_net_krw = ?,
                    fees_total_usd = ?,
                    fx_pnl_krw = ?,
                    estimated_tax_krw = ?,
                    notes = ?,
                    ended_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    status,
                    realized_pnl_usd,
                    realized_pnl_net_usd,
                    realized_pnl_net_krw,
                    fees_total_usd,
                    fx_pnl_krw,
                    estimated_tax_krw,
                    notes,
                    run_id,
                ),
            )

    def save_auto_trade_action(
        self,
        run_id: int,
        action_no: int,
        created_at: str,
        side: str,
        symbol: str,
        qty: int,
        price: float,
        reason: str,
        broker_order_no: str | None,
        status: str,
        realized_pnl_usd: float,
        realized_pnl_net_usd: float,
        realized_pnl_net_krw: float,
        fees_usd: float,
        fx_rate_krw: float,
        fx_pnl_krw: float,
        estimated_tax_delta_krw: float,
        raw_payload: dict,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO auto_trade_actions (
                    run_id, action_no, created_at, side, symbol, qty, price,
                    reason, broker_order_no, status, realized_pnl_usd,
                    realized_pnl_net_usd, realized_pnl_net_krw, fees_usd,
                    fx_rate_krw, fx_pnl_krw, estimated_tax_delta_krw, raw_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    action_no,
                    created_at,
                    side,
                    symbol,
                    qty,
                    price,
                    reason,
                    broker_order_no,
                    status,
                    realized_pnl_usd,
                    realized_pnl_net_usd,
                    realized_pnl_net_krw,
                    fees_usd,
                    fx_rate_krw,
                    fx_pnl_krw,
                    estimated_tax_delta_krw,
                    json.dumps(raw_payload, ensure_ascii=False, default=str),
                ),
            )
