from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from kinvest_trade.repository import SqliteRepository


def test_prune_operational_logs_preserves_trade_history(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "test.db")
    repository.save_api_call(
        created_at="2026-06-01T00:00:00+00:00",
        method="GET",
        path="/old",
    )
    repository.save_api_call(
        created_at="2026-07-26T00:00:00+00:00",
        method="GET",
        path="/new",
    )
    repository.save_telegram_message(
        created_at="2026-01-01T00:00:00+00:00",
        direction="out",
        text="old",
    )
    repository.save_telegram_message(
        created_at="2026-07-26T00:00:00+00:00",
        direction="out",
        text="new",
    )
    repository.save_cycle_log(
        logged_at="2025-01-01T00:00:00+00:00",
        market="domestic",
        symbol="005930",
        exchange_code=None,
        action_bias="SELL_REAL",
        action_reason="history",
    )

    deleted = repository.prune_operational_logs(
        api_call_retention_days=30,
        telegram_message_retention_days=90,
        now=datetime(2026, 7, 27, tzinfo=timezone.utc),
    )

    assert deleted == {"api_call_log": 1, "telegram_message_log": 1}
    with repository._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM api_call_log").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM telegram_message_log").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM cycle_log").fetchone()[0] == 1


def test_abort_stale_auto_trade_runs_marks_old_running_rows(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    repository = SqliteRepository(db_path)
    run_id = repository.create_auto_trade_run(
        mode="SOXL_VOLATILITY_AWARE",
        profile="paper",
        symbol="SOXL",
        exchange_code="AMEX",
        max_actions=20,
    )

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE auto_trade_runs SET started_at = datetime('now', '-1 day') WHERE id = ?",
            (run_id,),
        )

    updated = repository.abort_stale_auto_trade_runs(
        older_than_minutes=60,
        reason="stale test cleanup",
    )

    assert updated == 1

    with sqlite3.connect(db_path) as conn:
        status, notes, ended_at = conn.execute(
            "SELECT status, notes, ended_at FROM auto_trade_runs WHERE id = ?",
            (run_id,),
        ).fetchone()

    assert status == "ABORTED"
    assert notes == "stale test cleanup"
    assert ended_at is not None


def test_save_telegram_control_session_persists_summary(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    repository = SqliteRepository(db_path)

    record_id = repository.save_telegram_control_session(
        command="stop",
        profile="paper",
        started_at="2026-06-25 17:00:00 KST",
        cycles_completed=3,
        domestic_paper_runs=2,
        domestic_paper_realized_pnl_krw=1500,
        domestic_orders_submitted=1,
        overseas_orders_submitted=0,
        domestic_orders_failed=0,
        overseas_orders_failed=1,
        summary_json={"hello": "world"},
    )

    assert record_id >= 1

    with sqlite3.connect(db_path) as conn:
        command, profile, cycles_completed, pnl = conn.execute(
            """
            SELECT command, profile, cycles_completed, domestic_paper_realized_pnl_krw
            FROM telegram_control_sessions WHERE id = ?
            """,
            (record_id,),
        ).fetchone()

    assert command == "stop"
    assert profile == "paper"
    assert cycles_completed == 3
    assert pnl == 1500


def test_cycle_log_can_be_saved_and_filtered(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "test.db")
    repository.save_cycle_log(
        logged_at="2026-07-01T00:00:00+00:00",
        market="overseas",
        symbol="SOXL",
        exchange_code="NASD",
        action_bias="BUY",
        action_reason="pullback_entry",
        price=20.5,
        pnl_pct=0.012,
        holding_qty=1,
        rsi14=58.0,
        volume_ratio=2.0,
        intraday_momentum=0.003,
        intraday_bar_return=0.001,
        minute_ma_fast=20.3,
        minute_ma_slow=20.1,
        activity_score=15.0,
        cycle_no=7,
        session_id="sess-a",
        strategy_flag="VWAP+VOL",
        entry_by="VWAP",
        exit_by="",
        vwap=20.4,
        macd_line=0.5,
        macd_signal=0.3,
        macd_golden=1,
        breakout_distance_pct=0.002,
        atr=0.4,
        spread_pct=0.001,
        consecutive_losses=2,
        hold_cycles=6,
        entry_price=20.1,
        qty_executed=1,
        net_pnl_usd=0.0,
        net_pnl_krw=0.0,
        commission_usd=0.1,
        commission_krw=138.0,
        is_virtual=0,
        orderable_qty=1,
        stock_name="SOXL",
        hold_duration_min=0.0,
        entry_time="2026-07-01T00:00:00+00:00",
        exit_cooldown_remaining=0.0,
        cb_active=0,
        pool_size=12,
    )
    repository.save_cycle_log(
        logged_at="2026-07-01T00:01:00+00:00",
        market="overseas",
        symbol="SOXL",
        exchange_code="NASD",
        action_bias="SELL",
        action_reason="marginal_profit_exit",
        cycle_no=8,
        session_id="sess-a",
        exit_by="VWAP",
    )

    buy_rows = repository.query_cycle_log(symbol="SOXL", action_bias="BUY", limit=10)
    sell_rows = repository.query_cycle_log(action_bias="SELL", limit=10)

    assert len(buy_rows) == 1
    assert buy_rows[0]["action_reason"] == "pullback_entry"
    assert buy_rows[0]["cycle_no"] == 7
    assert buy_rows[0]["strategy_flag"] == "VWAP+VOL"
    assert buy_rows[0]["entry_by"] == "VWAP"
    assert buy_rows[0]["vwap"] == 20.4
    assert buy_rows[0]["macd_line"] == 0.5
    assert buy_rows[0]["macd_signal"] == 0.3
    assert buy_rows[0]["macd_golden"] == 1
    assert buy_rows[0]["breakout_distance_pct"] == 0.002
    assert buy_rows[0]["atr"] == 0.4
    assert buy_rows[0]["spread_pct"] == 0.001
    assert buy_rows[0]["consecutive_losses"] == 2
    assert buy_rows[0]["hold_cycles"] == 6
    assert buy_rows[0]["entry_price"] == 20.1
    assert buy_rows[0]["qty_executed"] == 1
    assert buy_rows[0]["commission_usd"] == 0.1
    assert buy_rows[0]["stock_name"] == "SOXL"
    assert buy_rows[0]["pool_size"] == 12
    assert len(sell_rows) == 1
    assert sell_rows[0]["action_reason"] == "marginal_profit_exit"
    assert sell_rows[0]["exit_by"] == "VWAP"


def test_repository_backfills_non_trade_cycle_log_flags(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    repository = SqliteRepository(db_path)
    for action_bias in ("HOLD", "WAIT", "BUY", "SELL", "SKIP", "BUY_REAL", "SELL_REAL"):
        repository.save_cycle_log(
            logged_at="2026-07-01T00:00:00+00:00",
            market="overseas",
            symbol=action_bias,
            exchange_code="NASD",
            action_bias=action_bias,
            action_reason="legacy",
            is_session_trade=1,
        )

    SqliteRepository(db_path)

    with sqlite3.connect(db_path) as conn:
        rows = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT action_bias, is_session_trade FROM cycle_log"
            ).fetchall()
        }

    assert rows["HOLD"] == 0
    assert rows["WAIT"] == 0
    assert rows["BUY"] == 0
    assert rows["SELL"] == 0
    assert rows["SKIP"] == 0
    assert rows["BUY_REAL"] == 1
    assert rows["SELL_REAL"] == 1


