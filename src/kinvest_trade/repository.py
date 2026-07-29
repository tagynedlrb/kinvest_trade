from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .auto_trade_math import (
    DOMESTIC_COST_CALCULATION_VERSION,
    estimate_domestic_trade_costs,
)
from .market_sessions import KST, NEW_YORK
from .time_utils import parse_datetime


CONFIRMED_SELL_CYCLE_PREDICATE = """
COALESCE(cycle_log.execution_group_id, '') != ''
AND EXISTS (
    SELECT 1
    FROM broker_order_executions AS confirmed_execution
    WHERE confirmed_execution.execution_group_id = cycle_log.execution_group_id
      AND UPPER(confirmed_execution.side) = 'SELL'
      AND confirmed_execution.filled_qty > 0
)
""".strip()

CONFIRMED_BUY_CYCLE_PREDICATE = """
COALESCE(cycle_log.execution_group_id, '') != ''
AND EXISTS (
    SELECT 1
    FROM broker_order_executions AS confirmed_execution
    WHERE confirmed_execution.execution_group_id = cycle_log.execution_group_id
      AND UPPER(confirmed_execution.side) = 'BUY'
      AND confirmed_execution.filled_qty > 0
)
""".strip()

CONFIRMED_SESSION_OWNERSHIP_PREDICATE = """
COALESCE(cycle_log.session_id, '') != ''
AND EXISTS (
    SELECT 1
    FROM broker_order_executions AS session_buy
    WHERE session_buy.session_id = cycle_log.session_id
      AND session_buy.market = cycle_log.market
      AND session_buy.symbol = cycle_log.symbol
      AND UPPER(session_buy.side) = 'BUY'
      AND session_buy.filled_qty > 0
      AND session_buy.is_session_trade = 1
      AND session_buy.created_at <= cycle_log.logged_at
)
""".strip()

