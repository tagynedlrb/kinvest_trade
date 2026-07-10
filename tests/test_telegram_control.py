import asyncio
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import kinvest_trade.telegram_control as telegram_control_module
import pytest
from kinvest_trade.client import KisApiError
from kinvest_trade.liquidity_lab import LiquidityLabReport, LiquidityLabService, VirtualTradeManager
from kinvest_trade.repository import SqliteRepository
from kinvest_trade.telegram_control import (
    BOT_COMMANDS,
    SessionPerformance,
    TelegramLiquidityLabController,
)


def test_parse_command() -> None:
    assert TelegramLiquidityLabController.parse_command("/lab_start") == "start"
    assert TelegramLiquidityLabController.parse_command("/lab_pause") == "pause"
    assert TelegramLiquidityLabController.parse_command("/lab_resume") == "resume"
    assert TelegramLiquidityLabController.parse_command("/lab_stop") == "stop"
    assert TelegramLiquidityLabController.parse_command("/lab_terminate") == "terminate"
    assert TelegramLiquidityLabController.parse_command("/lab_service_restart") == "service_restart"
    assert TelegramLiquidityLabController.parse_command("/lab_status") == "status"
    assert TelegramLiquidityLabController.parse_command("/lab_watchlist") == "watchlist"
    assert TelegramLiquidityLabController.parse_command("/lab_log") == "log"
    assert TelegramLiquidityLabController.parse_command("/lab_orders") == "orders"
    assert TelegramLiquidityLabController.parse_command("/lab_cancel_stale_domestic") == "cancel_stale_domestic"
    assert (
        TelegramLiquidityLabController.parse_command("/lab_cancel_stale_domestic_confirm")
        == "cancel_stale_domestic_confirm"
    )
    assert TelegramLiquidityLabController.parse_command("/lab_cancel_stale_overseas") == "cancel_stale_overseas"
    assert (
        TelegramLiquidityLabController.parse_command("/lab_cancel_stale_overseas_confirm")
        == "cancel_stale_overseas_confirm"
    )
    assert TelegramLiquidityLabController.parse_command("/lab_portfolio") == "portfolio"
    assert TelegramLiquidityLabController.parse_command("/lab_reset") == "reset_virtual"
    assert TelegramLiquidityLabController.parse_command("/lab_reset_confirm") == "reset_virtual_confirm"
    assert TelegramLiquidityLabController.parse_command("/lab_relist NVDA TSLA") == ("relist", "NVDA TSLA")
    assert TelegramLiquidityLabController.parse_command("/lab_relist_schedule") == "relist_schedule"
    assert TelegramLiquidityLabController.parse_command("/lab_cb_reset") == "cb_reset"
    assert TelegramLiquidityLabController.parse_command("/lab_gitlog 2026-07-03") == ("gitlog", "2026-07-03")
    assert TelegramLiquidityLabController.parse_command("/lab_positions") is None
    assert TelegramLiquidityLabController.parse_command("/lab_virtual") is None
    assert TelegramLiquidityLabController.parse_command("/lab_paper_test 005930") == ("paper_test", "005930")
    assert TelegramLiquidityLabController.parse_command("/lab_paper_test") == ("paper_test", None)
    assert TelegramLiquidityLabController.parse_command("/lab_help") == "help"
    assert TelegramLiquidityLabController.parse_command("/unknown") is None


def test_accumulate_session_performance_collects_realized_pnl_and_reasons() -> None:
    controller = TelegramLiquidityLabController.__new__(TelegramLiquidityLabController)
    controller.session_performance = SessionPerformance(started_at=datetime(2026, 6, 25, tzinfo=timezone.utc))

    controller._accumulate_session_performance(
        SimpleNamespace(
            primary_target="SOXL",
            primary_market="overseas",
            primary_selection_reason="highest_current_activity_in_open_market",
            domestic_order={"submitted": True},
            overseas_order={"skipped": True, "reason": "mock_us_session_not_supported"},
        )
    )

    perf = controller.session_performance
    assert perf.cycles_completed == 1
    assert perf.domestic_paper_runs == 0
    assert perf.domestic_paper_realized_pnl_krw == 0
    assert perf.domestic_orders_submitted == 1
    assert perf.overseas_orders_submitted == 0
    assert perf.skip_reasons["mock_us_session_not_supported"] == 1
    assert perf.primary_targets["SOXL"] == 1


def test_format_watch_target_line_is_compact() -> None:
    line = TelegramLiquidityLabController._format_watch_target_line(
        {
            "market": "overseas",
            "code": "SOXL",
            "action_bias": "BUY",
            "signal_state": "BUY_READY",
            "ma_summary": "20d>60d 5>20",
            "strategy_flag": "VWAP+VOL",
            "note": "ma_fast_reclaim_entry",
            "price": 218.03,
            "holding_qty": 1,
        }
    )

    assert line == "해외 SOXL 상태=매수신호 전략=VWAP+VOL 가격=$218.0300"


def test_build_positions_message_formats_held_positions() -> None:
    controller = TelegramLiquidityLabController.__new__(TelegramLiquidityLabController)
    controller.last_report_summary = {
        "domestic_positions": [
            {
                "market": "domestic",
                "stock_code": "005930",
                "quantity": 3,
                "avg_price": 80000.0,
                "current_price": 82400.0,
                "pnl_pct": 0.03,
                "currency": "KRW",
            }
        ],
        "overseas_positions": [
            {
                "market": "overseas",
                "symbol": "SOXL",
                "quantity": 2,
                "avg_price": 19.25,
                "current_price": 19.75,
                "pnl_pct": 0.025974,
                "currency": "USD",
            },
            {
                "market": "overseas",
                "symbol": "AAPL",
                "quantity": 1,
                "avg_price": 201.0,
                "current_price": 199.5,
                "pnl_pct": -0.007463,
                "currency": "USD",
            },
        ]
    }
    controller.current_cycle_no = 7

    message = controller._build_positions_message()

    assert "국내 005930 수량=3 매입=80,000원 현재=82,400원 손익=+3.00%" in message
    assert "해외 SOXL 수량=2 매입=$19.2500 현재=$19.7500 손익=+2.60%" in message
    assert "해외 AAPL 수량=1 매입=$201.0000 현재=$199.5000 손익=-0.75%" in message
    assert "평균손익=+1.62%" in message


def test_build_positions_message_uses_domestic_name_when_available() -> None:
    controller = TelegramLiquidityLabController.__new__(TelegramLiquidityLabController)
    controller.last_report_summary = {
        "domestic_positions": [
            {
                "market": "domestic",
                "stock_code": "005930",
                "quantity": 3,
                "avg_price": 80000.0,
                "current_price": 82400.0,
                "pnl_pct": 0.03,
                "currency": "KRW",
            }
        ],
        "overseas_positions": [],
        "domestic_ranked": [
            {"stock_code": "005930", "stock_name": "삼성전자"},
        ],
    }
    controller.current_cycle_no = 7
    controller.lab_service = SimpleNamespace(_dynamic_domestic_names={"005930": "삼성전자"})

    message = controller._build_positions_message()

    assert "국내 005930(삼성전자) 수량=3 매입=80,000원 현재=82,400원 손익=+3.00%" in message


def test_build_positions_message_returns_none_when_no_positions() -> None:
    controller = TelegramLiquidityLabController.__new__(TelegramLiquidityLabController)
    controller.last_report_summary = {"domestic_positions": [], "overseas_positions": []}
    controller.current_cycle_no = 3

    message = controller._build_positions_message()

    assert "보유종목=없음" in message


def test_build_virtual_portfolio_message_formats_positions_and_summary(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_virtual.db")
    manager = VirtualTradeManager(repository)
    manager.record_buy(
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        qty=1,
        fill_price=20.0,
        currency="USD",
        session="daytime",
        reason="session_not_orderable_in_profile",
        created_at="2026-06-30 19:55:00 KST",
    )
    manager.record_sell(
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        qty=1,
        fill_price=21.0,
        currency="USD",
        session="premarket",
        reason="take_profit",
        created_at="2026-06-30 20:05:00 KST",
    )
    manager.record_buy(
        market="overseas",
        symbol="AAPL",
        exchange_code="NASD",
        qty=2,
        fill_price=200.0,
        currency="USD",
        session="daytime",
        reason="session_not_orderable_in_profile",
        created_at="2026-06-30 20:10:00 KST",
    )
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=DummyNotifier(),
    )

    message = controller._build_virtual_portfolio_message()

    assert "[KIS][VIRTUAL_PORTFOLIO]" in message
    assert "AAPL (virtual) 수량=2 평균단가=$200.0000" in message
    assert "해외 체결=1 승률=+100.00% 실현손익=$1.0000" in message


