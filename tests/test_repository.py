from __future__ import annotations

import sqlite3

from kinvest_trade.repository import SqliteRepository


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


def test_lab_symbol_state_can_be_upserted_and_loaded(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "test.db")
    snapshot = {"price": 170.0, "volume_ratio": 2.1}

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
    assert state["snapshot_json"]["price"] == 170.0


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

    backup_path = repository.backup_db(suffix="pre_reset")

    assert backup_path.exists()
    assert backup_path.name.startswith("test_backup_")
    assert backup_path.name.endswith("_pre_reset.db")


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
    assert repository.list_virtual_orders(limit=10) == []
    assert repository.list_virtual_sell_pending() == []


def test_get_session_pnl_summary_real_only(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "test.db")
    repository.save_cycle_log(
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
    repository.save_cycle_log(
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
    repository.save_cycle_log(
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
    repository.save_cycle_log(
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


def test_get_realized_strategy_performance_excludes_signal_rows(tmp_path) -> None:
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
    repository.save_cycle_log(
        logged_at="2026-07-01T00:01:00+00:00",
        market="overseas",
        symbol="SOXL",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="stop_loss",
        strategy_flag="VWAP",
        entry_by="VWAP",
        pnl_pct=-0.02,
        qty_executed=2,
        net_pnl_usd=-4.0,
        net_pnl_krw=-5400.0,
    )
    repository.save_cycle_log(
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
    repository.save_cycle_log(
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

    rows = repository.get_realized_strategy_performance(
        after_logged_at="2026-07-01T00:00:00+00:00",
        limit=10,
    )

    assert len(rows) == 3
    by_key = {(row["market"], row["strategy_flag"], row["exit_by"]): row for row in rows}
    assert by_key[("overseas", "VWAP", "stop_loss")]["trade_count"] == 1
    assert by_key[("overseas", "VWAP", "stop_loss")]["total_qty"] == 2
    assert by_key[("overseas", "VWAP", "stop_loss")]["total_net_pnl_usd"] == -4.0
    assert by_key[("overseas", "VWAP", "take_profit")]["trade_count"] == 1
    assert by_key[("overseas", "VWAP", "take_profit")]["win_rate"] == 1.0
    assert by_key[("domestic", "RSI", "take_profit")]["win_rate"] == 1.0


def test_get_sell_reason_counts_groups_recent_sell_real_only(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "sell_reason_counts.db")
    repository.save_cycle_log(
        logged_at="2026-07-01T00:00:00+00:00",
        market="overseas",
        symbol="OLD",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="trend_filter_lost",
    )
    repository.save_cycle_log(
        logged_at="2026-07-02T00:00:00+00:00",
        market="overseas",
        symbol="NEW1",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="trend_filter_lost",
    )
    repository.save_cycle_log(
        logged_at="2026-07-02T00:01:00+00:00",
        market="overseas",
        symbol="NEW2",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="stop_loss",
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

    by_reason = {row["action_reason"]: row["cnt"] for row in rows}
    assert by_reason == {"trend_filter_lost": 1, "stop_loss": 1}


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
        session="regular",
        reason="stop_loss",
        realized_pnl=-2.0,
        realized_pnl_pct=-0.01,
    )

    summary = repository.get_session_pnl_summary(include_virtual=True)

    assert summary["real"] == {}
    assert summary["virtual"]["overseas_USD"]["trade_count"] == 2
    assert summary["virtual"]["overseas_USD"]["win_count"] == 1
    assert summary["virtual"]["overseas_USD"]["total_pnl"] == -1.0


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


def test_get_session_pnl_summary_after_filter(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "test.db")
    repository.save_cycle_log(
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
    repository.save_cycle_log(
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