CONFIRMED_STRATEGY_SELL_CYCLE_PREDICATE = f"""
({CONFIRMED_SELL_CYCLE_PREDICATE})
AND (
    COALESCE(cycle_log.is_session_trade, 0) = 1
    OR ({CONFIRMED_SESSION_OWNERSHIP_PREDICATE})
)
""".strip()


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
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        tag = f"_{suffix}" if suffix else ""
        backup_path = self.db_path.parent / f"{self.db_path.stem}_backup_{ts}{tag}.db"
        with sqlite3.connect(self.db_path) as source, sqlite3.connect(backup_path) as target:
            source.backup(target)
        return backup_path

    def reset_virtual_trades(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self._connect() as conn:
            for table in ("virtual_positions", "virtual_orders", "virtual_sell_pending"):
                cursor = conn.execute(f"DELETE FROM {table}")
                counts[table] = cursor.rowcount
            conn.commit()
        return counts

    _RESET_ALL_HISTORY_TABLES: tuple[str, ...] = (
        "cycle_log",
        "event_log",
        "broker_order_events",
        "broker_order_executions",
        "inverse_shadow_trades",
        "virtual_positions",
        "virtual_orders",
        "virtual_sell_pending",
        "lab_symbol_state",
        "market_regimes",
        "policy_evaluation_log",
    )

    def count_rows(self, table: str) -> int:
        if table not in self._RESET_ALL_HISTORY_TABLES:
            raise ValueError(f"unsupported table for count_rows: {table}")
        with self._connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0]) if row else 0

    def reset_all_history(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self._connect() as conn:
            for table in self._RESET_ALL_HISTORY_TABLES:
                cursor = conn.execute(f"DELETE FROM {table}")
                counts[table] = cursor.rowcount
            conn.commit()
        return counts

    def prune_operational_logs(
        self,
        *,
        api_call_retention_days: int,
        telegram_message_retention_days: int,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """Delete only expired high-volume operational logs.

        Trading decisions, orders, events, and performance history are not
        touched. SQLite can reuse the released pages without an online VACUUM.
        """

        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        targets = (
            ("api_call_log", "created_at", int(api_call_retention_days)),
            (
                "telegram_message_log",
                "created_at",
                int(telegram_message_retention_days),
            ),
        )
        deleted: dict[str, int] = {}
        with self._connect() as conn:
            for table, time_column, retention_days in targets:
                if retention_days <= 0:
                    deleted[table] = 0
                    continue
                cutoff = (current - timedelta(days=retention_days)).isoformat()
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE {time_column} < ?",
                    (cutoff,),
                )
                deleted[table] = max(0, int(cursor.rowcount))
            conn.commit()
        return deleted

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
                    realized_pnl_pct REAL NOT NULL DEFAULT 0,
                    excluded_from_performance INTEGER NOT NULL DEFAULT 0,
                    exclude_reason TEXT NOT NULL DEFAULT '',
                    excluded_at TEXT
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
                    entry_time TEXT,
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
                CREATE TABLE IF NOT EXISTS broker_order_executions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    broker_event_id INTEGER NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    order_date TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    exchange_code TEXT,
                    side TEXT NOT NULL,
                    broker_order_no TEXT NOT NULL,
                    normalized_order_no TEXT NOT NULL,
                    execution_group_id TEXT NOT NULL,
                    group_target_qty INTEGER NOT NULL,
                    requested_qty INTEGER NOT NULL,
                    requested_price REAL,
                    filled_qty INTEGER NOT NULL DEFAULT 0,
                    filled_amount REAL NOT NULL DEFAULT 0,
                    avg_fill_price REAL,
                    remaining_qty INTEGER NOT NULL DEFAULT 0,
                    canceled_qty INTEGER NOT NULL DEFAULT 0,
                    rejected_qty INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    strategy_flag TEXT NOT NULL DEFAULT '',
                    entry_by TEXT NOT NULL DEFAULT '',
                    exit_by TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    cycle_no INTEGER NOT NULL DEFAULT 0,
                    is_session_trade INTEGER NOT NULL DEFAULT 0,
                    entry_price REAL,
                    entry_time TEXT,
                    hold_duration_min REAL,
                    context_json TEXT NOT NULL DEFAULT '{}',
                    history_json TEXT NOT NULL DEFAULT '{}',
                    last_checked_at TEXT,
                    fill_recorded_at TEXT,
                    finalized_at TEXT,
                    cycle_log_id INTEGER
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_broker_execution_order
                    ON broker_order_executions(
                        market, order_date, normalized_order_no
                    );
                CREATE INDEX IF NOT EXISTS idx_broker_execution_pending
                    ON broker_order_executions(finalized_at, market, order_date);
                CREATE INDEX IF NOT EXISTS idx_broker_execution_group
                    ON broker_order_executions(execution_group_id);
                CREATE TABLE IF NOT EXISTS inverse_shadow_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    opened_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    exchange_code TEXT,
                    entry_session_date TEXT NOT NULL,
                    policy_id TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    entry_spread_pct REAL NOT NULL DEFAULT 0,
                    commission_rate REAL NOT NULL DEFAULT 0,
                    benchmark_name TEXT NOT NULL DEFAULT '',
                    benchmark_return_pct REAL,
                    benchmark_regime_key TEXT NOT NULL DEFAULT '',
                    entry_reason TEXT NOT NULL DEFAULT '',
                    strategy_flag TEXT NOT NULL DEFAULT '',
                    entry_by TEXT NOT NULL DEFAULT '',
                    hold_cycles INTEGER NOT NULL DEFAULT 0,
                    peak_price REAL NOT NULL,
                    trough_price REAL NOT NULL,
                    last_price REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    closed_at TEXT,
                    exit_price REAL,
                    gross_pnl_pct REAL,
                    net_pnl_pct REAL,
                    exit_reason TEXT NOT NULL DEFAULT '',
                    context_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_inverse_shadow_session
                    ON inverse_shadow_trades(
                        market, symbol, entry_session_date
                    );
                CREATE INDEX IF NOT EXISTS idx_inverse_shadow_open
                    ON inverse_shadow_trades(status, market, symbol);
                CREATE TABLE IF NOT EXISTS telegram_message_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    command TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL DEFAULT '',
                    success INTEGER NOT NULL DEFAULT 1,
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_telegram_message_log_created_at
                    ON telegram_message_log(created_at);
                CREATE TABLE IF NOT EXISTS api_call_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    method TEXT NOT NULL,
                    tr_id TEXT NOT NULL DEFAULT '',
                    path TEXT NOT NULL DEFAULT '',
                    success INTEGER NOT NULL DEFAULT 1,
                    http_status INTEGER,
                    msg_cd TEXT NOT NULL DEFAULT '',
                    msg1 TEXT NOT NULL DEFAULT '',
                    elapsed_ms INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_api_call_log_created_at
                    ON api_call_log(created_at);

                CREATE TABLE IF NOT EXISTS market_regimes (
                    market TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    benchmark_code TEXT NOT NULL,
                    benchmark_name TEXT NOT NULL,
                    source TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    is_final INTEGER NOT NULL DEFAULT 0,
                    open_price REAL,
                    high_price REAL,
                    low_price REAL,
                    close_price REAL,
                    previous_close REAL,
                    return_pct REAL,
                    volume INTEGER,
                    turnover REAL,
                    volume_avg_20 REAL,
                    volume_ratio_20 REAL,
                    range_pct REAL,
                    range_avg_20 REAL,
                    range_ratio_20 REAL,
                    trend_regime TEXT NOT NULL DEFAULT 'unknown',
                    activity_regime TEXT NOT NULL DEFAULT 'unknown',
                    volatility_regime TEXT NOT NULL DEFAULT 'unknown',
                    regime_key TEXT NOT NULL DEFAULT 'unknown|unknown|unknown',
                    sample_days INTEGER NOT NULL DEFAULT 0,
                    calculation_version TEXT NOT NULL DEFAULT 'intraday_range_v1',
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (market, session_date)
                );
                CREATE INDEX IF NOT EXISTS idx_market_regimes_date
                    ON market_regimes(session_date);
                CREATE INDEX IF NOT EXISTS idx_market_regimes_key
                    ON market_regimes(market, regime_key, session_date);

                CREATE TABLE IF NOT EXISTS policy_evaluation_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    market TEXT NOT NULL,
                    evaluation_kind TEXT NOT NULL,
                    window_start TEXT,
                    window_end TEXT,
                    subject TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    hypothesis TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    financial_principles_json TEXT NOT NULL DEFAULT '[]',
                    alternatives_json TEXT NOT NULL DEFAULT '[]',
                    confidence REAL,
                    falsification_criteria TEXT NOT NULL DEFAULT '',
                    validation_due_at TEXT,
                    reasoning_mode TEXT NOT NULL DEFAULT '',
                    comparison_baseline TEXT NOT NULL DEFAULT '',
                    comparative_value_status TEXT NOT NULL DEFAULT 'unverified',
                    outcome_json TEXT NOT NULL DEFAULT '{}',
                    reviewed_at TEXT,
                    git_commit TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_policy_evaluation_created
                    ON policy_evaluation_log(created_at);
                CREATE INDEX IF NOT EXISTS idx_policy_evaluation_subject
                    ON policy_evaluation_log(market, subject, created_at);
                """
            )
            self._ensure_column(
                conn,
                "market_regimes",
                "calculation_version",
                "TEXT NOT NULL DEFAULT 'intraday_range_v1'",
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
            self._ensure_column(
                conn,
                "virtual_orders",
                "excluded_from_performance",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(conn, "virtual_orders", "exclude_reason", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "virtual_orders", "excluded_at", "TEXT")
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
            self._ensure_column(conn, "cycle_log", "product_type", "TEXT")
            self._ensure_column(
                conn,
                "cycle_log",
                "cost_calculation_version",
                "TEXT NOT NULL DEFAULT 'legacy_unversioned'",
            )
            self._ensure_column(conn, "cycle_log", "hold_duration_min", "REAL")
            self._ensure_column(conn, "cycle_log", "entry_time", "TEXT")
            self._ensure_column(conn, "cycle_log", "exit_cooldown_remaining", "REAL")
            self._ensure_column(conn, "cycle_log", "cb_active", "INTEGER")
            self._ensure_column(conn, "cycle_log", "pool_size", "INTEGER")
            self._ensure_column(
                conn,
                "cycle_log",
                "execution_group_id",
                "TEXT NOT NULL DEFAULT ''",
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_cycle_log_execution_group
                ON cycle_log(execution_group_id)
                WHERE execution_group_id != ''
                """
            )
            self._ensure_column(conn, "lab_symbol_state", "entry_price", "REAL")
            self._ensure_column(conn, "lab_symbol_state", "entry_time", "TEXT")
            self._ensure_column(conn, "lab_symbol_state", "peak_price", "REAL")
            self._ensure_column(conn, "lab_symbol_state", "has_position", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "lab_symbol_state", "snapshot_json", "TEXT")
            self._ensure_column(conn, "broker_order_events", "strategy_flag", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "broker_order_events", "entry_by", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "broker_order_events", "exit_by", "TEXT NOT NULL DEFAULT ''")
            self._backfill_non_trade_cycle_log_flags(conn)
            self._backfill_missing_exit_labels(conn)

    @staticmethod
    def _backfill_non_trade_cycle_log_flags(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            UPDATE cycle_log
            SET is_session_trade = 0
            WHERE action_bias NOT IN ('BUY_REAL', 'SELL_REAL')
              AND COALESCE(is_session_trade, 1) != 0
            """
        )

    @staticmethod
    def _backfill_missing_exit_labels(conn: sqlite3.Connection) -> None:
        """Populate legacy empty exit_by fields from recorded sell reasons."""
        conn.execute(
            """
            UPDATE cycle_log
            SET exit_by = action_reason
            WHERE action_bias = 'SELL_REAL'
              AND COALESCE(exit_by, '') = ''
              AND COALESCE(action_reason, '') != ''
            """
        )
        conn.execute(
            """
            UPDATE broker_order_events
            SET exit_by = reason
            WHERE UPPER(side) = 'SELL'
              AND COALESCE(exit_by, '') = ''
              AND COALESCE(reason, '') != ''
              AND COALESCE(reason, '') NOT IN (
                  'stale_live_order_cancel_failed',
                  'stale_pending_exit_cancel_failed',
                  'stale_pending_exit_cancelled',
                  'conflicting_buy_cancel_failed',
                  'conflicting_buy_cancelled'
              )
            """
        )

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

    def upsert_market_regime(self, regime: dict) -> None:
        columns = (
            "market",
            "session_date",
            "benchmark_code",
            "benchmark_name",
            "source",
            "captured_at",
            "is_final",
            "open_price",
            "high_price",
            "low_price",
            "close_price",
            "previous_close",
            "return_pct",
            "volume",
            "turnover",
            "volume_avg_20",
            "volume_ratio_20",
            "range_pct",
            "range_avg_20",
            "range_ratio_20",
            "trend_regime",
            "activity_regime",
            "volatility_regime",
            "regime_key",
            "sample_days",
            "calculation_version",
            "raw_json",
        )
        values = []
        for column in columns:
            value = regime.get(column)
            if column == "calculation_version":
                value = str(value or "intraday_range_v1")
            if column == "raw_json" and not isinstance(value, str):
                value = json.dumps(value or {}, ensure_ascii=False, default=str)
            values.append(value)
        update_columns = columns[2:]
        assignments = ", ".join(f"{column} = excluded.{column}" for column in update_columns)
        placeholders = ", ".join("?" for _ in columns)
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO market_regimes ({", ".join(columns)})
                VALUES ({placeholders})
                ON CONFLICT(market, session_date) DO UPDATE SET
                    {assignments}
                """,
                values,
            )

    def get_market_regime(
        self,
        market: str,
        session_date: str | None = None,
        *,
        final_only: bool = False,
    ) -> dict | None:
        where = ["market = ?"]
        params: list[object] = [str(market).strip().lower()]
        if session_date:
            where.append("session_date = ?")
            params.append(str(session_date))
        if final_only:
            where.append("is_final = 1")
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT *
                FROM market_regimes
                WHERE {" AND ".join(where)}
                ORDER BY session_date DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        return dict(row) if row is not None else None

    def list_market_regimes(
        self,
        *,
        market: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        final_only: bool = False,
        limit: int = 250,
    ) -> list[dict]:
        where: list[str] = []
        params: list[object] = []
        if market:
            where.append("market = ?")
            params.append(str(market).strip().lower())
        if start_date:
            where.append("session_date >= ?")
            params.append(str(start_date))
        if end_date:
            where.append("session_date <= ?")
            params.append(str(end_date))
        if final_only:
            where.append("is_final = 1")
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM market_regimes
                {where_sql}
                ORDER BY session_date DESC, market ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def has_outdated_market_regime_calculation(
        self,
        *,
        market: str,
        calculation_version: str,
        start_date: str,
    ) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM market_regimes
                WHERE market = ?
                  AND session_date >= ?
                  AND COALESCE(calculation_version, '') != ?
                LIMIT 1
                """,
                (
                    str(market).strip().lower(),
                    str(start_date),
                    str(calculation_version),
                ),
            ).fetchone()
        return row is not None

    def save_policy_evaluation(
        self,
        *,
        created_at: str,
        market: str,
        evaluation_kind: str,
        subject: str,
        decision: str,
        hypothesis: str,
        evidence: dict | list | None = None,
        financial_principles: list | dict | None = None,
        alternatives: list | dict | None = None,
        confidence: float | None = None,
        falsification_criteria: str = "",
        validation_due_at: str | None = None,
        reasoning_mode: str = "",
        comparison_baseline: str = "",
        comparative_value_status: str = "unverified",
        window_start: str | None = None,
        window_end: str | None = None,
        outcome: dict | list | None = None,
        reviewed_at: str | None = None,
        git_commit: str = "",
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO policy_evaluation_log (
                    created_at, market, evaluation_kind, window_start, window_end,
                    subject, decision, hypothesis, evidence_json,
                    financial_principles_json, alternatives_json, confidence,
                    falsification_criteria, validation_due_at, reasoning_mode,
                    comparison_baseline, comparative_value_status, outcome_json,
                    reviewed_at, git_commit
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    str(market).strip().lower(),
                    evaluation_kind,
                    window_start,
                    window_end,
                    subject,
                    decision,
                    hypothesis,
                    json.dumps(evidence or {}, ensure_ascii=False, default=str),
                    json.dumps(
                        financial_principles or [],
                        ensure_ascii=False,
                        default=str,
                    ),
                    json.dumps(alternatives or [], ensure_ascii=False, default=str),
                    confidence,
                    falsification_criteria,
                    validation_due_at,
                    reasoning_mode,
                    comparison_baseline,
                    comparative_value_status,
                    json.dumps(outcome or {}, ensure_ascii=False, default=str),
                    reviewed_at,
                    git_commit,
                ),
            )
            return int(cursor.lastrowid)

    def update_policy_evaluation_outcome(
        self,
        evaluation_id: int,
        *,
        outcome: dict | list,
        reviewed_at: str,
        comparative_value_status: str,
        git_commit: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE policy_evaluation_log
                SET outcome_json = ?,
                    reviewed_at = ?,
                    comparative_value_status = ?,
                    git_commit = CASE WHEN ? != '' THEN ? ELSE git_commit END
                WHERE id = ?
                """,
                (
                    json.dumps(outcome, ensure_ascii=False, default=str),
                    reviewed_at,
                    comparative_value_status,
                    git_commit,
                    git_commit,
                    int(evaluation_id),
                ),
            )

    def list_policy_evaluations(
        self,
        *,
        market: str | None = None,
        subject: str | None = None,
        pending_only: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        where: list[str] = []
        params: list[object] = []
        if market:
            where.append("market = ?")
            params.append(str(market).strip().lower())
        if subject:
            where.append("subject = ?")
            params.append(subject)
        if pending_only:
            where.append("reviewed_at IS NULL")
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM policy_evaluation_log
                {where_sql}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        result: list[dict] = []
        for row in rows:
            item = dict(row)
            for key in (
                "evidence_json",
                "financial_principles_json",
                "alternatives_json",
                "outcome_json",
            ):
                try:
                    item[key] = json.loads(str(item.get(key) or ""))
                except json.JSONDecodeError:
                    pass
            result.append(item)
        return result

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
        product_type: str | None = None,
        cost_calculation_version: str = "legacy_unversioned",
        hold_duration_min: float | None = None,
        entry_time: str | None = None,
        exit_cooldown_remaining: float | None = None,
        cb_active: int | None = None,
        pool_size: int | None = None,
        execution_group_id: str = "",
    ) -> bool:
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
                "product_type",
                "cost_calculation_version",
                "hold_duration_min",
                "entry_time",
                "exit_cooldown_remaining",
                "cb_active",
                "pool_size",
                "execution_group_id",
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
                product_type,
                cost_calculation_version,
                hold_duration_min,
                entry_time,
                exit_cooldown_remaining,
                cb_active,
                pool_size,
                execution_group_id,
            )
            insert_clause = "INSERT OR IGNORE" if execution_group_id else "INSERT"
            cursor = conn.execute(
                f"""
                {insert_clause} INTO cycle_log ({', '.join(columns)})
                VALUES ({', '.join(['?'] * len(columns))})
                """,
                values,
            )
            inserted = int(cursor.rowcount or 0) > 0
            if execution_group_id:
                row = conn.execute(
                    """
                    SELECT id
                    FROM cycle_log
                    WHERE execution_group_id = ?
                    LIMIT 1
                    """,
                    (execution_group_id,),
                ).fetchone()
                cycle_log_id = int(row["id"]) if row is not None else None
                conn.execute(
                    """
                    UPDATE broker_order_executions
                    SET finalized_at = COALESCE(finalized_at, ?),
                        cycle_log_id = COALESCE(cycle_log_id, ?),
                        updated_at = ?
                    WHERE execution_group_id = ?
                    """,
                    (
                        logged_at,
                        cycle_log_id,
                        logged_at,
                        execution_group_id,
                    ),
                )
            return inserted

    def reconcile_domestic_sell_costs(
        self,
        *,
        product_types: dict[str, str],
        commission_rate: float,
        stock_sell_tax_rate: float,
        calculation_version: str = DOMESTIC_COST_CALCULATION_VERSION,
    ) -> dict[str, object]:
        normalized_types = {
            str(symbol).strip().upper(): str(product_type or "").strip()
            for symbol, product_type in product_types.items()
            if str(symbol).strip() and str(product_type or "").strip()
        }
        stats: dict[str, object] = {
            "eligible": 0,
            "updated": 0,
            "unchanged": 0,
            "missing_product_type": [],
            "applied_sell_tax_krw": 0.0,
            "net_adjustment_krw": 0.0,
        }
        missing: set[str] = set()
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    id,
                    symbol,
                    entry_price,
                    price,
                    qty_executed,
                    realized_pnl_krw,
                    net_pnl_krw,
                    commission_krw,
                    product_type,
                    cost_calculation_version
                FROM cycle_log
                WHERE market = 'domestic'
                  AND action_bias = 'SELL_REAL'
                  AND COALESCE(entry_price, 0) > 0
                  AND COALESCE(price, 0) > 0
                  AND COALESCE(qty_executed, 0) > 0
                  AND {CONFIRMED_SELL_CYCLE_PREDICATE}
                ORDER BY id
                """
            ).fetchall()
            stats["eligible"] = len(rows)
            for row in rows:
                symbol = str(row["symbol"] or "").strip().upper()
                product_type = normalized_types.get(
                    symbol,
                    str(row["product_type"] or "").strip(),
                )
                if not product_type:
                    missing.add(symbol)
                    continue
                estimate = estimate_domestic_trade_costs(
                    entry_price=float(row["entry_price"]),
                    exit_price=float(row["price"]),
                    qty=int(row["qty_executed"]),
                    commission_rate=commission_rate,
                    stock_sell_tax_rate=stock_sell_tax_rate,
                    product_type=product_type,
                )
                old_net = float(
                    row["net_pnl_krw"]
                    if row["net_pnl_krw"] is not None
                    else row["realized_pnl_krw"]
                    or 0.0
                )
                unchanged = (
                    str(row["product_type"] or "") == product_type
                    and str(row["cost_calculation_version"] or "")
                    == calculation_version
                    and abs(
                        float(row["realized_pnl_krw"] or 0.0)
                        - estimate.gross_pnl_krw
                    )
                    < 0.005
                    and abs(old_net - estimate.net_pnl_krw) < 0.005
                    and abs(
                        float(row["commission_krw"] or 0.0)
                        - estimate.sell_cost_krw
                    )
                    < 0.005
                )
                if unchanged:
                    stats["unchanged"] = int(stats["unchanged"]) + 1
                    continue
                conn.execute(
                    """
                    UPDATE cycle_log
                    SET realized_pnl_krw = ?,
                        net_pnl_krw = ?,
                        commission_krw = ?,
                        product_type = ?,
                        cost_calculation_version = ?
                    WHERE id = ?
                    """,
                    (
                        estimate.gross_pnl_krw,
                        estimate.net_pnl_krw,
                        round(estimate.sell_cost_krw, 2),
                        product_type,
                        calculation_version,
                        int(row["id"]),
                    ),
                )
                stats["updated"] = int(stats["updated"]) + 1
                stats["applied_sell_tax_krw"] = round(
                    float(stats["applied_sell_tax_krw"])
                    + estimate.sell_tax_krw,
                    2,
                )
                stats["net_adjustment_krw"] = round(
                    float(stats["net_adjustment_krw"])
                    + estimate.net_pnl_krw
                    - old_net,
                    2,
                )
        stats["missing_product_type"] = sorted(missing)
        return stats

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

    def has_inverse_policy_observation(
        self,
        *,
        event_type: str,
        market: str,
        symbol: str,
        expected_session_date: str,
        reason: str,
        observation_version: str = "",
    ) -> bool:
        """Return whether one durable observation key was already recorded."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT detail
                FROM event_log
                WHERE event_type = ?
                  AND market = ?
                  AND symbol = ?
                ORDER BY id DESC
                """,
                (
                    str(event_type),
                    str(market).strip().lower(),
                    str(symbol).strip().upper(),
                ),
            ).fetchall()
        for row in rows:
            try:
                detail = json.loads(str(row["detail"] or "{}"))
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                str(detail.get("expected_session_date") or "")
                == str(expected_session_date)
                and str(detail.get("reason") or "") == str(reason)
                and str(
                    detail.get("observation_version")
                    or detail.get("entry_formula")
                    or ""
                )
                == str(observation_version or "")
            ):
                return True
        return False

    def get_inverse_policy_observation_summary(
        self,
        *,
        after_logged_at: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Summarize unique inverse-policy observations without restart inflation."""
        observation_types = (
            "inverse_regime_observed",
            "inverse_quote_failed",
            "inverse_quote_excluded",
            "inverse_product_blocked",
            "inverse_product_ready",
        )
        placeholders = ", ".join("?" for _ in observation_types)
        where = [f"event_type IN ({placeholders})"]
        params: list[object] = [*observation_types]
        if after_logged_at:
            where.append("logged_at >= ?")
            params.append(str(after_logged_at))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT logged_at, event_type, market, symbol, detail
                FROM event_log
                WHERE {' AND '.join(where)}
                ORDER BY id DESC
                """,
                params,
            ).fetchall()

        grouped: dict[tuple[str, str, str, str], dict] = {}
        for row in rows:
            try:
                detail = json.loads(str(row["detail"] or "{}"))
            except (TypeError, json.JSONDecodeError):
                detail = {}
            event_type = str(row["event_type"] or "")
            market = str(row["market"] or "").strip().lower()
            reason = str(detail.get("reason") or "unknown")
            observation_version = str(
                detail.get("observation_version")
                or detail.get("entry_formula")
                or ""
            )
            key = (market, event_type, reason, observation_version)
            bucket = grouped.setdefault(
                key,
                {
                    "market": market,
                    "event_type": event_type,
                    "reason": reason,
                    "observation_version": observation_version,
                    "observation_count": 0,
                    "latest_logged_at": str(row["logged_at"] or ""),
                    "symbols": set(),
                    "_keys": set(),
                },
            )
            symbol = str(row["symbol"] or "").strip().upper()
            session_date = str(detail.get("expected_session_date") or "")
            observation_key = (session_date, symbol)
            keys = bucket["_keys"]
            if observation_key not in keys:
                keys.add(observation_key)
                bucket["observation_count"] += 1
            if symbol:
                bucket["symbols"].add(symbol)

        result: list[dict] = []
        for bucket in grouped.values():
            result.append(
                {
                    "market": bucket["market"],
                    "event_type": bucket["event_type"],
                    "reason": bucket["reason"],
                    "observation_version": bucket["observation_version"],
                    "observation_count": bucket["observation_count"],
                    "latest_logged_at": bucket["latest_logged_at"],
                    "symbols": sorted(bucket["symbols"]),
                }
            )
        result.sort(
            key=lambda row: (
                -int(row["observation_count"]),
                str(row["market"]),
                str(row["event_type"]),
                str(row["reason"]),
            )
        )
        return result[: max(1, int(limit))]

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
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
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
            return int(cursor.lastrowid)

    @staticmethod
    def normalize_broker_order_no(order_no: object) -> str:
        text = str(order_no or "").strip()
        if not text:
            return ""
        if text.isdigit():
            return text.lstrip("0") or "0"
        return text.upper()

    @staticmethod
    def _broker_order_date(created_at: str, market: str) -> str:
        parsed = parse_datetime(created_at)
        if parsed is None:
            parsed = datetime.now(timezone.utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        market_key = str(market).strip().lower()
        market_timezone = NEW_YORK if market_key == "overseas" else KST
        return parsed.astimezone(market_timezone).date().isoformat()

    def save_broker_order_execution(
        self,
        *,
        broker_event_id: int,
        created_at: str,
        market: str,
        symbol: str,
        exchange_code: str | None,
        side: str,
        broker_order_no: str,
        requested_qty: int,
        requested_price: float | None,
        strategy_flag: str = "",
        entry_by: str = "",
        exit_by: str = "",
        reason: str = "",
        session_id: str = "",
        cycle_no: int = 0,
        is_session_trade: int = 0,
        entry_price: float | None = None,
        entry_time: str | None = None,
        hold_duration_min: float | None = None,
        context: dict | None = None,
        replacement_for_order_no: str = "",
    ) -> dict | None:
        normalized_order_no = self.normalize_broker_order_no(broker_order_no)
        if not normalized_order_no or requested_qty <= 0:
            return None
        market_key = str(market).strip().lower()
        order_date = self._broker_order_date(created_at, market_key)
        symbol_key = str(symbol).strip().upper()
        replacement_normalized = self.normalize_broker_order_no(
            replacement_for_order_no
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        execution_group_id = uuid.uuid4().hex
        group_target_qty = int(requested_qty)

        with self._connect() as conn:
            if replacement_normalized:
                replacement = conn.execute(
                    """
                    SELECT execution_group_id, group_target_qty
                    FROM broker_order_executions
                    WHERE market = ?
                      AND symbol = ?
                      AND normalized_order_no = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (market_key, symbol_key, replacement_normalized),
                ).fetchone()
                if replacement is not None:
                    execution_group_id = str(replacement["execution_group_id"])
                    group_target_qty = int(replacement["group_target_qty"])

            conn.execute(
                """
                INSERT OR IGNORE INTO broker_order_executions (
                    broker_event_id, created_at, order_date, updated_at,
                    market, symbol, exchange_code, side, broker_order_no,
                    normalized_order_no, execution_group_id, group_target_qty,
                    requested_qty, requested_price, strategy_flag, entry_by,
                    exit_by, reason, session_id, cycle_no, is_session_trade,
                    entry_price, entry_time, hold_duration_min, context_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    int(broker_event_id),
                    created_at,
                    order_date,
                    now_iso,
                    market_key,
                    symbol_key,
                    exchange_code,
                    str(side).strip().upper(),
                    str(broker_order_no).strip(),
                    normalized_order_no,
                    execution_group_id,
                    group_target_qty,
                    int(requested_qty),
                    requested_price,
                    strategy_flag,
                    entry_by,
                    exit_by,
                    reason,
                    session_id,
                    int(cycle_no),
                    int(is_session_trade),
                    entry_price,
                    entry_time,
                    hold_duration_min,
                    json.dumps(context or {}, ensure_ascii=False, default=str),
                ),
            )
            row = conn.execute(
                """
                SELECT *
                FROM broker_order_executions
                WHERE broker_event_id = ?
                LIMIT 1
                """,
                (int(broker_event_id),),
            ).fetchone()
        return self._decode_broker_execution_row(row)

    @staticmethod
    def _decode_broker_execution_row(
        row: sqlite3.Row | None,
    ) -> dict | None:
        if row is None:
            return None
        item = dict(row)
        for key in ("context_json", "history_json"):
            try:
                item[key] = json.loads(str(item.get(key) or "{}"))
            except json.JSONDecodeError:
                item[key] = {}
        return item

    def list_unfinalized_broker_executions(
        self,
        *,
        market: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        where = ["finalized_at IS NULL"]
        params: list[object] = []
        if market:
            where.append("market = ?")
            params.append(str(market).strip().lower())
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM broker_order_executions
                WHERE {' AND '.join(where)}
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            decoded
            for row in rows
            if (decoded := self._decode_broker_execution_row(row)) is not None
        ]

    def update_broker_order_execution(
        self,
        execution_id: int,
        *,
        filled_qty: int,
        filled_amount: float,
        avg_fill_price: float | None,
        remaining_qty: int,
        canceled_qty: int,
        rejected_qty: int,
        status: str,
        history: dict,
        checked_at: str,
        fill_recorded_at: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE broker_order_executions
                SET filled_qty = ?,
                    filled_amount = ?,
                    avg_fill_price = ?,
                    remaining_qty = ?,
                    canceled_qty = ?,
                    rejected_qty = ?,
                    status = ?,
                    history_json = ?,
                    last_checked_at = ?,
                    fill_recorded_at = COALESCE(?, fill_recorded_at),
                    updated_at = ?
                WHERE id = ?
                  AND finalized_at IS NULL
                """,
                (
                    max(0, int(filled_qty)),
                    max(0.0, float(filled_amount)),
                    avg_fill_price,
                    max(0, int(remaining_qty)),
                    max(0, int(canceled_qty)),
                    max(0, int(rejected_qty)),
                    str(status).strip().upper(),
                    json.dumps(history, ensure_ascii=False, default=str),
                    checked_at,
                    fill_recorded_at,
                    checked_at,
                    int(execution_id),
                ),
            )

    def mark_broker_execution_checked(
        self,
        execution_id: int,
        *,
        checked_at: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE broker_order_executions
                SET last_checked_at = ?,
                    updated_at = ?
                WHERE id = ?
                  AND finalized_at IS NULL
                """,
                (checked_at, checked_at, int(execution_id)),
            )

    def finalize_broker_execution_group_without_fill(
        self,
        execution_group_id: str,
        *,
        finalized_at: str,
    ) -> bool:
        return self.finalize_broker_execution_group(
            execution_group_id,
            finalized_at=finalized_at,
        )

    def finalize_broker_execution_group(
        self,
        execution_group_id: str,
        *,
        finalized_at: str,
    ) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE broker_order_executions
                SET finalized_at = ?,
                    updated_at = ?
                WHERE execution_group_id = ?
                  AND finalized_at IS NULL
                """,
                (finalized_at, finalized_at, execution_group_id),
            )
            return int(cursor.rowcount or 0) > 0

    def update_cycle_log_execution_risk(
        self,
        execution_group_id: str,
        *,
        consecutive_losses: int,
        cb_active: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE cycle_log
                SET consecutive_losses = ?,
                    cb_active = ?
                WHERE execution_group_id = ?
                """,
                (
                    max(0, int(consecutive_losses)),
                    1 if cb_active else 0,
                    str(execution_group_id),
                ),
            )

    def open_inverse_shadow_trade(
        self,
        *,
        opened_at: str,
        market: str,
        symbol: str,
        exchange_code: str | None,
        entry_session_date: str,
        policy_id: str,
        entry_price: float,
        entry_spread_pct: float,
        commission_rate: float,
        benchmark_name: str,
        benchmark_return_pct: float | None,
        benchmark_regime_key: str,
        entry_reason: str,
        strategy_flag: str,
        entry_by: str,
        context: dict | None = None,
    ) -> bool:
        if entry_price <= 0:
            return False
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO inverse_shadow_trades (
                    opened_at, updated_at, market, symbol, exchange_code,
                    entry_session_date, policy_id, entry_price,
                    entry_spread_pct, commission_rate, benchmark_name,
                    benchmark_return_pct, benchmark_regime_key, entry_reason,
                    strategy_flag, entry_by, peak_price, trough_price,
                    last_price, context_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?
                )
                """,
                (
                    opened_at,
                    opened_at,
                    str(market).strip().lower(),
                    str(symbol).strip().upper(),
                    exchange_code,
                    str(entry_session_date),
                    str(policy_id),
                    float(entry_price),
                    max(0.0, float(entry_spread_pct)),
                    max(0.0, float(commission_rate)),
                    str(benchmark_name),
                    benchmark_return_pct,
                    str(benchmark_regime_key),
                    str(entry_reason),
                    str(strategy_flag),
                    str(entry_by),
                    float(entry_price),
                    float(entry_price),
                    float(entry_price),
                    json.dumps(context or {}, ensure_ascii=False, default=str),
                ),
            )
            return int(cursor.rowcount or 0) > 0

    def get_open_inverse_shadow_trade(
        self,
        market: str,
        symbol: str,
    ) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM inverse_shadow_trades
                WHERE market = ?
                  AND symbol = ?
                  AND status = 'OPEN'
                ORDER BY id DESC
                LIMIT 1
                """,
                (
                    str(market).strip().lower(),
                    str(symbol).strip().upper(),
                ),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        try:
            item["context_json"] = json.loads(str(item.get("context_json") or "{}"))
        except json.JSONDecodeError:
            item["context_json"] = {}
        return item

    def list_open_inverse_shadow_trades(
        self,
        *,
        market: str | None = None,
    ) -> list[dict]:
        where = ["status = 'OPEN'"]
        params: list[object] = []
        if market:
            where.append("market = ?")
            params.append(str(market).strip().lower())
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM inverse_shadow_trades
                WHERE {' AND '.join(where)}
                ORDER BY opened_at ASC, id ASC
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def update_inverse_shadow_trade(
        self,
        trade_id: int,
        *,
        updated_at: str,
        hold_cycles: int,
        peak_price: float,
        trough_price: float,
        last_price: float,
        closed_at: str | None = None,
        exit_price: float | None = None,
        gross_pnl_pct: float | None = None,
        net_pnl_pct: float | None = None,
        exit_reason: str = "",
    ) -> bool:
        status = "CLOSED" if closed_at else "OPEN"
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE inverse_shadow_trades
                SET updated_at = ?,
                    hold_cycles = ?,
                    peak_price = ?,
                    trough_price = ?,
                    last_price = ?,
                    status = ?,
                    closed_at = ?,
                    exit_price = ?,
                    gross_pnl_pct = ?,
                    net_pnl_pct = ?,
                    exit_reason = ?
                WHERE id = ?
                  AND status = 'OPEN'
                """,
                (
                    updated_at,
                    max(0, int(hold_cycles)),
                    float(peak_price),
                    float(trough_price),
                    float(last_price),
                    status,
                    closed_at,
                    exit_price,
                    gross_pnl_pct,
                    net_pnl_pct,
                    str(exit_reason),
                    int(trade_id),
                ),
            )
            return int(cursor.rowcount or 0) > 0

    def get_inverse_shadow_performance(
        self,
        *,
        after_opened_at: str | None = None,
    ) -> list[dict]:
        where = ["1 = 1"]
        params: list[object] = []
        if after_opened_at:
            where.append("opened_at >= ?")
            params.append(str(after_opened_at))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    market,
                    SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END)
                        AS open_count,
                    SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END)
                        AS closed_count,
                    SUM(
                        CASE
                            WHEN status = 'CLOSED' AND net_pnl_pct > 0 THEN 1
                            ELSE 0
                        END
                    ) AS win_count,
                    AVG(
                        CASE WHEN status = 'CLOSED' THEN net_pnl_pct END
                    ) AS avg_net_pnl_pct,
                    SUM(
                        CASE WHEN status = 'CLOSED' THEN net_pnl_pct ELSE 0 END
                    ) AS total_net_pnl_pct
                FROM inverse_shadow_trades
                WHERE {' AND '.join(where)}
                GROUP BY market
                ORDER BY market
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def save_telegram_message(
        self,
        *,
        created_at: str,
        direction: str,
        command: str = "",
        text: str = "",
        success: bool = True,
        error: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO telegram_message_log (
                    created_at, direction, command, text, success, error
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (created_at, direction, command, text, 1 if success else 0, error),
            )

    def list_telegram_messages(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM telegram_message_log ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_api_call(
        self,
        *,
        created_at: str,
        method: str,
        tr_id: str = "",
        path: str = "",
        success: bool = True,
        http_status: int | None = None,
        msg_cd: str = "",
        msg1: str = "",
        elapsed_ms: int | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO api_call_log (
                    created_at, method, tr_id, path, success, http_status,
                    msg_cd, msg1, elapsed_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    method,
                    tr_id,
                    path,
                    1 if success else 0,
                    http_status,
                    msg_cd,
                    msg1,
                    elapsed_ms,
                ),
            )

    def list_api_calls(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM api_call_log ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

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

    def list_broker_order_executions(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM broker_order_executions
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [
            decoded
            for row in rows
            if (decoded := self._decode_broker_execution_row(row)) is not None
        ]

    def list_confirmed_session_buy_symbols(
        self,
        *,
        session_id: str,
    ) -> list[str]:
        session_key = str(session_id).strip()
        if not session_key:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT symbol
                FROM broker_order_executions
                WHERE session_id = ?
                  AND UPPER(side) = 'BUY'
                  AND filled_qty > 0
                  AND is_session_trade = 1
                ORDER BY symbol
                """,
                (session_key,),
            ).fetchall()
        return [
            str(row["symbol"] or "").strip().upper()
            for row in rows
            if str(row["symbol"] or "").strip()
        ]

    def get_recent_completed_sell_execution(
        self,
        *,
        market: str,
        symbol: str,
        after_updated_at: str,
    ) -> dict | None:
        """Return a recent execution group only when its full sell target filled."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    execution_group_id,
                    MAX(group_target_qty) AS target_qty,
                    SUM(COALESCE(filled_qty, 0)) AS filled_qty,
                    MAX(updated_at) AS latest_updated_at,
                    MAX(fill_recorded_at) AS latest_fill_recorded_at
                FROM broker_order_executions
                WHERE market = ?
                  AND symbol = ?
                  AND side = 'SELL'
                  AND updated_at >= ?
                GROUP BY execution_group_id
                HAVING MAX(group_target_qty) > 0
                   AND SUM(COALESCE(filled_qty, 0)) >= MAX(group_target_qty)
                ORDER BY latest_updated_at DESC
                LIMIT 1
                """,
                (
                    str(market).strip().lower(),
                    str(symbol).strip().upper(),
                    str(after_updated_at),
                ),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_recent_unfinalized_sell_execution(
        self,
        *,
        market: str,
        symbol: str,
        after_created_at: str,
    ) -> dict | None:
        """Return the latest accepted sell group still awaiting broker finality."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    execution_group_id,
                    MAX(group_target_qty) AS target_qty,
                    SUM(COALESCE(requested_qty, 0)) AS pending_requested_qty,
                    SUM(COALESCE(filled_qty, 0)) AS filled_qty,
                    MAX(created_at) AS latest_created_at,
                    MAX(updated_at) AS latest_updated_at
                FROM broker_order_executions
                WHERE market = ?
                  AND symbol = ?
                  AND side = 'SELL'
                  AND created_at >= ?
                  AND finalized_at IS NULL
                  AND status IN ('PENDING', 'PARTIAL')
                GROUP BY execution_group_id
                HAVING MAX(group_target_qty) > 0
                ORDER BY latest_created_at DESC
                LIMIT 1
                """,
                (
                    str(market).strip().lower(),
                    str(symbol).strip().upper(),
                    str(after_created_at),
                ),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_submitted_order_audit_rows(
        self,
        *,
        limit: int = 20,
        source_limit: int = 500,
        include_canceled: bool = False,
    ) -> list[dict]:
        """Return real SUBMITTED orders that still need external fill confirmation.

        New orders are resolved from broker_order_executions. Legacy submissions
        without an execution row remain visible as externally unverified.
        """
        rows = self.list_broker_order_events(limit=source_limit)
        executions_by_event_id = {
            int(row["broker_event_id"]): row
            for row in self.list_broker_order_executions(limit=source_limit)
        }
        followups_by_original_order_no: dict[str, dict] = {}
        terminal_cancel_statuses = {"CANCELED", "CANCELLED"}

        for row in rows:
            status = str(row.get("status") or "").strip().upper()
            order_kind = str(row.get("order_kind") or "").strip().lower()
            if status == "SUBMITTED" and order_kind != "cancel":
                continue
            payload = row.get("payload_json") if isinstance(row.get("payload_json"), dict) else {}
            order_numbers: set[str] = set()
            broker_order_no = str(row.get("broker_order_no") or "").strip()
            if broker_order_no:
                order_numbers.add(broker_order_no)
            if isinstance(payload, dict):
                original_order_no = str(payload.get("original_order_no") or "").strip()
                if original_order_no:
                    order_numbers.add(original_order_no)
            for order_no in order_numbers:
                followups_by_original_order_no.setdefault(order_no, row)

        result: list[dict] = []
        for row in rows:
            status = str(row.get("status") or "").strip().upper()
            order_kind = str(row.get("order_kind") or "").strip().lower()
            if status != "SUBMITTED" or order_kind == "cancel":
                continue
            if int(row.get("is_virtual") or 0):
                continue

            item = dict(row)
            execution = executions_by_event_id.get(int(item.get("id") or 0))
            if execution is not None:
                if execution.get("finalized_at"):
                    continue
                item["fill_status"] = str(execution.get("status") or "")
                item["filled_qty"] = int(execution.get("filled_qty") or 0)
                item["remaining_qty"] = int(execution.get("remaining_qty") or 0)
                item["avg_fill_price"] = execution.get("avg_fill_price")
                item["last_fill_checked_at"] = execution.get("last_checked_at")
            broker_order_no = str(item.get("broker_order_no") or "").strip()
            followup = followups_by_original_order_no.get(broker_order_no)
            if followup is not None:
                followup_status = str(followup.get("status") or "").strip().upper()
                item["followup_status"] = followup_status
                item["followup_reason"] = str(followup.get("reason") or "")
                item["followup_created_at"] = str(followup.get("created_at") or "")
                if followup_status in terminal_cancel_statuses and not include_canceled:
                    continue

            result.append(item)
            if len(result) >= limit:
                break
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
        entry_time: str | None = None,
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
                    entry_price, entry_time, peak_price, has_position, snapshot_json,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    entry_time = COALESCE(excluded.entry_time, lab_symbol_state.entry_time),
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
                    entry_time,
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

    def get_latest_position_entry_time(self, market: str, symbol: str) -> str | None:
        """Recover the active entry time from immutable fill/position ledgers."""
        market_key = str(market).strip().lower()
        symbol_key = str(symbol).strip().upper()
        with self._connect() as conn:
            confirmed = conn.execute(
                """
                WITH buy_groups AS (
                    SELECT
                        execution_group_id,
                        MIN(
                            COALESCE(fill_recorded_at, updated_at, created_at)
                        ) AS entry_time,
                        MAX(
                            COALESCE(fill_recorded_at, updated_at, created_at)
                        ) AS latest_fill_at
                    FROM broker_order_executions
                    WHERE market = ?
                      AND symbol = ?
                      AND UPPER(side) = 'BUY'
                      AND filled_qty > 0
                    GROUP BY execution_group_id
                )
                SELECT entry_time
                FROM buy_groups
                ORDER BY latest_fill_at DESC
                LIMIT 1
                """,
                (market_key, symbol_key),
            ).fetchone()
            latest_sell = conn.execute(
                """
                WITH sell_groups AS (
                    SELECT
                        execution_group_id,
                        MAX(group_target_qty) AS target_qty,
                        SUM(filled_qty) AS filled_qty,
                        MAX(
                            COALESCE(fill_recorded_at, updated_at, created_at)
                        ) AS latest_fill_at
                    FROM broker_order_executions
                    WHERE market = ?
                      AND symbol = ?
                      AND UPPER(side) = 'SELL'
                    GROUP BY execution_group_id
                )
                SELECT MAX(latest_fill_at) AS latest_fill_at
                FROM sell_groups
                WHERE target_qty > 0
                  AND filled_qty >= target_qty
                """,
                (market_key, symbol_key),
            ).fetchone()
            virtual = conn.execute(
                """
                SELECT opened_at
                FROM virtual_positions
                WHERE market = ?
                  AND symbol = ?
                  AND qty > 0
                LIMIT 1
                """,
                (market_key, symbol_key),
            ).fetchone()

        candidates: list[tuple[datetime, str]] = []
        confirmed_text = (
            "" if confirmed is None else str(confirmed["entry_time"] or "").strip()
        )
        confirmed_time = parse_datetime(confirmed_text)
        latest_sell_time = parse_datetime(
            "" if latest_sell is None else str(latest_sell["latest_fill_at"] or "")
        )
        if (
            confirmed_time is not None
            and (
                latest_sell_time is None
                or confirmed_time > latest_sell_time
            )
        ):
            candidates.append((confirmed_time, confirmed_text))

        virtual_text = (
            "" if virtual is None else str(virtual["opened_at"] or "").strip()
        )
        virtual_time = parse_datetime(virtual_text)
        if virtual_time is not None:
            candidates.append((virtual_time, virtual_text))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]

    def repair_confirmed_cycle_entry_timing(
        self,
        *,
        apply: bool = False,
        tolerance_seconds: float = 5.0,
    ) -> list[dict]:
        """Audit or repair SELL_REAL entry timing from confirmed execution groups."""
        tolerance = max(0.0, float(tolerance_seconds))
        repairs: list[dict] = []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    cycle_log.id,
                    cycle_log.market,
                    cycle_log.symbol,
                    cycle_log.action_reason,
                    cycle_log.execution_group_id,
                    cycle_log.entry_time,
                    cycle_log.hold_duration_min,
                    MIN(sell_execution.created_at) AS signal_at
                FROM cycle_log
                JOIN broker_order_executions AS sell_execution
                  ON sell_execution.execution_group_id =
                     cycle_log.execution_group_id
                 AND UPPER(sell_execution.side) = 'SELL'
                WHERE cycle_log.action_bias = 'SELL_REAL'
                  AND COALESCE(cycle_log.execution_group_id, '') != ''
                  AND EXISTS (
                      SELECT 1
                      FROM broker_order_executions AS confirmed_sell
                      WHERE confirmed_sell.execution_group_id =
                            cycle_log.execution_group_id
                        AND UPPER(confirmed_sell.side) = 'SELL'
                        AND confirmed_sell.filled_qty > 0
                  )
                GROUP BY cycle_log.id
                ORDER BY cycle_log.id
                """
            ).fetchall()
            for row in rows:
                signal_text = str(row["signal_at"] or "").strip()
                signal_at = parse_datetime(signal_text)
                if signal_at is None:
                    continue
                buy = conn.execute(
                    """
                    WITH buy_groups AS (
                        SELECT
                            execution_group_id,
                            MIN(
                                COALESCE(
                                    fill_recorded_at,
                                    updated_at,
                                    created_at
                                )
                            ) AS entry_at,
                            MAX(
                                COALESCE(
                                    fill_recorded_at,
                                    updated_at,
                                    created_at
                                )
                            ) AS latest_fill_at
                        FROM broker_order_executions
                        WHERE market = ?
                          AND symbol = ?
                          AND UPPER(side) = 'BUY'
                          AND filled_qty > 0
                        GROUP BY execution_group_id
                    )
                    SELECT entry_at, latest_fill_at
                    FROM buy_groups
                    WHERE latest_fill_at <= ?
                    ORDER BY latest_fill_at DESC
                    LIMIT 1
                    """,
                    (
                        str(row["market"] or "").strip().lower(),
                        str(row["symbol"] or "").strip().upper(),
                        signal_text,
                    ),
                ).fetchone()
                if buy is None:
                    continue
                canonical_entry_text = str(buy["entry_at"] or "").strip()
                canonical_entry_at = parse_datetime(canonical_entry_text)
                if canonical_entry_at is None or canonical_entry_at > signal_at:
                    continue

                prior_sell = conn.execute(
                    """
                    WITH sell_groups AS (
                        SELECT
                            execution_group_id,
                            MAX(group_target_qty) AS target_qty,
                            SUM(COALESCE(filled_qty, 0)) AS filled_qty,
                            MAX(
                                COALESCE(
                                    fill_recorded_at,
                                    updated_at,
                                    created_at
                                )
                            ) AS completed_at
                        FROM broker_order_executions
                        WHERE market = ?
                          AND symbol = ?
                          AND UPPER(side) = 'SELL'
                          AND execution_group_id != ?
                        GROUP BY execution_group_id
                    )
                    SELECT MAX(completed_at) AS completed_at
                    FROM sell_groups
                    WHERE target_qty > 0
                      AND filled_qty >= target_qty
                      AND completed_at <= ?
                    """,
                    (
                        str(row["market"] or "").strip().lower(),
                        str(row["symbol"] or "").strip().upper(),
                        str(row["execution_group_id"] or ""),
                        signal_text,
                    ),
                ).fetchone()
                prior_sell_at = parse_datetime(
                    ""
                    if prior_sell is None
                    else str(prior_sell["completed_at"] or "")
                )
                if prior_sell_at is not None and prior_sell_at >= canonical_entry_at:
                    continue

                canonical_hold_min = round(
                    max(
                        0.0,
                        (signal_at - canonical_entry_at).total_seconds() / 60.0,
                    ),
                    2,
                )
                recorded_entry_text = str(row["entry_time"] or "").strip()
                recorded_entry_at = parse_datetime(recorded_entry_text)
                entry_error_seconds = (
                    float("inf")
                    if recorded_entry_at is None
                    else abs(
                        (recorded_entry_at - canonical_entry_at).total_seconds()
                    )
                )
                recorded_hold = row["hold_duration_min"]
                hold_error_seconds = (
                    float("inf")
                    if recorded_hold is None
                    else abs(
                        (float(recorded_hold) - canonical_hold_min) * 60.0
                    )
                )
                if (
                    entry_error_seconds <= tolerance
                    and hold_error_seconds <= tolerance
                ):
                    continue

                repair = {
                    "cycle_log_id": int(row["id"]),
                    "market": str(row["market"] or ""),
                    "symbol": str(row["symbol"] or ""),
                    "action_reason": str(row["action_reason"] or ""),
                    "signal_at": signal_text,
                    "recorded_entry_time": recorded_entry_text or None,
                    "canonical_entry_time": canonical_entry_text,
                    "recorded_hold_duration_min": (
                        None
                        if recorded_hold is None
                        else float(recorded_hold)
                    ),
                    "canonical_hold_duration_min": canonical_hold_min,
                }
                repairs.append(repair)
                if apply:
                    conn.execute(
                        """
                        UPDATE cycle_log
                        SET entry_time = ?,
                            hold_duration_min = ?
                        WHERE id = ?
                        """,
                        (
                            canonical_entry_text,
                            canonical_hold_min,
                            int(row["id"]),
                        ),
                    )
            if apply:
                conn.commit()
        return repairs

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

    def clear_stale_lab_positions(
        self,
        *,
        markets: set[str],
        active_keys: set[tuple[str, str]],
        updated_at: str,
    ) -> list[dict]:
        market_keys = {market.strip().lower() for market in markets if market.strip()}
        if not market_keys:
            return []
        active_normalized = {
            (market.strip().lower(), symbol.strip().upper())
            for market, symbol in active_keys
            if market.strip() and symbol.strip()
        }
        placeholders = ",".join("?" for _ in market_keys)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM lab_symbol_state
                WHERE has_position = 1
                  AND market IN ({placeholders})
                """,
                sorted(market_keys),
            ).fetchall()
            stale_rows = [
                dict(row)
                for row in rows
                if (
                    str(row["market"]).strip().lower(),
                    str(row["symbol"]).strip().upper(),
                )
                not in active_normalized
            ]
            for row in stale_rows:
                conn.execute(
                    """
                    UPDATE lab_symbol_state
                    SET has_position = 0,
                        holding_qty = 0,
                        action_bias = 'HOLD',
                        signal_state = 'HOLD',
                        note = 'stale_position_cleared',
                        updated_at = ?
                    WHERE market = ?
                      AND symbol = ?
                    """,
                    (updated_at, row["market"], row["symbol"]),
                )
        return stale_rows

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
                       entry_time,
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
        include_non_session_real: bool = False,
    ) -> dict:
        after_dt = parse_datetime(after_logged_at)
        with self._connect() as conn:
            real_query = (
                "SELECT * FROM cycle_log "
                "WHERE action_bias = 'SELL_REAL' "
                "AND COALESCE(qty_executed, 0) > 0 "
                f"AND {CONFIRMED_SELL_CYCLE_PREDICATE}"
            )
            real_params: list[object] = []
            cycle_log_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(cycle_log)").fetchall()
            }
            if (
                "is_session_trade" in cycle_log_columns
                and not include_non_session_real
            ):
                real_query += (
                    " AND (is_session_trade IS NULL OR is_session_trade = 1 "
                    f"OR {CONFIRMED_SESSION_OWNERSHIP_PREDICATE})"
                )
            if session_id:
                real_query += " AND session_id = ?"
                real_params.append(session_id)
            real_rows = [dict(row) for row in conn.execute(real_query, real_params).fetchall()]

            virtual_rows: list[dict] = []
            if include_virtual:
                virtual_rows = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT * FROM virtual_orders
                        WHERE side = 'sell'
                          AND COALESCE(excluded_from_performance, 0) = 0
                        """
                    ).fetchall()
                ]

        real_summary: dict[str, dict[str, float | int | None]] = {}
        real_by_symbol: dict[str, dict[str, float | int | str]] = {}
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
            qty = int(row.get("qty_executed") or 0)
            entry_price = float(row.get("entry_price") or 0.0)
            net_pnl_usd = (
                float(row["net_pnl_usd"])
                if row.get("net_pnl_usd") is not None
                else float(row.get("realized_pnl_usd") or 0.0)
            )
            net_pnl_krw = (
                float(row["net_pnl_krw"])
                if row.get("net_pnl_krw") is not None
                else float(row.get("realized_pnl_krw") or 0.0)
            )
            if market.lower() == "overseas" and entry_price > 0 and qty > 0:
                pnl_pct = net_pnl_usd / (entry_price * qty)
                is_win = net_pnl_usd > 0
            elif market.lower() == "domestic" and entry_price > 0 and qty > 0:
                pnl_pct = net_pnl_krw / (entry_price * qty)
                is_win = net_pnl_krw > 0
            else:
                pnl_pct = float(row.get("pnl_pct") or 0.0)
                is_win = net_pnl_krw > 0 or (
                    abs(net_pnl_krw) <= 1e-9 and net_pnl_usd > 0
                )
            stats["trade_count"] = int(stats["trade_count"]) + 1
            if is_win:
                stats["win_count"] = int(stats["win_count"]) + 1
            else:
                stats["loss_count"] = int(stats["loss_count"]) + 1
            stats["_sum_pnl_pct"] = float(stats["_sum_pnl_pct"]) + pnl_pct
            stats["total_pnl_usd"] = float(stats["total_pnl_usd"]) + net_pnl_usd
            stats["total_pnl_krw"] = float(stats["total_pnl_krw"]) + net_pnl_krw

            symbol = str(row.get("symbol") or "").strip().upper()
            if symbol:
                symbol_stats = real_by_symbol.setdefault(
                    symbol,
                    {
                        "symbol": symbol,
                        "market": market,
                        "trade_count": 0,
                        "win_count": 0,
                        "total_pnl_usd": 0.0,
                        "total_pnl_krw": 0.0,
                    },
                )
                symbol_stats["trade_count"] = int(symbol_stats["trade_count"]) + 1
                symbol_stats["win_count"] = int(symbol_stats["win_count"]) + int(
                    is_win
                )
                symbol_stats["total_pnl_usd"] = (
                    float(symbol_stats["total_pnl_usd"]) + net_pnl_usd
                )
                symbol_stats["total_pnl_krw"] = (
                    float(symbol_stats["total_pnl_krw"]) + net_pnl_krw
                )

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
            "real_by_symbol": real_by_symbol,
            "virtual": virtual_summary,
        }

    def get_realized_strategy_performance(
        self,
        *,
        after_logged_at: str = "",
        limit: int = 12,
    ) -> list[dict]:
        """Summarize session-owned confirmed fills by reason and exit signal."""
        params: list[object] = []
        where = [
            "action_bias = 'SELL_REAL'",
            "COALESCE(qty_executed, 0) > 0",
            CONFIRMED_STRATEGY_SELL_CYCLE_PREDICATE,
        ]
        if after_logged_at:
            where.append("logged_at >= ?")
            params.append(after_logged_at)
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                WITH realized AS (
                    SELECT
                        market,
                        COALESCE(NULLIF(strategy_flag, ''), '-') AS strategy_label,
                        COALESCE(NULLIF(entry_by, ''), '-') AS entry_label,
                        COALESCE(
                            NULLIF(action_reason, ''),
                            NULLIF(exit_by, ''),
                            '-'
                        ) AS exit_reason_label,
                        COALESCE(NULLIF(exit_by, ''), '-') AS exit_signal_label,
                        CASE
                            WHEN lower(market) = 'overseas'
                              AND net_pnl_usd IS NOT NULL
                              AND COALESCE(entry_price, 0) > 0
                              AND COALESCE(qty_executed, 0) > 0
                            THEN net_pnl_usd / (entry_price * qty_executed)
                            WHEN lower(market) = 'domestic'
                              AND net_pnl_krw IS NOT NULL
                              AND COALESCE(entry_price, 0) > 0
                              AND COALESCE(qty_executed, 0) > 0
                            THEN net_pnl_krw / (entry_price * qty_executed)
                            ELSE COALESCE(pnl_pct, 0)
                        END AS pnl_pct,
                        CASE
                            WHEN lower(market) = 'overseas'
                              AND net_pnl_usd IS NOT NULL
                            THEN net_pnl_usd
                            WHEN lower(market) = 'domestic'
                              AND net_pnl_krw IS NOT NULL
                            THEN net_pnl_krw
                            ELSE COALESCE(
                                realized_pnl_krw,
                                realized_pnl_usd,
                                pnl_pct,
                                0
                            )
                        END AS net_result,
                        qty_executed,
                        net_pnl_usd,
                        realized_pnl_usd,
                        net_pnl_krw,
                        realized_pnl_krw,
                        hold_duration_min
                    FROM cycle_log
                    WHERE {' AND '.join(where)}
                )
                SELECT
                    market,
                    strategy_label AS strategy_flag,
                    entry_label AS entry_by,
                    exit_reason_label AS exit_by,
                    exit_reason_label AS exit_reason,
                    exit_signal_label AS exit_signal_by,
                    COUNT(*) AS trade_count,
                    SUM(CASE WHEN net_result > 0 THEN 1 ELSE 0 END) AS win_count,
                    SUM(CASE WHEN net_result <= 0 THEN 1 ELSE 0 END) AS loss_count,
                    AVG(COALESCE(pnl_pct, 0)) AS avg_pnl_pct,
                    SUM(COALESCE(qty_executed, 0)) AS total_qty,
                    SUM(COALESCE(net_pnl_usd, realized_pnl_usd, 0)) AS total_net_pnl_usd,
                    SUM(COALESCE(net_pnl_krw, realized_pnl_krw, 0)) AS total_net_pnl_krw,
                    AVG(hold_duration_min) AS avg_hold_duration_min
                FROM realized
                GROUP BY
                    market,
                    strategy_label,
                    entry_label,
                    exit_reason_label,
                    exit_signal_label
                ORDER BY total_net_pnl_krw DESC, total_net_pnl_usd DESC, trade_count DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        result: list[dict] = []
        for row in rows:
            item = dict(row)
            trade_count = int(item.get("trade_count") or 0)
            win_count = int(item.get("win_count") or 0)
            item["win_rate"] = (win_count / trade_count) if trade_count else 0.0
            result.append(item)
        return result

    def get_sell_reason_counts(self, *, after_logged_at: str = "") -> list[dict]:
        """Return session-owned confirmed sell counts by market and reason."""
        params: list[object] = []
        where = [
            "action_bias = 'SELL_REAL'",
            "COALESCE(qty_executed, 0) > 0",
            CONFIRMED_STRATEGY_SELL_CYCLE_PREDICATE,
        ]
        if after_logged_at:
            where.append("logged_at >= ?")
            params.append(after_logged_at)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT lower(COALESCE(market, '')) AS market,
                       COALESCE(action_reason, '') AS action_reason,
                       COUNT(*) AS cnt
                FROM cycle_log
                WHERE {' AND '.join(where)}
                GROUP BY lower(COALESCE(market, '')), action_reason
                ORDER BY market, cnt DESC
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_recent_strategy_guard_performance(
        self,
        *,
        after_logged_at: str = "",
        cost_pct: float = 0.005,
    ) -> list[dict]:
        """Summarize session-owned confirmed performance for entry guards."""
        params: list[object] = [float(cost_pct)]
        where = [
            "action_bias = 'SELL_REAL'",
            "COALESCE(qty_executed, 0) > 0",
            CONFIRMED_STRATEGY_SELL_CYCLE_PREDICATE,
        ]
        if after_logged_at:
            where.append("logged_at >= ?")
            params.append(after_logged_at)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                WITH evaluated AS (
                    SELECT
                        market,
                        COALESCE(NULLIF(strategy_flag, ''), 'N/A') AS strategy_flag,
                        COALESCE(pnl_pct, 0) AS gross_pnl_pct,
                        CASE
                            WHEN lower(market) = 'overseas'
                              AND net_pnl_usd IS NOT NULL
                              AND COALESCE(entry_price, 0) > 0
                              AND COALESCE(qty_executed, 0) > 0
                            THEN net_pnl_usd / (entry_price * qty_executed)
                            WHEN lower(market) = 'domestic'
                              AND net_pnl_krw IS NOT NULL
                              AND COALESCE(entry_price, 0) > 0
                              AND COALESCE(qty_executed, 0) > 0
                            THEN net_pnl_krw / (entry_price * qty_executed)
                            ELSE COALESCE(pnl_pct, 0) - ?
                        END AS net_pnl_pct,
                        COALESCE(net_pnl_usd, realized_pnl_usd, 0) AS net_pnl_usd_value,
                        COALESCE(net_pnl_krw, realized_pnl_krw, 0) AS net_pnl_krw_value
                    FROM cycle_log
                    WHERE {' AND '.join(where)}
                )
                SELECT
                    market,
                    strategy_flag,
                    COUNT(*) AS trade_count,
                    SUM(CASE WHEN net_pnl_pct > 0 THEN 1 ELSE 0 END) AS win_count,
                    AVG(gross_pnl_pct) AS avg_gross_pnl_pct,
                    AVG(net_pnl_pct) AS avg_net_pnl_pct,
                    SUM(net_pnl_usd_value) AS total_net_pnl_usd,
                    SUM(net_pnl_krw_value) AS total_net_pnl_krw
                FROM evaluated
                GROUP BY market, strategy_flag
                ORDER BY avg_net_pnl_pct ASC, trade_count DESC
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_recent_confirmed_sell_risk_outcomes(
        self,
        *,
        limit: int = 1000,
        cost_pct: float = 0.005,
    ) -> list[dict]:
        """Return recent confirmed sell outcomes for risk-state restoration."""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    id,
                    logged_at,
                    market,
                    CASE
                        WHEN lower(market) = 'overseas'
                          AND net_pnl_usd IS NOT NULL
                          AND COALESCE(entry_price, 0) > 0
                          AND COALESCE(qty_executed, 0) > 0
                        THEN net_pnl_usd / (entry_price * qty_executed)
                        WHEN lower(market) = 'domestic'
                          AND net_pnl_krw IS NOT NULL
                          AND COALESCE(entry_price, 0) > 0
                          AND COALESCE(qty_executed, 0) > 0
                        THEN net_pnl_krw / (entry_price * qty_executed)
                        ELSE COALESCE(pnl_pct, 0) - ?
                    END AS net_pnl_pct
                FROM cycle_log
                WHERE action_bias = 'SELL_REAL'
                  AND COALESCE(qty_executed, 0) > 0
                  AND {CONFIRMED_SELL_CYCLE_PREDICATE}
                ORDER BY logged_at DESC, id DESC
                LIMIT ?
                """,
                (
                    float(cost_pct),
                    max(1, int(limit)),
                ),
            ).fetchall()
        return [dict(row) for row in rows]

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
        excluded_from_performance: bool = False,
        exclude_reason: str = "",
        excluded_at: str | None = None,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO virtual_orders
                    (created_at, market, symbol, exchange_code, side, qty,
                     fill_price, currency, session, reason, realized_pnl, realized_pnl_pct,
                     excluded_from_performance, exclude_reason, excluded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    1 if excluded_from_performance else 0,
                    exclude_reason,
                    excluded_at,
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

    def exclude_virtual_orders_from_performance(
        self,
        order_ids: list[int],
        *,
        reason: str,
        excluded_at: str,
    ) -> int:
        if not order_ids:
            return 0
        placeholders = ",".join("?" for _ in order_ids)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE virtual_orders
                SET excluded_from_performance = 1,
                    exclude_reason = ?,
                    excluded_at = ?
                WHERE id IN ({placeholders})
                """,
                [reason, excluded_at, *order_ids],
            )
            return int(cursor.rowcount)

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
                  AND COALESCE(excluded_from_performance, 0) = 0
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