def test_build_portfolio_message_formats_real_virtual_pending_and_summary(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_portfolio.db")
    manager = VirtualTradeManager(repository)
    manager.record_buy(
        market="overseas",
        symbol="AAPL",
        exchange_code="NASD",
        qty=2,
        fill_price=200.0,
        currency="USD",
        session="daytime",
        reason="session_not_orderable_in_profile",
        created_at="2026-06-30 20:10:00 KST",
    )
    repository.upsert_virtual_sell_pending(
        market="overseas",
        symbol="TSLA",
        exchange_code="NASD",
        qty=1,
        avg_sell_price=250.0,
        currency="USD",
        updated_at="2026-06-30 21:00:00 KST",
    )
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=DummyNotifier(),
    )
    controller.last_report_summary = {
        "domestic_positions": [
            {
                "market": "domestic",
                "stock_code": "005930",
                "quantity": 3,
                "avg_price": 80000.0,
                "current_price": 82400.0,
                "pnl_pct": 0.03,
                "currency": "KRW",
            }
        ],
        "overseas_positions": [
            {
                "market": "overseas",
                "symbol": "SOXL",
                "quantity": 1,
                "avg_price": 19.25,
                "current_price": 19.75,
                "pnl_pct": 0.025974,
                "currency": "USD",
            }
        ],
        "watch_targets": [
            {"code": "AAPL", "price": 210.0},
        ],
    }
    controller.lab_service = SimpleNamespace(_last_overseas_available_usd=1000.0)

    message = controller._build_portfolio_message()

    assert "[KIS][포트폴리오]" in message
    assert "거래루프=중지됨 (/lab_start 필요)" in message
    assert "─── 실보유 종목 ───" in message
    assert "국내 005930 수량=3 매입=80,000원 현재=82,400원 손익=+3.00%" in message
    assert "해외 SOXL 수량=1 매입=$19.2500 현재=$19.7500 손익=+2.60%" in message
    assert "─── 가상보유 종목 ───" in message
    assert "국내 005930 수량=3 매입=80,000원 현재=82,400원 손익=+3.00%" in message
    assert "해외 SOXL 수량=1 매입=$19.2500 현재=$19.7500 손익=+2.60%" in message
    assert "해외 AAPL 수량=2 매입=$200.0000 현재=$210.0000 손익=+5.00%" in message
    assert "─── 가상 노출 ───" in message
    assert (
        "해외 가상매수노출=$400.00 1종목 "
        "한도=주문가능USD x100% 최근한도=$1,000.00 상태=정상"
    ) in message
    assert "─── 정산 대기 매도 ───" in message
    assert "해외 TSLA(v) 수량=-1 가상매도가=$250.0000" in message
    assert "─── 누적 성과 (virtual) ───" in message


def test_build_status_message_shows_stopped_loop_notice() -> None:
    controller = _build_async_controller()
    controller.mode = "stopped"

    message = controller._build_status_message()

    assert "모드=stopped" in message
    assert "거래루프=중지됨 (/lab_start 필요)" in message
    assert "감시데이터=없음 (/lab_start 후 생성)" in message
    assert "다음실행=-" in message
    assert "다음간격=-" in message


def test_build_watchlist_message_explains_missing_report() -> None:
    controller = TelegramLiquidityLabController.__new__(TelegramLiquidityLabController)
    controller.last_report_summary = None
    controller.current_cycle_no = 0
    controller.mode = "stopped"
    controller.lab_service = None
    controller.repository = None

    message = controller._build_watchlist_message()

    assert "감시데이터=없음 (/lab_start 후 생성)" in message
    assert "감시종목=없음" in message


def test_build_portfolio_message_uses_live_real_position_override(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_portfolio_live.db")
    repository.upsert_virtual_position(
        market="overseas",
        symbol="AAPL",
        exchange_code="NASD",
        qty=1,
        avg_price=200.0,
        currency="USD",
        opened_at="2026-07-01T00:00:00+00:00",
        updated_at="2026-07-01T00:00:00+00:00",
    )
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=DummyNotifier(),
    )
    controller.last_report_summary = {
        "domestic_positions": [],
        "overseas_positions": [],
        "watch_targets": [{"market": "overseas", "code": "AAPL", "price": 210.0}],
    }

    message = controller._build_portfolio_message(
        real_positions_override=[
            {
                "market": "overseas",
                "symbol": "AAPL",
                "quantity": 2,
                "orderable_qty": 2,
                "avg_price": 190.0,
                "current_price": 210.0,
                "pnl_pct": (210.0 - 190.0) / 190.0,
                "currency": "USD",
            }
        ]
    )

    assert "해외 AAPL 수량=2 매입=$190.0000 현재=$210.0000 손익=+10.53%" in message
    assert "해외 AAPL 수량=3 매입=$193.3333 현재=$210.0000 손익=+8.62%" in message


def test_build_portfolio_message_warns_real_position_risk_when_loop_stopped(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_portfolio_risk.db")
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20, overseas_stop_loss_pct=0.01),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0, hard_stop_loss_pct=0.01),
        ),
        repository=repository,
        notifier=DummyNotifier(),
    )
    controller.mode = "stopped"

    message = controller._build_portfolio_message(
        real_positions_override=[
            {
                "market": "domestic",
                "stock_code": "058730",
                "quantity": 184,
                "avg_price": 5310.0,
                "current_price": 5030.0,
                "pnl_pct": (5030.0 - 5310.0) / 5310.0,
                "currency": "KRW",
            }
        ],
    )

    assert "─── 실보유 리스크 ───" in message
    assert "국내 058730 손익=-5.27% 기준=-1.00% 수량=184 상태=감시중지" in message
    assert "주의=거래루프가 중지되어 자동 청산 감시가 동작하지 않습니다" in message


def test_build_portfolio_message_uses_available_usd_override_for_virtual_exposure(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_portfolio_available_usd.db")
    repository.upsert_virtual_position(
        market="overseas",
        symbol="AAPL",
        exchange_code="NASD",
        qty=2,
        avg_price=200.0,
        currency="USD",
        opened_at="2026-07-01T00:00:00+00:00",
        updated_at="2026-07-01T00:00:00+00:00",
    )
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20, max_virtual_exposure_pct=0.5),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=DummyNotifier(),
    )

    message = controller._build_portfolio_message(
        price_lookup_override={("overseas", "AAPL"): 210.0},
        virtual_exposure_available_usd=1000.0,
    )

    assert (
        "해외 가상매수노출=$400.00 1종목 "
        "한도=주문가능USD x50% 최근한도=$500.00 상태=정상"
    ) in message


def test_build_portfolio_message_warns_virtual_risk_and_exposure_when_stopped(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_portfolio_virtual_risk.db")
    repository.upsert_virtual_position(
        market="overseas",
        symbol="AAPL",
        exchange_code="NASD",
        qty=3,
        avg_price=200.0,
        currency="USD",
        opened_at="2026-07-01T00:00:00+00:00",
        updated_at="2026-07-01T00:00:00+00:00",
    )
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(
                loop_interval_sec=20,
                max_virtual_exposure_pct=0.5,
                overseas_stop_loss_pct=0.01,
            ),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0, hard_stop_loss_pct=0.01),
        ),
        repository=repository,
        notifier=DummyNotifier(),
    )
    controller.mode = "stopped"

    message = controller._build_portfolio_message(
        price_lookup_override={("overseas", "AAPL"): 190.0},
        virtual_exposure_available_usd=1000.0,
    )

    assert "─── 가상보유 리스크 ───" in message
    assert "해외 AAPL 손익=-5.00% 기준=-1.00% 수량=3 상태=감시중지" in message
    assert "주의=거래루프가 중지되어 가상 포지션 청산 감시가 동작하지 않습니다" in message
    assert (
        "해외 가상매수노출=$600.00 1종목 "
        "한도=주문가능USD x50% 최근한도=$500.00 상태=초과 감시=중지"
    ) in message
    assert "주의=가상 노출 한도 초과 상태에서 거래루프가 중지되어 있습니다" in message


def test_build_portfolio_message_uses_live_virtual_price_override(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_portfolio_virtual_price.db")
    repository.upsert_virtual_position(
        market="overseas",
        symbol="AAPL",
        exchange_code="NASD",
        qty=1,
        avg_price=200.0,
        currency="USD",
        opened_at="2026-07-01T00:00:00+00:00",
        updated_at="2026-07-01T00:00:00+00:00",
    )
    repository.upsert_lab_symbol_state(
        market="overseas",
        symbol="AAPL",
        exchange_code="NASD",
        action_bias="HOLD",
        signal_state="HOLD",
        note="stale",
        holding_qty=1,
        last_price=210.0,
        pnl_pct=0.05,
        strategy_flag="VWAP",
        entry_by="VWAP",
        updated_at="2026-07-01T00:00:00+00:00",
        has_position=1,
    )
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=DummyNotifier(),
    )

    message = controller._build_portfolio_message(
        price_lookup_override={("overseas", "AAPL"): 220.0}
    )

    assert "해외 AAPL 수량=1 매입=$200.0000 현재=$220.0000 손익=+10.00%" in message


def test_load_live_virtual_price_lookup_fetches_quotes_with_fallback(tmp_path) -> None:
    class FakeQuoteClient:
        async def get_overseas_price(self, symbol, exchange_code):
            if symbol == "AAPL":
                return {"last_price": "220.5", "bid": "220.4", "ask": "220.6"}
            if symbol == "MSFT":
                return {"last_price": "", "bid": "300.0", "ask": "302.0"}
            raise AssertionError(symbol)

    repository = SqliteRepository(tmp_path / "telegram_portfolio_live_quote.db")
    manager = VirtualTradeManager(repository)
    manager.record_buy(
        market="overseas",
        symbol="AAPL",
        exchange_code="NASD",
        qty=1,
        fill_price=200.0,
        currency="USD",
        session="regular",
        reason="test",
        created_at="2026-07-01T00:00:00+00:00",
    )
    manager.record_buy(
        market="overseas",
        symbol="MSFT",
        exchange_code="NASD",
        qty=1,
        fill_price=290.0,
        currency="USD",
        session="regular",
        reason="test",
        created_at="2026-07-01T00:00:00+00:00",
    )
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=DummyNotifier(),
    )
    controller.lab_service = SimpleNamespace(client=FakeQuoteClient())

    prices = asyncio.run(controller._load_live_virtual_price_lookup())

    assert prices[("overseas", "AAPL")] == 220.5
    assert prices[("overseas", "MSFT")] == 301.0


