import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import kinvest_trade.telegram_control as telegram_control_module
from kinvest_trade.telegram_control import (
    SessionPerformance,
    TelegramLiquidityLabController,
)


def test_parse_command() -> None:
    assert TelegramLiquidityLabController.parse_command("/lab_start") == "start"
    assert TelegramLiquidityLabController.parse_command("/lab_pause") == "pause"
    assert TelegramLiquidityLabController.parse_command("/lab_resume") == "resume"
    assert TelegramLiquidityLabController.parse_command("/lab_stop") == "stop"
    assert TelegramLiquidityLabController.parse_command("/lab_terminate") == "terminate"
    assert TelegramLiquidityLabController.parse_command("/lab_status") == "status"
    assert TelegramLiquidityLabController.parse_command("/lab_watchlist") == "watchlist"
    assert TelegramLiquidityLabController.parse_command("/lab_positions") == "positions"
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
            "code": "SOXL",
            "signal_state": "BUY_READY",
            "ma_summary": "20d>60d 5>20",
            "note": "ma_fast_reclaim_entry",
            "price": 218.03,
            "holding_qty": 1,
        }
    )

    assert line == "SOXL BUY_READY 20d>60d 5>20 ma_fast_reclaim_entry px=218.0300 hold=1"


def test_build_positions_message_formats_held_positions() -> None:
    controller = TelegramLiquidityLabController.__new__(TelegramLiquidityLabController)
    controller.last_report_summary = {
        "overseas_positions": [
            {
                "symbol": "SOXL",
                "quantity": 2,
                "avg_price": 19.25,
                "current_price": 19.75,
                "pnl_pct": 0.025974,
            },
            {
                "symbol": "AAPL",
                "quantity": 1,
                "avg_price": 201.0,
                "current_price": 199.5,
                "pnl_pct": -0.007463,
            },
        ]
    }
    controller.current_cycle_no = 7

    message = controller._build_positions_message()

    assert "SOXL qty=2 avg=19.2500 px=19.7500 pnl=+2.60%" in message
    assert "AAPL qty=1 avg=201.0000 px=199.5000 pnl=-0.75%" in message
    assert "avg_pnl=+0.93%" in message


def test_build_positions_message_returns_none_when_no_positions() -> None:
    controller = TelegramLiquidityLabController.__new__(TelegramLiquidityLabController)
    controller.last_report_summary = {"overseas_positions": []}
    controller.current_cycle_no = 3

    message = controller._build_positions_message()

    assert "held=none" in message


def test_format_watch_target_line_includes_pnl_when_holding() -> None:
    line = TelegramLiquidityLabController._format_watch_target_line(
        {
            "code": "SOXL",
            "signal_state": "HOLD",
            "ma_summary": "20d>60d 5>20",
            "note": "trend_holding",
            "price": 19.75,
            "holding_qty": 3,
        },
        pnl_pct=0.012,
    )

    assert "pnl=+1.20%" in line


def test_format_watch_target_line_no_pnl_when_not_holding() -> None:
    line = TelegramLiquidityLabController._format_watch_target_line(
        {
            "code": "SOXL",
            "signal_state": "WAIT",
            "ma_summary": "20d>60d 5>20",
            "note": "watch",
            "price": 19.75,
            "holding_qty": 0,
        },
        pnl_pct=0.012,
    )

    assert "pnl=" not in line


class DummyNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.enabled = True

    async def send(self, message: str) -> None:
        self.messages.append(message)


class DummyRepository:
    def save_telegram_control_session(self, **kwargs) -> int:
        return 1

    def save_heartbeat(self, status: str, message: str) -> None:
        return None

    def save_risk_event(self, **kwargs) -> None:
        return None


class DummyAsyncClient:
    def __init__(self, credentials) -> None:
        self.credentials = credentials

    async def __aenter__(self):
        return object()

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
        self.overseas_positions: list[DummyHeldPosition] = []
        self.estimated_api_calls_per_cycle = 0
        self.krx_market_open = False
        self.us_market_open = False
        self.watch_targets: list[dict] = []

    def to_dict(self) -> dict:
        return {"watch_targets": [], "overseas_positions": []}


def _build_async_controller() -> TelegramLiquidityLabController:
    controller = TelegramLiquidityLabController(
        config=SimpleNamespace(
            credentials=SimpleNamespace(
                profile_name="paper",
                env="vps",
            ),
            liquidity_lab=SimpleNamespace(loop_interval_sec=20),
            storage=SimpleNamespace(runtime_state_path=Path("/tmp/kinvest_trade_test_runtime_state.json")),
            auto_trade=SimpleNamespace(usd_krw_fallback_rate=1350.0),
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
