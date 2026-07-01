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
    )

    buy_rows = repository.query_cycle_log(symbol="SOXL", action_bias="BUY", limit=10)
    sell_rows = repository.query_cycle_log(action_bias="SELL", limit=10)

    assert len(buy_rows) == 1
    assert buy_rows[0]["action_reason"] == "pullback_entry"
    assert buy_rows[0]["cycle_no"] == 7
    assert len(sell_rows) == 1
    assert sell_rows[0]["action_reason"] == "marginal_profit_exit"


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

    summary = repository.get_session_pnl_summary(session_id="sess-real", include_virtual=False)

    assert summary["virtual"] == {}
    assert summary["real"]["domestic"]["trade_count"] == 2
    assert summary["real"]["domestic"]["win_count"] == 1
    assert summary["real"]["overseas"]["trade_count"] == 1
    assert summary["real"]["overseas"]["total_pnl_usd"] == 12.5


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