def test_repository_backfills_missing_exit_labels(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    repository = SqliteRepository(db_path)
    repository.save_cycle_log(
        logged_at="2026-07-01T00:00:00+00:00",
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        action_bias="SELL_REAL",
        action_reason="trend_filter_lost",
        exit_by="",
    )
    repository.save_broker_order_event(
        created_at="2026-07-01T00:01:00+00:00",
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        side="SELL",
        order_kind="limit",
        requested_qty=1,
        requested_price=20.0,
        status="SUBMITTED",
        reason="trend_filter_lost",
        exit_by="",
    )
    repository.save_broker_order_event(
        created_at="2026-07-01T00:02:00+00:00",
        market="domestic",
        symbol="073240",
        exchange_code=None,
        side="BUY",
        order_kind="cancel",
        requested_qty=1,
        requested_price=6990.0,
        status="REJECTED",
        reason="stale_live_order_cancel_failed",
        exit_by="",
    )

    SqliteRepository(db_path)

    with sqlite3.connect(db_path) as conn:
        cycle_exit_by = conn.execute(
            "SELECT exit_by FROM cycle_log WHERE symbol = 'SOXL'"
        ).fetchone()[0]
        broker_rows = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT symbol, exit_by FROM broker_order_events ORDER BY id"
            ).fetchall()
        }

    assert cycle_exit_by == "trend_filter_lost"
    assert broker_rows["SOXL"] == "trend_filter_lost"
    assert broker_rows["073240"] == ""


