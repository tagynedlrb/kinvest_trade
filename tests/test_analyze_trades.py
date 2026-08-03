from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.analyze_trades import (
    compare_before_after,
    main,
    summarize_exit_forward_performance,
    summarize_market_regime_performance,
    summarize_wait_bottlenecks,
    summarize_wait_forward_performance,
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
    save_confirmed_sell(
        repository,
        logged_at="2026-07-09T16:15:00+00:00",
        market="domestic",
        symbol="IMPORTED",
        exchange_code="KRX",
        action_bias="SELL_REAL",
        action_reason="atr_hard_stop",
        strategy_flag="EXTERNAL",
        pnl_pct=-0.20,
        is_session_trade=0,
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
    assert "EXTERNAL" not in output
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


def test_main_excludes_unconfirmed_and_wrong_side_real_rows(
    tmp_path,
    monkeypatch,
    capsys,
    save_confirmed_buy,
    save_confirmed_sell,
) -> None:
    repository = SqliteRepository(tmp_path / "confirmed_boundary.db")
    logged_at = "2026-07-28T01:00:00+00:00"
    save_confirmed_buy(
        repository,
        logged_at=logged_at,
        market="domestic",
        symbol="GOODBUY",
        exchange_code="KRX",
        action_bias="BUY_REAL",
        action_reason="confirmed_buy",
        strategy_flag="VOL",
        price=1000.0,
        pnl_pct=0.0,
    )
    save_confirmed_sell(
        repository,
        logged_at="2026-07-28T01:01:00+00:00",
        market="domestic",
        symbol="GOODSELL",
        exchange_code="KRX",
        action_bias="SELL_REAL",
        action_reason="confirmed_sell",
        strategy_flag="VOL",
        price=1010.0,
        entry_price=1000.0,
        qty_executed=1,
        pnl_pct=0.01,
        net_pnl_krw=9.0,
    )
    repository.save_cycle_log(
        logged_at="2026-07-28T01:02:00+00:00",
        market="domestic",
        symbol="NOFILLBUY",
        exchange_code="KRX",
        action_bias="BUY_REAL",
        action_reason="unconfirmed_buy",
        strategy_flag="VWAP",
        price=1000.0,
        qty_executed=1,
    )
    repository.save_cycle_log(
        logged_at="2026-07-28T01:03:00+00:00",
        market="domestic",
        symbol="NOFILLSELL",
        exchange_code="KRX",
        action_bias="SELL_REAL",
        action_reason="unconfirmed_sell",
        strategy_flag="VWAP",
        price=100.0,
        entry_price=1000.0,
        qty_executed=1,
        pnl_pct=-0.9,
        net_pnl_krw=-900.0,
    )
    save_confirmed_sell(
        repository,
        logged_at="2026-07-28T01:04:00+00:00",
        market="domestic",
        symbol="WRONGSIDE",
        exchange_code="KRX",
        action_bias="BUY_REAL",
        action_reason="wrong_side_execution",
        strategy_flag="RSI",
        price=1000.0,
        pnl_pct=0.0,
    )
    save_confirmed_sell(
        repository,
        logged_at="2026-07-28T01:05:00+00:00",
        market="domestic",
        symbol="IMPORTED",
        exchange_code="KRX",
        action_bias="SELL_REAL",
        action_reason="external_stop",
        strategy_flag="EXTERNAL",
        price=900.0,
        entry_price=1000.0,
        qty_executed=1,
        pnl_pct=-0.1,
        net_pnl_krw=-100.0,
        is_session_trade=0,
    )

    monkeypatch.setattr(sys, "argv", ["analyze_trades.py", str(repository.db_path)])
    main()
    output = capsys.readouterr().out

    assert "실거래 성과/빈도는 KIS 체결확정 원장" in output
    assert "BUY_REAL     체결확정=1건 미체결제외=2건 전략제외=0건" in output
    assert "SELL_REAL    체결확정=2건 미체결제외=1건 전략제외=1건" in output
    assert "domestic   매수=1건 청산=1건" in output
    assert "confirmed_buy" in output
    assert "confirmed_sell" in output
    assert "unconfirmed_buy" not in output
    assert "unconfirmed_sell" not in output
    assert "wrong_side_execution" not in output
    assert "external_stop" not in output
    assert "GOODBUY" in output
    assert "NOFILLBUY" not in output
    assert "WRONGSIDE" not in output
    assert "거래=2건" in output
    assert "누적=-91원" in output


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


def test_wait_forward_performance_deduplicates_scans_and_uses_market_cost_floor(
    tmp_path,
) -> None:
    repository = SqliteRepository(tmp_path / "wait_forward.db")
    _save_final_domestic_regime(repository, "2026-07-29")
    for logged_at, symbol, action_bias, reason, price in (
        ("2026-07-29T00:00:00+00:00", "AAA", "WAIT", "volume_low", 100.0),
        ("2026-07-29T00:01:00+00:00", "AAA", "WAIT", "volume_low", 100.0),
        ("2026-07-29T00:15:00+00:00", "AAA", "HOLD", "watch", 102.0),
        ("2026-07-29T00:00:00+00:00", "BBB", "WAIT", "volume_low", 200.0),
        ("2026-07-29T00:15:00+00:00", "BBB", "HOLD", "watch", 198.0),
    ):
        repository.save_cycle_log(
            logged_at=logged_at,
            market="domestic",
            symbol=symbol,
            exchange_code="KRX",
            action_bias=action_bias,
            action_reason=reason,
            strategy_flag="VOL",
            price=price,
        )

    output = summarize_wait_forward_performance(
        repository.db_path,
        hours=24,
        market="domestic",
        reason="volume_low",
        horizons=(15,),
        now=datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc),
    )

    assert "[WAIT 선행성과]" in output
    assert "주문가능세션=vps" in output
    assert "raw=3 ep=2" in output
    assert "급락/매우활발/극단변동·확정" in output
    assert "15m n=2/2(100%)" in output
    assert "Gross=+0.500%" in output
    assert "양수=50%" in output
    assert "최소비용Net=+0.470%" in output
    assert "국장 0.03%·미장 0.50%" in output


def test_exit_forward_performance_uses_confirmed_exits_and_final_regime(
    tmp_path,
    save_confirmed_sell,
) -> None:
    repository = SqliteRepository(tmp_path / "exit_forward.db")
    _save_final_domestic_regime(repository, "2026-07-29")
    for symbol, exit_price, price_5m, price_15m in (
        ("AAA", 100.0, 99.0, 97.0),
        ("BBB", 200.0, 198.0, 194.0),
    ):
        save_confirmed_sell(
            repository,
            logged_at="2026-07-29T00:00:00+00:00",
            market="domestic",
            symbol=symbol,
            exchange_code="KRX",
            action_bias="SELL_REAL",
            action_reason="trend_filter_lost",
            strategy_flag="VWAP",
            price=exit_price,
            qty_executed=1,
        )
        repository.save_cycle_log(
            logged_at="2026-07-29T00:05:00+00:00",
            market="domestic",
            symbol=symbol,
            exchange_code="KRX",
            action_bias="WAIT",
            action_reason="watch",
            strategy_flag="VWAP",
            price=price_5m,
        )
        repository.save_cycle_log(
            logged_at="2026-07-29T00:15:00+00:00",
            market="domestic",
            symbol=symbol,
            exchange_code="KRX",
            action_bias="WAIT",
            action_reason="watch",
            strategy_flag="VWAP",
            price=price_15m,
        )
    repository.save_cycle_log(
        logged_at="2026-07-29T00:00:00+00:00",
        market="domestic",
        symbol="UNCONFIRMED",
        exchange_code="KRX",
        action_bias="SELL_REAL",
        action_reason="trend_filter_lost",
        strategy_flag="VWAP",
        price=100.0,
        qty_executed=1,
    )
    repository.save_cycle_log(
        logged_at="2026-07-29T00:15:00+00:00",
        market="domestic",
        symbol="UNCONFIRMED",
        exchange_code="KRX",
        action_bias="WAIT",
        action_reason="watch",
        strategy_flag="VWAP",
        price=200.0,
    )

    output = summarize_exit_forward_performance(
        repository.db_path,
        hours=24,
        market="domestic",
        reason="trend_filter_lost",
        horizons=(5, 15),
        now=datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc),
    )

    assert "[청산 후행성과]" in output
    assert "주문가능세션=vps" in output
    assert "급락/매우활발/극단변동·확정" in output
    assert "exit=2 2종목/1일 전략=VWAP:2" in output
    assert "5m n=2/2(100%) 지연차이=-1.000%" in output
    assert "15m n=2/2(100%) 지연차이=-3.000%" in output
    assert "지연우위=0%" in output
    assert "정책표본=관찰계속(2/5건,1/3일)" in output
    assert "UNCONFIRMED" not in output