def test_load_live_portfolio_positions_parses_domestic_balance_without_ranked_scan(tmp_path) -> None:
    class FakeBalanceClient:
        async def get_balance(self):
            return {
                "positions": [
                    {
                        "pdno": "058730",
                        "hldg_qty": "1,184",
                        "ord_psbl_qty": "184",
                        "pchs_avg_pric": "5,310",
                        "prpr": "5,030",
                    }
                ]
            }

    class FakeLab:
        client = FakeBalanceClient()

        async def _load_overseas_positions(self, _ranked):
            return []

    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=SqliteRepository(tmp_path / "telegram_portfolio_live_domestic.db"),
        notifier=DummyNotifier(),
    )

    positions = asyncio.run(controller._load_live_portfolio_positions(FakeLab()))

    assert positions == [
        {
            "market": "domestic",
            "stock_code": "058730",
            "quantity": 1184,
            "orderable_qty": 184,
            "avg_price": 5310.0,
            "current_price": 5030.0,
            "pnl_pct": (5030.0 - 5310.0) / 5310.0,
            "currency": "KRW",
        }
    ]


def test_load_live_overseas_available_usd_uses_real_position_candidate(tmp_path) -> None:
    class FakeLab:
        def __init__(self) -> None:
            self.calls = []

        async def _get_overseas_available_usd(self, *, symbol, exchange_code, price):
            self.calls.append((symbol, exchange_code, price))
            return 1234.5

    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=SqliteRepository(tmp_path / "telegram_available_usd.db"),
        notifier=DummyNotifier(),
    )
    lab = FakeLab()

    available = asyncio.run(
        controller._load_live_overseas_available_usd(
            lab,
            real_positions=[
                {
                    "market": "overseas",
                    "symbol": "AAPL",
                    "exchange_code": "NASD",
                    "current_price": 210.0,
                }
            ],
            price_lookup={},
        )
    )

    assert available == 1234.5
    assert lab.calls == [("AAPL", "NASD", 210.0)]


def test_build_portfolio_message_applies_pending_sell_to_effective_qty(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_portfolio_pending.db")
    repository.upsert_virtual_sell_pending(
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        qty=1,
        avg_sell_price=20.5,
        currency="USD",
        updated_at="2026-06-30 21:00:00 KST",
    )
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=DummyNotifier(),
    )
    controller.last_report_summary = {
        "domestic_positions": [],
        "overseas_positions": [
            {
                "market": "overseas",
                "symbol": "SOXL",
                "quantity": 3,
                "avg_price": 19.25,
                "current_price": 19.75,
                "pnl_pct": 0.025974,
                "currency": "USD",
            }
        ],
    }

    message = controller._build_portfolio_message()

    assert "해외 SOXL 수량=2 매입=$19.2500 현재=$19.7500 손익=+2.60%" in message


def test_build_portfolio_message_uses_domestic_name_when_available(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_portfolio_name.db")
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=DummyNotifier(),
    )
    controller.last_report_summary = {
        "domestic_positions": [
            {
                "market": "domestic",
                "stock_code": "005930",
                "quantity": 3,
                "avg_price": 80000.0,
                "current_price": 82400.0,
                "pnl_pct": 0.03,
                "currency": "KRW",
            }
        ],
        "overseas_positions": [],
        "watch_targets": [],
        "domestic_ranked": [
            {"stock_code": "005930", "stock_name": "삼성전자"},
        ],
    }
    controller.lab_service = SimpleNamespace(_dynamic_domestic_names={"005930": "삼성전자"})

    message = controller._build_portfolio_message()

    assert "국내 005930(삼성전자) 수량=3 매입=80,000원 현재=82,400원 손익=+3.00%" in message


def test_build_portfolio_message_marks_virtual_position_without_price(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_portfolio_virtual_missing_price.db")
    manager = VirtualTradeManager(repository)
    manager.record_buy(
        market="overseas",
        symbol="SOLS",
        exchange_code="NASD",
        qty=3,
        fill_price=68.7,
        currency="USD",
        session="daytime",
        reason="session_not_orderable_in_profile",
        created_at="2026-06-30 20:10:00 KST",
    )
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=DummyNotifier(),
    )
    controller.last_report_summary = {
        "domestic_positions": [],
        "overseas_positions": [],
        "watch_targets": [],
    }

    message = controller._build_portfolio_message()

    assert "해외 SOLS 수량=3 평균단가=$68.7000 (현재가 없음)" in message


def test_build_portfolio_message_uses_lab_symbol_state_price_for_virtual_position(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_portfolio_virtual_price.db")
    manager = VirtualTradeManager(repository)
    manager.record_buy(
        market="overseas",
        symbol="SOLS",
        exchange_code="NASD",
        qty=3,
        fill_price=68.7,
        currency="USD",
        session="daytime",
        reason="session_not_orderable_in_profile",
        created_at="2026-06-30 20:10:00 KST",
    )
    repository.upsert_lab_symbol_state(
        market="overseas",
        symbol="SOLS",
        exchange_code="NASD",
        action_bias="HOLD",
        signal_state="HOLD",
        note="persisted",
        strategy_flag="VWAP",
        entry_by="VWAP",
        holding_qty=3,
        last_price=61.25,
        pnl_pct=(61.25 - 68.7) / 68.7,
        has_position=1,
        updated_at="2026-07-09T11:00:00+00:00",
    )
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=DummyNotifier(),
    )
    controller.last_report_summary = {
        "domestic_positions": [],
        "overseas_positions": [],
        "watch_targets": [],
    }

    message = controller._build_portfolio_message()

    assert "해외 SOLS 수량=3 매입=$68.7000 현재=$61.2500 손익=-10.84%" in message


def test_send_recent_trade_log_formats_latest_buy_and_sell(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_log.db")
    repository.save_cycle_log(
        logged_at="2026-07-01T00:00:00+00:00",
        market="overseas",
        symbol="SOXL",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="take_profit",
        pnl_pct=0.012,
        realized_pnl_usd=1.5,
        realized_pnl_krw=2025,
        cycle_no=1,
        session_id="sess-log",
    )
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=DummyNotifier(),
    )
    controller.active_session_id = "sess-log"
    controller.session_performance = SessionPerformance(started_at=datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc))

    asyncio.run(controller._send_recent_trade_log())

    message = controller.notifier.messages[-1]
    assert "[KIS][손익요약]" in message
    assert "실주문접수 기준" in message
    assert "주의=체결확정은 MTS/잔고 기준 확인" in message
    assert "거래=1건" in message
    assert "해외손익=+$1.50" in message


def test_lab_log_command_sends_pnl_summary(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_log_command.db")
    repository.save_cycle_log(
        logged_at="2026-07-01T00:00:00+00:00",
        market="domestic",
        symbol="005930",
        exchange_code=None,
        action_bias="SELL_REAL",
        action_reason="take_profit",
        pnl_pct=0.015,
        realized_pnl_krw=3900,
        cycle_no=3,
        session_id="sess-log-cmd",
    )
    notifier = DummyNotifier()
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=notifier,
    )
    controller.active_session_id = "sess-log-cmd"
    controller.session_performance = SessionPerformance(
        started_at=datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    )

    asyncio.run(
        controller._handle_update(
            {
                "message": {
                    "chat": {"id": 123456},
                    "text": "/lab_log",
                }
            }
        )
    )

    message = notifier.messages[-1]
    assert "[KIS][손익요약]" in message
    assert "실주문접수 기준" in message
    assert "환산손익=+3,900원" in message


def test_build_recent_order_events_message_formats_submission_cancel_and_virtual(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_orders.db")
    repository.save_broker_order_event(
        created_at="2026-07-10T01:00:00+00:00",
        market="overseas",
        symbol="AAPL",
        exchange_code="NASD",
        side="BUY",
        order_kind="limit",
        requested_qty=2,
        requested_price=210.5,
        status="SUBMITTED",
        reason="strategy_buy_signal",
        broker_order_no="12345",
        is_virtual=0,
        payload={"output": {"ODNO": "12345"}},
    )
    repository.save_broker_order_event(
        created_at="2026-07-10T01:01:00+00:00",
        market="overseas",
        symbol="AAPL",
        exchange_code="NASD",
        side="SELL",
        order_kind="cancel",
        requested_qty=2,
        requested_price=210.5,
        status="CANCELED",
        reason="stale_exit_replace",
        broker_order_no="12346",
        is_virtual=0,
        payload={"output": {"ODNO": "12346"}},
    )
    repository.save_broker_order_event(
        created_at="2026-07-10T01:02:00+00:00",
        market="overseas",
        symbol="MSFT",
        exchange_code="NASD",
        side="SELL",
        order_kind="virtual_limit",
        requested_qty=1,
        requested_price=300.0,
        status="RECORDED",
        reason="stop_loss",
        is_virtual=1,
        payload={},
    )
    repository.save_broker_order_event(
        created_at="2026-07-10T01:03:00+00:00",
        market="domestic",
        symbol="073240",
        exchange_code="KRX",
        side="BUY",
        order_kind="cancel",
        requested_qty=126,
        requested_price=6990,
        status="REJECTED",
        reason="stale_live_order_cancel_failed",
        broker_order_no="0000013669",
        is_virtual=0,
        payload={"error": "모의투자 장종료 입니다."},
    )
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=DummyNotifier(),
    )

    message = controller._build_recent_order_events_message(limit=5)

    assert "[KIS][주문기록]" in message
    assert "기준=주문 접수/취소/가상기록 (체결확정 아님)" in message
    assert "국내 073240 취소거부 6,990원 x126 상태=REJECTED" in message
    assert "오류=모의투자 장종료 입니다." in message
    assert "해외 MSFT virtual 가상매도기록 $300.0000 x1 상태=RECORDED 사유=손절" in message
    assert "해외 AAPL 취소 $210.5000 x2 상태=CANCELED 사유=미체결 정리 후 재주문 주문번호=12346" in message
    assert "해외 AAPL 매수접수 $210.5000 x2 상태=SUBMITTED 사유=전략 매수 신호 주문번호=12345" in message


