from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .time_utils import parse_datetime


class SqliteRepository:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def backup_db(self, suffix: str = "") -> Path:
        import shutil
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        tag = f"_{suffix}" if suffix else ""
        backup_path = self.db_path.parent / f"{self.db_path.stem}_backup_{ts}{tag}.db"
        shutil.copy2(self.db_path, backup_path)
        return backup_path

    def reset_virtual_trades(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self._connect() as conn:
            for table in ("virtual_positions", "virtual_orders", "virtual_sell_pending"):
                cursor = conn.execute(f"DELETE FROM {table}")
                counts[table] = cursor.rowcount
            conn.commit()
        return counts

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

                CREATE TABLE IF NOT EXISTS cycle_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    logged_at TEXT NOT NULL,
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    exchange_code TEXT,
                    action_bias TEXT NOT NULL,
                    action_reason TEXT NOT NULL,
                    price REAL,
                    pnl_pct REAL,
                    holding_qty INTEGER DEFAULT 0,
                    rsi14 REAL,
                    volume_ratio REAL,
                    intraday_momentum REAL,
                    intraday_bar_return REAL,
                    minute_ma_fast REAL,
                    minute_ma_slow REAL,
                    activity_score REAL,
                    cycle_no INTEGER DEFAULT 0,
                    realized_pnl_usd REAL,
                    realized_pnl_krw REAL,
                    session_id TEXT NOT NULL DEFAULT '',
                    strategy_flag TEXT NOT NULL DEFAULT '',
                    entry_by TEXT NOT NULL DEFAULT '',
                    is_session_trade INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_cycle_log_logged_at
                    ON cycle_log(logged_at);
                CREATE INDEX IF NOT EXISTS idx_cycle_log_symbol
                    ON cycle_log(symbol);
                CREATE INDEX IF NOT EXISTS idx_cycle_log_action
                    ON cycle_log(action_bias);

                CREATE TABLE IF NOT EXISTS event_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    logged_at TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL,
                    market TEXT DEFAULT '',
                    symbol TEXT DEFAULT '',
                    detail TEXT DEFAULT '',
                    cycle_no INTEGER DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_event_log_logged_at
                    ON event_log(logged_at);
                CREATE INDEX IF NOT EXISTS idx_event_log_type
                    ON event_log(event_type);

                CREATE TABLE IF NOT EXISTS lab_symbol_state (
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    exchange_code TEXT,
                    action_bias TEXT NOT NULL DEFAULT '',
                    signal_state TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    strategy_flag TEXT NOT NULL DEFAULT '',
                    entry_by TEXT NOT NULL DEFAULT '',
                    exit_by TEXT NOT NULL DEFAULT '',
                    holding_qty INTEGER NOT NULL DEFAULT 0,
                    last_price REAL,
                    pnl_pct REAL,
                    entry_price REAL,
                    peak_price REAL,
                    has_position INTEGER NOT NULL DEFAULT 0,
                    snapshot_json TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (market, symbol)
                );
                CREATE INDEX IF NOT EXISTS idx_lab_symbol_state_updated_at
                    ON lab_symbol_state(updated_at);

                CREATE TABLE IF NOT EXISTS broker_order_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    exchange_code TEXT,
                    side TEXT NOT NULL,
                    order_kind TEXT NOT NULL,
                    requested_qty INTEGER NOT NULL DEFAULT 0,
                    requested_price REAL,
                    strategy_flag TEXT NOT NULL DEFAULT '',
                    entry_by TEXT NOT NULL DEFAULT '',
                    exit_by TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    broker_order_no TEXT,
                    is_virtual INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_broker_order_events_created_at
                    ON broker_order_events(created_at);
                CREATE INDEX IF NOT EXISTS idx_broker_order_events_symbol
                    ON broker_order_events(symbol);
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
            self._ensure_column(conn, "cycle_log", "realized_pnl_usd", "REAL")
            self._ensure_column(conn, "cycle_log", "realized_pnl_krw", "REAL")
            self._ensure_column(conn, "cycle_log", "session_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "cycle_log", "strategy_flag", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "cycle_log", "entry_by", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "cycle_log", "exit_by", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "cycle_log", "is_session_trade", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(conn, "cycle_log", "vwap", "REAL")
            self._ensure_column(conn, "cycle_log", "macd_line", "REAL")
            self._ensure_column(conn, "cycle_log", "macd_signal", "REAL")
            self._ensure_column(conn, "cycle_log", "macd_golden", "INTEGER")
            self._ensure_column(conn, "cycle_log", "breakout_distance_pct", "REAL")
            self._ensure_column(conn, "cycle_log", "atr", "REAL")
            self._ensure_column(conn, "cycle_log", "spread_pct", "REAL")
            self._ensure_column(conn, "cycle_log", "consecutive_losses", "INTEGER")
            self._ensure_column(conn, "cycle_log", "hold_cycles", "INTEGER")
            self._ensure_column(conn, "cycle_log", "entry_price", "REAL")
            self._ensure_column(conn, "cycle_log", "qty_executed", "INTEGER")
            self._ensure_column(conn, "cycle_log", "net_pnl_usd", "REAL")
            self._ensure_column(conn, "cycle_log", "net_pnl_krw", "REAL")
            self._ensure_column(conn, "cycle_log", "commission_usd", "REAL")
            self._ensure_column(conn, "cycle_log", "commission_krw", "REAL")
            self._ensure_column(conn, "cycle_log", "is_virtual", "INTEGER")
            self._ensure_column(conn, "cycle_log", "orderable_qty", "INTEGER")
            self._ensure_column(conn, "cycle_log", "stock_name", "TEXT")
            self._ensure_column(conn, "cycle_log", "hold_duration_min", "REAL")
            self._ensure_column(conn, "cycle_log", "entry_time", "TEXT")
            self._ensure_column(conn, "cycle_log", "exit_cooldown_remaining", "REAL")
            self._ensure_column(conn, "cycle_log", "cb_active", "INTEGER")
            self._ensure_column(conn, "cycle_log", "pool_size", "INTEGER")
            self._ensure_column(conn, "lab_symbol_state", "entry_price", "REAL")
            self._ensure_column(conn, "lab_symbol_state", "peak_price", "REAL")
            self._ensure_column(conn, "lab_symbol_state", "has_position", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "lab_symbol_state", "snapshot_json", "TEXT")

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

    def save_cycle_log(
        self,
        *,
        logged_at: str,
        market: str,
        symbol: str,
        exchange_code: str | None,
        action_bias: str,
        action_reason: str,
        price: float | None = None,
        pnl_pct: float | None = None,
        realized_pnl_usd: float | None = None,
        realized_pnl_krw: float | None = None,
        holding_qty: int = 0,
        rsi14: float | None = None,
        volume_ratio: float | None = None,
        intraday_momentum: float | None = None,
        intraday_bar_return: float | None = None,
        minute_ma_fast: float | None = None,
        minute_ma_slow: float | None = None,
        activity_score: float | None = None,
        cycle_no: int = 0,
        session_id: str = "",
        strategy_flag: str = "",
        entry_by: str = "",
        exit_by: str = "",
        is_session_trade: int = 1,
        vwap: float | None = None,
        macd_line: float | None = None,
        macd_signal: float | None = None,
        macd_golden: int | None = None,
        breakout_distance_pct: float | None = None,
        atr: float | None = None,
        spread_pct: float | None = None,
        consecutive_losses: int | None = None,
        hold_cycles: int | None = None,
        entry_price: float | None = None,
        qty_executed: int | None = None,
        net_pnl_usd: float | None = None,
        net_pnl_krw: float | None = None,
        commission_usd: float | None = None,
        commission_krw: float | None = None,
        is_virtual: int | None = None,
        orderable_qty: int | None = None,
        stock_name: str | None = None,
        hold_duration_min: float | None = None,
        entry_time: str | None = None,
        exit_cooldown_remaining: float | None = None,
        cb_active: int | None = None,
        pool_size: int | None = None,
    ) -> None:
        with self._connect() as conn:
            columns = [
                "logged_at",
                "market",
                "symbol",
                "exchange_code",
                "action_bias",
                "action_reason",
                "price",
                "pnl_pct",
                "realized_pnl_usd",
                "realized_pnl_krw",
                "holding_qty",
                "rsi14",
                "volume_ratio",
                "intraday_momentum",
                "intraday_bar_return",
                "minute_ma_fast",
                "minute_ma_slow",
                "activity_score",
                "cycle_no",
                "session_id",
                "strategy_flag",
                "entry_by",
                "exit_by",
                "is_session_trade",
                "vwap",
                "macd_line",
                "macd_signal",
                "macd_golden",
                "breakout_distance_pct",
                "atr",
                "spread_pct",
                "consecutive_losses",
                "hold_cycles",
                "entry_price",
                "qty_executed",
                "net_pnl_usd",
                "net_pnl_krw",
                "commission_usd",
                "commission_krw",
                "is_virtual",
                "orderable_qty",
                "stock_name",
                "hold_duration_min",
                "entry_time",
                "exit_cooldown_remaining",
                "cb_active",
                "pool_size",
            ]
            values = (
                logged_at,
                market,
                symbol,
                exchange_code,
                action_bias,
                action_reason,
                price,
                pnl_pct,
                realized_pnl_usd,
                realized_pnl_krw,
                holding_qty,
                rsi14,
                volume_ratio,
                intraday_momentum,
                intraday_bar_return,
                minute_ma_fast,
                minute_ma_slow,
                activity_score,
                cycle_no,
                session_id,
                strategy_flag,
                entry_by,
                exit_by,
                is_session_trade,
                vwap,
                macd_line,
                macd_signal,
                macd_golden,
                breakout_distance_pct,
                atr,
                spread_pct,
                consecutive_losses,
                hold_cycles,
                entry_price,
                qty_executed,
                net_pnl_usd,
                net_pnl_krw,
                commission_usd,
                commission_krw,
                is_virtual,
                orderable_qty,
                stock_name,
                hold_duration_min,
                entry_time,
                exit_cooldown_remaining,
                cb_active,
                pool_size,
            )
            conn.execute(
                f"""
                INSERT INTO cycle_log ({', '.join(columns)})
                VALUES ({', '.join(['?'] * len(columns))})
                """,
                values,
            )

    def save_event(
        self,
        *,
        event_type: str,
        market: str = "",
        symbol: str = "",
        detail: dict | str = "",
        cycle_no: int = 0,
        session_id: str = "",
    ) -> None:
        from datetime import datetime, timezone

        detail_str = (
            json.dumps(detail, ensure_ascii=False, default=str)
            if isinstance(detail, dict)
            else str(detail)
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO event_log
                    (logged_at, session_id, event_type, market, symbol, detail, cycle_no)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    session_id,
                    event_type,
                    market,
                    symbol,
                    detail_str,
                    cycle_no,
                ),
            )

    def query_cycle_log(
        self,
        *,
        symbol: str | None = None,
        action_bias: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        with self._connect() as conn:
            where_parts: list[str] = []
            params: list[object] = []
            if symbol:
                where_parts.append("symbol = ?")
                params.append(symbol)
            if action_bias:
                where_parts.append("action_bias = ?")
                params.append(action_bias)
            clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
            rows = conn.execute(
                f"SELECT * FROM cycle_log {clause} ORDER BY id DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        return [dict(row) for row in rows]

    def list_event_log(
        self,
        *,
        event_type: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        with self._connect() as conn:
            params: list[object] = []
            clause = ""
            if event_type:
                clause = "WHERE event_type = ?"
                params.append(event_type)
            rows = conn.execute(
                f"SELECT * FROM event_log {clause} ORDER BY id DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        return [dict(row) for row in rows]

    def save_broker_order_event(
        self,
        *,
        created_at: str,
        market: str,
        symbol: str,
        exchange_code: str | None,
        side: str,
        order_kind: str,
        requested_qty: int,
        requested_price: float | None = None,
        strategy_flag: str = "",
        entry_by: str = "",
        exit_by: str = "",
        status: str = "",
        reason: str = "",
        broker_order_no: str | None = None,
        is_virtual: int = 0,
        payload: dict | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO broker_order_events (
                    created_at, market, symbol, exchange_code, side, order_kind,
                    requested_qty, requested_price, strategy_flag, entry_by, exit_by,
                    status, reason, broker_order_no, is_virtual, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    market,
                    symbol,
                    exchange_code,
                    side,
                    order_kind,
                    requested_qty,
                    requested_price,
                    strategy_flag,
                    entry_by,
                    exit_by,
                    status,
                    reason,
                    broker_order_no,
                    is_virtual,
                    json.dumps(payload or {}, ensure_ascii=False, default=str),
                ),
            )

    def list_broker_order_events(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM broker_order_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result: list[dict] = []
        for row in rows:
            item = dict(row)
            payload_text = item.get("payload_json")
            if payload_text:
                try:
                    item["payload_json"] = json.loads(str(payload_text))
                except json.JSONDecodeError:
                    item["payload_json"] = {}
            result.append(item)
        return result

    def upsert_lab_symbol_state(
        self,
        *,
        market: str,
        symbol: str,
        exchange_code: str | None,
        action_bias: str,
        signal_state: str,
        note: str,
        strategy_flag: str = "",
        entry_by: str = "",
        exit_by: str = "",
        holding_qty: int = 0,
        last_price: float | None = None,
        pnl_pct: float | None = None,
        entry_price: float | None = None,
        peak_price: float | None = None,
        has_position: int = 0,
        snapshot_json: dict | None = None,
        updated_at: str = "",
    ) -> None:
        if not updated_at:
            from datetime import datetime, timezone

            updated_value = datetime.now(timezone.utc).isoformat()
        else:
            updated_value = updated_at
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO lab_symbol_state (
                    market, symbol, exchange_code, action_bias, signal_state, note,
                    strategy_flag, entry_by, exit_by, holding_qty, last_price, pnl_pct,
                    entry_price, peak_price, has_position, snapshot_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market, symbol) DO UPDATE SET
                    exchange_code = excluded.exchange_code,
                    action_bias = excluded.action_bias,
                    signal_state = excluded.signal_state,
                    note = excluded.note,
                    strategy_flag = excluded.strategy_flag,
                    entry_by = excluded.entry_by,
                    exit_by = excluded.exit_by,
                    holding_qty = excluded.holding_qty,
                    last_price = excluded.last_price,
                    pnl_pct = excluded.pnl_pct,
                    entry_price = COALESCE(excluded.entry_price, lab_symbol_state.entry_price),
                    peak_price = COALESCE(excluded.peak_price, lab_symbol_state.peak_price),
                    has_position = excluded.has_position,
                    snapshot_json = COALESCE(excluded.snapshot_json, lab_symbol_state.snapshot_json),
                    updated_at = excluded.updated_at
                """,
                (
                    market,
                    symbol,
                    exchange_code,
                    action_bias,
                    signal_state,
                    note,
                    strategy_flag,
                    entry_by,
                    exit_by,
                    holding_qty,
                    last_price,
                    pnl_pct,
                    entry_price,
                    peak_price,
                    has_position,
                    None
                    if snapshot_json is None
                    else json.dumps(snapshot_json, ensure_ascii=False, default=str),
                    updated_value,
                ),
            )

    def get_lab_symbol_state(self, market: str, symbol: str) -> dict | None:
        symbol_upper = symbol.strip().upper()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM lab_symbol_state
                WHERE market = ? AND symbol = ?
                """,
                (market, symbol_upper),
            ).fetchone()
        if row is None:
            return self.get_latest_strategy_context(market, symbol_upper)
        result = dict(row)
        snapshot_text = result.get("snapshot_json")
        if snapshot_text:
            try:
                result["snapshot_json"] = json.loads(str(snapshot_text))
            except json.JSONDecodeError:
                result["snapshot_json"] = None
        return result

    def list_lab_symbol_states(
        self,
        *,
        market: str | None = None,
        only_positions: bool = False,
    ) -> list[dict]:
        where_parts: list[str] = []
        params: list[object] = []
        if market:
            where_parts.append("market = ?")
            params.append(market)
        if only_positions:
            where_parts.append("has_position = 1")
        clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM lab_symbol_state {clause} ORDER BY updated_at DESC",
                params,
            ).fetchall()
        result: list[dict] = []
        for row in rows:
            item = dict(row)
            snapshot_text = item.get("snapshot_json")
            if snapshot_text:
                try:
                    item["snapshot_json"] = json.loads(str(snapshot_text))
                except json.JSONDecodeError:
                    item["snapshot_json"] = None
            result.append(item)
        return result

    def get_latest_strategy_context(self, market: str, symbol: str) -> dict | None:
        symbol_upper = symbol.strip().upper()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT market,
                       symbol,
                       exchange_code,
                       action_bias,
                       action_reason AS note,
                       strategy_flag,
                       entry_by,
                       '' AS exit_by,
                       holding_qty,
                       price AS last_price,
                       pnl_pct,
                       NULL AS entry_price,
                       NULL AS peak_price,
                       CASE WHEN holding_qty > 0 THEN 1 ELSE 0 END AS has_position,
                       NULL AS snapshot_json,
                       logged_at AS updated_at
                FROM cycle_log
                WHERE market = ?
                  AND symbol = ?
                  AND (
                    strategy_flag != ''
                    OR entry_by != ''
                    OR holding_qty > 0
                  )
                ORDER BY id DESC
                LIMIT 1
                """,
                (market, symbol_upper),
            ).fetchone()
        return None if row is None else dict(row)

    def get_session_pnl_summary(
        self,
        *,
        session_id: str = "",
        include_virtual: bool = True,
        after_logged_at: str = "",
    ) -> dict:
        after_dt = parse_datetime(after_logged_at)
        with self._connect() as conn:
            real_query = "SELECT * FROM cycle_log WHERE action_bias = 'SELL_REAL'"
            real_params: list[object] = []
            cycle_log_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(cycle_log)").fetchall()
            }
            if "is_session_trade" in cycle_log_columns:
                real_query += " AND (is_session_trade IS NULL OR is_session_trade = 1)"
            if session_id:
                real_query += " AND session_id = ?"
                real_params.append(session_id)
            real_rows = [dict(row) for row in conn.execute(real_query, real_params).fetchall()]

            virtual_rows: list[dict] = []
            if include_virtual:
                virtual_rows = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT * FROM virtual_orders WHERE side = 'sell'"
                    ).fetchall()
                ]

        real_summary: dict[str, dict[str, float | int | None]] = {}
        for row in real_rows:
            logged_at_dt = parse_datetime(row.get("logged_at"))
            if after_dt is not None and logged_at_dt is not None and logged_at_dt < after_dt:
                continue
            market = str(row.get("market") or "unknown")
            stats = real_summary.setdefault(
                market,
                {
                    "market": market,
                    "trade_count": 0,
                    "win_count": 0,
                    "loss_count": 0,
                    "avg_pnl_pct": 0.0,
                    "_sum_pnl_pct": 0.0,
                    "total_pnl_usd": 0.0,
                    "total_pnl_krw": 0.0,
                },
            )
            pnl_pct = float(row.get("pnl_pct") or 0.0)
            stats["trade_count"] = int(stats["trade_count"]) + 1
            if pnl_pct > 0:
                stats["win_count"] = int(stats["win_count"]) + 1
            else:
                stats["loss_count"] = int(stats["loss_count"]) + 1
            stats["_sum_pnl_pct"] = float(stats["_sum_pnl_pct"]) + pnl_pct
            stats["total_pnl_usd"] = float(stats["total_pnl_usd"]) + float(row.get("realized_pnl_usd") or 0.0)
            stats["total_pnl_krw"] = float(stats["total_pnl_krw"]) + float(row.get("realized_pnl_krw") or 0.0)

        for stats in real_summary.values():
            trade_count = int(stats["trade_count"])
            stats["avg_pnl_pct"] = (float(stats["_sum_pnl_pct"]) / trade_count) if trade_count else 0.0
            del stats["_sum_pnl_pct"]

        virtual_summary: dict[str, dict[str, float | int | str]] = {}
        for row in virtual_rows:
            created_at_dt = parse_datetime(row.get("created_at"))
            if after_dt is not None and created_at_dt is not None and created_at_dt < after_dt:
                continue
            market = str(row.get("market") or "unknown")
            currency = str(row.get("currency") or "USD")
            key = f"{market}_{currency}"
            stats = virtual_summary.setdefault(
                key,
                {
                    "market": market,
                    "currency": currency,
                    "trade_count": 0,
                    "win_count": 0,
                    "loss_count": 0,
                    "avg_pnl_pct": 0.0,
                    "_sum_pnl_pct": 0.0,
                    "total_pnl": 0.0,
                },
            )
            pnl = float(row.get("realized_pnl") or 0.0)
            pnl_pct = float(row.get("realized_pnl_pct") or 0.0)
            stats["trade_count"] = int(stats["trade_count"]) + 1
            if pnl > 0:
                stats["win_count"] = int(stats["win_count"]) + 1
            else:
                stats["loss_count"] = int(stats["loss_count"]) + 1
            stats["_sum_pnl_pct"] = float(stats["_sum_pnl_pct"]) + pnl_pct
            stats["total_pnl"] = float(stats["total_pnl"]) + pnl

        for stats in virtual_summary.values():
            trade_count = int(stats["trade_count"])
            stats["avg_pnl_pct"] = (float(stats["_sum_pnl_pct"]) / trade_count) if trade_count else 0.0
            del stats["_sum_pnl_pct"]

        return {
            "real": real_summary,
            "virtual": virtual_summary,
        }

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


Repository = SqliteRepository