def test_compare_before_after_uses_market_specific_fallback_costs(
    tmp_path,
    save_confirmed_sell,
) -> None:
    repository = SqliteRepository(tmp_path / "market_cost_fallback.db")
    for symbol, product_type in (("ETF1", "ETF"), ("STOCK1", "STOCK")):
        save_confirmed_sell(
            repository,
            logged_at="2026-07-09T16:00:00+00:00",
            market="domestic",
            symbol=symbol,
            exchange_code="KRX",
            action_bias="SELL_REAL",
            action_reason="take_profit",
            strategy_flag="VOL",
            pnl_pct=0.01,
            product_type=product_type,
        )

    output = compare_before_after(repository.db_path, "2026-07-10")

    assert "domestic VOL" in output
    assert "net=+0.870%" in output


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
    save_confirmed_sell(
        repository,
        logged_at="2026-07-22T01:09:00+00:00",
        market="domestic",
        symbol="IMPORTED",
        exchange_code="KRX",
        action_bias="SELL_REAL",
        action_reason="atr_hard_stop",
        strategy_flag="EXTERNAL",
        pnl_pct=-0.20,
        entry_price=100.0,
        qty_executed=1,
        net_pnl_krw=-20.0,
        is_session_trade=0,
    )
    assert repository.refresh_final_market_session_reviews() == 3

    output = summarize_market_regime_performance(repository.db_path)

    assert "[최근 시장 환경]" in output
    assert "KOSPI=98.00" in output
    assert "레짐=급락/매우활발/극단변동" in output
    assert "[시장 레짐별 세션소유 KIS 체결확정 손익]" in output
    assert "5건/3일" in output
    assert "6건/3일" not in output
    assert "EXTERNAL" not in output
    assert "Net=+1.000%" in output
    assert "평가가능" in output
    assert "단일 장세 결과로 자동변경 금지" in output
    assert "[시장환경 기록 품질]" in output
    assert "domestic 2026-07-22 진입환경=0/0(100%)" in output
    assert "정책평가원장=비어있음" in output


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