def test_build_recent_order_events_message_includes_live_open_orders(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_orders_live.db")
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=DummyNotifier(),
    )
    live_open_orders = [
        {
            "created_at": datetime(2026, 7, 10, 1, 0, tzinfo=timezone.utc),
            "symbol": "AAPL",
            "sll_buy_dvsn_cd": "01",
            "open_qty": 3,
            "order_price": 210.5,
            "order_no": "999",
        }
    ]

    message = controller._build_recent_order_events_message(
        live_open_orders=live_open_orders
    )

    assert "─── live 해외 미체결 ───" in message
    assert "해외 AAPL 매도미체결 $210.5000 x3 주문번호=999" in message
    assert "주문기록=없음" in message


def test_build_recent_order_events_message_includes_live_domestic_open_orders(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_orders_live_domestic.db")
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=DummyNotifier(),
    )
    live_open_domestic_orders = [
        {
            "created_at": datetime(2026, 7, 10, 0, 59, 55, tzinfo=timezone.utc),
            "symbol": "073240",
            "name": "금호타이어",
            "sll_buy_dvsn_cd": "02",
            "open_qty": 126,
            "order_price": 6990,
            "order_no": "0000013669",
        }
    ]

    message = controller._build_recent_order_events_message(
        live_open_domestic_orders=live_open_domestic_orders
    )

    assert "─── live 국내 미체결 ───" in message
    assert "국내 073240(금호타이어) 매수미체결 6,990원 x126 주문번호=0000013669" in message
    assert "주문기록=없음" in message


def test_parse_live_open_domestic_order_rows_filters_closed_and_computes_open_qty(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_orders_parse_live_domestic.db")
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=DummyNotifier(),
    )

    parsed = controller._parse_live_open_domestic_order_rows(
        [
            {
                "pdno": "005930",
                "prdt_name": "삼성전자",
                "ord_qty": "10",
                "tot_ccld_qty": "10",
                "rmn_qty": "0",
                "odno": "closed",
            },
            {
                "pdno": "073240",
                "prdt_name": "금호타이어",
                "ord_qty": "126",
                "tot_ccld_qty": "0",
                "rmn_qty": "",
                "sll_buy_dvsn_cd": "02",
                "ord_unpr": "6,990",
                "ord_dt": "20260710",
                "ord_tmd": "095955",
                "odno": "open",
            },
        ]
    )

    assert len(parsed) == 1
    assert parsed[0]["symbol"] == "073240"
    assert parsed[0]["name"] == "금호타이어"
    assert parsed[0]["open_qty"] == 126
    assert parsed[0]["order_no"] == "open"
    assert parsed[0]["order_price"] == 6990.0


def test_parse_live_open_overseas_order_rows_filters_zero_open_qty(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_orders_parse_live.db")
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=DummyNotifier(),
    )

    parsed = controller._parse_live_open_overseas_order_rows(
        [
            {
                "pdno": "AAPL",
                "nccs_qty": "0",
                "odno": "zero",
                "ft_ord_unpr3": "210.5",
            },
            {
                "pdno": "MSFT",
                "nccs_qty": "1,200",
                "odno": "open",
                "ft_ord_unpr3": "300.25",
                "dmst_ord_dt": "20260710",
                "thco_ord_tmd": "010203",
            },
        ]
    )

    assert len(parsed) == 1
    assert parsed[0]["symbol"] == "MSFT"
    assert parsed[0]["open_qty"] == 1200
    assert parsed[0]["order_no"] == "open"
    assert parsed[0]["order_price"] == 300.25


def test_format_open_order_age_parts_marks_stale_order() -> None:
    now = datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)
    created_at = now - timedelta(minutes=95)

    parts = TelegramLiquidityLabController._format_open_order_age_parts(
        created_at,
        now=now,
        stale_threshold_min=30,
    )

    assert parts == ["경과=1시간35분", "주의=장기미체결"]


def test_format_live_open_domestic_order_line_marks_cancel_session_when_closed(tmp_path) -> None:
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=SqliteRepository(tmp_path / "telegram_open_order_line.db"),
        notifier=DummyNotifier(),
    )
    now = datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)
    row = {
        "created_at": now - timedelta(minutes=95),
        "symbol": "073240",
        "name": "금호타이어",
        "sll_buy_dvsn_cd": "02",
        "open_qty": 126,
        "order_price": 6990,
        "order_no": "0000013669",
    }

    line = controller._format_live_open_domestic_order_line(row, now=now)

    assert "주의=장기미체결" in line
    assert "취소가능=국내장중" in line


def test_send_cancel_stale_domestic_prompt_lists_stale_orders(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_cancel_prompt.db")
    notifier = DummyNotifier()
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=notifier,
    )
    row = {
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=45),
        "symbol": "073240",
        "name": "금호타이어",
        "sll_buy_dvsn_cd": "02",
        "open_qty": 126,
        "order_price": 6990,
        "order_no": "0000013669",
    }
    controller._load_live_open_domestic_orders = lambda: asyncio.sleep(0, result=[row])  # type: ignore[method-assign]

    asyncio.run(controller._send_cancel_stale_domestic_prompt())

    message = notifier.messages[-1]
    assert "[KIS][국내미체결취소]" in message
    assert "대상=1건" in message
    assert "073240(금호타이어) 매수미체결" in message
    assert "실행=/lab_cancel_stale_domestic_confirm" in message


def test_send_cancel_stale_overseas_prompt_lists_stale_orders(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_cancel_overseas_prompt.db")
    notifier = DummyNotifier()
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=notifier,
    )
    row = {
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=45),
        "symbol": "AAPL",
        "exchange_code": "NASD",
        "sll_buy_dvsn_cd": "02",
        "open_qty": 2,
        "order_price": 210.5,
        "order_no": "ov-001",
    }
    controller._load_live_open_overseas_orders = lambda: asyncio.sleep(0, result=[row])  # type: ignore[method-assign]

    asyncio.run(controller._send_cancel_stale_overseas_prompt())

    message = notifier.messages[-1]
    assert "[KIS][해외미체결취소]" in message
    assert "대상=1건" in message
    assert "해외 AAPL 매수미체결" in message
    assert "실행=/lab_cancel_stale_overseas_confirm" in message


def test_execute_cancel_stale_domestic_orders_records_cancel_event(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_cancel_execute.db")
    notifier = DummyNotifier()
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=notifier,
    )
    row = {
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=45),
        "symbol": "073240",
        "name": "금호타이어",
        "sll_buy_dvsn_cd": "02",
        "ord_gno_brno": "00950",
        "ord_dvsn_cd": "00",
        "excg_id_dvsn_cd": "KRX",
        "open_qty": 126,
        "order_price": 6990,
        "order_no": "0000013669",
    }
    controller._load_live_open_domestic_orders = lambda: asyncio.sleep(0, result=[row])  # type: ignore[method-assign]

    class FakeKisClient:
        calls: list[dict] = []

        def __init__(self, credentials) -> None:
            self.credentials = credentials

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def revise_or_cancel_domestic_order(self, **kwargs):
            self.calls.append(kwargs)
            return {"output": {"ODNO": "0000014000"}}

    original_client = telegram_control_module.KisRestClient
    telegram_control_module.KisRestClient = FakeKisClient
    try:
        asyncio.run(controller._execute_cancel_stale_domestic_orders())
    finally:
        telegram_control_module.KisRestClient = original_client

    assert FakeKisClient.calls == [
        {
            "krx_order_orgno": "00950",
            "original_order_no": "0000013669",
            "order_division": "00",
            "rvse_cncl_dvsn_cd": "02",
            "qty": 0,
            "price": 0,
            "qty_all_order_yn": "Y",
            "exchange_code": "KRX",
        }
    ]
    assert "073240(금호타이어) 취소요청 x126" in notifier.messages[-1]
    rows = repository.list_broker_order_events(limit=1)
    assert rows[0]["market"] == "domestic"
    assert rows[0]["symbol"] == "073240"
    assert rows[0]["side"] == "BUY"
    assert rows[0]["status"] == "CANCELED"
    assert rows[0]["reason"] == "stale_live_order_cancel"
    assert rows[0]["broker_order_no"] == "0000014000"


def test_execute_cancel_stale_domestic_orders_records_rejected_event(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_cancel_rejected.db")
    notifier = DummyNotifier()
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=notifier,
    )
    row = {
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=45),
        "symbol": "073240",
        "name": "금호타이어",
        "sll_buy_dvsn_cd": "02",
        "ord_gno_brno": "00950",
        "ord_dvsn_cd": "00",
        "excg_id_dvsn_cd": "KRX",
        "open_qty": 126,
        "order_price": 6990,
        "order_no": "0000013669",
    }
    controller._load_live_open_domestic_orders = lambda: asyncio.sleep(0, result=[row])  # type: ignore[method-assign]

    class FakeKisClient:
        def __init__(self, credentials) -> None:
            self.credentials = credentials

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def revise_or_cancel_domestic_order(self, **kwargs):
            del kwargs
            raise KisApiError("VTTC0803U error: 40580000 모의투자 장종료 입니다.")

    original_client = telegram_control_module.KisRestClient
    telegram_control_module.KisRestClient = FakeKisClient
    try:
        asyncio.run(controller._execute_cancel_stale_domestic_orders())
    finally:
        telegram_control_module.KisRestClient = original_client

    assert "073240 취소실패=장종료(국내장중 재시도 필요)" in notifier.messages[-1]
    rows = repository.list_broker_order_events(limit=1)
    assert rows[0]["market"] == "domestic"
    assert rows[0]["symbol"] == "073240"
    assert rows[0]["side"] == "BUY"
    assert rows[0]["status"] == "REJECTED"
    assert rows[0]["reason"] == "stale_live_order_cancel_failed"
    assert rows[0]["broker_order_no"] == "0000013669"
    payload = rows[0]["payload_json"]
    assert "장종료" in payload["error"]


