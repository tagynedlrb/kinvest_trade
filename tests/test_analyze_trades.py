from __future__ import annotations

from scripts.analyze_trades import compare_before_after
from kinvest_trade.repository import SqliteRepository


def test_compare_before_after_splits_sell_real_by_kst_cutoff(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "analysis.db")
    repository.save_cycle_log(
        logged_at="2026-07-09T14:30:00+00:00",
        market="overseas",
        symbol="AAA",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="take_profit",
        strategy_flag="VWAP",
        pnl_pct=0.010,
    )
    repository.save_cycle_log(
        logged_at="2026-07-09T15:30:00+00:00",
        market="overseas",
        symbol="BBB",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="stop_loss",
        strategy_flag="RSI",
        pnl_pct=-0.020,
    )
    repository.save_cycle_log(
        logged_at="2026-07-09T16:00:00+00:00",
        market="domestic",
        symbol="005930",
        exchange_code=None,
        action_bias="SELL_REAL",
        action_reason="take_profit",
        strategy_flag="VOL",
        pnl_pct=0.015,
    )
    repository.save_cycle_log(
        logged_at="2026-07-09T16:30:00+00:00",
        market="overseas",
        symbol="CCC",
        exchange_code="NASD",
        action_bias="SELL",
        action_reason="signal_only",
        strategy_flag="VWAP",
        pnl_pct=0.100,
    )

    output = compare_before_after(repository.db_path, "2026-07-10")

    assert "[전략 전후 비교] 기준=2026-07-10 KST" in output
    assert "[이전 2026-07-10]" in output
    assert "overseas VWAP" in output
    assert "net=+0.500%" in output
    assert "[이후 2026-07-10]" in output
    assert "overseas RSI" in output
    assert "net=-2.500%" in output
    assert "domestic VOL" in output
    assert "signal_only" not in output


def test_compare_before_after_accepts_kst_time_cutoff(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "analysis_time_cutoff.db")
    repository.save_cycle_log(
        logged_at="2026-07-09T15:10:00+00:00",
        market="overseas",
        symbol="AAA",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="take_profit",
        strategy_flag="VWAP",
        pnl_pct=0.010,
    )
    repository.save_cycle_log(
        logged_at="2026-07-09T15:20:00+00:00",
        market="overseas",
        symbol="BBB",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="stop_loss",
        strategy_flag="RSI",
        pnl_pct=-0.010,
    )

    output = compare_before_after(repository.db_path, "2026-07-10T00:15")

    assert "[전략 전후 비교] 기준=2026-07-10 00:15 KST" in output
    previous_section = output.split("[이전 2026-07-10 00:15]", 1)[1].split(
        "[이후 2026-07-10 00:15]", 1
    )[0]
    after_section = output.split("[이후 2026-07-10 00:15]", 1)[1]
    assert "VWAP" in previous_section
    assert "RSI" not in previous_section
    assert "RSI" in after_section


def test_compare_before_after_prefers_recorded_net_pnl_pct(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "analysis_recorded_net.db")
    repository.save_cycle_log(
        logged_at="2026-07-09T16:00:00+00:00",
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
    repository.save_cycle_log(
        logged_at="2026-07-09T16:01:00+00:00",
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

    output = compare_before_after(repository.db_path, "2026-07-10")

    assert "domestic VWAP" in output
    assert "net=-0.500%" in output
    assert "승률=50%" in output