def test_cycle_log_strategy_columns_exist(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "test.db")

    with sqlite3.connect(repository.db_path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(cycle_log)").fetchall()]

    assert "strategy_flag" in columns
    assert "entry_by" in columns
    assert "exit_by" in columns
    assert "is_session_trade" in columns
    assert "vwap" in columns
    assert "macd_line" in columns
    assert "macd_signal" in columns
    assert "macd_golden" in columns
    assert "breakout_distance_pct" in columns
    assert "atr" in columns
    assert "spread_pct" in columns
    assert "consecutive_losses" in columns
    assert "hold_cycles" in columns
    assert "entry_price" in columns
    assert "qty_executed" in columns
    assert "net_pnl_usd" in columns
    assert "net_pnl_krw" in columns
    assert "commission_usd" in columns
    assert "commission_krw" in columns
    assert "is_virtual" in columns
    assert "orderable_qty" in columns
    assert "stock_name" in columns
    assert "hold_duration_min" in columns
    assert "entry_time" in columns
    assert "exit_cooldown_remaining" in columns
    assert "cb_active" in columns
    assert "pool_size" in columns


def test_lab_symbol_state_entry_time_column_migrates_existing_db(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE lab_symbol_state (
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (market, symbol)
            )
            """
        )

    SqliteRepository(db_path)

    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(lab_symbol_state)")
        }

    assert "entry_time" in columns


def test_event_log_can_be_saved_and_queried(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "test.db")

    repository.save_event(
        event_type="trade_skip",
        market="overseas",
        symbol="PLTR",
        detail={"reason": "entry_rsi_too_high"},
        cycle_no=12,
        session_id="sess-event",
    )

    rows = repository.list_event_log(limit=5)

    assert len(rows) == 1
    assert rows[0]["event_type"] == "trade_skip"
    assert rows[0]["symbol"] == "PLTR"
    assert "entry_rsi_too_high" in rows[0]["detail"]


def test_broker_order_events_table_and_save(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "test.db")
    repository.save_broker_order_event(
        created_at="2026-07-08T04:50:00+00:00",
        market="overseas",
        symbol="PLBL",
        exchange_code="NASD",
        side="BUY",
        order_kind="limit",
        requested_qty=100,
        requested_price=10.1234,
        strategy_flag="VWAP",
        entry_by="VWAP",
        status="SUBMITTED",
        reason="strategy_buy_signal",
        broker_order_no="12345678",
        is_virtual=0,
        payload={"output": {"ODNO": "12345678"}},
    )

    rows = repository.list_broker_order_events(limit=5)

    assert len(rows) == 1
    assert rows[0]["symbol"] == "PLBL"
    assert rows[0]["broker_order_no"] == "12345678"
    assert rows[0]["payload_json"]["output"]["ODNO"] == "12345678"


def test_broker_execution_order_date_uses_each_market_trading_date(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "execution_dates.db")
    created_at = "2026-07-27T15:24:13+00:00"

    domestic_event_id = repository.save_broker_order_event(
        created_at=created_at,
        market="domestic",
        symbol="005930",
        exchange_code=None,
        side="BUY",
        order_kind="limit",
        requested_qty=1,
        requested_price=100.0,
        status="SUBMITTED",
        broker_order_no="1001",
    )
    overseas_event_id = repository.save_broker_order_event(
        created_at=created_at,
        market="overseas",
        symbol="LXFR",
        exchange_code="NYSE",
        side="BUY",
        order_kind="limit",
        requested_qty=1,
        requested_price=10.0,
        status="SUBMITTED",
        broker_order_no="1002",
    )
    domestic = repository.save_broker_order_execution(
        broker_event_id=domestic_event_id,
        created_at=created_at,
        market="domestic",
        symbol="005930",
        exchange_code=None,
        side="BUY",
        broker_order_no="1001",
        requested_qty=1,
        requested_price=100.0,
    )
    overseas = repository.save_broker_order_execution(
        broker_event_id=overseas_event_id,
        created_at=created_at,
        market="overseas",
        symbol="LXFR",
        exchange_code="NYSE",
        side="BUY",
        broker_order_no="1002",
        requested_qty=1,
        requested_price=10.0,
    )

    assert domestic["order_date"] == "2026-07-28"
    assert overseas["order_date"] == "2026-07-27"


def test_telegram_message_log_can_be_saved_and_listed(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "test.db")
    repository.save_telegram_message(
        created_at="2026-07-13T00:00:00+00:00",
        direction="received",
        command="/lab_status",
        text="/lab_status",
    )
    repository.save_telegram_message(
        created_at="2026-07-13T00:00:05+00:00",
        direction="sent",
        text="[KIS][STATUS] ...",
        success=False,
        error="timeout",
    )

    rows = repository.list_telegram_messages(limit=5)

    assert len(rows) == 2
    assert rows[0]["direction"] == "sent"
    assert rows[0]["success"] == 0
    assert rows[0]["error"] == "timeout"
    assert rows[1]["direction"] == "received"
    assert rows[1]["command"] == "/lab_status"


def test_api_call_log_can_be_saved_and_listed(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "test.db")
    repository.save_api_call(
        created_at="2026-07-13T00:00:00+00:00",
        method="POST",
        tr_id="VTTC0012U",
        path="/uapi/domestic-stock/v1/trading/order-cash",
        success=False,
        http_status=500,
        msg_cd="IGW00007",
        msg1="MCA 전문바디 구성 중 오류가 발생하였습니다.",
        elapsed_ms=123,
        dispatched_at="2026-07-13T00:00:00.100000+00:00",
        throttle_wait_ms=1010,
        network_elapsed_ms=42,
    )

    rows = repository.list_api_calls(limit=5)

    assert len(rows) == 1
    assert rows[0]["tr_id"] == "VTTC0012U"
    assert rows[0]["success"] == 0
    assert rows[0]["msg_cd"] == "IGW00007"
    assert rows[0]["elapsed_ms"] == 123
    assert rows[0]["logical_request_id"] == ""
    assert rows[0]["attempt_no"] == 1
    assert rows[0]["logical_terminal"] == 1
    assert rows[0]["dispatched_at"] == "2026-07-13T00:00:00.100000+00:00"
    assert rows[0]["throttle_wait_ms"] == 1010
    assert rows[0]["network_elapsed_ms"] == 42


def test_api_call_health_separates_recovered_retry_from_terminal_failure(
    tmp_path,
) -> None:
    repository = SqliteRepository(tmp_path / "api_health.db")
    repository.save_api_call(
        created_at="2026-07-29T00:00:00+00:00",
        method="GET",
        tr_id="QUOTE",
        success=False,
        http_status=500,
        msg_cd="EGW00201",
        logical_request_id="request-recovered",
        attempt_no=1,
        max_attempts=3,
        retry_scheduled=True,
        retry_reason="rate_limit",
        logical_terminal=False,
    )
    repository.save_api_call(
        created_at="2026-07-29T00:00:02+00:00",
        method="GET",
        tr_id="QUOTE",
        success=True,
        http_status=200,
        logical_request_id="request-recovered",
        attempt_no=2,
        max_attempts=3,
        logical_terminal=True,
    )
    repository.save_api_call(
        created_at="2026-07-29T00:00:04+00:00",
        method="GET",
        tr_id="BALANCE",
        success=False,
        http_status=500,
        msg_cd="EGW00201",
        logical_request_id="request-failed",
        attempt_no=3,
        max_attempts=3,
        logical_terminal=True,
    )
    repository.save_api_call(
        created_at="2026-07-29T00:00:06+00:00",
        method="GET",
        tr_id="BALANCE",
        success=False,
        http_status=200,
        msg_cd="90020000",
        logical_request_id="service-delay-recovered",
        attempt_no=1,
        max_attempts=3,
        retry_scheduled=True,
        retry_reason="service_delay",
        logical_terminal=False,
    )
    repository.save_api_call(
        created_at="2026-07-29T00:00:08+00:00",
        method="GET",
        tr_id="BALANCE",
        success=True,
        http_status=200,
        logical_request_id="service-delay-recovered",
        attempt_no=2,
        max_attempts=3,
        logical_terminal=True,
    )

    summary = repository.summarize_api_call_health(
        since="2026-07-29T00:00:00+00:00"
    )

    assert summary == {
        "attempt_count": 5,
        "attempt_failure_count": 3,
        "tracked_request_count": 3,
        "terminal_failure_count": 1,
        "recovered_request_count": 2,
        "retry_scheduled_count": 2,
        "rate_limit_retry_count": 1,
        "service_delay_retry_count": 1,
        "attempt_failure_rate": pytest.approx(3 / 5),
        "terminal_failure_rate": pytest.approx(1 / 3),
    }


def test_api_call_lineage_columns_migrate_legacy_table(tmp_path) -> None:
    db_path = tmp_path / "legacy_api_log.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE api_call_log (
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
            )
            """
        )
        conn.execute(
            """
            INSERT INTO api_call_log (created_at, method, success)
            VALUES ('2026-07-28T00:00:00+00:00', 'GET', 0)
            """
        )

    repository = SqliteRepository(db_path)
    row = repository.list_api_calls(limit=1)[0]

    assert row["logical_request_id"] == ""
    assert row["attempt_no"] == 1
    assert row["max_attempts"] == 1
    assert row["retry_scheduled"] == 0
    assert row["retry_reason"] == ""
    assert row["logical_terminal"] == 1
    assert row["dispatched_at"] == ""
    assert row["throttle_wait_ms"] is None
    assert row["network_elapsed_ms"] is None
    summary = repository.summarize_api_call_health(
        since="2026-07-28T00:00:00+00:00"
    )
    assert summary["attempt_failure_count"] == 1
    assert summary["tracked_request_count"] == 0
    assert summary["terminal_failure_count"] == 0


def test_submitted_order_audit_rows_excludes_canceled_and_keeps_cancel_rejected(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "test.db")
    repository.save_broker_order_event(
        created_at="2026-07-10T01:00:00+00:00",
        market="overseas",
        symbol="FOO",
        exchange_code="NASD",
        side="SELL",
        order_kind="limit",
        requested_qty=10,
        requested_price=10.0,
        status="SUBMITTED",
        reason="stop_loss",
        broker_order_no="ord-001",
        is_virtual=0,
        payload={},
    )
    repository.save_broker_order_event(
        created_at="2026-07-10T01:05:00+00:00",
        market="overseas",
        symbol="FOO",
        exchange_code="NASD",
        side="SELL",
        order_kind="cancel",
        requested_qty=10,
        requested_price=10.0,
        status="REJECTED",
        reason="stale_live_overseas_order_cancel_failed",
        broker_order_no="ord-001",
        is_virtual=0,
        payload={"original_order_no": "ord-001"},
    )
    repository.save_broker_order_event(
        created_at="2026-07-10T01:10:00+00:00",
        market="overseas",
        symbol="BAR",
        exchange_code="NASD",
        side="BUY",
        order_kind="limit",
        requested_qty=20,
        requested_price=20.0,
        status="SUBMITTED",
        reason="strategy_buy_signal",
        broker_order_no="ord-002",
        is_virtual=0,
        payload={},
    )
    repository.save_broker_order_event(
        created_at="2026-07-10T01:15:00+00:00",
        market="overseas",
        symbol="BAR",
        exchange_code="NASD",
        side="BUY",
        order_kind="cancel",
        requested_qty=20,
        requested_price=20.0,
        status="CANCELED",
        reason="stale_live_overseas_order_cancel",
        broker_order_no="cancel-002",
        is_virtual=0,
        payload={"original_order_no": "ord-002"},
    )
    repository.save_broker_order_event(
        created_at="2026-07-10T01:20:00+00:00",
        market="overseas",
        symbol="VIRT",
        exchange_code="NASD",
        side="BUY",
        order_kind="virtual_limit",
        requested_qty=1,
        requested_price=30.0,
        status="SUBMITTED",
        reason="strategy_buy_signal",
        broker_order_no="virt-001",
        is_virtual=1,
        payload={},
    )

    rows = repository.list_submitted_order_audit_rows(limit=10)

    assert [row["symbol"] for row in rows] == ["FOO"]
    assert rows[0]["followup_status"] == "REJECTED"
    assert rows[0]["followup_reason"] == "stale_live_overseas_order_cancel_failed"


def test_lab_symbol_state_can_be_upserted_and_loaded(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "test.db")
    snapshot = {"price": 170.0, "volume_ratio": 2.1}
    entry_time = "2026-07-06T07:15:00+00:00"

    repository.upsert_lab_symbol_state(
        market="overseas",
        symbol="COIN",
        exchange_code="NASD",
        action_bias="HOLD",
        signal_state="HOLD",
        note="vr=2.1x mom=+0.42%",
        strategy_flag="VWAP+VOL",
        entry_by="VWAP",
        holding_qty=57,
        last_price=170.0,
        pnl_pct=0.028,
        entry_price=165.03,
        entry_time=entry_time,
        peak_price=171.5,
        has_position=1,
        snapshot_json=snapshot,
        updated_at="2026-07-06T09:00:00+00:00",
    )

    state = repository.get_lab_symbol_state("overseas", "COIN")

    assert state is not None
    assert state["strategy_flag"] == "VWAP+VOL"
    assert state["entry_by"] == "VWAP"
    assert state["has_position"] == 1
    assert state["entry_time"] == entry_time
    assert state["snapshot_json"]["price"] == 170.0

    repository.upsert_lab_symbol_state(
        market="overseas",
        symbol="COIN",
        exchange_code="NASD",
        action_bias="HOLD",
        signal_state="HOLD",
        note="next_cycle",
        holding_qty=57,
        last_price=171.0,
        has_position=1,
        updated_at="2026-07-06T09:05:00+00:00",
    )

    assert repository.get_lab_symbol_state("overseas", "COIN")["entry_time"] == entry_time


def test_latest_position_entry_time_uses_confirmed_buy_fill(
    tmp_path,
    save_confirmed_buy,
) -> None:
    repository = SqliteRepository(tmp_path / "test.db")
    fill_time = "2026-07-06T07:15:00+00:00"
    save_confirmed_buy(
        repository,
        logged_at=fill_time,
        market="overseas",
        symbol="COIN",
        exchange_code="NASD",
        action_bias="BUY_REAL",
        action_reason="strategy_buy_signal",
        price=165.03,
        holding_qty=57,
        qty_executed=57,
        strategy_flag="VWAP+VOL",
        entry_by="VWAP",
        entry_price=165.03,
    )

    assert repository.get_latest_position_entry_time("overseas", "COIN") == fill_time

    sell_time = "2026-07-06T08:15:00+00:00"
    event_id = repository.save_broker_order_event(
        created_at=sell_time,
        market="overseas",
        symbol="COIN",
        exchange_code="NASD",
        side="SELL",
        order_kind="limit",
        requested_qty=57,
        requested_price=170.0,
        strategy_flag="VWAP+VOL",
        entry_by="VWAP",
        exit_by="VWAP",
        status="SUBMITTED",
        reason="take_profit",
        broker_order_no="test-sell-coin",
    )
    execution = repository.save_broker_order_execution(
        broker_event_id=event_id,
        created_at=sell_time,
        market="overseas",
        symbol="COIN",
        exchange_code="NASD",
        side="SELL",
        broker_order_no="test-sell-coin",
        requested_qty=57,
        requested_price=170.0,
        strategy_flag="VWAP+VOL",
        entry_by="VWAP",
        exit_by="VWAP",
        reason="take_profit",
        entry_price=165.03,
    )
    assert execution is not None
    repository.update_broker_order_execution(
        int(execution["id"]),
        filled_qty=4,
        filled_amount=680.0,
        avg_fill_price=170.0,
        remaining_qty=53,
        canceled_qty=0,
        rejected_qty=0,
        status="PARTIAL",
        history={},
        checked_at=sell_time,
        fill_recorded_at=sell_time,
    )

    assert repository.get_latest_position_entry_time("overseas", "COIN") == fill_time

    repository.update_broker_order_execution(
        int(execution["id"]),
        filled_qty=57,
        filled_amount=9690.0,
        avg_fill_price=170.0,
        remaining_qty=0,
        canceled_qty=0,
        rejected_qty=0,
        status="FILLED",
        history={},
        checked_at=sell_time,
        fill_recorded_at=sell_time,
    )

    assert repository.get_latest_position_entry_time("overseas", "COIN") is None


def test_repair_confirmed_cycle_entry_timing_uses_active_buy_fill(
    tmp_path,
    save_confirmed_buy,
    save_confirmed_sell,
) -> None:
    repository = SqliteRepository(tmp_path / "test.db")
    save_confirmed_buy(
        repository,
        logged_at="2026-07-06T10:00:00+00:00",
        market="overseas",
        symbol="COIN",
        exchange_code="NASD",
        action_bias="BUY_REAL",
        action_reason="strategy_buy_signal",
        price=100.0,
        qty_executed=2,
        entry_price=100.0,
    )
    save_confirmed_sell(
        repository,
        logged_at="2026-07-06T11:00:00+00:00",
        market="overseas",
        symbol="COIN",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="trend_filter_lost",
        price=101.0,
        pnl_pct=0.01,
        qty_executed=2,
        entry_price=100.0,
        entry_time="2026-07-06T10:45:00+00:00",
        hold_duration_min=15.0,
    )
    save_confirmed_sell(
        repository,
        logged_at="2026-07-06T12:00:00+00:00",
        market="overseas",
        symbol="COIN",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="stale_balance_duplicate",
        price=101.0,
        pnl_pct=0.01,
        qty_executed=2,
        entry_price=100.0,
        entry_time="2026-07-06T11:45:00+00:00",
        hold_duration_min=15.0,
    )

    dry_run = repository.repair_confirmed_cycle_entry_timing()

    assert len(dry_run) == 1
    assert dry_run[0]["action_reason"] == "trend_filter_lost"
    assert dry_run[0]["canonical_entry_time"] == "2026-07-06T10:00:00+00:00"
    assert dry_run[0]["canonical_hold_duration_min"] == 60.0
    before = repository.query_cycle_log(action_bias="SELL_REAL", limit=10)
    original = next(row for row in before if row["action_reason"] == "trend_filter_lost")
    assert original["entry_time"] == "2026-07-06T10:45:00+00:00"

    applied = repository.repair_confirmed_cycle_entry_timing(apply=True)

    assert applied == dry_run
    after = repository.query_cycle_log(action_bias="SELL_REAL", limit=10)
    repaired = next(row for row in after if row["action_reason"] == "trend_filter_lost")
    duplicate = next(
        row for row in after if row["action_reason"] == "stale_balance_duplicate"
    )
    assert repaired["entry_time"] == "2026-07-06T10:00:00+00:00"
    assert repaired["hold_duration_min"] == 60.0
    assert duplicate["entry_time"] == "2026-07-06T11:45:00+00:00"
    assert repository.repair_confirmed_cycle_entry_timing() == []


def test_get_lab_symbol_state_falls_back_to_cycle_log(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "test.db")
    repository.save_cycle_log(
        logged_at="2026-07-06T07:00:36+00:00",
        market="overseas",
        symbol="COIN",
        exchange_code="NASD",
        action_bias="HOLD",
        action_reason="vr=3.9x mom=+0.42%",
        price=170.29,
        pnl_pct=0.0027,
        holding_qty=57,
        cycle_no=10,
        session_id="sess-1",
        strategy_flag="VWAP+VOL",
        entry_by="VWAP",
    )

    state = repository.get_lab_symbol_state("overseas", "COIN")

    assert state is not None
    assert state["strategy_flag"] == "VWAP+VOL"
    assert state["entry_by"] == "VWAP"
    assert state["holding_qty"] == 57


def test_clear_stale_lab_positions_preserves_active_keys(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "test.db")
    for symbol in ("COIN", "ADBE"):
        repository.upsert_lab_symbol_state(
            market="overseas",
            symbol=symbol,
            exchange_code="NASD",
            action_bias="SELL",
            signal_state="SELL_READY",
            note="atr_hard_stop",
            holding_qty=10,
            last_price=100.0,
            pnl_pct=-0.02,
            has_position=1,
            updated_at="2026-07-06T09:00:00+00:00",
        )
    repository.upsert_lab_symbol_state(
        market="domestic",
        symbol="005930",
        exchange_code=None,
        action_bias="SELL",
        signal_state="SELL_READY",
        note="trend_filter_lost",
        holding_qty=3,
        last_price=82000.0,
        pnl_pct=-0.01,
        has_position=1,
        updated_at="2026-07-06T09:00:00+00:00",
    )

    cleared = repository.clear_stale_lab_positions(
        markets={"overseas"},
        active_keys={("overseas", "COIN")},
        updated_at="2026-07-10T08:00:00+00:00",
    )

    assert [row["symbol"] for row in cleared] == ["ADBE"]
    assert repository.get_lab_symbol_state("overseas", "COIN")["has_position"] == 1
    adbe = repository.get_lab_symbol_state("overseas", "ADBE")
    assert adbe["has_position"] == 0
    assert adbe["holding_qty"] == 0
    assert adbe["note"] == "stale_position_cleared"
    assert repository.get_lab_symbol_state("domestic", "005930")["has_position"] == 1


def test_backup_db_creates_copy(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "test.db")
    repository.save_event(event_type="before_backup", detail={"ok": True})

    backup_path = repository.backup_db(suffix="pre_reset")

    assert backup_path.exists()
    assert backup_path.name.startswith("test_backup_")
    assert backup_path.name.endswith("_pre_reset.db")
    with sqlite3.connect(backup_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM event_log").fetchone()[0] == 1


def test_reconcile_domestic_sell_costs_is_product_aware_and_idempotent(
    tmp_path,
    save_confirmed_sell,
) -> None:
    repository = SqliteRepository(tmp_path / "domestic_costs.db")
    for symbol in ("STOCK", "FUND"):
        save_confirmed_sell(
            repository,
            logged_at="2026-07-28T01:00:00+00:00",
            market="domestic",
            symbol=symbol,
            exchange_code="KRX",
            action_bias="SELL_REAL",
            action_reason="trend_filter_lost",
            entry_price=100.0,
            price=101.0,
            qty_executed=10,
            realized_pnl_krw=10.0,
            net_pnl_krw=7.99,
            commission_krw=1.01,
        )

    first = repository.reconcile_domestic_sell_costs(
        product_types={"STOCK": "KOSPI", "FUND": "ETF"},
        commission_rate=0.001,
        stock_sell_tax_rate=0.002,
    )

    assert first["eligible"] == 2
    assert first["updated"] == 2
    assert first["unchanged"] == 0
    assert first["missing_product_type"] == []
    assert first["applied_sell_tax_krw"] == 2.02
    assert first["net_adjustment_krw"] == -2.02
    rows = {
        row["symbol"]: row
        for row in repository.query_cycle_log(action_bias="SELL_REAL", limit=10)
    }
    assert rows["STOCK"]["net_pnl_krw"] == 5.97
    assert rows["STOCK"]["commission_krw"] == 3.03
    assert rows["STOCK"]["product_type"] == "KOSPI"
    assert (
        rows["STOCK"]["cost_calculation_version"]
        == "domestic_product_tax_v2"
    )
    assert rows["FUND"]["net_pnl_krw"] == 7.99
    assert rows["FUND"]["commission_krw"] == 1.01

    second = repository.reconcile_domestic_sell_costs(
        product_types={"STOCK": "KOSPI", "FUND": "ETF"},
        commission_rate=0.001,
        stock_sell_tax_rate=0.002,
    )

    assert second["updated"] == 0
    assert second["unchanged"] == 2
    assert second["net_adjustment_krw"] == 0.0


def test_reset_virtual_trades_clears_virtual_tables(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "test.db")
    repository.upsert_virtual_position(
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        qty=1,
        avg_price=20.0,
        currency="USD",
        opened_at="2026-07-01T00:00:00+00:00",
        updated_at="2026-07-01T00:00:00+00:00",
    )
    repository.save_virtual_order(
        created_at="2026-07-01T00:00:00+00:00",
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        side="buy",
        qty=1,
        fill_price=20.0,
        currency="USD",
        session="regular",
        reason="test_buy",
    )
    repository.upsert_virtual_sell_pending(
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        qty=1,
        avg_sell_price=21.0,
        currency="USD",
        updated_at="2026-07-01T00:01:00+00:00",
    )

    deleted = repository.reset_virtual_trades()

    assert deleted["virtual_positions"] == 1
    assert deleted["virtual_orders"] == 1
    assert deleted["virtual_sell_pending"] == 1
    assert repository.list_virtual_positions() == []


def test_reset_all_history_clears_performance_and_virtual_tables(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "test.db")
    repository.save_cycle_log(
        logged_at="2026-07-01T00:00:00+00:00",
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        action_bias="SELL_REAL",
        action_reason="take_profit",
    )
    repository.save_event(
        event_type="trade_skip",
        market="overseas",
        symbol="SOXL",
        detail={"reason": "pending_exit_order"},
    )
    repository.save_broker_order_event(
        created_at="2026-07-01T00:00:00+00:00",
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        side="SELL",
        order_kind="limit",
        requested_qty=1,
        requested_price=20.0,
        strategy_flag="",
        entry_by="",
        exit_by="",
        status="SUBMITTED",
        reason="take_profit",
        payload={},
    )
    repository.upsert_virtual_position(
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        qty=1,
        avg_price=20.0,
        currency="USD",
        opened_at="2026-07-01T00:00:00+00:00",
        updated_at="2026-07-01T00:00:00+00:00",
    )
    repository.upsert_lab_symbol_state(
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        action_bias="HOLD",
        signal_state="HOLD",
        note="",
        holding_qty=1,
        last_price=20.0,
        pnl_pct=0.0,
        has_position=1,
        updated_at="2026-07-01T00:00:00+00:00",
    )

    for table in (
        "cycle_log",
        "event_log",
        "broker_order_events",
        "virtual_positions",
        "lab_symbol_state",
    ):
        assert repository.count_rows(table) == 1

    deleted = repository.reset_all_history()

    assert deleted["cycle_log"] == 1
    assert deleted["event_log"] == 1
    assert deleted["broker_order_events"] == 1
    assert deleted["virtual_positions"] == 1
    assert deleted["lab_symbol_state"] == 1
    for table in (
        "cycle_log",
        "event_log",
        "broker_order_events",
        "virtual_positions",
        "lab_symbol_state",
    ):
        assert repository.count_rows(table) == 0


def test_count_rows_rejects_unknown_table(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "test.db")

    with pytest.raises(ValueError):
        repository.count_rows("sqlite_master")
    assert repository.list_virtual_orders(limit=10) == []
    assert repository.list_virtual_sell_pending() == []


def test_get_session_pnl_summary_real_only(tmp_path, save_confirmed_sell) -> None:
    repository = SqliteRepository(tmp_path / "test.db")
    save_confirmed_sell(
        repository,
        logged_at="2026-07-01T00:00:00+00:00",
        market="domestic",
        symbol="005930",
        exchange_code=None,
        action_bias="SELL_REAL",
        action_reason="stop_loss",
        pnl_pct=0.01,
        realized_pnl_krw=5000,
        cycle_no=1,
        session_id="sess-real",
    )
    save_confirmed_sell(
        repository,
        logged_at="2026-07-01T00:01:00+00:00",
        market="domestic",
        symbol="000660",
        exchange_code=None,
        action_bias="SELL_REAL",
        action_reason="take_profit",
        pnl_pct=-0.02,
        realized_pnl_krw=-3000,
        cycle_no=1,
        session_id="sess-real",
    )
    save_confirmed_sell(
        repository,
        logged_at="2026-07-01T00:02:00+00:00",
        market="overseas",
        symbol="SOXL",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="take_profit",
        pnl_pct=0.03,
        realized_pnl_usd=12.5,
        realized_pnl_krw=17000,
        cycle_no=1,
        session_id="sess-real",
    )
    save_confirmed_sell(
        repository,
        logged_at="2026-07-01T00:03:00+00:00",
        market="overseas",
        symbol="OLDPOS",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="take_profit",
        pnl_pct=0.20,
        realized_pnl_usd=50.0,
        realized_pnl_krw=68000,
        cycle_no=1,
        session_id="sess-real",
        is_session_trade=0,
    )

    summary = repository.get_session_pnl_summary(session_id="sess-real", include_virtual=False)

    assert summary["virtual"] == {}
    assert summary["real"]["domestic"]["trade_count"] == 2
    assert summary["real"]["domestic"]["win_count"] == 1
    assert summary["real"]["overseas"]["trade_count"] == 1
    assert summary["real"]["overseas"]["total_pnl_usd"] == 12.5

    account_risk_summary = repository.get_session_pnl_summary(
        session_id="sess-real",
        include_virtual=False,
        include_non_session_real=True,
    )
    assert account_risk_summary["real"]["overseas"]["trade_count"] == 2
    assert account_risk_summary["real"]["overseas"]["total_pnl_usd"] == 62.5


def test_confirmed_performance_excludes_unverified_rows_and_uses_net_pnl(
    tmp_path,
    save_confirmed_sell,
) -> None:
    repository = SqliteRepository(tmp_path / "confirmed_only.db")
    save_confirmed_sell(
        repository,
        logged_at="2026-07-01T00:00:00+00:00",
        market="overseas",
        symbol="NETLOSS",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="marginal_profit_exit",
        strategy_flag="VWAP",
        entry_by="VWAP",
        pnl_pct=0.05,
        entry_price=10.0,
        qty_executed=1,
        realized_pnl_usd=0.5,
        realized_pnl_krw=675.0,
        net_pnl_usd=-0.3,
        net_pnl_krw=-405.0,
        session_id="sess-confirmed",
    )
    repository.save_cycle_log(
        logged_at="2026-07-01T00:01:00+00:00",
        market="overseas",
        symbol="UNVERIFIED",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="take_profit",
        strategy_flag="VWAP",
        entry_by="VWAP",
        pnl_pct=1.0,
        entry_price=10.0,
        qty_executed=100,
        net_pnl_usd=1000.0,
        net_pnl_krw=1_350_000.0,
        session_id="sess-confirmed",
    )

    summary = repository.get_session_pnl_summary(
        session_id="sess-confirmed",
        include_virtual=False,
    )
    strategy_rows = repository.get_realized_strategy_performance(limit=10)

    overseas = summary["real"]["overseas"]
    assert overseas["trade_count"] == 1
    assert overseas["win_count"] == 0
    assert overseas["total_pnl_usd"] == -0.3
    assert overseas["total_pnl_krw"] == -405.0
    assert summary["real_by_symbol"]["NETLOSS"]["total_pnl_krw"] == -405.0
    assert "UNVERIFIED" not in summary["real_by_symbol"]
    assert len(strategy_rows) == 1
    assert strategy_rows[0]["trade_count"] == 1
    assert strategy_rows[0]["win_rate"] == 0.0
    assert round(float(strategy_rows[0]["avg_pnl_pct"]), 6) == -0.03


def test_get_realized_strategy_performance_excludes_signal_rows(
    tmp_path,
    save_confirmed_buy,
    save_confirmed_sell,
) -> None:
    repository = SqliteRepository(tmp_path / "strategy_performance.db")
    repository.save_cycle_log(
        logged_at="2026-07-01T00:00:00+00:00",
        market="overseas",
        symbol="SOXL",
        exchange_code="NASD",
        action_bias="SELL",
        action_reason="trend_filter_lost",
        strategy_flag="VWAP",
        entry_by="VWAP",
        pnl_pct=-0.10,
    )
    save_confirmed_sell(
        repository,
        logged_at="2026-07-01T00:01:00+00:00",
        market="overseas",
        symbol="SOXL",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="stop_loss",
        strategy_flag="VWAP",
        entry_by="VWAP",
        exit_by="VWAP",
        pnl_pct=-0.02,
        qty_executed=2,
        net_pnl_usd=-4.0,
        net_pnl_krw=-5400.0,
    )
    save_confirmed_sell(
        repository,
        logged_at="2026-07-01T00:01:30+00:00",
        market="overseas",
        symbol="SOXL",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="take_profit",
        strategy_flag="VWAP",
        entry_by="VWAP",
        pnl_pct=0.03,
        qty_executed=1,
        net_pnl_usd=5.0,
        net_pnl_krw=6750.0,
    )
    save_confirmed_sell(
        repository,
        logged_at="2026-07-01T00:02:00+00:00",
        market="domestic",
        symbol="005930",
        exchange_code=None,
        action_bias="SELL_REAL",
        action_reason="take_profit",
        strategy_flag="RSI",
        entry_by="RSI",
        exit_by="take_profit",
        pnl_pct=0.01,
        qty_executed=1,
        net_pnl_krw=3000.0,
    )
    save_confirmed_sell(
        repository,
        logged_at="2026-07-01T00:03:00+00:00",
        market="domestic",
        symbol="IMPORTED",
        exchange_code="KRX",
        action_bias="SELL_REAL",
        action_reason="atr_hard_stop",
        strategy_flag="EXTERNAL",
        entry_by="EXTERNAL",
        pnl_pct=-0.20,
        qty_executed=1,
        net_pnl_krw=-20_000.0,
        is_session_trade=0,
    )
    save_confirmed_buy(
        repository,
        logged_at="2026-07-01T00:04:00+00:00",
        market="overseas",
        symbol="RECOVERED",
        exchange_code="NASD",
        action_bias="BUY_REAL",
        action_reason="strategy_buy_signal",
        strategy_flag="VOL",
        entry_by="VOL",
        qty_executed=1,
        session_id="legacy-session",
        is_session_trade=1,
    )
    save_confirmed_sell(
        repository,
        logged_at="2026-07-01T00:05:00+00:00",
        market="overseas",
        symbol="RECOVERED",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="trend_filter_lost",
        strategy_flag="VOL",
        entry_by="VOL",
        exit_by="trend_filter_lost",
        pnl_pct=-0.01,
        qty_executed=1,
        net_pnl_usd=-1.0,
        net_pnl_krw=-1350.0,
        session_id="legacy-session",
        is_session_trade=0,
    )

    rows = repository.get_realized_strategy_performance(
        after_logged_at="2026-07-01T00:00:00+00:00",
        limit=10,
    )

    assert len(rows) == 4
    by_key = {(row["market"], row["strategy_flag"], row["exit_by"]): row for row in rows}
    assert by_key[("overseas", "VWAP", "stop_loss")]["trade_count"] == 1
    assert by_key[("overseas", "VWAP", "stop_loss")]["total_qty"] == 2
    assert by_key[("overseas", "VWAP", "stop_loss")]["total_net_pnl_usd"] == -4.0
    assert by_key[("overseas", "VWAP", "stop_loss")]["exit_reason"] == "stop_loss"
    assert by_key[("overseas", "VWAP", "stop_loss")]["exit_signal_by"] == "VWAP"
    assert by_key[("overseas", "VWAP", "take_profit")]["trade_count"] == 1
    assert by_key[("overseas", "VWAP", "take_profit")]["win_rate"] == 1.0
    assert by_key[("domestic", "RSI", "take_profit")]["win_rate"] == 1.0
    assert by_key[("overseas", "VOL", "trend_filter_lost")]["trade_count"] == 1
    assert not any(row["strategy_flag"] == "EXTERNAL" for row in rows)


def test_get_sell_reason_counts_groups_recent_sell_real_only(
    tmp_path,
    save_confirmed_sell,
) -> None:
    repository = SqliteRepository(tmp_path / "sell_reason_counts.db")
    save_confirmed_sell(
        repository,
        logged_at="2026-07-01T00:00:00+00:00",
        market="overseas",
        symbol="OLD",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="trend_filter_lost",
    )
    save_confirmed_sell(
        repository,
        logged_at="2026-07-02T00:00:00+00:00",
        market="overseas",
        symbol="NEW1",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="trend_filter_lost",
    )
    save_confirmed_sell(
        repository,
        logged_at="2026-07-02T00:01:00+00:00",
        market="overseas",
        symbol="NEW2",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="stop_loss",
    )
    save_confirmed_sell(
        repository,
        logged_at="2026-07-02T00:01:30+00:00",
        market="domestic",
        symbol="005930",
        exchange_code="KRX",
        action_bias="SELL_REAL",
        action_reason="trend_filter_lost",
    )
    save_confirmed_sell(
        repository,
        logged_at="2026-07-02T00:01:45+00:00",
        market="domestic",
        symbol="IMPORTED",
        exchange_code="KRX",
        action_bias="SELL_REAL",
        action_reason="stop_loss",
        is_session_trade=0,
    )
    repository.save_cycle_log(
        logged_at="2026-07-02T00:02:00+00:00",
        market="overseas",
        symbol="SIGNAL",
        exchange_code="NASD",
        action_bias="SELL",
        action_reason="trend_filter_lost",
    )

    rows = repository.get_sell_reason_counts(after_logged_at="2026-07-02T00:00:00+00:00")

    by_reason = {
        (row["market"], row["action_reason"]): row["cnt"]
        for row in rows
    }
    assert by_reason == {
        ("domestic", "trend_filter_lost"): 1,
        ("overseas", "trend_filter_lost"): 1,
        ("overseas", "stop_loss"): 1,
    }


def test_get_recent_strategy_guard_performance_groups_executed_sell_real(
    tmp_path,
    save_confirmed_sell,
) -> None:
    repository = SqliteRepository(tmp_path / "strategy_guard.db")
    save_confirmed_sell(
        repository,
        logged_at="2026-07-01T00:00:00+00:00",
        market="overseas",
        symbol="OLD",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="trend_filter_lost",
        strategy_flag="VWAP",
        pnl_pct=-0.10,
        qty_executed=1,
    )
    save_confirmed_sell(
        repository,
        logged_at="2026-07-02T00:00:00+00:00",
        market="overseas",
        symbol="A",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="trend_filter_lost",
        strategy_flag="RSI",
        pnl_pct=-0.01,
        qty_executed=1,
        net_pnl_krw=-1000.0,
    )
    save_confirmed_sell(
        repository,
        logged_at="2026-07-02T00:01:00+00:00",
        market="overseas",
        symbol="B",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="take_profit",
        strategy_flag="RSI",
        pnl_pct=0.02,
        qty_executed=1,
        net_pnl_krw=2000.0,
    )
    save_confirmed_sell(
        repository,
        logged_at="2026-07-02T00:01:30+00:00",
        market="overseas",
        symbol="IMPORTED",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="atr_hard_stop",
        strategy_flag="RSI",
        pnl_pct=-0.50,
        qty_executed=1,
        net_pnl_krw=-50_000.0,
        is_session_trade=0,
    )
    repository.save_cycle_log(
        logged_at="2026-07-02T00:02:00+00:00",
        market="overseas",
        symbol="SIGNAL",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="signal_only",
        strategy_flag="RSI",
        pnl_pct=-0.50,
        qty_executed=0,
    )

    rows = repository.get_recent_strategy_guard_performance(
        after_logged_at="2026-07-02T00:00:00+00:00",
        cost_pct=0.005,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["market"] == "overseas"
    assert row["strategy_flag"] == "RSI"
    assert row["trade_count"] == 2
    assert row["win_count"] == 1
    assert round(row["avg_gross_pnl_pct"], 6) == 0.005
    assert round(row["avg_net_pnl_pct"], 6) == 0.0
    assert row["total_net_pnl_krw"] == 1000.0


def test_get_recent_strategy_guard_performance_prefers_recorded_net_pnl_pct(
    tmp_path,
    save_confirmed_sell,
) -> None:
    repository = SqliteRepository(tmp_path / "strategy_guard_recorded_net.db")
    save_confirmed_sell(
        repository,
        logged_at="2026-07-02T00:00:00+00:00",
        market="domestic",
        symbol="AAA",
        exchange_code="KRX",
        action_bias="SELL_REAL",
        action_reason="trend_filter_lost",
        strategy_flag="VWAP",
        pnl_pct=0.10,
        entry_price=1000.0,
        qty_executed=10,
        net_pnl_krw=-200.0,
    )
    save_confirmed_sell(
        repository,
        logged_at="2026-07-02T00:01:00+00:00",
        market="domestic",
        symbol="BBB",
        exchange_code="KRX",
        action_bias="SELL_REAL",
        action_reason="take_profit",
        strategy_flag="VWAP",
        pnl_pct=0.10,
        entry_price=1000.0,
        qty_executed=10,
        net_pnl_krw=100.0,
    )

    rows = repository.get_recent_strategy_guard_performance(
        after_logged_at="2026-07-02T00:00:00+00:00",
        cost_pct=0.005,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["trade_count"] == 2
    assert row["win_count"] == 1
    assert round(row["avg_gross_pnl_pct"], 6) == 0.10
    assert round(row["avg_net_pnl_pct"], 6) == -0.005
    assert row["total_net_pnl_krw"] == -100.0


def test_get_session_pnl_summary_includes_virtual(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "test.db")
    repository.save_virtual_order(
        created_at="2026-07-01 10:00:00 KST",
        market="overseas",
        symbol="SOXL",
        exchange_code="NASD",
        side="sell",
        qty=1,
        fill_price=21.0,
        currency="USD",
        session="regular",
        reason="take_profit",
        realized_pnl=1.0,
        realized_pnl_pct=0.05,
    )
    repository.save_virtual_order(
        created_at="2026-07-01 10:10:00 KST",
        market="overseas",
        symbol="AAPL",
        exchange_code="NASD",
        side="sell",
        qty=1,
        fill_price=199.0,
        currency="USD",
        session="aftermarket",
        reason="stop_loss",
        realized_pnl=-2.0,
        realized_pnl_pct=-0.01,
    )

    summary = repository.get_session_pnl_summary(include_virtual=True)

    assert summary["real"] == {}
    assert summary["virtual"]["overseas_USD"]["trade_count"] == 2
    assert summary["virtual"]["overseas_USD"]["win_count"] == 1
    assert summary["virtual"]["overseas_USD"]["total_pnl"] == -1.0
    assert set(summary["virtual_by_exit_session"]) == {
        "overseas_USD_regular",
        "overseas_USD_aftermarket",
    }

    cost_summary = repository.get_session_pnl_summary(
        include_virtual=True,
        virtual_overseas_commission_rate=0.0025,
        virtual_overseas_sec_fee_rate=0.0000206,
    )
    aggregate = cost_summary["virtual"]["overseas_USD"]
    assert aggregate["net_win_count"] == 1
    assert round(aggregate["total_gross_pnl"], 6) == -1.0
    assert round(aggregate["total_estimated_cost"], 6) == 1.107032
    assert round(aggregate["total_estimated_net_pnl"], 6) == -2.107032
    assert round(
        cost_summary["virtual_by_exit_session"]["overseas_USD_regular"][
            "total_estimated_net_pnl"
        ],
        6,
    ) == 0.897067
    assert round(
        cost_summary["virtual_by_exit_session"]["overseas_USD_aftermarket"][
            "total_estimated_net_pnl"
        ],
        6,
    ) == -3.004099


def test_virtual_performance_summary_excludes_flagged_orders(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "test.db")
    keep_id = repository.save_virtual_order(
        created_at="2026-07-01 10:00:00 KST",
        market="overseas",
        symbol="KEEP",
        exchange_code="NASD",
        side="sell",
        qty=1,
        fill_price=21.0,
        currency="USD",
        session="regular",
        reason="take_profit",
        realized_pnl=1.0,
        realized_pnl_pct=0.05,
    )
    excluded_id = repository.save_virtual_order(
        created_at="2026-07-01 10:10:00 KST",
        market="overseas",
        symbol="BAD",
        exchange_code="NASD",
        side="sell",
        qty=1,
        fill_price=1.0,
        currency="USD",
        session="regular",
        reason="bad_quote",
        realized_pnl=-100.0,
        realized_pnl_pct=-0.50,
    )

    updated = repository.exclude_virtual_orders_from_performance(
        [excluded_id],
        reason="bad_quote_audit",
        excluded_at="2026-07-01T00:00:00+00:00",
    )
    summary = repository.get_virtual_performance_summary()
    session_summary = repository.get_session_pnl_summary(include_virtual=True)
    rows = {int(row["id"]): row for row in repository.list_virtual_orders(limit=10)}

    assert updated == 1
    assert rows[keep_id]["excluded_from_performance"] == 0
    assert rows[excluded_id]["excluded_from_performance"] == 1
    assert rows[excluded_id]["exclude_reason"] == "bad_quote_audit"
    assert summary["overseas_USD"]["trade_count"] == 1
    assert summary["overseas_USD"]["total_pnl"] == 1.0
    assert session_summary["virtual"]["overseas_USD"]["trade_count"] == 1
    assert session_summary["virtual"]["overseas_USD"]["total_pnl"] == 1.0


def test_get_session_pnl_summary_after_filter(tmp_path, save_confirmed_sell) -> None:
    repository = SqliteRepository(tmp_path / "test.db")
    save_confirmed_sell(
        repository,
        logged_at="2026-07-01T00:00:00+00:00",
        market="overseas",
        symbol="SOXL",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="take_profit",
        pnl_pct=0.02,
        realized_pnl_usd=5.0,
        realized_pnl_krw=7000,
        cycle_no=1,
    )
    save_confirmed_sell(
        repository,
        logged_at="2026-07-01T01:00:00+00:00",
        market="overseas",
        symbol="SOXL",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="take_profit",
        pnl_pct=0.03,
        realized_pnl_usd=7.0,
        realized_pnl_krw=9800,
        cycle_no=2,
    )
    repository.save_virtual_order(
        created_at="2026-07-01 08:50:00 KST",
        market="overseas",
        symbol="OLD",
        exchange_code="NASD",
        side="sell",
        qty=1,
        fill_price=10.0,
        currency="USD",
        session="regular",
        reason="old",
        realized_pnl=1.0,
        realized_pnl_pct=0.01,
    )
    repository.save_virtual_order(
        created_at="2026-07-01 10:30:00 KST",
        market="overseas",
        symbol="NEW",
        exchange_code="NASD",
        side="sell",
        qty=1,
        fill_price=10.0,
        currency="USD",
        session="regular",
        reason="new",
        realized_pnl=2.0,
        realized_pnl_pct=0.02,
    )

    summary = repository.get_session_pnl_summary(
        include_virtual=True,
        after_logged_at="2026-07-01T00:30:00+00:00",
    )

    assert summary["real"]["overseas"]["trade_count"] == 1
    assert summary["real"]["overseas"]["total_pnl_usd"] == 7.0
    assert summary["virtual"]["overseas_USD"]["trade_count"] == 1
    assert summary["virtual"]["overseas_USD"]["total_pnl"] == 2.0


def test_policy_evaluation_log_preserves_reasoning_and_later_validation(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "policy_evaluation.db")
    evaluation_id = repository.save_policy_evaluation(
        created_at="2026-07-28T15:00:00+00:00",
        market="overseas",
        evaluation_kind="frequency_review",
        window_start="2026-07-27",
        window_end="2026-07-28",
        subject="entry_frequency",
        decision="hold",
        hypothesis="Blocked entries do not clear round-trip costs.",
        evidence={"blocked_30m_avg_pct": 0.145, "cost_pct": 0.5},
        financial_principles=["evaluate net expectancy", "avoid one-day overfit"],
        alternatives=["loosen volume gate", "keep policy"],
        confidence=0.82,
        falsification_criteria="Revisit after five exits across three final sessions.",
        validation_due_at="2026-07-31T23:59:00+09:00",
        reasoning_mode="high_context",
        comparison_baseline="unchanged_policy_checklist",
        comparative_value_status="unverified",
    )

    pending = repository.list_policy_evaluations(pending_only=True)

    assert len(pending) == 1
    assert pending[0]["id"] == evaluation_id
    assert pending[0]["evidence_json"]["cost_pct"] == 0.5
    assert pending[0]["financial_principles_json"] == [
        "evaluate net expectancy",
        "avoid one-day overfit",
    ]
    assert pending[0]["comparative_value_status"] == "unverified"

    repository.update_policy_evaluation_outcome(
        evaluation_id,
        outcome={"result": "policy remained protected from negative net entries"},
        reviewed_at="2026-07-31T10:00:00+00:00",
        comparative_value_status="confirmed",
        git_commit="abc1234",
    )
    reviewed = repository.list_policy_evaluations(subject="entry_frequency")

    assert reviewed[0]["reviewed_at"] == "2026-07-31T10:00:00+00:00"
    assert reviewed[0]["comparative_value_status"] == "confirmed"
    assert reviewed[0]["outcome_json"]["result"].startswith("policy remained")
    assert reviewed[0]["git_commit"] == "abc1234"