def test_maybe_auto_cancel_stale_domestic_orders_only_bot_submitted_orders(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_auto_cancel.db")
    repository.save_broker_order_event(
        created_at="2026-07-10T00:59:55+00:00",
        market="domestic",
        symbol="073240",
        exchange_code="KRX",
        side="BUY",
        order_kind="limit",
        requested_qty=126,
        requested_price=6990,
        status="SUBMITTED",
        reason="domestic_buy",
        broker_order_no="0000013669",
        is_virtual=0,
        payload={},
    )
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=DummyNotifier(),
    )
    now = datetime(2026, 7, 10, 1, 0, tzinfo=timezone.utc)
    stale_bot_order = {
        "created_at": now - timedelta(minutes=45),
        "symbol": "073240",
        "name": "금호타이어",
        "sll_buy_dvsn_cd": "02",
        "ord_gno_brno": "00950",
        "ord_dvsn_cd": "00",
        "excg_id_dvsn_cd": "KRX",
        "open_qty": 126,
        "order_price": 6990,
        "order_no": "0000013669",
    }
    stale_manual_order = {
        **stale_bot_order,
        "symbol": "005930",
        "name": "삼성전자",
        "order_no": "manual-order",
    }
    controller._load_live_open_domestic_orders = lambda: asyncio.sleep(  # type: ignore[method-assign]
        0,
        result=[stale_bot_order, stale_manual_order],
    )
    calls: list[dict] = []

    async def fake_execute(*, source="manual", candidate_orders=None):
        calls.append({"source": source, "candidate_orders": candidate_orders})

    controller._execute_cancel_stale_domestic_orders = fake_execute  # type: ignore[method-assign]

    first = asyncio.run(controller._maybe_auto_cancel_stale_domestic_orders(now=now))
    second = asyncio.run(
        controller._maybe_auto_cancel_stale_domestic_orders(now=now + timedelta(minutes=5))
    )

    assert first is True
    assert second is False
    assert calls[0]["source"] == "auto"
    assert [row["order_no"] for row in calls[0]["candidate_orders"]] == ["0000013669"]


def test_auto_cancel_domestic_uses_kst_date_for_holiday_check(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_auto_cancel_holiday_date.db")
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=DummyNotifier(),
    )
    now = datetime(2026, 7, 10, 1, 0, tzinfo=timezone.utc)
    seen_dates: list[date | None] = []
    original_is_krx_holiday = telegram_control_module.is_krx_holiday

    def fake_krx_holiday(target_date=None):
        seen_dates.append(target_date)
        return True

    telegram_control_module.is_krx_holiday = fake_krx_holiday
    try:
        result = asyncio.run(controller._maybe_auto_cancel_stale_domestic_orders(now=now))
    finally:
        telegram_control_module.is_krx_holiday = original_is_krx_holiday

    assert result is False
    assert seen_dates == [date(2026, 7, 10)]


def test_maybe_auto_cancel_stale_overseas_orders_only_bot_submitted_orders(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_auto_cancel_overseas.db")
    repository.save_broker_order_event(
        created_at="2026-07-10T13:35:00+00:00",
        market="overseas",
        symbol="AAPL",
        exchange_code="NASD",
        side="BUY",
        order_kind="limit",
        requested_qty=2,
        requested_price=210.5,
        status="SUBMITTED",
        reason="strategy_buy_signal",
        broker_order_no="ov-001",
        is_virtual=0,
        payload={},
    )
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=DummyNotifier(),
    )
    now = datetime(2026, 7, 10, 14, 30, tzinfo=timezone.utc)
    stale_bot_order = {
        "created_at": now - timedelta(minutes=45),
        "symbol": "AAPL",
        "exchange_code": "NASD",
        "sll_buy_dvsn_cd": "02",
        "open_qty": 2,
        "order_price": 210.5,
        "order_no": "ov-001",
    }
    stale_manual_order = {
        **stale_bot_order,
        "symbol": "MSFT",
        "order_no": "manual-overseas",
    }
    controller._load_live_open_overseas_orders = lambda: asyncio.sleep(  # type: ignore[method-assign]
        0,
        result=[stale_bot_order, stale_manual_order],
    )
    calls: list[dict] = []

    async def fake_execute(*, source="auto", candidate_orders=None):
        calls.append({"source": source, "candidate_orders": candidate_orders})

    controller._execute_cancel_stale_overseas_orders = fake_execute  # type: ignore[method-assign]

    first = asyncio.run(controller._maybe_auto_cancel_stale_overseas_orders(now=now))
    second = asyncio.run(
        controller._maybe_auto_cancel_stale_overseas_orders(now=now + timedelta(minutes=5))
    )

    assert first is True
    assert second is False
    assert calls[0]["source"] == "auto"
    assert [row["order_no"] for row in calls[0]["candidate_orders"]] == ["ov-001"]
    assert calls[0]["candidate_orders"][0]["exchange_code"] == "NASD"


def test_auto_cancel_overseas_uses_new_york_date_for_holiday_check(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_auto_cancel_overseas_holiday_date.db")
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=DummyNotifier(),
    )
    now = datetime(2026, 7, 10, 14, 30, tzinfo=timezone.utc)
    seen_dates: list[date | None] = []
    original_is_nyse_holiday = telegram_control_module.is_nyse_holiday

    def fake_nyse_holiday(target_date=None):
        seen_dates.append(target_date)
        return True

    telegram_control_module.is_nyse_holiday = fake_nyse_holiday
    try:
        result = asyncio.run(controller._maybe_auto_cancel_stale_overseas_orders(now=now))
    finally:
        telegram_control_module.is_nyse_holiday = original_is_nyse_holiday

    assert result is False
    assert seen_dates == [date(2026, 7, 10)]


def test_execute_cancel_stale_overseas_orders_records_cancel_event(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_cancel_overseas.db")
    notifier = DummyNotifier()
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=notifier,
    )
    row = {
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=45),
        "symbol": "AAPL",
        "exchange_code": "NASD",
        "sll_buy_dvsn_cd": "02",
        "open_qty": 2,
        "order_price": 210.5,
        "order_no": "ov-001",
    }

    class FakeKisClient:
        calls: list[dict] = []

        def __init__(self, credentials) -> None:
            self.credentials = credentials

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def revise_or_cancel_overseas_order(self, **kwargs):
            self.calls.append(kwargs)
            return {"output": {"ODNO": "ov-cancel-001"}}

    original_client = telegram_control_module.KisRestClient
    telegram_control_module.KisRestClient = FakeKisClient
    try:
        asyncio.run(
            controller._execute_cancel_stale_overseas_orders(
                source="auto",
                candidate_orders=[row],
            )
        )
    finally:
        telegram_control_module.KisRestClient = original_client

    assert FakeKisClient.calls == [
        {
            "symbol": "AAPL",
            "exchange_code": "NASD",
            "original_order_no": "ov-001",
            "rvse_cncl_dvsn_cd": "02",
            "qty": 2,
            "price": "0",
        }
    ]
    assert "AAPL 취소요청 x2 원주문=ov-001 취소주문=ov-cancel-001" in notifier.messages[-1]
    rows = repository.list_broker_order_events(limit=1)
    assert rows[0]["market"] == "overseas"
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["side"] == "BUY"
    assert rows[0]["status"] == "CANCELED"
    assert rows[0]["reason"] == "stale_live_overseas_order_cancel"
    assert rows[0]["broker_order_no"] == "ov-cancel-001"


def test_lab_orders_command_sends_recent_order_events(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_orders_command.db")
    repository.save_broker_order_event(
        created_at="2026-07-10T01:00:00+00:00",
        market="domestic",
        symbol="005930",
        exchange_code=None,
        side="SELL",
        order_kind="limit",
        requested_qty=3,
        requested_price=82000.0,
        status="SUBMITTED",
        reason="take_profit",
        broker_order_no="777",
        is_virtual=0,
        payload={"output": {"ODNO": "777"}},
    )
    notifier = DummyNotifier()
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=notifier,
    )
    controller._load_live_open_overseas_orders = lambda: asyncio.sleep(0, result=[])  # type: ignore[method-assign]
    controller._load_live_open_domestic_orders = lambda: asyncio.sleep(0, result=[])  # type: ignore[method-assign]

    asyncio.run(
        controller._handle_update(
            {
                "message": {
                    "chat": {"id": 123456},
                    "text": "/lab_orders",
                }
            }
        )
    )

    message = notifier.messages[-1]
    assert "[KIS][주문기록]" in message
    assert "국내 005930 매도접수 82,000원 x3 상태=SUBMITTED 사유=익절 주문번호=777" in message


