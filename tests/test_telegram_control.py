import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import kinvest_trade.telegram_control as telegram_control_module
import pytest
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
    assert TelegramLiquidityLabController.parse_command("/lab_portfolio") == "portfolio"
    assert TelegramLiquidityLabController.parse_command("/lab_reset") == "reset_virtual"
    assert TelegramLiquidityLabController.parse_command("/lab_reset_confirm") == "reset_virtual_confirm"
    assert TelegramLiquidityLabController.parse_command("/lab_relist NVDA TSLA") == ("relist", "NVDA TSLA")
    assert TelegramLiquidityLabController.parse_command("/lab_relist_schedule") == "relist_schedule"
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
            paper_run={"run_id": 1, "realized_pnl_krw": 1200},
            domestic_order={"submitted": True},
            overseas_order={"skipped": True, "reason": "mock_us_session_not_supported"},
        )
    )

    perf = controller.session_performance
    assert perf.cycles_completed == 1
    assert perf.domestic_paper_runs == 1
    assert perf.domestic_paper_realized_pnl_krw == 1200
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
    }

    message = controller._build_portfolio_message()

    assert "[KIS][포트폴리오]" in message
    assert "─── 실보유 종목 ───" in message
    assert "국내 005930 수량=3 매입=80,000원 현재=82,400원 손익=+3.00%" in message
    assert "해외 SOXL 수량=1 매입=$19.2500 현재=$19.7500 손익=+2.60%" in message
    assert "─── 가상보유 종목 ───" in message
    assert "국내 005930 수량=3 평균단가=80,000원" in message
    assert "해외 SOXL 수량=1 평균단가=$19.2500" in message
    assert "해외 AAPL 수량=2 평균단가=$200.0000" in message
    assert "─── 정산 대기 매도 ───" in message
    assert "해외 TSLA(v) 수량=-1 가상매도가=$250.0000" in message
    assert "─── 누적 성과 (virtual) ───" in message


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

    assert "해외 SOXL 수량=2 평균단가=$19.2500" in message


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
    assert "실거래" in message
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
    assert "실거래" in message
    assert "환산손익=+3,900원" in message


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
        paper_run=None,
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
        paper_run=None,
        domestic_order=None,
        overseas_order=None,
    )

    asyncio.run(service._send_summary(report))

    assert len(service.notifier.messages) == 1
    assert "동작=매수" in service.notifier.messages[0]


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
        self.paper_run = {"skipped": True, "reason": "market_closed"}
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


def test_restore_runtime_state_recovers_update_offset() -> None:
    controller = _build_async_controller()
    controller.config.storage.runtime_state_path.write_text(
        '{"telegram_update_offset": 4321}',
        encoding="utf-8",
    )

    controller._restore_runtime_state()

    assert controller.update_offset == 4321


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
        return True, "https://github.com/tagynedlrb/kinvest_trade/blob/master/logs/trades/test.csv"

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


def test_run_calls_set_commands_before_start_message() -> None:
    controller = _build_async_controller()

    async def fake_scheduler_loop() -> None:
        raise asyncio.CancelledError

    async def fake_command_loop() -> None:
        raise asyncio.CancelledError

    controller._scheduler_loop = fake_scheduler_loop  # type: ignore[method-assign]
    controller._command_loop = fake_command_loop  # type: ignore[method-assign]

    asyncio.run(controller.run())

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

    asyncio.run(controller.run())

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
