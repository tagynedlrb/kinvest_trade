import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import kinvest_trade.telegram_control as telegram_control_module
from kinvest_trade.liquidity_lab import LiquidityLabReport, LiquidityLabService
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
    assert TelegramLiquidityLabController.parse_command("/lab_positions") == "positions"
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
            "signal_state": "BUY_READY",
            "ma_summary": "20d>60d 5>20",
            "note": "ma_fast_reclaim_entry",
            "price": 218.03,
            "holding_qty": 1,
        }
    )

    assert line == "해외 SOXL 상태=BUY_READY 이평=20d>60d 5>20 메모=ma_fast_reclaim_entry 가격=$218.0300 보유=1주"


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


def test_format_watch_target_line_includes_pnl_when_holding() -> None:
    line = TelegramLiquidityLabController._format_watch_target_line(
        {
            "market": "overseas",
            "code": "SOXL",
            "signal_state": "HOLD",
            "ma_summary": "20d>60d 5>20",
            "note": "trend_holding",
            "price": 19.75,
            "holding_qty": 3,
        },
        pnl_pct=0.012,
    )

    assert "손익=+1.20%" in line


def test_format_watch_target_line_no_pnl_when_not_holding() -> None:
    line = TelegramLiquidityLabController._format_watch_target_line(
        {
            "market": "overseas",
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


def test_bot_commands_all_match_telegram_naming_rules() -> None:
    pattern = re.compile(r"^[a-z0-9_]{1,32}$")

    for command in BOT_COMMANDS:
        assert pattern.fullmatch(command["command"]) is not None