def test_pnl_summary_excludes_virtual_for_prod(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_prod.db")
    repository.save_cycle_log(
        logged_at="2026-07-01T00:00:00+00:00",
        market="overseas",
        symbol="SOXL",
        exchange_code="NASD",
        action_bias="SELL_REAL",
        action_reason="take_profit",
        pnl_pct=0.008,
        realized_pnl_usd=2.0,
        realized_pnl_krw=2700,
        cycle_no=2,
        session_id="sess-prod",
    )
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
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="live", env="prod"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=DummyNotifier(),
    )
    controller.active_session_id = "sess-prod"

    message = controller._build_session_pnl_message(
        started_at="2026-07-01T00:00:00+00:00",
        session_id="sess-prod",
    )

    assert "[KIS][손익요약]" in message
    assert "가상거래" not in message


def test_pnl_summary_includes_virtual_for_paper(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "telegram_paper.db")
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
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=DummyNotifier(),
    )

    message = controller._build_session_pnl_message(started_at="2026-07-01T00:00:00+00:00")

    assert "가상거래(virtual)" in message


def test_format_watch_target_line_includes_pnl_when_holding() -> None:
    line = TelegramLiquidityLabController._format_watch_target_line(
        {
            "market": "overseas",
            "action_bias": "HOLD",
            "code": "SOXL",
            "signal_state": "HOLD",
            "ma_summary": "20d>60d 5>20",
            "strategy_flag": "VWAP",
            "note": "trend_holding",
            "price": 19.75,
            "holding_qty": 3,
        },
        pnl_pct=0.012,
    )

    assert "상태=보유중" in line
    assert "전략=VWAP" in line
    assert "가격=$19.7500" in line
    assert "보유=3주" in line
    assert "손익=+1.20%" in line


def test_format_watch_target_line_no_pnl_when_not_holding() -> None:
    line = TelegramLiquidityLabController._format_watch_target_line(
        {
            "market": "overseas",
            "action_bias": "WAIT",
            "code": "SOXL",
            "signal_state": "WAIT",
            "ma_summary": "20d>60d 5>20",
            "note": "watch",
            "price": 19.75,
            "holding_qty": 0,
        },
        pnl_pct=0.012,
    )

    assert "손익=" not in line
    assert "상태=대기" in line
    assert "전략=-" in line


def test_build_watchlist_message_uses_balance_cache_for_held_pnl() -> None:
    controller = TelegramLiquidityLabController.__new__(TelegramLiquidityLabController)
    controller.last_report_summary = {
        "watch_targets": [
            {
                "market": "overseas",
                "code": "HOOD",
                "action_bias": "HOLD",
                "signal_state": "HOLD",
                "strategy_flag": "VWAP",
                "note": "trend_holding",
                "price": 28.5,
                "holding_qty": 2,
            }
        ],
        "domestic_positions": [],
        "overseas_positions": [],
        "estimated_api_calls_per_cycle": 12,
    }
    controller.current_cycle_no = 11
    controller.mode = "running"
    controller.lab_service = SimpleNamespace(
        _overseas_balance_cache={
            "data": {
                "NASD": {
                    "positions": [
                        {
                            "ovrs_pdno": "HOOD",
                            "ovrs_cblc_qty": "2",
                            "pchs_avg_pric": "25.00",
                            "now_pric2": "28.50",
                        }
                    ]
                }
            }
        }
    )

    message = controller._build_watchlist_message()

    assert "해외 HOOD 상태=보유중 전략=VWAP 가격=$28.5000 보유=2주 손익=+14.00%" in message


def test_build_watchlist_message_uses_domestic_name_when_available() -> None:
    controller = TelegramLiquidityLabController.__new__(TelegramLiquidityLabController)
    controller.last_report_summary = {
        "watch_targets": [
            {
                "market": "domestic",
                "code": "005930",
                "action_bias": "BUY",
                "signal_state": "BUY",
                "strategy_flag": "VWAP",
                "note": "volume_breakout_entry",
                "price": 82400,
                "holding_qty": 0,
            }
        ],
        "domestic_positions": [],
        "overseas_positions": [],
        "estimated_api_calls_per_cycle": 12,
        "domestic_ranked": [
            {"stock_code": "005930", "stock_name": "삼성전자"},
        ],
    }
    controller.current_cycle_no = 11
    controller.mode = "running"
    controller.lab_service = SimpleNamespace(_dynamic_domestic_names={"005930": "삼성전자"})

    message = controller._build_watchlist_message()

    assert "국내 005930(삼성전자) 상태=매수신호 전략=VWAP 가격=82,400원" in message


def test_format_watch_target_line_ready_status_is_readable() -> None:
    line = TelegramLiquidityLabController._format_watch_target_line(
        {
            "market": "overseas",
            "action_bias": "READY",
            "code": "SOXL",
            "signal_state": "READY",
            "ma_summary": "20d>60d 5>20",
            "strategy_flag": "VOL",
            "note": "near_breakout",
            "price": 19.75,
            "holding_qty": 0,
        }
    )

    assert "상태=📊진입준비" in line
    assert "전략=VOL" in line


def test_format_watch_target_line_marks_stale_signal_cache() -> None:
    line = TelegramLiquidityLabController._format_watch_target_line(
        {
            "market": "overseas",
            "action_bias": "HOLD",
            "code": "PGC",
            "signal_state": "HOLD",
            "strategy_flag": "RSI",
            "note": "vr=0.0x mom=+0.54%|stale_signal_cache",
            "price": 45.06,
            "holding_qty": 439,
        },
        pnl_pct=-0.007,
    )

    assert "해외 PGC 상태=보유중 전략=RSI 가격=$45.0600" in line
    assert "신호=캐시" in line


def test_liquidity_lab_send_summary_skips_when_action_raw_is_wait() -> None:
    service = LiquidityLabService.__new__(LiquidityLabService)
    service.notifier = DummyNotifier()
    service._build_action_summary = lambda report: {  # type: ignore[method-assign]
        "action_raw": "WAIT",
        "action": "대기",
        "price": "-",
        "qty": "-",
        "indicator": "-",
        "reason": "watchlist_wait",
    }
    report = LiquidityLabReport(
        scanned_at="2026-06-30 20:00:00 KST",
        krx_market_open=False,
        us_market_open=True,
        us_market_session="daytime",
        us_orderable_in_profile=False,
        primary_market="overseas",
        primary_target="SMCI",
        primary_selection_reason="watchlist_wait",
        domestic_ranked=[],
        overseas_ranked=[],
        domestic_excluded=[],
        overseas_excluded=[],
        domestic_positions=[],
        overseas_positions=[],
        watch_targets=[],
        estimated_api_calls_per_cycle=0,
        domestic_order=None,
        overseas_order=None,
    )

    asyncio.run(service._send_summary(report))

    assert service.notifier.messages == []


def test_liquidity_lab_send_summary_sends_when_action_raw_is_buy() -> None:
    service = LiquidityLabService.__new__(LiquidityLabService)
    service.notifier = DummyNotifier()
    service._build_action_summary = lambda report: {  # type: ignore[method-assign]
        "action_raw": "BUY",
        "action": "매수",
        "price": "$41.0000",
        "qty": "1",
        "indicator": "RSI 61.0, 거래량 2.5x",
        "reason": "거래량 돌파 진입",
    }
    report = LiquidityLabReport(
        scanned_at="2026-06-30 20:00:00 KST",
        krx_market_open=False,
        us_market_open=True,
        us_market_session="daytime",
        us_orderable_in_profile=False,
        primary_market="overseas",
        primary_target="SMCI",
        primary_selection_reason="watchlist_wait",
        domestic_ranked=[],
        overseas_ranked=[],
        domestic_excluded=[],
        overseas_excluded=[],
        domestic_positions=[],
        overseas_positions=[],
        watch_targets=[],
        estimated_api_calls_per_cycle=0,
        domestic_order=None,
        overseas_order=None,
    )

    asyncio.run(service._send_summary(report))

    assert len(service.notifier.messages) == 1
    assert "동작=매수접수" in service.notifier.messages[0]


class DummyNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.command_calls: list[list[dict[str, str]]] = []
        self.raise_on_set_commands = False
        self.enabled = True

    async def send(self, message: str) -> None:
        self.messages.append(message)

    async def set_commands(self, commands: list[dict[str, str]]) -> bool:
        self.command_calls.append(commands)
        if self.raise_on_set_commands:
            raise RuntimeError("setMyCommands failed")
        return True

    def is_authorized_chat(self, chat_id) -> bool:
        return True


class DummyRepository:
    def __init__(self) -> None:
        self.db_path = Path("/tmp/kinvest_trade_test.db")

    def save_telegram_control_session(self, **kwargs) -> int:
        return 1

    def save_heartbeat(self, status: str, message: str) -> None:
        return None

    def save_risk_event(self, **kwargs) -> None:
        return None

    def query_cycle_log(self, **kwargs) -> list[dict]:
        return []

    def get_session_pnl_summary(self, **kwargs) -> dict:
        return {"real": {}, "virtual": {}}


class DummyAsyncClient:
    def __init__(self, credentials) -> None:
        self.credentials = credentials
        self._client = object()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None


class DummyHeldPosition:
    def __init__(self, symbol: str = "SOXL") -> None:
        self.symbol = symbol
        self.quantity = 0
        self.avg_price = 0.0
        self.current_price = 0.0
        self.pnl_pct = 0.0
        self.exchange_code = "NASD"


