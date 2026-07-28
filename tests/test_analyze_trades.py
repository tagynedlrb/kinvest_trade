from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from scripts.analyze_trades import (
    compare_before_after,
    main,
    summarize_market_regime_performance,
    summarize_wait_bottlenecks,
)
from kinvest_trade.repository import SqliteRepository


def test_compare_before_after_splits_sell_real_by_kst_cutoff(
    tmp_path,
    save_confirmed_sell,
) -> None:
    repository = SqliteRepository(tmp_path / "analysis.db")
    save_confirmed_sell(
        repository,
        logged_at="2026-07-09T14:30:00+00:00",
        market="overseas",
        symbol="AAA",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="take_profit",
        strategy_flag="VWAP",
        pnl_pct=0.010,
    )
    save_confirmed_sell(
        repository,
        logged_at="2026-07-09T15:30:00+00:00",
        market="overseas",
        symbol="BBB",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="stop_loss",
        strategy_flag="RSI",
        pnl_pct=-0.020,
    )
    save_confirmed_sell(
        repository,
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


def test_compare_before_after_accepts_kst_time_cutoff(
    tmp_path,
    save_confirmed_sell,
) -> None:
    repository = SqliteRepository(tmp_path / "analysis_time_cutoff.db")
    save_confirmed_sell(
        repository,
        logged_at="2026-07-09T15:10:00+00:00",
        market="overseas",
        symbol="AAA",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="take_profit",
        strategy_flag="VWAP",
        pnl_pct=0.010,
    )
    save_confirmed_sell(
        repository,
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


def test_compare_before_after_prefers_recorded_net_pnl_pct(
    tmp_path,
    save_confirmed_sell,
) -> None:
    repository = SqliteRepository(tmp_path / "analysis_recorded_net.db")
    save_confirmed_sell(
        repository,
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
    save_confirmed_sell(
        repository,
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


def test_strategy_breakdown_prefers_action_reason_over_exit_by(
    tmp_path,
    monkeypatch,
    capsys,
    save_confirmed_sell,
) -> None:
    # Regression test for a mislabeling bug found 2026-07-24: exit_by holds a
    # coarser label from the per-symbol strategy manager's own preview check
    # (often just "VWAP"/"RSI"), which is a DIFFERENT, less authoritative
    # signal than action_reason (momentum_policy's actual exit decision, e.g.
    # atr_hard_stop/momentum_loss_cut). When both are set, the strategy
    # breakdown must group by action_reason -- otherwise a real hard-stop
    # exit gets silently reclassified as a generic "VWAP" exit, corrupting
    # the whole per-exit-reason performance breakdown.
    repository = SqliteRepository(tmp_path / "strategy_breakdown.db")
    save_confirmed_sell(
        repository,
        logged_at="2026-07-22T14:19:03+00:00",
        market="overseas",
        symbol="BANC",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="atr_hard_stop",
        exit_by="VWAP",
        strategy_flag="VWAP+VOL",
        pnl_pct=-0.0161,
    )

    monkeypatch.setattr(sys, "argv", ["analyze_trades.py", str(repository.db_path)])
    main()
    output = capsys.readouterr().out

    assert "exit=atr_hard_stop" in output
    assert "exit=VWAP" not in output


def test_summarize_wait_bottlenecks_groups_recent_wait_rows(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "wait_bottleneck.db")
    now = datetime.now(timezone.utc)
    repository.save_cycle_log(
        logged_at=now.isoformat(),
        market="overseas",
        symbol="PLTR",
        exchange_code="NYSE",
        action_bias="WAIT",
        action_reason="volume_low",
        strategy_flag="VWAP",
        volume_ratio=0.25,
        rsi14=55.0,
        intraday_momentum=-0.001,
    )
    repository.save_cycle_log(
        logged_at=(now - timedelta(minutes=5)).isoformat(),
        market="overseas",
        symbol="COIN",
        exchange_code="NASD",
        action_bias="WAIT",
        action_reason="volume_low",
        strategy_flag="VWAP",
        volume_ratio=0.35,
        rsi14=57.0,
        intraday_momentum=0.002,
    )
    repository.save_cycle_log(
        logged_at=(now - timedelta(hours=30)).isoformat(),
        market="domestic",
        symbol="005930",
        exchange_code="KRX",
        action_bias="WAIT",
        action_reason="trend_down",
        strategy_flag="RSI",
        volume_ratio=2.0,
    )

    output = summarize_wait_bottlenecks(repository.db_path, hours=24, limit=3)

    assert "[WAIT 병목] 범위=최근 24시간" in output
    assert "overseas VWAP" in output
    assert "volume_low" in output
    assert "2건" in output
    assert "vr=0.30" in output
    assert "rsi=56.0" in output
    assert "domestic" not in output


def _save_final_domestic_regime(
    repository: SqliteRepository,
    session_date: str,
    *,
    return_pct: float = -2.0,
) -> None:
    repository.upsert_market_regime(
        {
            "market": "domestic",
            "session_date": session_date,
            "benchmark_code": "0001",
            "benchmark_name": "KOSPI",
            "source": "KIS:FHKUP03500100",
            "captured_at": f"{session_date}T07:00:00+00:00",
            "is_final": 1,
            "open_price": 100.0,
            "high_price": 102.0,
            "low_price": 95.0,
            "close_price": 98.0,
            "previous_close": 100.0,
            "return_pct": return_pct,
            "volume": 200,
            "turnover": 1000.0,
            "volume_avg_20": 100.0,
            "volume_ratio_20": 2.0,
            "range_pct": 7.0,
            "range_avg_20": 2.0,
            "range_ratio_20": 3.5,
            "trend_regime": "strong_down",
            "activity_regime": "very_active",
            "volatility_regime": "extreme",
            "regime_key": "strong_down|very_active|extreme",
            "sample_days": 20,
            "raw_json": {},
        }
    )


def test_market_regime_performance_requires_multiple_days_before_policy_evaluation(
    tmp_path,
    save_confirmed_sell,
) -> None:
    repository = SqliteRepository(tmp_path / "regime_analysis.db")
    session_dates = ["2026-07-20", "2026-07-21", "2026-07-22"]
    for session_date in session_dates:
        _save_final_domestic_regime(repository, session_date)
    trade_dates = [
        "2026-07-20",
        "2026-07-20",
        "2026-07-21",
        "2026-07-21",
        "2026-07-22",
    ]
    for index, session_date in enumerate(trade_dates):
        save_confirmed_sell(
            repository,
            logged_at=f"{session_date}T01:0{index}:00+00:00",
            market="domestic",
            symbol=f"00{index}",
            exchange_code="KRX",
            action_bias="SELL_REAL",
            action_reason="trend_filter_lost",
            strategy_flag="VWAP",
            pnl_pct=0.02,
            entry_price=100.0,
            qty_executed=1,
            net_pnl_krw=1.0,
        )

    output = summarize_market_regime_performance(repository.db_path)

    assert "[최근 시장 환경]" in output
    assert "KOSPI=98.00" in output
    assert "레짐=급락/매우활발/극단변동" in output
    assert "[시장 레짐별 KIS 체결확정 손익]" in output
    assert "5건/3일" in output
    assert "Net=+1.000%" in output
    assert "평가가능" in output
    assert "단일 장세 결과로 자동변경 금지" in output


def test_market_regime_performance_marks_one_day_sample_insufficient(
    tmp_path,
    save_confirmed_sell,
) -> None:
    repository = SqliteRepository(tmp_path / "regime_one_day.db")
    _save_final_domestic_regime(repository, "2026-07-20")
    for index in range(5):
        save_confirmed_sell(
            repository,
            logged_at=f"2026-07-20T01:0{index}:00+00:00",
            market="domestic",
            symbol=f"10{index}",
            exchange_code="KRX",
            action_bias="SELL_REAL",
            action_reason="trend_filter_lost",
            strategy_flag="VWAP",
            pnl_pct=-0.01,
        )

    output = summarize_market_regime_performance(repository.db_path)

    assert "표본부족(5/5건,1/3일)" in output


def test_market_regime_performance_separates_provisional_from_missing_data(
    tmp_path,
    save_confirmed_sell,
) -> None:
    repository = SqliteRepository(tmp_path / "regime_pending.db")
    repository.upsert_market_regime(
        {
            "market": "overseas",
            "session_date": "2026-07-28",
            "benchmark_code": "COMP",
            "benchmark_name": "NASDAQ Composite",
            "source": "test",
            "captured_at": "2026-07-28T18:00:00+00:00",
            "is_final": 0,
            "close_price": 100.0,
            "return_pct": -0.2,
            "trend_regime": "sideways",
            "activity_regime": "normal",
            "volatility_regime": "normal",
            "regime_key": "sideways|normal|normal",
            "sample_days": 20,
            "raw_json": {},
        }
    )
    save_confirmed_sell(
        repository,
        logged_at="2026-07-28T18:30:00+00:00",
        market="overseas",
        symbol="AAA",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="take_profit",
        strategy_flag="VWAP",
        pnl_pct=0.01,
    )
    save_confirmed_sell(
        repository,
        logged_at="2026-07-27T18:30:00+00:00",
        market="overseas",
        symbol="BBB",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="stop_loss",
        strategy_flag="VWAP",
        pnl_pct=-0.01,
    )

    output = summarize_market_regime_performance(repository.db_path)

    assert "확정대기=1건(임시 지수자료 존재; 확정 전 정책평가 제외)" in output
    assert "미연결=1건(해당일 지수자료 자체가 없음)" in output