class DummyReport:
    def __init__(self, reason: str = "no_supported_market_open") -> None:
        self.scanned_at = "2026-06-30 09:00:00 KST"
        self.primary_market = "none"
        self.primary_target = None
        self.primary_selection_reason = reason
        self.domestic_order = {"skipped": True, "reason": "market_closed"}
        self.overseas_order = {"skipped": True, "reason": "market_closed"}
        self.domestic_positions: list[SimpleNamespace] = []
        self.overseas_positions: list[DummyHeldPosition] = []
        self.estimated_api_calls_per_cycle = 0
        self.krx_market_open = False
        self.us_market_open = False
        self.watch_targets: list[dict] = []

    def to_dict(self) -> dict:
        return {"watch_targets": [], "domestic_positions": [], "overseas_positions": []}


def _build_async_controller() -> TelegramLiquidityLabController:
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(
                profile_name="paper",
                env="vps",
            ),
            liquidity_lab=SimpleNamespace(
                loop_interval_sec=20,
                overseas_candidates=[
                    SimpleNamespace(symbol="NVDA", exchange_code="NASD"),
                    SimpleNamespace(symbol="TSLA", exchange_code="NASD"),
                ],
                overseas_relist_schedule_kst="22:35,01:00,03:30",
            ),
            storage=SimpleNamespace(runtime_state_path=Path("/tmp/kinvest_trade_test_runtime_state.json")),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
            github_token="test-token",
            github_repo="tagynedlrb/kinvest_trade",
            skip_holiday_overseas=True,
            skip_holiday_domestic=True,
        ),
        repository=DummyRepository(),
        notifier=DummyNotifier(),
    )
    controller.mode = "running"
    controller.current_task_started_at = datetime.now(timezone.utc)
    return controller


def test_handle_cb_reset_resets_circuit_breaker_state() -> None:
    controller = TelegramLiquidityLabController.__new__(TelegramLiquidityLabController)
    controller.notifier = DummyNotifier()
    controller.lab_service = SimpleNamespace(
        _consecutive_losses=4,
        _halted_at=datetime.now(timezone.utc),
    )

    asyncio.run(controller._handle_cb_reset())

    assert controller.lab_service._consecutive_losses == 0
    assert controller.lab_service._halted_at is None
    assert "서킷브레이커 수동 해제" in controller.notifier.messages[-1]


def test_handle_start_like_command_resume_resets_circuit_breaker() -> None:
    controller = TelegramLiquidityLabController.__new__(TelegramLiquidityLabController)
    controller.mode = "paused"
    controller.active_session_id = "sess-1"
    controller.session_performance = SessionPerformance(
        started_at=datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    )
    controller.lab_service = SimpleNamespace(
        _consecutive_losses=3,
        _halted_at=datetime.now(timezone.utc),
    )
    controller.notifier = DummyNotifier()
    controller._consecutive_errors = 2
    controller.last_error = "boom"
    controller._write_runtime_state = lambda: None

    asyncio.run(controller._handle_start_like_command("running", "resumed"))

    assert controller.mode == "running"
    assert controller.lab_service._consecutive_losses == 0
    assert controller.lab_service._halted_at is None
    assert controller._consecutive_errors == 0
    assert controller.last_error is None


def test_run_cycle_does_not_stop_on_market_closed() -> None:
    controller = _build_async_controller()

    class FakeLiquidityLabService:
        def __init__(self, config, client, repository, notifier) -> None:
            pass

        async def run(self):
            return DummyReport("no_supported_market_open")

    original_client = telegram_control_module.KisRestClient
    original_service = telegram_control_module.LiquidityLabService
    telegram_control_module.KisRestClient = DummyAsyncClient
    telegram_control_module.LiquidityLabService = FakeLiquidityLabService
    try:
        asyncio.run(controller._run_cycle(1))
    finally:
        telegram_control_module.KisRestClient = original_client
        telegram_control_module.LiquidityLabService = original_service

    assert controller.mode == "running"
    assert controller.last_report_summary is not None
    assert controller.last_report_summary["market_closed"] is True


def test_run_cycle_increments_consecutive_errors_on_exception() -> None:
    controller = _build_async_controller()

    class FailingLiquidityLabService:
        def __init__(self, config, client, repository, notifier) -> None:
            pass

        async def run(self):
            raise RuntimeError("boom")

    original_client = telegram_control_module.KisRestClient
    original_service = telegram_control_module.LiquidityLabService
    telegram_control_module.KisRestClient = DummyAsyncClient
    telegram_control_module.LiquidityLabService = FailingLiquidityLabService
    try:
        asyncio.run(controller._run_cycle(2))
    finally:
        telegram_control_module.KisRestClient = original_client
        telegram_control_module.LiquidityLabService = original_service

    assert controller._consecutive_errors == 1
    assert controller.last_error == "boom"
    assert any("TELEGRAM_CONTROL_ERROR" in message for message in controller.notifier.messages)


def test_run_cycle_cancellation_is_not_persisted_as_error() -> None:
    controller = _build_async_controller()
    controller.last_error = "previous"

    class CancelledLiquidityLabService:
        def __init__(self, config, client, repository, notifier) -> None:
            pass

        async def run(self):
            raise asyncio.CancelledError

    original_client = telegram_control_module.KisRestClient
    original_service = telegram_control_module.LiquidityLabService
    telegram_control_module.KisRestClient = DummyAsyncClient
    telegram_control_module.LiquidityLabService = CancelledLiquidityLabService
    try:
        try:
            asyncio.run(controller._run_cycle(9))
        except asyncio.CancelledError:
            pass
    finally:
        telegram_control_module.KisRestClient = original_client
        telegram_control_module.LiquidityLabService = original_service

    assert controller.last_error is None


def test_run_cycle_resets_consecutive_errors_on_success() -> None:
    controller = _build_async_controller()
    controller._consecutive_errors = 3

    class SuccessfulLiquidityLabService:
        def __init__(self, config, client, repository, notifier) -> None:
            pass

        async def run(self):
            return DummyReport("watchlist_wait")

    original_client = telegram_control_module.KisRestClient
    original_service = telegram_control_module.LiquidityLabService
    telegram_control_module.KisRestClient = DummyAsyncClient
    telegram_control_module.LiquidityLabService = SuccessfulLiquidityLabService
    try:
        asyncio.run(controller._run_cycle(3))
    finally:
        telegram_control_module.KisRestClient = original_client
        telegram_control_module.LiquidityLabService = original_service

    assert controller._consecutive_errors == 0
    assert controller.last_error is None


def test_run_sends_fatal_notification_and_reraises() -> None:
    controller = _build_async_controller()
    controller._restore_runtime_state = lambda: None  # type: ignore[method-assign]
    controller._write_runtime_state = lambda: None  # type: ignore[method-assign]

    async def failing_scheduler_loop() -> None:
        raise RuntimeError("fatal boom")

    async def idle_command_loop() -> None:
        await asyncio.sleep(60)

    original_acquire = telegram_control_module._acquire_pid_lock
    original_release = telegram_control_module._release_pid_lock
    original_signal = telegram_control_module.signal.signal
    telegram_control_module._acquire_pid_lock = lambda: None
    telegram_control_module._release_pid_lock = lambda: None
    telegram_control_module.signal.signal = lambda *_args, **_kwargs: None
    controller._scheduler_loop = failing_scheduler_loop  # type: ignore[method-assign]
    controller._command_loop = idle_command_loop  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="fatal boom"):
            asyncio.run(controller.run())
    finally:
        telegram_control_module._acquire_pid_lock = original_acquire
        telegram_control_module._release_pid_lock = original_release
        telegram_control_module.signal.signal = original_signal

    assert any("TELEGRAM_CONTROL_START" in message for message in controller.notifier.messages)
    assert any("FATAL" in message and "fatal boom" in message for message in controller.notifier.messages)


def test_restore_runtime_state_recovers_update_offset() -> None:
    controller = _build_async_controller()
    controller.config.storage.runtime_state_path.write_text(
        json.dumps(
            {
                "telegram_update_offset": 4321,
                "last_error": "prev_error",
                "telegram_control": {
                    "mode": "running",
                    "current_cycle_no": 12,
                    "next_run_at": "2026-07-09 20:55:00 KST",
                    "last_command": "watchlist",
                    "last_command_at": "2026-07-09 20:54:00 KST",
                    "last_completed_at": "2026-07-09 20:54:40 KST",
                    "last_report_summary": {"primary_target": "SOLS"},
                    "session_performance": {
                        "started_at": "2026-07-09 20:00:00 KST",
                        "cycles_completed": 12,
                        "domestic_paper_runs": 0,
                        "domestic_paper_realized_pnl_krw": 0,
                        "estimated_overseas_realized_pnl_krw": 0,
                        "domestic_orders_submitted": 0,
                        "overseas_orders_submitted": 1,
                        "domestic_orders_failed": 0,
                        "overseas_orders_failed": 0,
                        "skip_reasons": {"no_action": 3},
                        "primary_targets": {"SOLS": 12},
                        "symbol_stats": {"SOLS": {"buy": 1}},
                    },
                    "last_error": "cycle_timeout",
                },
            }
        ),
        encoding="utf-8",
    )

    controller._restore_runtime_state()

    assert controller.update_offset == 4321
    assert controller.mode == "running"
    assert controller.current_cycle_no == 12
    assert controller.last_command == "watchlist"
    assert controller.last_report_summary == {"primary_target": "SOLS"}
    assert controller.last_error == "cycle_timeout"
    assert controller.session_performance.cycles_completed == 12
    assert controller.session_performance.primary_targets == {"SOLS": 12}


def test_restore_runtime_state_ignores_cancelled_cycle_error() -> None:
    controller = _build_async_controller()
    controller.config.storage.runtime_state_path.write_text(
        json.dumps(
            {
                "telegram_update_offset": 4321,
                "last_error": "cycle_1149_cancelled",
                "telegram_control": {
                    "mode": "stopped",
                    "current_cycle_no": 1149,
                    "last_error": "cycle_1149_cancelled",
                },
            }
        ),
        encoding="utf-8",
    )

    controller._restore_runtime_state()

    assert controller.last_error is None


def test_write_runtime_state_persists_update_offset() -> None:
    controller = _build_async_controller()
    controller.update_offset = 9876

    controller._write_runtime_state()

    payload = json.loads(controller.config.storage.runtime_state_path.read_text(encoding="utf-8"))
    assert payload["telegram_update_offset"] == 9876


def test_handle_service_restart_rejects_when_service_missing() -> None:
    controller = _build_async_controller()
    controller._service_restart_supported = lambda: False  # type: ignore[method-assign]

    asyncio.run(controller._handle_service_restart())

    assert "상태=실패" in controller.notifier.messages[-1]


def test_handle_service_restart_schedules_restart_when_service_exists() -> None:
    controller = _build_async_controller()
    controller._service_restart_supported = lambda: True  # type: ignore[method-assign]
    restart_calls: list[str] = []

    async def fake_restart_service_soon() -> None:
        restart_calls.append("called")

    original_create_task = telegram_control_module.asyncio.create_task
    telegram_control_module.asyncio.create_task = lambda coro: asyncio.get_running_loop().create_task(coro)
    controller._restart_service_soon = fake_restart_service_soon  # type: ignore[method-assign]
    try:
        asyncio.run(controller._handle_service_restart())
    finally:
        telegram_control_module.asyncio.create_task = original_create_task

    assert "상태=요청접수" in controller.notifier.messages[-1]
    assert restart_calls == ["called"]


def test_send_reset_virtual_prompt() -> None:
    controller = _build_async_controller()

    asyncio.run(controller._send_reset_virtual_prompt())

    assert "가상거래 초기화" in controller.notifier.messages[-1]
    assert "/lab_reset_confirm" in controller.notifier.messages[-1]


def test_execute_reset_virtual_backs_up_and_clears_virtual_data(tmp_path) -> None:
    repository = SqliteRepository(tmp_path / "reset_virtual.db")
    manager = VirtualTradeManager(repository)
    manager.record_buy(
        market="overseas",
        symbol="SOXL",
        exchange_code="AMEX",
        qty=1,
        fill_price=20.0,
        currency="USD",
        session="regular",
        reason="test_buy",
        created_at="2026-07-01T00:00:00+00:00",
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
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(profile_name="paper", env="vps"),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=tmp_path / "runtime_state.json"),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
        ),
        repository=repository,
        notifier=DummyNotifier(),
    )
    controller.lab_service = SimpleNamespace(
        _exit_cooldown={"overseas:SOXL": datetime.now(timezone.utc)},
        _wait_cycles={"overseas:SOXL": 3},
        _strategy_managers={"SOXL": object()},
        _session_owned_symbols={"SOXL"},
    )

    asyncio.run(controller._execute_reset_virtual())

    assert repository.list_virtual_positions() == []
    assert repository.list_virtual_orders(limit=10) == []
    assert repository.list_virtual_sell_pending() == []
    assert controller.lab_service._exit_cooldown == {}
    assert controller.lab_service._wait_cycles == {}
    assert controller.lab_service._strategy_managers == {}
    assert controller.lab_service._session_owned_symbols == set()
    assert "가상거래 초기화 완료" in controller.notifier.messages[-1]
    backups = sorted(tmp_path.glob("reset_virtual_backup_*_pre_reset.db"))
    assert backups


def test_handle_relist_updates_manual_pool() -> None:
    controller = _build_async_controller()
    controller.lab_service = SimpleNamespace(
        _dynamic_overseas_pool=None,
        _manual_overseas_pool=None,
        _awaiting_relist=True,
        _signal_cache={"OLD": object()},
    )

    asyncio.run(controller._handle_relist("NVDA TSLA"))

    assert controller.manual_overseas_pool == [
        {"symbol": "NVDA", "exchange_code": "NASD"},
        {"symbol": "TSLA", "exchange_code": "NASD"},
    ]
    assert controller.lab_service._dynamic_overseas_pool == controller.manual_overseas_pool
    assert controller.lab_service._awaiting_relist is False
    assert controller.lab_service._signal_cache == {}


def test_handle_relist_parses_exchange_suffix() -> None:
    controller = _build_async_controller()

    asyncio.run(controller._handle_relist("NVDA GM:NYSE BA:NYSE"))

    assert controller.manual_overseas_pool == [
        {"symbol": "NVDA", "exchange_code": "NASD"},
        {"symbol": "GM", "exchange_code": "NYSE"},
        {"symbol": "BA", "exchange_code": "NYSE"},
    ]


def test_send_relist_schedule_reports_configured_times() -> None:
    controller = _build_async_controller()

    asyncio.run(controller._send_relist_schedule())

    assert "[KIS][RELIST_SCHEDULE]" in controller.notifier.messages[-1]
    assert "22:35,01:00,03:30" in controller.notifier.messages[-1]


def test_handle_gitlog_reports_success() -> None:
    controller = _build_async_controller()

    async def fake_upload_log(**kwargs):
        return True, {
            "trades": {
                "url": "https://github.com/tagynedlrb/kinvest_trade/blob/master/logs/trades/test.csv",
                "path": "logs/trades/test.csv",
                "rows": 12,
            },
            "events": {
                "url": "https://github.com/tagynedlrb/kinvest_trade/blob/master/logs/events/test.csv",
                "path": "logs/events/test.csv",
                "rows": 5,
            },
        }

    original_client = telegram_control_module.KisRestClient
    original_upload = telegram_control_module.upload_log
    telegram_control_module.KisRestClient = DummyAsyncClient
    telegram_control_module.upload_log = fake_upload_log
    try:
        asyncio.run(controller._handle_gitlog("2026-07-03"))
    finally:
        telegram_control_module.KisRestClient = original_client
        telegram_control_module.upload_log = original_upload

    assert "📤 GitHub 로그 업로드 중..." in controller.notifier.messages[0]
    assert "✅ 업로드 완료" in controller.notifier.messages[-1]
    assert "trades=test.csv (12건)" in controller.notifier.messages[-1]
    assert "events=test.csv (5건)" in controller.notifier.messages[-1]


def test_run_calls_set_commands_before_start_message() -> None:
    controller = _build_async_controller()

    async def fake_scheduler_loop() -> None:
        raise asyncio.CancelledError

    async def fake_command_loop() -> None:
        raise asyncio.CancelledError

    controller._scheduler_loop = fake_scheduler_loop  # type: ignore[method-assign]
    controller._command_loop = fake_command_loop  # type: ignore[method-assign]
    original_acquire = telegram_control_module._acquire_pid_lock
    original_release = telegram_control_module._release_pid_lock
    telegram_control_module._acquire_pid_lock = lambda: None
    telegram_control_module._release_pid_lock = lambda: None
    try:
        asyncio.run(controller.run())
    finally:
        telegram_control_module._acquire_pid_lock = original_acquire
        telegram_control_module._release_pid_lock = original_release

    assert controller.notifier.command_calls == [BOT_COMMANDS]
    assert controller.notifier.messages[0].startswith("[KIS][TELEGRAM_CONTROL_START]")


def test_run_continues_when_set_commands_raises() -> None:
    controller = _build_async_controller()
    controller.notifier.raise_on_set_commands = True

    async def fake_scheduler_loop() -> None:
        raise asyncio.CancelledError

    async def fake_command_loop() -> None:
        raise asyncio.CancelledError

    controller._scheduler_loop = fake_scheduler_loop  # type: ignore[method-assign]
    controller._command_loop = fake_command_loop  # type: ignore[method-assign]
    original_acquire = telegram_control_module._acquire_pid_lock
    original_release = telegram_control_module._release_pid_lock
    telegram_control_module._acquire_pid_lock = lambda: None
    telegram_control_module._release_pid_lock = lambda: None
    try:
        asyncio.run(controller.run())
    finally:
        telegram_control_module._acquire_pid_lock = original_acquire
        telegram_control_module._release_pid_lock = original_release

    assert controller.notifier.command_calls == [BOT_COMMANDS]
    assert any(message.startswith("[KIS][TELEGRAM_CONTROL_START]") for message in controller.notifier.messages)


def test_acquire_pid_lock_replaces_stale_file(tmp_path) -> None:
    pid_file = tmp_path / "telegram_control.pid"
    original_pid_file = telegram_control_module._PID_FILE
    telegram_control_module._PID_FILE = str(pid_file)
    pid_file.write_text("99999999", encoding="utf-8")
    try:
        telegram_control_module._acquire_pid_lock()
        assert pid_file.read_text(encoding="utf-8").strip() == str(os.getpid())
    finally:
        telegram_control_module._release_pid_lock()
        telegram_control_module._PID_FILE = original_pid_file


def test_acquire_pid_lock_raises_when_process_alive(tmp_path) -> None:
    pid_file = tmp_path / "telegram_control.pid"
    original_pid_file = telegram_control_module._PID_FILE
    telegram_control_module._PID_FILE = str(pid_file)
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    try:
        with pytest.raises(SystemExit) as exc_info:
            telegram_control_module._acquire_pid_lock()
        assert exc_info.value.code == 1
    finally:
        telegram_control_module._release_pid_lock()
        telegram_control_module._PID_FILE = original_pid_file


def test_bot_commands_all_match_telegram_naming_rules() -> None:
    pattern = re.compile(r"^[a-z0-9_]{1,32}$")

    for command in BOT_COMMANDS:
        assert pattern.fullmatch(command["command"]) is not None
