from __future__ import annotations

import atexit
import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
import traceback
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import TypeAlias

from .client import KisRestClient, parse_kis_number
from .config import AppConfig
from .git_uploader import upload_log
from .liquidity_lab import LiquidityLabReport, LiquidityLabService, VirtualTradeManager
from .market_calendar import is_krx_holiday, is_nyse_holiday
from .market_sessions import (
    determine_loop_interval_sec,
    get_us_trading_session,
    is_krx_regular_session,
    is_us_orderable_session_for_env,
    minutes_until_next_tradeable_session,
    us_holiday_date_for_kis_session,
)
from .message_format import (
    format_krw,
    format_market_korean,
    format_pct,
    format_reason_korean,
    format_side_korean,
    format_usd,
)
from .notifier import TelegramNotifier
from .paper import PaperTradingService
from .repository import SqliteRepository
from .telegram_orders import OrderAdminHelper
from .telegram_reports import ReportHelper
from .time_utils import (
    KST,
    ensure_timezone,
    format_display_times,
    format_kst,
    format_kst_korean,
    parse_datetime,
)
from .trade_analysis import compare_before_after, summarize_wait_bottlenecks


# Grouped by category so /lab_help doesn't dump 20+ commands as one flat list.
# Keep in sync with MENU_CATEGORIES below (used by the /lab_menu inline-button browser).
MENU_CATEGORIES: list[tuple[str, str, list[tuple[str, str]]]] = [
    (
        "lifecycle",
        "🎛 운영 제어",
        [
            ("/lab_start", "거래 루프 시작"),
            ("/lab_pause", "현재 사이클 종료 후 일시정지"),
            ("/lab_resume", "일시정지 해제"),
            ("/lab_stop", "즉시 중지 후 세션 요약"),
            ("/lab_terminate", "강제 종료 후 대기"),
            ("/lab_service_restart", "텔레그램 제어 서비스 재시작"),
        ],
    ),
    (
        "status",
        "📊 상태 조회",
        [
            ("/lab_status", "현재 상태"),
            ("/lab_watchlist", "감시 종목 요약"),
            ("/lab_portfolio", "보유현황 통합 (실보유·가상·성과)"),
            ("/lab_guard", "현재 성과 기반 전략 차단 상태"),
        ],
    ),
    (
        "logs",
        "📜 로그 및 성과",
        [
            ("/lab_log", "최근 매매 내역 조회"),
            ("/lab_performance [시간]", "최근 실주문접수 전략 성과"),
            ("/lab_report compare <YYYY-MM-DD>", "기준일 전후 전략 성과 비교"),
            ("/lab_report wait [시간]", "최근 WAIT 병목 요약"),
            ("/lab_orders", "최근 주문 접수/취소 기록"),
            ("/lab_gitlog [날짜]", "거래 로그를 GitHub에 업로드"),
        ],
    ),
    (
        "data",
        "🗄 데이터/성과 초기화",
        [
            ("/lab_trim_virtual", "가상보유 초과분만 성과 제외 정리"),
            ("/lab_reset", "가상거래만 초기화 (DB 백업 후)"),
            ("/lab_reset_all", "전체 거래이력·성과 초기화 (테스트 환경 재구성용)"),
            ("/lab_cb_reset", "서킷브레이커 강제 해제 (연속손절 카운터 초기화)"),
        ],
    ),
    (
        "watch",
        "👀 감시종목 설정",
        [
            ("/lab_relist <심볼...>", "해외 감시 풀 수동 교체"),
            ("/lab_relist_schedule", "해외 relist 알림 시간 확인"),
        ],
    ),
    (
        "test",
        "🧪 테스트",
        [
            ("/lab_paper_test <종목코드>", "수동 페이퍼 테스트"),
        ],
    ),
]


def _build_help_message() -> str:
    lines = ["[KIS][TELEGRAM_CONTROL_HELP]", "카테고리별 명령 목록 (버튼 메뉴: /lab_menu)"]
    for _key, label, commands in MENU_CATEGORIES:
        lines.append("")
        lines.append(f"── {label} ──")
        lines.extend(f"{command} - {desc}" for command, desc in commands)
    lines.append("")
    lines.append("/lab_help - 명령 목록")
    return "\n".join(lines)


HELP_MESSAGE = _build_help_message()

BOT_COMMANDS: list[dict[str, str]] = [
    {"command": "lab_start", "description": "거래 루프 시작"},
    {"command": "lab_pause", "description": "사이클 종료 후 일시정지"},
    {"command": "lab_resume", "description": "일시정지 해제"},
    {"command": "lab_stop", "description": "즉시 중지 후 세션 요약"},
    {"command": "lab_terminate", "description": "강제 종료 후 대기"},
    {"command": "lab_service_restart", "description": "제어 서비스 재시작"},
    {"command": "lab_status", "description": "현재 상태 조회"},
    {"command": "lab_watchlist", "description": "감시 종목 요약"},
    {"command": "lab_log", "description": "최근 매매 내역"},
    {"command": "lab_performance", "description": "최근 실주문접수 전략 성과"},
    {"command": "lab_report", "description": "기준일 전후 전략 성과 비교"},
    {"command": "lab_guard", "description": "전략 차단 상태"},
    {"command": "lab_orders", "description": "최근 주문 접수/취소 기록"},
    {"command": "lab_portfolio", "description": "보유현황 통합 보기"},
    {"command": "lab_trim_virtual", "description": "가상보유 초과분 정리"},
    {"command": "lab_reset", "description": "가상거래 초기화 (백업 후)"},
    {"command": "lab_reset_all", "description": "전체 이력·성과 초기화 (백업 후)"},
    {"command": "lab_relist", "description": "해외 감시 풀 수동 교체"},
    {"command": "lab_relist_schedule", "description": "해외 relist 알림 시간"},
    {"command": "lab_cb_reset", "description": "서킷브레이커 강제 해제"},
    {"command": "lab_gitlog", "description": "거래 로그 GitHub 업로드"},
    {"command": "lab_paper_test", "description": "페이퍼 테스트(종목코드 필요)"},
    {"command": "lab_menu", "description": "카테고리별 명령 버튼 메뉴"},
    {"command": "lab_help", "description": "명령 목록 보기"},
]

ParsedCommand: TypeAlias = str | tuple[str, str | None]
SERVICE_UNIT_NAME = "kinvest-telegram-control.service"
_PID_FILE = "data/telegram_control.pid"
_STARTUP_NOTIFICATION_THROTTLE_MINUTES = 10
_logger = logging.getLogger(__name__)


def _release_pid_lock() -> None:
    try:
        if os.path.exists(_PID_FILE):
            with open(_PID_FILE, encoding="utf-8") as handle:
                pid = int(handle.read().strip())
            if pid == os.getpid():
                os.remove(_PID_FILE)
    except Exception:  # noqa: BLE001
        pass


def _acquire_pid_lock() -> None:
    os.makedirs(os.path.dirname(_PID_FILE), exist_ok=True)

    if os.path.exists(_PID_FILE):
        try:
            with open(_PID_FILE, encoding="utf-8") as handle:
                old_pid = int(handle.read().strip())
            os.kill(old_pid, 0)
            print(
                f"[WARN] 이미 실행 중인 인스턴스가 있습니다 (PID {old_pid}). 종료합니다.",
                flush=True,
            )
            raise SystemExit(1)
        except (OSError, ValueError):
            pass

    with open(_PID_FILE, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))

    atexit.register(_release_pid_lock)


@dataclass(slots=True)
class ControllerSnapshot:
    mode: str
    current_cycle_no: int
    active_session_id: str
    active_cycle_started_at: str | None
    next_run_at: str | None
    last_command: str | None
    last_command_at: str | None
    last_completed_at: str | None
    last_report_summary: dict | None
    session_performance: dict | None
    last_error: str | None

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "current_cycle_no": self.current_cycle_no,
            "active_session_id": self.active_session_id,
            "active_cycle_started_at": self.active_cycle_started_at,
            "next_run_at": self.next_run_at,
            "last_command": self.last_command,
            "last_command_at": self.last_command_at,
            "last_completed_at": self.last_completed_at,
            "last_report_summary": self.last_report_summary,
            "session_performance": self.session_performance,
            "last_error": self.last_error,
        }


@dataclass(slots=True)
class SessionPerformance:
    started_at: datetime | None = None
    cycles_completed: int = 0
    domestic_paper_runs: int = 0
    domestic_paper_realized_pnl_krw: int = 0
    estimated_overseas_realized_pnl_krw: int = 0
    domestic_orders_submitted: int = 0
    overseas_orders_submitted: int = 0
    domestic_orders_failed: int = 0
    overseas_orders_failed: int = 0
    skip_reasons: dict[str, int] | None = None
    primary_targets: dict[str, int] | None = None
    symbol_stats: dict[str, dict[str, int]] | None = None

    def __post_init__(self) -> None:
        if self.skip_reasons is None:
            self.skip_reasons = {}
        if self.primary_targets is None:
            self.primary_targets = {}
        if self.symbol_stats is None:
            self.symbol_stats = {}

    def to_dict(self) -> dict:
        return {
            "started_at": format_kst(self.started_at),
            "cycles_completed": self.cycles_completed,
            "domestic_paper_runs": self.domestic_paper_runs,
            "domestic_paper_realized_pnl_krw": self.domestic_paper_realized_pnl_krw,
            "estimated_overseas_realized_pnl_krw": self.estimated_overseas_realized_pnl_krw,
            "domestic_orders_submitted": self.domestic_orders_submitted,
            "overseas_orders_submitted": self.overseas_orders_submitted,
            "domestic_orders_failed": self.domestic_orders_failed,
            "overseas_orders_failed": self.overseas_orders_failed,
            "skip_reasons": dict(sorted((self.skip_reasons or {}).items(), key=lambda item: (-item[1], item[0]))),
            "primary_targets": dict(sorted((self.primary_targets or {}).items(), key=lambda item: (-item[1], item[0]))),
            "symbol_stats": dict(sorted((self.symbol_stats or {}).items(), key=lambda item: item[0])),
        }


class TelegramLiquidityLabController:
    def __init__(
        self,
        config: AppConfig,
        repository: SqliteRepository,
        notifier: TelegramNotifier,
    ) -> None:
        self.config = config
        self.repository = repository
        self.notifier = notifier
        self.mode = "stopped"
        self.current_cycle_no = 0
        self.current_task: asyncio.Task[None] | None = None
        self.current_task_started_at: datetime | None = None
        self.next_run_at: datetime | None = None
        self.last_command: str | None = None
        self.last_command_at: datetime | None = None
        self.last_completed_at: datetime | None = None
        self.last_report_summary: dict | None = None
        self.session_performance = SessionPerformance()
        self.last_error: str | None = None
        self.update_offset: int | None = None
        self._consecutive_errors: int = 0
        self._last_market_state: str = ""
        self.active_session_id: str = ""
        self.lab_service: LiquidityLabService | None = None
        self._restored_lab_runtime_state: dict = {}
        self.manual_overseas_pool: list[dict[str, str]] | None = None
        self._last_auto_stale_domestic_cancel_at: datetime | None = None
        self._last_auto_stale_overseas_cancel_at: datetime | None = None
        self._last_startup_notification_at: datetime | None = None
        self.order_admin = OrderAdminHelper(self)
        self.reports = ReportHelper(self)

    def _get_order_admin_helper(self) -> OrderAdminHelper:
        helper = getattr(self, "order_admin", None)
        if helper is None:
            helper = OrderAdminHelper(self)
            self.order_admin = helper
        return helper

    def _get_report_helper(self) -> ReportHelper:
        helper = getattr(self, "reports", None)
        if helper is None:
            helper = ReportHelper(self)
            self.reports = helper
        return helper

    async def run(self) -> None:
        _acquire_pid_lock()
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _on_sigterm(_sig: int, _frame: object) -> None:
            _logger.info("SIGTERM received; stopping telegram control gracefully.")
            loop.call_soon_threadsafe(stop_event.set)

        previous_sigterm_handler = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _on_sigterm)
        if not self.notifier.enabled:
            raise RuntimeError("Telegram bot token/chat id are required for telegram-control.")
        self._restore_runtime_state()
        self._write_runtime_state()
        try:
            await self.notifier.set_commands(BOT_COMMANDS)
        except Exception:  # noqa: BLE001
            pass
        await self._send_startup_message_if_due()
        scheduler = asyncio.create_task(self._scheduler_loop())
        command_loop = asyncio.create_task(self._command_loop())
        stop_task = asyncio.create_task(stop_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {scheduler, command_loop, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_task in done:
                # SIGTERM(서비스 재시작/배포)은 사용자 정지 명령이 아니므로
                # mode를 강제로 stopped로 바꾸지 않고 그대로 저장한다.
                # running 상태에서 재시작하면 기동 후 루프가 자동 재개된다.
                self._write_runtime_state()
            else:
                await asyncio.gather(scheduler, command_loop)
        except asyncio.CancelledError:
            self._write_runtime_state()
        except Exception as exc:  # noqa: BLE001
            tb_str = traceback.format_exc()
            _logger.critical(
                "[FATAL] 메인 루프 예외 종료: %s\n%s",
                exc,
                tb_str,
            )
            try:
                await self.notifier.send(
                    f"💥 [FATAL] 서비스 크래시\n"
                    f"오류: {type(exc).__name__}: {exc}\n"
                    f"스택(마지막 3줄):\n"
                    f"{''.join(tb_str.splitlines(keepends=True)[-3:])}"
                )
            except Exception:  # noqa: BLE001
                pass
            raise
        finally:
            if self.current_task is not None and not self.current_task.done():
                self.current_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.current_task
            scheduler.cancel()
            command_loop.cancel()
            stop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler
            with contextlib.suppress(asyncio.CancelledError):
                await command_loop
            with contextlib.suppress(asyncio.CancelledError):
                await stop_task
            signal.signal(signal.SIGTERM, previous_sigterm_handler)
            _release_pid_lock()

    async def _scheduler_loop(self) -> None:
        while True:
            await self._drain_finished_cycle()
            await self._maybe_auto_cancel_stale_domestic_orders()
            await self._maybe_auto_cancel_stale_overseas_orders()
            if self.mode == "running" and self.current_task is None:
                now = datetime.now(timezone.utc)
                if self.next_run_at is None or now >= self.next_run_at:
                    self.current_cycle_no += 1
                    self.current_task_started_at = now
                    self.current_task = asyncio.create_task(self._run_cycle(self.current_cycle_no))
                    self._write_runtime_state()
            await asyncio.sleep(1.0)

    def _log_api_call(self, info: dict) -> None:
        repository = getattr(self, "repository", None)
        if repository is None:
            return
        try:
            repository.save_api_call(
                created_at=datetime.now(timezone.utc).isoformat(),
                method=str(info.get("method", "")),
                tr_id=str(info.get("tr_id", "")),
                path=str(info.get("path", "")),
                success=bool(info.get("success", False)),
                http_status=info.get("http_status"),
                msg_cd=str(info.get("msg_cd", "")),
                msg1=str(info.get("msg1", ""))[:200],
                elapsed_ms=info.get("elapsed_ms"),
            )
        except Exception:  # noqa: BLE001
            pass

    def _log_inbound_command(self, text: str) -> None:
        repository = getattr(self, "repository", None)
        if repository is None:
            return
        try:
            repository.save_telegram_message(
                created_at=datetime.now(timezone.utc).isoformat(),
                direction="received",
                command=text.split()[0] if text.split() else text,
                text=text,
            )
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _build_menu_root_text() -> str:
        return "[KIS][메뉴]\n카테고리를 선택하세요"

    @staticmethod
    def _build_menu_root_keyboard() -> dict:
        buttons: list[list[dict[str, str]]] = []
        row: list[dict[str, str]] = []
        for key, label, _commands in MENU_CATEGORIES:
            row.append({"text": label, "callback_data": f"menu:cat:{key}"})
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        return {"inline_keyboard": buttons}

    @staticmethod
    def _build_menu_back_keyboard() -> dict:
        return {"inline_keyboard": [[{"text": "◀ 메뉴", "callback_data": "menu:root"}]]}

    @staticmethod
    def _build_menu_category_text(key: str) -> str | None:
        for cat_key, label, commands in MENU_CATEGORIES:
            if cat_key != key:
                continue
            lines = [f"[KIS][메뉴] {label}"]
            lines.extend(f"{command} - {desc}" for command, desc in commands)
            return "\n".join(lines)
        return None

    async def _handle_menu(self) -> None:
        await self.notifier.send(
            self._build_menu_root_text(),
            reply_markup=self._build_menu_root_keyboard(),
        )

    async def _handle_menu_callback(self, callback_query: dict) -> None:
        callback_id = str(callback_query.get("id") or "")
        message = callback_query.get("message") if isinstance(callback_query, dict) else None
        message = message if isinstance(message, dict) else {}
        chat = message.get("chat") if isinstance(message, dict) else {}
        chat = chat if isinstance(chat, dict) else {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")

        if not self.notifier.is_authorized_chat(chat_id):
            if callback_id:
                with contextlib.suppress(Exception):
                    await self.notifier.answer_callback_query(callback_id)
            return

        data = str(callback_query.get("data") or "")
        if isinstance(message_id, int):
            if data == "menu:root":
                with contextlib.suppress(Exception):
                    # Telegram returns 400 "message is not modified" for a
                    # double-tap on the same button (identical text/keyboard),
                    # which must not be treated as a real failure.
                    await self.notifier.edit_message(
                        message_id=message_id,
                        text=self._build_menu_root_text(),
                        reply_markup=self._build_menu_root_keyboard(),
                    )
            elif data.startswith("menu:cat:"):
                key = data.split(":", 2)[2]
                text = self._build_menu_category_text(key)
                if text is not None:
                    with contextlib.suppress(Exception):
                        await self.notifier.edit_message(
                            message_id=message_id,
                            text=text,
                            reply_markup=self._build_menu_back_keyboard(),
                        )

        if callback_id:
            with contextlib.suppress(Exception):
                await self.notifier.answer_callback_query(callback_id)

    async def _command_loop(self) -> None:
        while True:
            updates = await self.notifier.get_updates(offset=self.update_offset)
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    self.update_offset = update_id + 1
                try:
                    await self._handle_update(update)
                except Exception:  # noqa: BLE001
                    # A single malformed/edge-case Telegram update must never be
                    # able to crash the whole service (which also kills the
                    # scheduler loop and stops all trading/exit monitoring).
                    # Log it and keep processing subsequent updates; the offset
                    # below is still persisted so the same update isn't replayed
                    # forever on restart.
                    _logger.exception(
                        "[TELEGRAM] 업데이트 처리 중 예외 발생 (update_id=%s) - 다음 업데이트로 계속 진행",
                        update_id,
                    )
                self._write_runtime_state()
            await asyncio.sleep(0.2)

    async def _handle_update(self, update: dict) -> None:
        callback_query = update.get("callback_query") if isinstance(update, dict) else None
        if isinstance(callback_query, dict):
            await self._handle_menu_callback(callback_query)
            return

        message = update.get("message", {}) if isinstance(update, dict) else {}
        chat = message.get("chat", {}) if isinstance(message, dict) else {}
        chat_id = chat.get("id")
        text = str(message.get("text", "") or "").strip()
        if not text:
            return
        if not self.notifier.is_authorized_chat(chat_id):
            return
        self._log_inbound_command(text)

        parsed_command = self.parse_command(text)
        if parsed_command is None:
            return

        command_name = parsed_command if isinstance(parsed_command, str) else parsed_command[0]
        self.last_command = command_name
        self.last_command_at = datetime.now(timezone.utc)

        if command_name == "help":
            await self.notifier.send(HELP_MESSAGE)
            return
        if command_name == "menu":
            await self._handle_menu()
            return
        if command_name == "status":
            await self._send_status_message()
            return
        if command_name == "watchlist":
            await self.notifier.send(self._build_watchlist_message())
            return
        if command_name == "portfolio":
            await self._send_portfolio_message()
            return
        if command_name == "trim_virtual":
            await self._send_trim_virtual_prompt()
            return
        if command_name == "trim_virtual_confirm":
            await self._execute_trim_virtual()
            return
        if command_name == "log":
            await self._send_recent_trade_log()
            return
        if command_name == "performance":
            hours_text = parsed_command[1] if isinstance(parsed_command, tuple) else None
            await self._send_performance_message(hours_text)
            return
        if command_name == "report":
            report_args = parsed_command[1] if isinstance(parsed_command, tuple) else None
            await self._send_report_message(report_args)
            return
        if command_name == "guard":
            await self._send_guard_message()
            return
        if command_name == "orders":
            await self._send_recent_order_events()
            return
        if command_name == "paper_test":
            stock_code = parsed_command[1] if isinstance(parsed_command, tuple) else None
            await self._handle_paper_test(stock_code)
            return
        if command_name == "service_restart":
            await self._handle_service_restart()
            return
        if command_name == "reset_virtual":
            await self._send_reset_virtual_prompt()
            return
        if command_name == "reset_virtual_confirm":
            await self._execute_reset_virtual()
            return
        if command_name == "reset_all":
            await self._send_reset_all_prompt()
            return
        if command_name == "reset_all_confirm":
            await self._execute_reset_all()
            return
        if command_name == "relist":
            symbols_text = parsed_command[1] if isinstance(parsed_command, tuple) else None
            await self._handle_relist(symbols_text)
            return
        if command_name == "relist_schedule":
            await self._send_relist_schedule()
            return
        if command_name == "cb_reset":
            await self._handle_cb_reset()
            return
        if command_name == "gitlog":
            date_kst = parsed_command[1] if isinstance(parsed_command, tuple) else None
            await self._handle_gitlog(date_kst)
            return
        if command_name == "start":
            await self._handle_start_like_command("running", "started")
            return
        if command_name == "resume":
            await self._handle_start_like_command("running", "resumed")
            return
        if command_name == "pause":
            await self._handle_pause()
            return
        if command_name == "stop":
            await self._handle_stop()
            return
        if command_name == "terminate":
            await self._handle_terminate()

    async def _handle_paper_test(self, stock_code: str | None) -> None:
        if not stock_code:
            await self.notifier.send(
                "\n".join(
                    [
                        "[KIS][PAPER_TEST]",
                        "실행실패=종목코드를 함께 보내주세요",
                        "예시=/lab_paper_test 005930",
                        "참고=메뉴에서 누르면 종목코드가 빠지니 직접 입력해주세요",
                    ]
                )
            )
            return

        code = stock_code.strip().upper()
        try:
            async with KisRestClient(self.config.credentials) as client:
                service = PaperTradingService(self.config, client, self.repository, self.notifier)
                state = await service.run(
                    iterations=self.config.liquidity_lab.domestic_paper_iterations,
                    interval_sec=self.config.liquidity_lab.domestic_paper_interval_sec,
                    watchlist_override=[code],
                )
        except Exception as exc:  # noqa: BLE001
            await self.notifier.send(
                "\n".join(
                    [
                        "[KIS][PAPER_TEST]",
                        f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                        f"종목={code}",
                        f"상태=실패",
                        f"사유={exc}",
                    ]
                )
            )
            return

        await self.notifier.send(
            "\n".join(
                [
                    "[KIS][PAPER_TEST]",
                    f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                    f"종목={code}",
                    "상태=완료",
                    f"반복={self.config.liquidity_lab.domestic_paper_iterations}회",
                    f"실현손익={int(state.realized_pnl_krw):,}원",
                    f"현금={int(state.cash_krw):,}원",
                ]
            )
        )

    async def _handle_service_restart(self) -> None:
        if not self._service_restart_supported():
            await self.notifier.send(
                "\n".join(
                    [
                        "[KIS][SERVICE]",
                        f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                        f"서비스={SERVICE_UNIT_NAME}",
                        "상태=실패",
                        "사유=systemd 사용자 서비스가 확인되지 않음",
                    ]
                )
            )
            return

        await self.notifier.send(
            "\n".join(
                [
                    "[KIS][SERVICE]",
                    f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                    f"서비스={SERVICE_UNIT_NAME}",
                    "동작=재시작",
                    "상태=요청접수",
                ]
            )
        )
        self._write_runtime_state()
        asyncio.create_task(self._restart_service_soon())

    async def _send_reset_virtual_prompt(self) -> None:
        lines = [
            "⚠️ [가상거래 초기화]",
            "",
            *self._build_virtual_reset_summary_lines(),
            "",
            "삭제 대상:",
            "  • virtual_positions  (현재 가상 보유)",
            "  • virtual_orders     (체결 이력)",
            "  • virtual_sell_pending (정산 대기)",
            "",
            "cycle_log 는 보존됩니다.",
            "삭제 전 DB 파일이 자동 백업됩니다.",
            "",
            "진행: /lab_reset_confirm",
            "취소: 무시",
        ]
        await self.notifier.send("\n".join(lines))

    def _build_virtual_reset_summary_lines(self) -> list[str]:
        repository = getattr(self, "repository", None)
        if repository is None or not hasattr(repository, "list_virtual_positions"):
            return ["현재상태=조회불가"]
        try:
            positions = repository.list_virtual_positions()
        except Exception as exc:  # noqa: BLE001
            return [f"현재상태=조회실패 ({str(exc)[:80]})"]
        pending_sells: list[dict] = []
        if hasattr(repository, "list_virtual_sell_pending"):
            try:
                pending_sells = repository.list_virtual_sell_pending()
            except Exception:  # noqa: BLE001
                pending_sells = []

        by_market_currency = self._group_virtual_positions_by_market_currency(positions)

        if not by_market_currency and not pending_sells:
            return ["현재상태=가상보유/정산대기 없음"]

        max_overseas_positions = self._max_concurrent_overseas_positions()
        lines = ["현재상태:"]
        for (market, currency), item in sorted(by_market_currency.items()):
            count = int(item["count"])
            notional = float(item["notional"])
            parts = [
                f"  • {format_market_korean(market)} 가상보유={self._format_notional_price(notional, currency)}",
                f"{count}종목",
            ]
            if market == "overseas" and currency == "USD" and max_overseas_positions > 0:
                cap_status = "초과" if count > max_overseas_positions else "정상"
                parts.append(f"포지션한도={count}/{max_overseas_positions} {cap_status}")
            lines.append(" ".join(parts))
        if pending_sells:
            lines.append(f"  • 정산대기={len(pending_sells)}건")
        return lines

    def _select_virtual_trim_candidates(
        self,
        *,
        price_lookup: dict[tuple[str, str], float] | None = None,
        require_live_price: bool = False,
    ) -> tuple[list[dict[str, object]], int, int]:
        max_overseas_positions = self._max_concurrent_overseas_positions()
        if max_overseas_positions <= 0:
            return [], 0, max_overseas_positions

        rows = [
            row
            for row in self.repository.list_virtual_positions()
            if str(row.get("market", "")).strip().lower() == "overseas"
            and int(row.get("qty", 0) or 0) > 0
        ]
        total = len(rows)
        excess_count = total - max_overseas_positions
        if excess_count <= 0:
            return [], total, max_overseas_positions

        now = datetime.now(timezone.utc)
        price_lookup = price_lookup or {}
        candidates: list[dict[str, object]] = []
        for row in rows:
            symbol = str(row.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            market = str(row.get("market", "overseas")).strip().lower()
            qty = int(row.get("qty", 0) or 0)
            avg_price = float(row.get("avg_price", 0.0) or 0.0)
            if qty <= 0 or avg_price <= 0:
                continue
            live_price = float(price_lookup.get((market, symbol), 0.0) or 0.0)
            state = self.repository.get_lab_symbol_state(market, symbol)
            saved_price = 0.0
            saved_age_min: int | None = None
            if state is not None:
                saved_price = float(state.get("last_price") or 0.0)
                updated_at = parse_datetime(state.get("updated_at"))
                if updated_at is not None:
                    saved_age_min = int(
                        max((now - ensure_timezone(updated_at)).total_seconds(), 0.0) // 60
                    )
            price_source = "live" if live_price > 0 else "saved" if saved_price > 0 else "missing"
            current_price = live_price if live_price > 0 else saved_price
            price_missing = current_price <= 0
            if require_live_price and price_source != "live":
                continue
            if price_missing:
                current_price = avg_price
            pnl_pct = (current_price - avg_price) / avg_price
            opened_at = ensure_timezone(parse_datetime(row.get("opened_at")) or now)
            age_hours = max(0.0, (now - opened_at).total_seconds() / 3600)
            candidates.append(
                {
                    "market": market,
                    "symbol": symbol,
                    "exchange_code": row.get("exchange_code"),
                    "qty": qty,
                    "avg_price": avg_price,
                    "current_price": current_price,
                    "currency": str(row.get("currency", "USD")),
                    "pnl_pct": pnl_pct,
                    "age_hours": age_hours,
                    "price_missing": price_missing,
                    "price_source": price_source,
                    "saved_price_age_min": saved_age_min,
                }
            )

        candidates.sort(
            key=lambda item: (
                float(item["pnl_pct"]),
                -float(item["age_hours"]),
                -(float(item["avg_price"]) * int(item["qty"])),
            )
        )
        return candidates[:excess_count], total, max_overseas_positions

    def _format_virtual_trim_candidate_line(self, item: dict[str, object]) -> str:
        symbol = str(item["symbol"])
        qty = int(item["qty"])
        avg_price = float(item["avg_price"])
        current_price = float(item["current_price"])
        pnl_pct = float(item["pnl_pct"])
        currency = str(item["currency"])
        price_note = ""
        if bool(item.get("price_missing")):
            price_note = " 현재가없음"
        elif str(item.get("price_source") or "") == "saved":
            age_min = item.get("saved_price_age_min")
            if isinstance(age_min, int):
                price_note = f" 저장가={self._format_saved_price_age(age_min)}"
            else:
                price_note = " 저장가"
        return (
            f"해외 {symbol} 수량={qty} "
            f"매입={self._format_price(avg_price, currency)} "
            f"정리가={self._format_price(current_price, currency)} "
            f"손익={format_pct(pnl_pct)}{price_note}"
        )

    @staticmethod
    def _format_saved_price_age(age_min: int) -> str:
        if age_min <= 0:
            return "방금"
        if age_min < 60:
            return f"{age_min}분전"
        hours = age_min // 60
        if hours < 48:
            return f"{hours}시간전"
        return f"{hours // 24}일전"

    async def _load_trim_virtual_price_lookup(self) -> dict[tuple[str, str], float]:
        try:
            async with KisRestClient(self.config.credentials) as client:
                lab = self._build_portfolio_lab_service(client)
                return await self._load_live_virtual_price_lookup(lab)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("trim_virtual_live_price_lookup_failed error=%s", exc)
            return {}

    async def _send_trim_virtual_prompt(self) -> None:
        live_prices = await self._load_trim_virtual_price_lookup()
        saved_candidates, total, max_positions = self._select_virtual_trim_candidates(
            price_lookup=live_prices,
        )
        candidates, _, _ = self._select_virtual_trim_candidates(
            price_lookup=live_prices,
            require_live_price=True,
        )
        if not saved_candidates:
            await self.notifier.send(
                "\n".join(
                    [
                        "[KIS][가상보유 정리]",
                        f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                        f"상태=정리불필요 ({total}/{max_positions})",
                    ]
                )
            )
            return
        if not candidates:
            lines = [
                "⚠️ [가상보유 초과분 정리 보류]",
                f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                f"포지션={total}/{max_positions} 초과={len(saved_candidates)}종목",
                f"가격소스=live {len(live_prices)}건",
                "사유=live 현재가 확보 실패",
                "",
                "저장가 기준 후보:",
            ]
            lines.extend(self._format_virtual_trim_candidate_line(item) for item in saved_candidates[:5])
            lines.extend(
                [
                    "",
                    "조치=/lab_start 후 재조회 또는 잠시 뒤 /lab_trim_virtual 재시도",
                ]
            )
            await self.notifier.send("\n".join(lines))
            return

        lines = [
            "⚠️ [가상보유 초과분 정리]",
            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
            f"포지션={total}/{max_positions} 정리가능={len(candidates)}/{len(saved_candidates)}종목",
            "방식=성과 제외 가상매도 기록 후 초과분 삭제",
            f"가격소스=live {len(live_prices)}건",
            "",
            "정리 후보:",
        ]
        lines.extend(self._format_virtual_trim_candidate_line(item) for item in candidates[:5])
        lines.extend(
            [
                "",
                "진행: /lab_trim_virtual_confirm",
                "취소: 무시",
            ]
        )
        await self.notifier.send("\n".join(lines))

    async def _execute_trim_virtual(self) -> None:
        now = datetime.now(timezone.utc)
        live_prices = await self._load_trim_virtual_price_lookup()
        candidates, total, max_positions = self._select_virtual_trim_candidates(
            price_lookup=live_prices,
            require_live_price=True,
        )
        if not candidates:
            await self.notifier.send(
                "\n".join(
                    [
                        "[KIS][가상보유 정리]",
                        f"시각={format_kst_korean(now)}",
                        f"상태=정리보류 ({total}/{max_positions})",
                        f"사유=live 현재가 확보 실패 또는 정리불필요 (live {len(live_prices)}건)",
                    ]
                )
            )
            return

        try:
            backup_path = self.repository.backup_db(suffix="pre_trim_virtual")
            trimmed: list[dict[str, object]] = []
            excluded_at = now.isoformat()
            created_at = format_kst(now) or excluded_at
            for item in candidates:
                market = str(item["market"])
                symbol = str(item["symbol"])
                exchange_code = item.get("exchange_code")
                qty = int(item["qty"])
                avg_price = float(item["avg_price"])
                fill_price = float(item["current_price"])
                currency = str(item["currency"])
                realized_pnl = (fill_price - avg_price) * qty
                realized_pnl_pct = (fill_price - avg_price) / avg_price if avg_price > 0 else 0.0
                self.repository.save_virtual_order(
                    created_at=created_at,
                    market=market,
                    symbol=symbol,
                    exchange_code=str(exchange_code) if exchange_code else None,
                    side="sell",
                    qty=qty,
                    fill_price=fill_price,
                    currency=currency,
                    session="manual",
                    reason="manual_virtual_trim",
                    realized_pnl=realized_pnl,
                    realized_pnl_pct=realized_pnl_pct,
                    excluded_from_performance=True,
                    exclude_reason="manual_virtual_trim",
                    excluded_at=excluded_at,
                )
                self.repository.delete_virtual_position(market, symbol)
                self.repository.delete_virtual_sell_pending(market, symbol)
                self.repository.upsert_lab_symbol_state(
                    market=market,
                    symbol=symbol,
                    exchange_code=str(exchange_code) if exchange_code else None,
                    action_bias="VIRTUAL_TRIM",
                    signal_state="CLOSED",
                    note="manual_virtual_trim",
                    holding_qty=0,
                    last_price=fill_price,
                    pnl_pct=realized_pnl_pct,
                    has_position=0,
                    updated_at=excluded_at,
                )
                trimmed.append(item)

            if self.lab_service is not None:
                symbols = {str(item["symbol"]) for item in trimmed}
                for attr in ("_exit_cooldown", "_wait_cycles", "_strategy_managers"):
                    mapping = getattr(self.lab_service, attr, None)
                    if mapping is None:
                        continue
                    for symbol in symbols:
                        mapping.pop(symbol, None)
                        mapping.pop(f"overseas:{symbol}", None)
                session_owned = getattr(self.lab_service, "_session_owned_symbols", None)
                if session_owned is not None:
                    session_owned.difference_update(symbols)

            lines = [
                "✅ [가상보유 정리 완료]",
                f"시각={format_kst_korean(now)}",
                f"백업={backup_path.name}",
                f"정리={len(trimmed)}종목",
                "성과반영=제외(manual_virtual_trim)",
            ]
            lines.extend(self._format_virtual_trim_candidate_line(item) for item in trimmed[:5])
            await self.notifier.send("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            await self.notifier.send(f"❌ [가상보유 정리 실패]\n오류={exc}")

    async def _execute_reset_virtual(self) -> None:
        now = datetime.now(timezone.utc)
        try:
            backup_path = self.repository.backup_db(suffix="pre_reset")
            deleted = self.repository.reset_virtual_trades()
            if self.lab_service is not None:
                exit_cooldown = getattr(self.lab_service, "_exit_cooldown", None)
                if exit_cooldown is not None:
                    exit_cooldown.clear()
                wait_cycles = getattr(self.lab_service, "_wait_cycles", None)
                if wait_cycles is not None:
                    wait_cycles.clear()
                strategy_managers = getattr(self.lab_service, "_strategy_managers", None)
                if strategy_managers is not None:
                    strategy_managers.clear()
                session_owned = getattr(self.lab_service, "_session_owned_symbols", None)
                if session_owned is not None:
                    session_owned.clear()
            lines = [
                "✅ [가상거래 초기화 완료]",
                f"시각={format_kst_korean(now)}",
                f"백업={backup_path.name}",
                f"삭제된 가상포지션={deleted.get('virtual_positions', 0)}건",
                f"삭제된 가상주문={deleted.get('virtual_orders', 0)}건",
                f"삭제된 정산대기={deleted.get('virtual_sell_pending', 0)}건",
                "",
                "이후 사이클부터 새 전략으로 집계됩니다.",
            ]
            await self.notifier.send("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            await self.notifier.send(f"❌ [가상거래 초기화 실패]\n오류={exc}")

    def _build_reset_all_summary_lines(self) -> list[str]:
        repository = getattr(self, "repository", None)
        if repository is None:
            return ["현재상태=조회불가"]
        try:
            counts = {
                table: repository.count_rows(table)
                for table in (
                    "cycle_log",
                    "event_log",
                    "broker_order_events",
                    "virtual_positions",
                    "lab_symbol_state",
                )
            }
        except Exception as exc:  # noqa: BLE001
            return [f"현재상태=조회실패 ({str(exc)[:80]})"]
        return [
            "현재상태:",
            f"  • 매매판단기록(cycle_log)={counts['cycle_log']:,}건",
            f"  • 시스템이벤트(event_log)={counts['event_log']:,}건",
            f"  • 실주문이력(broker_order_events)={counts['broker_order_events']:,}건",
            f"  • 가상보유(virtual_positions)={counts['virtual_positions']:,}건",
            f"  • 감시종목캐시(lab_symbol_state)={counts['lab_symbol_state']:,}건",
        ]

    async def _send_reset_all_prompt(self) -> None:
        lines = [
            "🛑 [전체 거래이력·성과 초기화]",
            "",
            *self._build_reset_all_summary_lines(),
            "",
            "삭제 대상:",
            "  • cycle_log (매매판단/실현손익 기록)",
            "  • event_log (시스템 이벤트, CB 발동 등)",
            "  • broker_order_events (실주문 접수·취소 이력)",
            "  • virtual_positions / virtual_orders / virtual_sell_pending (가상보유)",
            "  • lab_symbol_state (감시종목 캐시)",
            "",
            "함께 초기화: 연속손절 카운터, 세션 실현손익, 서킷브레이커, 주문거부 차단",
            "보존: telegram_message_log / api_call_log (감사용 운영 로그)",
            "",
            "삭제 전 DB 파일이 자동 백업됩니다.",
            "초기화 후 다음 사이클부터는 실제 계좌 보유 종목만 기준으로 새로 시작합니다",
            "(별도 입력 없이 실계좌/모의계좌 잔고를 그대로 반영).",
            "",
            "진행: /lab_reset_all_confirm",
            "취소: 무시",
        ]
        await self.notifier.send("\n".join(lines))

    async def _execute_reset_all(self) -> None:
        now = datetime.now(timezone.utc)
        try:
            backup_path = self.repository.backup_db(suffix="pre_reset_all")
            deleted = self.repository.reset_all_history()
            if self.lab_service is not None:
                lab_service = self.lab_service
                setattr(lab_service, "_consecutive_losses", 0)
                setattr(lab_service, "_session_realised_krw", 0.0)
                if hasattr(lab_service, "_session_realised_krw_overseas"):
                    setattr(lab_service, "_session_realised_krw_overseas", 0.0)
                setattr(lab_service, "_daily_loss_date", None)
                setattr(lab_service, "_halted_at", None)
                setattr(lab_service, "_daily_halted_at", None)
                reject_cb = getattr(lab_service, "cb", None)
                if reject_cb is not None:
                    reject_cb.reset_order_rejections()
                    reject_cb.reset()
                for attr in (
                    "_exit_cooldown",
                    "_wait_cycles",
                    "_strategy_managers",
                    "_no_orderable_retry",
                    "_no_orderable_counts",
                    "_signal_cache",
                    "_signal_cache_updated_at",
                    "_overseas_signal_failures",
                    "_overseas_signal_suppressed_until",
                    "_repeated_skip_notify_last",
                    "_exit_price_shock_guard",
                    "_stop_loss_confirm_guard",
                    "_last_held_symbols",
                ):
                    mapping = getattr(lab_service, attr, None)
                    if mapping is not None:
                        mapping.clear()
                session_owned = getattr(lab_service, "_session_owned_symbols", None)
                if session_owned is not None:
                    session_owned.clear()
            lines = [
                "✅ [전체 거래이력·성과 초기화 완료]",
                f"시각={format_kst_korean(now)}",
                f"백업={backup_path.name}",
                f"삭제된 매매판단기록={deleted.get('cycle_log', 0):,}건",
                f"삭제된 시스템이벤트={deleted.get('event_log', 0):,}건",
                f"삭제된 실주문이력={deleted.get('broker_order_events', 0):,}건",
                f"삭제된 가상포지션={deleted.get('virtual_positions', 0):,}건",
                f"삭제된 감시종목캐시={deleted.get('lab_symbol_state', 0):,}건",
                "",
                "성과/서킷브레이커 카운터 초기화 완료.",
                "다음 사이클부터 실제 계좌 보유 종목만 기준으로 새로 집계됩니다.",
            ]
            await self.notifier.send("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            await self.notifier.send(f"❌ [전체 초기화 실패]\n오류={exc}")

    async def _handle_relist(self, symbols_text: str | None) -> None:
        raw_text = str(symbols_text or "").replace(",", " ")
        symbols = [token.strip().upper() for token in raw_text.split() if token.strip()]
        if not symbols:
            await self.notifier.send(
                "사용법: /lab_relist PLTR NVDA AMD TSLA"
            )
            return

        pool: list[dict[str, str]] = []
        for token in symbols:
            if ":" in token:
                symbol, exchange_code = token.split(":", 1)
                pool.append(
                    {
                        "symbol": symbol.strip().upper(),
                        "exchange_code": exchange_code.strip().upper(),
                    }
                )
            else:
                pool.append(
                    {
                        "symbol": token.upper(),
                        "exchange_code": "NASD",
                    }
                )
        self.manual_overseas_pool = pool
        if self.lab_service is not None:
            self.lab_service._manual_overseas_pool = list(pool)
            self.lab_service._dynamic_overseas_pool = list(pool)
            self.lab_service._awaiting_relist = False
            self.lab_service._overseas_scan_cycle_count = 0
            self.lab_service._signal_cache.clear()
            signal_updated_at = getattr(self.lab_service, "_signal_cache_updated_at", None)
            if signal_updated_at is not None:
                signal_updated_at.clear()
        await self.notifier.send(
            "\n".join(
                [
                    f"🔄 [해외 relist 완료] {len(pool)}종목",
                    f"목록={', '.join(item['symbol'] for item in pool)}",
                    "※ NYSE 종목은 SYMBOL:NYSE 형식으로 지정 가능",
                    "예: /lab_relist NVDA TSLA GM:NYSE BA:NYSE",
                ]
            )
        )

    async def _send_relist_schedule(self) -> None:
        schedule_text = getattr(
            self.config.liquidity_lab,
            "overseas_relist_schedule_kst",
            "22:35,01:00,03:30",
        )
        current_pool = (
            self.manual_overseas_pool
            if self.manual_overseas_pool is not None
            else (
                getattr(self.lab_service, "_dynamic_overseas_pool", None)
                if self.lab_service is not None
                else None
            )
            or []
        )
        await self.notifier.send(
            "\n".join(
                [
                    "[KIS][RELIST_SCHEDULE]",
                    f"시간(KST)={schedule_text}",
                    f"현재풀={len(current_pool)}종목",
                    "수동교체=/lab_relist PLTR NVDA AMD ...",
                ]
            )
        )

    async def _handle_gitlog(self, date_kst: str | None) -> None:
        await self.notifier.send("📤 GitHub 로그 업로드 중...")
        try:
            async with KisRestClient(self.config.credentials) as client:
                success, result = await upload_log(
                    client=client._client,
                    db_path=self.repository.db_path,
                    github_token=self.config.github_token,
                    github_repo=self.config.github_repo,
                    date_kst=date_kst,
                )
        except Exception as exc:  # noqa: BLE001
            await self.notifier.send(f"❌ 업로드 실패\n`{type(exc).__name__}: {exc}`")
            return
        if success:
            uploaded = result if isinstance(result, dict) else {}
            lines = ["✅ 업로드 완료"]
            for key in ("trades", "events", "orders", "telegram", "api_calls"):
                info = uploaded.get(key)
                if not isinstance(info, dict):
                    continue
                path = str(info.get("path") or "-")
                filename = path.rsplit("/", 1)[-1]
                rows = info.get("rows", 0)
                url = str(info.get("url") or "-")
                lines.extend(
                    [
                        f"{key}={filename} ({rows}건)",
                        f"URL={url}",
                    ]
                )
            await self.notifier.send(
                "\n".join(lines)
            )
        else:
            await self.notifier.send(f"❌ 업로드 실패\n{result}")

    async def _restart_service_soon(self) -> None:
        await asyncio.sleep(0.5)
        subprocess.Popen(
            ["systemctl", "--user", "restart", SERVICE_UNIT_NAME],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    @staticmethod
    def _service_restart_supported() -> bool:
        result = subprocess.run(
            ["systemctl", "--user", "status", SERVICE_UNIT_NAME, "--no-pager"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0

    async def _handle_start_like_command(self, target_mode: str, verb: str) -> None:
        if self.mode == "stopped":
            self.session_performance = SessionPerformance(started_at=datetime.now(timezone.utc))
            self.active_session_id = uuid.uuid4().hex[:12]
        elif not self.active_session_id:
            self.active_session_id = uuid.uuid4().hex[:12]
        self.mode = target_mode
        if verb == "resumed" and self.lab_service is not None:
            setattr(self.lab_service, "_consecutive_losses", 0)
            setattr(self.lab_service, "_halted_at", None)
        self.next_run_at = datetime.now(timezone.utc)
        self.last_error = None
        self._consecutive_errors = 0
        self._write_runtime_state()
        lines = [
            "[KIS][TELEGRAM_CONTROL]",
            f"mode={self.mode}",
            f"command={verb}",
            "next_run=immediate",
        ]
        lines.extend(self._build_start_resume_virtual_position_notice_lines())
        lines.extend(await self._build_start_resume_open_order_notice_lines())
        await self.notifier.send("\n".join(lines))

    def _build_start_resume_virtual_position_notice_lines(self) -> list[str]:
        repository = getattr(self, "repository", None)
        config = getattr(getattr(self, "config", None), "liquidity_lab", None)
        if repository is None or config is None or not hasattr(repository, "list_virtual_positions"):
            return []
        max_overseas_positions = int(
            getattr(config, "max_concurrent_overseas_orders", 0) or 0
        )
        if max_overseas_positions <= 0:
            return []
        try:
            overseas_positions = [
                row
                for row in repository.list_virtual_positions()
                if str(row.get("market", "")).strip().lower() == "overseas"
                and int(row.get("qty", 0) or 0) > 0
            ]
        except Exception as exc:  # noqa: BLE001
            _logger.warning("start_virtual_position_notice_failed error=%s", exc)
            return []
        count = len(overseas_positions)
        if count <= max_overseas_positions:
            return []
        return [
            f"가상포지션=해외 {count}/{max_overseas_positions} 초과",
            "신규해외매수=한도 해소 전 제한",
            "정리=/lab_trim_virtual",
        ]

    async def _build_start_resume_open_order_notice_lines(self) -> list[str]:
        """Warn once on start/resume if old live orders may affect fresh orders."""
        config = getattr(self, "config", None)
        if config is None or getattr(config, "credentials", None) is None:
            return []
        try:
            domestic_orders, overseas_orders = await asyncio.wait_for(
                asyncio.gather(
                    self._load_live_open_domestic_orders(limit=20),
                    self._load_live_open_overseas_orders(limit=20),
                ),
                timeout=6.0,
            )
        except Exception as exc:  # noqa: BLE001
            return [
                f"미체결조회=실패 ({str(exc)[:80]})",
                "확인=/lab_orders",
            ]

        domestic_count = len(domestic_orders)
        overseas_count = len(overseas_orders)
        if not domestic_count and not overseas_count:
            return []

        lines = [
            f"미체결=국내 {domestic_count} / 해외 {overseas_count}",
            "주의=기존 미체결은 매도가능수량/중복주문에 영향을 줄 수 있음",
            "확인=/lab_orders",
            "30분 이상 미체결은 자동으로 취소됩니다",
        ]
        return lines

    async def _handle_cb_reset(self) -> None:
        if self.lab_service is None:
            await self.notifier.send("⚠️ lab 인스턴스에 접근할 수 없습니다.")
            return
        previous = int(getattr(self.lab_service, "_consecutive_losses", 0) or 0)
        setattr(self.lab_service, "_consecutive_losses", 0)
        setattr(self.lab_service, "_halted_at", None)
        reject_cb = getattr(self.lab_service, "cb", None)
        if reject_cb is not None:
            reject_cb.reset_order_rejections()
        await self.notifier.send(
            f"✅ 서킷브레이커 수동 해제\n"
            f"연속손절 카운터: {previous} → 0\n"
            f"주문거부 서킷브레이커도 함께 초기화\n"
            f"다음 사이클부터 매수 재개"
        )

    async def _handle_pause(self) -> None:
        self.mode = "paused"
        self._write_runtime_state()
        await self.notifier.send(
            "[KIS][TELEGRAM_CONTROL]\nmode=paused\ncommand=pause\nnote=current_cycle_finishes_then_pause"
        )

    async def _handle_stop(self) -> None:
        self.mode = "stopped"
        self.next_run_at = None
        if self.current_task is not None and not self.current_task.done():
            self.current_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.current_task
            self.last_error = None
        if self.lab_service is not None:
            with contextlib.suppress(Exception):
                await self.lab_service.flush_pending_trade_notifications(force=True)
        self.current_task = None
        self.current_task_started_at = None
        self.next_run_at = None
        summaries = self._finalize_session_summary(command="stop")
        self._write_runtime_state()
        for summary in summaries:
            await self.notifier.send(summary)

    async def _handle_terminate(self) -> None:
        self.mode = "stopped"
        self.next_run_at = None
        if self.current_task is not None and not self.current_task.done():
            self.current_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.current_task
            self.last_error = None
        if self.lab_service is not None:
            with contextlib.suppress(Exception):
                await self.lab_service.flush_pending_trade_notifications(force=True)
        self.current_task = None
        self.current_task_started_at = None
        self.next_run_at = None
        summaries = self._finalize_session_summary(command="terminate")
        self._write_runtime_state()
        for summary in summaries:
            await self.notifier.send(summary)

    async def _run_cycle(self, cycle_no: int) -> None:
        """
        Execute a single liquidity-lab cycle without auto-stopping on market close.
        """
        try:
            async with KisRestClient(
                self.config.credentials, on_api_call=self._log_api_call
            ) as client:
                service = self.lab_service
                if service is None:
                    service = LiquidityLabService(self.config, client, self.repository, self.notifier)
                    self._apply_restored_lab_runtime_state(service)
                    self.lab_service = service
                else:
                    service.config = self.config
                    service.client = client
                    service.repository = self.repository
                    service.notifier = self.notifier
                if self.manual_overseas_pool is not None:
                    service._manual_overseas_pool = list(self.manual_overseas_pool)
                    service._dynamic_overseas_pool = list(self.manual_overseas_pool)
                self.lab_service = service
                if self.active_session_id:
                    service._session_id = self.active_session_id
                report = await service.run()
            self.last_report_summary = self._summarize_report(report, cycle_no)
            self._accumulate_session_performance(report)
            self.last_completed_at = datetime.now(timezone.utc)
            self.last_error = None
            self._consecutive_errors = 0

            now_for_state = datetime.now(timezone.utc)
            krx_open = is_krx_regular_session(now_for_state)
            us_open = is_us_orderable_session_for_env(now_for_state, self.config.credentials.env)
            if krx_open:
                current_market_state = "krx_open"
            elif us_open:
                current_market_state = f"us_{get_us_trading_session(now_for_state)}"
            else:
                current_market_state = "both_closed"

            if self._last_market_state and self._last_market_state != current_market_state:
                await self.notifier.send(
                    f"[KIS][MARKET_STATE_CHANGE]\n"
                    f"from={self._last_market_state}\n"
                    f"to={current_market_state}\n"
                    f"time={format_kst(now_for_state)}"
                )
            self._last_market_state = current_market_state
        except asyncio.CancelledError:
            self.last_error = None
            raise
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            self._consecutive_errors += 1
            await self.notifier.send(
                f"[KIS][TELEGRAM_CONTROL_ERROR]\ncycle={cycle_no}\nerror={exc}"
            )
            if self._consecutive_errors == 5:
                await self.notifier.send(
                    "[KIS][TELEGRAM_CONTROL_WARNING]\n"
                    f"consecutive_errors={self._consecutive_errors}\n"
                    "루프는 계속 실행 중. 수동 확인 권장."
                )
        finally:
            now = datetime.now(timezone.utc)
            interval_sec = determine_loop_interval_sec(
                now_utc=now,
                env=self.config.credentials.env,
                consecutive_errors=self._consecutive_errors,
            )
            base_time = self.current_task_started_at or now
            scheduled = base_time + timedelta(seconds=interval_sec)
            self.next_run_at = scheduled if scheduled > now else now
            self._write_runtime_state()

    async def _drain_finished_cycle(self) -> None:
        if self.current_task is None:
            return
        if not self.current_task.done():
            return

        try:
            await self.current_task
        except asyncio.CancelledError:
            self.last_error = None
        self.current_task = None
        self.current_task_started_at = None
        self._write_runtime_state()

    def _lab_runtime_state_payload(self) -> dict:
        service = self.lab_service
        if service is None:
            return self._normalise_lab_runtime_state(self._restored_lab_runtime_state)

        now = datetime.now(timezone.utc)
        exit_cooldown = self._future_datetime_map(
            getattr(service, "_exit_cooldown", None),
            now=now,
        )
        no_orderable_retry = self._future_datetime_map(
            getattr(service, "_no_orderable_retry", None),
            now=now,
        )
        active_retry_keys = set(no_orderable_retry)
        raw_counts = getattr(service, "_no_orderable_counts", None) or {}
        no_orderable_counts = self._normalise_count_map(raw_counts, active_retry_keys)
        payload: dict[str, object] = {}
        if exit_cooldown:
            payload["exit_cooldown"] = exit_cooldown
        if no_orderable_retry:
            payload["no_orderable_retry"] = no_orderable_retry
        if no_orderable_counts:
            payload["no_orderable_counts"] = no_orderable_counts
        return payload

    def _apply_restored_lab_runtime_state(self, service: LiquidityLabService) -> None:
        state = self._normalise_lab_runtime_state(self._restored_lab_runtime_state)
        if not state:
            return
        exit_cooldown = self._parse_datetime_state_map(state.get("exit_cooldown"))
        no_orderable_retry = self._parse_datetime_state_map(state.get("no_orderable_retry"))
        if exit_cooldown:
            service._exit_cooldown.update(exit_cooldown)
        if no_orderable_retry:
            service._no_orderable_retry.update(no_orderable_retry)
        counts = self._normalise_count_map(
            state.get("no_orderable_counts"),
            set(no_orderable_retry),
        )
        if counts:
            existing = getattr(service, "_no_orderable_counts", None)
            if existing is None:
                existing = {}
                service._no_orderable_counts = existing
            existing.update(counts)
        self._restored_lab_runtime_state = {}

    @classmethod
    def _normalise_lab_runtime_state(cls, state: object) -> dict:
        if not isinstance(state, dict):
            return {}
        now = datetime.now(timezone.utc)
        exit_cooldown = cls._future_datetime_map(state.get("exit_cooldown"), now=now)
        no_orderable_retry = cls._future_datetime_map(
            state.get("no_orderable_retry"),
            now=now,
        )
        active_retry_keys = set(no_orderable_retry)
        raw_counts = state.get("no_orderable_counts") if isinstance(state, dict) else {}
        no_orderable_counts = cls._normalise_count_map(raw_counts, active_retry_keys)
        result: dict[str, object] = {}
        if exit_cooldown:
            result["exit_cooldown"] = exit_cooldown
        if no_orderable_retry:
            result["no_orderable_retry"] = no_orderable_retry
        if no_orderable_counts:
            result["no_orderable_counts"] = no_orderable_counts
        return result

    @staticmethod
    def _future_datetime_map(raw: object, *, now: datetime) -> dict[str, str]:
        if not isinstance(raw, dict):
            return {}
        result: dict[str, str] = {}
        for key, value in raw.items():
            parsed = value if isinstance(value, datetime) else parse_datetime(str(value or ""))
            if parsed is None:
                continue
            parsed = ensure_timezone(parsed)
            if parsed <= now:
                continue
            result[str(key)] = parsed.isoformat()
        return result

    @staticmethod
    def _parse_datetime_state_map(raw: object) -> dict[str, datetime]:
        if not isinstance(raw, dict):
            return {}
        now = datetime.now(timezone.utc)
        result: dict[str, datetime] = {}
        for key, value in raw.items():
            parsed = parse_datetime(str(value or ""))
            if parsed is None:
                continue
            parsed = ensure_timezone(parsed)
            if parsed <= now:
                continue
            result[str(key)] = parsed
        return result

    @staticmethod
    def _normalise_count_map(raw: object, active_keys: set[str]) -> dict[str, int]:
        if not isinstance(raw, dict):
            return {}
        result: dict[str, int] = {}
        for key, value in raw.items():
            key_text = str(key)
            if key_text not in active_keys:
                continue
            try:
                count = int(value or 0)
            except (TypeError, ValueError):
                continue
            if count > 0:
                result[key_text] = count
        return result

    def _write_runtime_state(self) -> None:
        path = self.config.storage.runtime_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "status": self.mode,
            "updated_at": format_kst(datetime.now(timezone.utc)),
            "linked_account": self.config.credentials.profile_name,
            "telegram_update_offset": self.update_offset,
            "telegram_control_start_notified_at": format_kst(
                self._last_startup_notification_at
            ),
            "watch_targets": (self.last_report_summary or {}).get("watch_targets", []),
            "last_error": self.last_error,
            "lab_runtime_state": self._lab_runtime_state_payload(),
            "notes": [
                "telegram-control daemon manages liquidity-lab loop state.",
                "Use Telegram commands to start, pause, resume, stop, or terminate.",
            ],
            "telegram_control": self._snapshot().to_dict(),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _restore_runtime_state(self) -> None:
        path = self.config.storage.runtime_state_path
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        stored_offset = payload.get("telegram_update_offset")
        if isinstance(stored_offset, int) and stored_offset >= 0:
            self.update_offset = stored_offset
        self._last_startup_notification_at = parse_datetime(
            str(payload.get("telegram_control_start_notified_at") or "")
        )
        snapshot = payload.get("telegram_control") or {}
        lab_runtime_state = payload.get("lab_runtime_state")
        if not isinstance(lab_runtime_state, dict):
            lab_runtime_state = snapshot.get("lab_runtime_state")
        self._restored_lab_runtime_state = (
            self._normalise_lab_runtime_state(lab_runtime_state)
            if isinstance(lab_runtime_state, dict)
            else {}
        )
        mode = snapshot.get("mode")
        if mode in {"running", "paused", "stopped"}:
            self.mode = mode
        self.current_cycle_no = int(snapshot.get("current_cycle_no", 0) or 0)
        self.active_session_id = str(snapshot.get("active_session_id") or "").strip()
        self.last_command = snapshot.get("last_command") or None
        self.last_command_at = parse_datetime(str(snapshot.get("last_command_at") or ""))
        self.last_completed_at = parse_datetime(str(snapshot.get("last_completed_at") or ""))
        self.next_run_at = parse_datetime(str(snapshot.get("next_run_at") or ""))
        self.last_report_summary = snapshot.get("last_report_summary") or None
        restored_error = str(
            snapshot.get("last_error")
            or payload.get("last_error")
            or ""
        ).strip()
        if restored_error.startswith("cycle_") and restored_error.endswith("_cancelled"):
            restored_error = ""
        self.last_error = restored_error or None

        session = snapshot.get("session_performance") or {}
        if isinstance(session, dict) and session:
            self.session_performance = SessionPerformance(
                started_at=parse_datetime(str(session.get("started_at") or "")),
                cycles_completed=int(session.get("cycles_completed", 0) or 0),
                domestic_paper_runs=int(session.get("domestic_paper_runs", 0) or 0),
                domestic_paper_realized_pnl_krw=int(
                    session.get("domestic_paper_realized_pnl_krw", 0) or 0
                ),
                estimated_overseas_realized_pnl_krw=int(
                    session.get("estimated_overseas_realized_pnl_krw", 0) or 0
                ),
                domestic_orders_submitted=int(
                    session.get("domestic_orders_submitted", 0) or 0
                ),
                overseas_orders_submitted=int(
                    session.get("overseas_orders_submitted", 0) or 0
                ),
                domestic_orders_failed=int(
                    session.get("domestic_orders_failed", 0) or 0
                ),
                overseas_orders_failed=int(
                    session.get("overseas_orders_failed", 0) or 0
                ),
                skip_reasons=dict(session.get("skip_reasons") or {}),
                primary_targets=dict(session.get("primary_targets") or {}),
                symbol_stats=dict(session.get("symbol_stats") or {}),
            )

    async def _send_startup_message_if_due(self) -> None:
        now = datetime.now(timezone.utc)
        last_notified = self._last_startup_notification_at
        if last_notified is not None:
            elapsed = now - ensure_timezone(last_notified)
            if elapsed < timedelta(minutes=_STARTUP_NOTIFICATION_THROTTLE_MINUTES):
                _logger.info(
                    "startup notification suppressed; last_sent=%s elapsed_sec=%.1f",
                    format_kst(last_notified),
                    elapsed.total_seconds(),
                )
                return

        await self.notifier.send(
            "\n".join(
                [
                    "[KIS][TELEGRAM_CONTROL_START]",
                    f"profile={self.config.credentials.profile_name}",
                    f"loop_interval_sec={self.config.liquidity_lab.loop_interval_sec}",
                    "controller_mode=persistent_service",
                    "use /lab_help for commands",
                ]
            )
        )
        self._last_startup_notification_at = now
        self._write_runtime_state()

    def _snapshot(self) -> ControllerSnapshot:
        return ControllerSnapshot(
            mode=self.mode,
            current_cycle_no=self.current_cycle_no,
            active_session_id=self.active_session_id,
            active_cycle_started_at=None
            if self.current_task_started_at is None
            else format_kst(self.current_task_started_at),
            next_run_at=None if self.next_run_at is None else format_kst(self.next_run_at),
            last_command=self.last_command,
            last_command_at=None if self.last_command_at is None else format_kst(self.last_command_at),
            last_completed_at=None
            if self.last_completed_at is None
            else format_kst(self.last_completed_at),
            last_report_summary=self.last_report_summary,
            session_performance=self.session_performance.to_dict(),
            last_error=self.last_error,
        )

    def _loop_mode_notice(self) -> str:
        return self._get_report_helper().loop_mode_notice()

    def _report_freshness_notice(self, now: datetime | None = None) -> str:
        return self._get_report_helper().report_freshness_notice(now)

    def _last_report_age_minutes(self, now: datetime | None = None) -> int | None:
        return self._get_report_helper().last_report_age_minutes(now)

    def _status_stale_threshold_min(self) -> int:
        return self._get_report_helper().status_stale_threshold_min()

    def _estimated_pnl_suffix(self, now: datetime | None = None) -> str:
        return self._get_report_helper().estimated_pnl_suffix(now)

    async def _send_status_message(self) -> None:
        await self._get_report_helper().send_status_message()

    def _build_status_message(
        self,
        *,
        domestic_open_count: int | None = None,
        overseas_open_count: int | None = None,
        open_order_error: str = "",
    ) -> str:
        return self._get_report_helper().build_status_message(
            domestic_open_count=domestic_open_count,
            overseas_open_count=overseas_open_count,
            open_order_error=open_order_error,
        )

    def _build_stopped_open_market_warning(
        self,
        *,
        krx_open: bool,
        us_watchable: bool,
        last_report: dict,
    ) -> str:
        return self._get_report_helper().build_stopped_open_market_warning(
            krx_open=krx_open,
            us_watchable=us_watchable,
            last_report=last_report,
        )

    def _build_virtual_exposure_status_line(self) -> str:
        return self._get_report_helper().build_virtual_exposure_status_line()

    def _build_signal_cache_status_line(self, last_report: dict) -> str:
        return self._get_report_helper().build_signal_cache_status_line(last_report)

    def _watch_target_count_text(self, last_report: dict) -> str:
        return self._get_report_helper().watch_target_count_text(last_report)

    @staticmethod
    def _format_recent_age_text(then: datetime | None, *, now: datetime | None = None) -> str:
        return ReportHelper.format_recent_age_text(then, now=now)

    def _build_recent_sell_block_status_line(self, *, lookback_hours: int = 12) -> str:
        return self._get_report_helper().build_recent_sell_block_status_line(lookback_hours=lookback_hours)

    def _build_watchlist_message(self) -> str:
        return self._get_report_helper().build_watchlist_message()

    def _watch_target_with_persisted_position(self, watch_target: dict) -> dict:
        return self._get_report_helper().watch_target_with_persisted_position(watch_target)

    def _is_closed_stale_watch_target(self, watch_target: dict) -> bool:
        return self._get_report_helper().is_closed_stale_watch_target(watch_target)

    def _build_positions_message(self) -> str:
        return self._get_report_helper().build_positions_message()

    def _build_portfolio_message(
        self,
        real_positions_override: list[dict] | None = None,
        price_lookup_override: dict[tuple[str, str], float] | None = None,
        virtual_exposure_available_usd: float | None = None,
    ) -> str:
        return self._get_report_helper().build_portfolio_message(
            real_positions_override,
            price_lookup_override,
            virtual_exposure_available_usd,
        )

    def _build_real_position_risk_lines(
        self,
        real_positions: list[dict],
        *,
        last_report: dict,
    ) -> list[str]:
        return self._get_report_helper().build_real_position_risk_lines(
            real_positions,
            last_report=last_report,
        )

    def _detect_holding_mismatch_lines(
        self,
        real_positions: list[dict],
        *,
        virtual_manager: VirtualTradeManager | None = None,
    ) -> list[str]:
        return self._get_report_helper().detect_holding_mismatch_lines(
            real_positions,
            virtual_manager=virtual_manager,
        )

    def _build_virtual_position_risk_lines(
        self,
        effective_positions: list[dict[str, object]],
        price_lookup: dict[tuple[str, str], float],
        *,
        last_report: dict,
    ) -> list[str]:
        return self._get_report_helper().build_virtual_position_risk_lines(
            effective_positions,
            price_lookup,
            last_report=last_report,
        )

    def _build_virtual_position_cleanup_lines(
        self,
        effective_positions: list[dict[str, object]],
        price_lookup: dict[tuple[str, str], float],
        *,
        last_report: dict,
    ) -> list[str]:
        return self._get_report_helper().build_virtual_position_cleanup_lines(
            effective_positions,
            price_lookup,
            last_report=last_report,
        )

    def _build_virtual_exposure_lines(
        self,
        *,
        available_usd_override: float | None = None,
    ) -> list[str]:
        return self._get_report_helper().build_virtual_exposure_lines(available_usd_override=available_usd_override)

    async def _send_portfolio_message(self) -> None:
        await self._get_report_helper().send_portfolio_message()

    def _build_portfolio_lab_service(self, client: KisRestClient) -> LiquidityLabService:
        return self._get_report_helper().build_portfolio_lab_service(client)

    async def _load_live_overseas_available_usd(
        self,
        lab: LiquidityLabService,
        *,
        real_positions: list[dict],
        price_lookup: dict[tuple[str, str], float],
    ) -> float | None:
        return await self._get_report_helper().load_live_overseas_available_usd(
            lab,
            real_positions=real_positions,
            price_lookup=price_lookup,
        )

    async def _load_live_virtual_price_lookup(
        self,
        lab: LiquidityLabService | None = None,
    ) -> dict[tuple[str, str], float]:
        return await self._get_report_helper().load_live_virtual_price_lookup(lab)

    async def _load_live_portfolio_positions(
        self,
        lab: LiquidityLabService | None = None,
    ) -> list[dict] | None:
        return await self._get_report_helper().load_live_portfolio_positions(lab)

    async def _send_recent_trade_log(self) -> None:
        await self._get_report_helper().send_recent_trade_log()

    async def _send_performance_message(self, hours_text: str | None = None) -> None:
        await self._get_report_helper().send_performance_message(hours_text)

    async def _send_report_message(self, report_args: str | None = None) -> None:
        await self._get_report_helper().send_report_message(report_args)

    async def _send_guard_message(self) -> None:
        await self._get_report_helper().send_guard_message()

    def _build_report_message(self, report_args: str | None = None) -> str:
        return self._get_report_helper().build_report_message(report_args)

    def _build_guard_message(self) -> str:
        return self._get_report_helper().build_guard_message()

    @staticmethod
    def _parse_performance_hours(hours_text: str | None) -> int:
        return ReportHelper.parse_performance_hours(hours_text)

    @staticmethod
    def _format_mixed_pnl(*, usd: float, krw: float) -> str:
        return ReportHelper.format_mixed_pnl(usd=usd, krw=krw)

    @staticmethod
    def _performance_row_score(row: dict) -> tuple[float, float]:
        return ReportHelper.performance_row_score(row)

    def _format_performance_row(self, row: dict) -> str:
        return self._get_report_helper().format_performance_row(row)

    def _build_performance_message(self, hours_text: str | None = None) -> str:
        return self._get_report_helper().build_performance_message(hours_text)

    async def _send_recent_order_events(self) -> None:
        live_open_domestic_orders: list[dict] | None = None
        live_open_domestic_error = ""
        live_open_orders: list[dict] | None = None
        live_open_error = ""
        try:
            live_open_domestic_orders = await self._load_live_open_domestic_orders()
        except Exception as exc:  # noqa: BLE001
            live_open_domestic_error = str(exc)
            _logger.warning("live_open_domestic_orders_failed error=%s", exc)
        try:
            live_open_orders = await self._load_live_open_overseas_orders()
        except Exception as exc:  # noqa: BLE001
            live_open_error = str(exc)
            _logger.warning("live_open_overseas_orders_failed error=%s", exc)
        await self.notifier.send(
            self._build_recent_order_events_message(
                live_open_domestic_orders=live_open_domestic_orders,
                live_open_domestic_error=live_open_domestic_error,
                live_open_orders=live_open_orders,
                live_open_error=live_open_error,
            )
        )

    async def _execute_cancel_stale_domestic_orders(
        self,
        *,
        source: str = "manual",
        candidate_orders: list[dict] | None = None,
        now: datetime | None = None,
    ) -> None:
        await self._get_order_admin_helper().execute_cancel_stale_domestic_orders(
            source=source,
            candidate_orders=candidate_orders,
            now=now,
        )

    async def _maybe_auto_cancel_stale_domestic_orders(
        self,
        *,
        now: datetime | None = None,
    ) -> bool:
        return await self._get_order_admin_helper().maybe_auto_cancel_stale_domestic_orders(now=now)

    def _filter_bot_submitted_domestic_orders(self, rows: list[dict]) -> list[dict]:
        return self._get_order_admin_helper().filter_bot_submitted_domestic_orders(rows)

    async def _maybe_auto_cancel_stale_overseas_orders(
        self,
        *,
        now: datetime | None = None,
    ) -> bool:
        return await self._get_order_admin_helper().maybe_auto_cancel_stale_overseas_orders(now=now)

    def _filter_bot_submitted_overseas_orders(self, rows: list[dict]) -> list[dict]:
        return self._get_order_admin_helper().filter_bot_submitted_overseas_orders(rows)

    async def _execute_cancel_stale_overseas_orders(
        self,
        *,
        source: str = "auto",
        candidate_orders: list[dict] | None = None,
    ) -> None:
        await self._get_order_admin_helper().execute_cancel_stale_overseas_orders(
            source=source,
            candidate_orders=candidate_orders,
        )

    def _build_recent_order_events_message(
        self,
        *,
        limit: int = 12,
        live_open_domestic_orders: list[dict] | None = None,
        live_open_domestic_error: str = "",
        live_open_orders: list[dict] | None = None,
        live_open_error: str = "",
    ) -> str:
        return self._get_order_admin_helper().build_recent_order_events_message(
            limit=limit,
            live_open_domestic_orders=live_open_domestic_orders,
            live_open_domestic_error=live_open_domestic_error,
            live_open_orders=live_open_orders,
            live_open_error=live_open_error,
        )

    @staticmethod
    def _live_open_order_keys(market: str, rows: list[dict]) -> set[tuple[str, str]]:
        return OrderAdminHelper.live_open_order_keys(market, rows)

    async def _load_live_open_domestic_orders(self, *, limit: int = 12) -> list[dict]:
        return await self._get_order_admin_helper().load_live_open_domestic_orders(limit=limit)

    def _parse_live_open_domestic_order_rows(self, rows: list[dict], *, limit: int = 12) -> list[dict]:
        return self._get_order_admin_helper().parse_live_open_domestic_order_rows(rows, limit=limit)

    @staticmethod
    def _parse_domestic_order_history_timestamp(row: dict) -> datetime | None:
        return OrderAdminHelper.parse_domestic_order_history_timestamp(row)

    def _format_live_open_domestic_order_line(
        self,
        row: dict,
        *,
        now: datetime | None = None,
    ) -> str:
        return self._get_order_admin_helper().format_live_open_domestic_order_line(row, now=now)

    async def _load_live_open_overseas_orders(self, *, limit: int = 12) -> list[dict]:
        return await self._get_order_admin_helper().load_live_open_overseas_orders(limit=limit)

    def _format_live_open_overseas_order_line(self, row: dict) -> str:
        return self._get_order_admin_helper().format_live_open_overseas_order_line(row)

    def _filter_stale_live_open_orders(
        self,
        rows: list[dict],
        *,
        stale_threshold_min: int = 30,
        now: datetime | None = None,
    ) -> list[dict]:
        return self._get_order_admin_helper().filter_stale_live_open_orders(
            rows,
            stale_threshold_min=stale_threshold_min,
            now=now,
        )

    @staticmethod
    def _domestic_order_side(row: dict) -> str:
        return OrderAdminHelper.domestic_order_side(row)

    @staticmethod
    def _overseas_order_side(row: dict) -> str:
        return OrderAdminHelper.overseas_order_side(row)

    @staticmethod
    def _format_open_order_age_parts(
        created_at: object,
        *,
        stale_threshold_min: int = 30,
        now: datetime | None = None,
    ) -> list[str]:
        return OrderAdminHelper.format_open_order_age_parts(
            created_at,
            stale_threshold_min=stale_threshold_min,
            now=now,
        )

    @staticmethod
    def _format_order_event_action(row: dict) -> str:
        return OrderAdminHelper.format_order_event_action(row)

    def _format_submitted_order_audit_line(
        self,
        row: dict,
        *,
        live_open_order_keys: set[tuple[str, str]] | None = None,
        live_checked_markets: set[str] | None = None,
    ) -> str:
        return self._get_order_admin_helper().format_submitted_order_audit_line(
            row,
            live_open_order_keys=live_open_order_keys,
            live_checked_markets=live_checked_markets,
        )

    def _build_session_pnl_message(
        self,
        *,
        started_at: str = "",
        session_id: str = "",
    ) -> str:
        is_prod = self.config.credentials.env == "prod"
        include_virtual = not is_prod
        summary = self.repository.get_session_pnl_summary(
            session_id=session_id,
            include_virtual=include_virtual,
            after_logged_at=started_at,
        )
        parsed_started = parse_datetime(started_at)
        period_label = (format_kst(parsed_started) or "")[:16] if parsed_started else "전체"
        lines = [
            "[KIS][손익요약]",
            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
            f"환경={'실거래' if is_prod else '모의투자'}",
            f"기간={period_label}~",
        ]

        real = summary.get("real", {})
        total_real_trades = sum(int(item.get("trade_count", 0) or 0) for item in real.values())
        total_real_wins = sum(int(item.get("win_count", 0) or 0) for item in real.values())
        total_pnl_krw = sum(float(item.get("total_pnl_krw") or 0.0) for item in real.values())
        total_pnl_usd = sum(float(item.get("total_pnl_usd") or 0.0) for item in real.values())
        if total_real_trades > 0:
            win_rate = (total_real_wins / total_real_trades) * 100.0
            lines.append("─── 실주문접수 기준 ───")
            lines.append(f"거래={total_real_trades}건 (승률 {win_rate:.0f}%)")
            if abs(total_pnl_usd) > 1e-9:
                usd_sign = "+" if total_pnl_usd >= 0 else ""
                lines.append(f"해외손익={usd_sign}${total_pnl_usd:,.2f}")
            krw_sign = "+" if total_pnl_krw >= 0 else ""
            lines.append(f"환산손익={krw_sign}{int(round(total_pnl_krw)):,}원")
            lines.append("주의=체결확정은 MTS/잔고 기준 확인")
            for market, stats in sorted(real.items()):
                trade_count = int(stats.get("trade_count", 0) or 0)
                win_count = int(stats.get("win_count", 0) or 0)
                market_win_rate = (win_count / trade_count * 100.0) if trade_count else 0.0
                lines.append(f"{market}: {trade_count}건 승률{market_win_rate:.0f}%")
        else:
            lines.append("실주문접수 내역 없음")

        if include_virtual:
            virtual = summary.get("virtual", {})
            total_virtual_trades = sum(int(item.get("trade_count", 0) or 0) for item in virtual.values())
            if total_virtual_trades > 0:
                lines.append("─── 가상거래(virtual) ───")
                for key, stats in sorted(virtual.items()):
                    trade_count = int(stats.get("trade_count", 0) or 0)
                    win_count = int(stats.get("win_count", 0) or 0)
                    pnl = float(stats.get("total_pnl") or 0.0)
                    currency_suffix = "원" if "KRW" in key else "$"
                    sign = "+" if pnl >= 0 else ""
                    win_rate = (win_count / trade_count * 100.0) if trade_count else 0.0
                    lines.append(
                        f"{key}: {trade_count}건 승률{win_rate:.0f}% 손익{sign}{pnl:,.2f}{currency_suffix}"
                    )
            else:
                lines.append("가상거래 내역 없음")
        return "\n".join(lines)

    def _accumulate_session_performance(self, report: LiquidityLabReport) -> None:
        perf = self.session_performance
        if perf.started_at is None:
            perf.started_at = datetime.now(timezone.utc)
        perf.cycles_completed += 1

        primary_target = str(report.primary_target or report.primary_market or "none")
        perf.primary_targets[primary_target] = perf.primary_targets.get(primary_target, 0) + 1

        self._accumulate_order_stats(report.domestic_order, market="domestic")
        self._accumulate_order_stats(report.overseas_order, market="overseas")

        selection_reason = str(report.primary_selection_reason or "")
        if selection_reason:
            perf.skip_reasons[selection_reason] = perf.skip_reasons.get(selection_reason, 0) + 1

    def _accumulate_order_stats(self, order_result: dict | None, *, market: str) -> None:
        if not order_result:
            return
        batched_orders = order_result.get("batched_orders")
        if isinstance(batched_orders, list) and batched_orders:
            for item in batched_orders:
                self._accumulate_order_stats(item, market=market)
            return
        perf = self.session_performance
        is_submitted = bool(order_result.get("submitted"))
        is_skipped = bool(order_result.get("skipped"))
        if market == "domestic":
            if is_submitted:
                perf.domestic_orders_submitted += 1
            elif not is_skipped:
                perf.domestic_orders_failed += 1
        else:
            if is_submitted:
                perf.overseas_orders_submitted += 1
                perf.estimated_overseas_realized_pnl_krw += self._estimate_overseas_realized_pnl_krw(order_result)
            elif not is_skipped:
                perf.overseas_orders_failed += 1

        self._accumulate_symbol_order_stats(order_result, market=market, submitted=is_submitted)

        if is_skipped:
            reason = str(order_result.get("reason", f"{market}_skipped"))
            perf.skip_reasons[reason] = perf.skip_reasons.get(reason, 0) + 1
        elif not is_submitted:
            reason = str(order_result.get("error", f"{market}_order_failed"))
            perf.skip_reasons[reason] = perf.skip_reasons.get(reason, 0) + 1

    def _accumulate_symbol_order_stats(
        self,
        order_result: dict,
        *,
        market: str,
        submitted: bool,
    ) -> None:
        candidate = order_result.get("candidate") or {}
        symbol = str(candidate.get("symbol") or candidate.get("stock_code") or "").strip().upper()
        if not symbol:
            return

        stats = self._ensure_symbol_stats(symbol)
        side = str(order_result.get("side", "")).strip().lower()
        qty = int(order_result.get("qty", 0) or 0)
        if submitted and side == "buy":
            stats["buy_count"] += 1
            stats["buy_qty"] += qty
        elif submitted and side == "sell":
            stats["sell_count"] += 1
            stats["sell_qty"] += qty
            if market == "overseas":
                stats["estimated_realized_pnl_krw"] += self._estimate_overseas_realized_pnl_krw(order_result)

    def _estimate_overseas_realized_pnl_krw(self, order_result: dict) -> int:
        candidate = order_result.get("candidate") or {}
        held = order_result.get("held_position") or {}
        qty = int(order_result.get("qty", 0) or 0)
        current_price = self._parse_float(candidate.get("last_price") or held.get("current_price"))
        avg_price = self._parse_float(held.get("avg_price"))
        fx_rate = self._parse_float(candidate.get("fx_rate_krw")) or self.config.auto_trade.usd_krw_fallback_rate
        if qty <= 0 or current_price <= 0 or avg_price <= 0:
            return 0
        return int(round((current_price - avg_price) * qty * fx_rate))

    def _ensure_symbol_stats(self, symbol: str) -> dict[str, int]:
        stats = self.session_performance.symbol_stats.setdefault(
            symbol,
            {
                "buy_count": 0,
                "sell_count": 0,
                "buy_qty": 0,
                "sell_qty": 0,
                "paper_runs": 0,
                "confirmed_realized_pnl_krw": 0,
                "estimated_realized_pnl_krw": 0,
            },
        )
        return stats

    @staticmethod
    def _format_symbol_stats_inline(symbol_stats: dict[str, dict]) -> str:
        if not symbol_stats:
            return "-"
        chunks: list[str] = []
        for symbol, stats in sorted(symbol_stats.items()):
            chunks.append(
                (
                    f"{symbol}(buy={int(stats.get('buy_count', 0))},sell={int(stats.get('sell_count', 0))},"
                    f"paper={int(stats.get('paper_runs', 0))},"
                    f"pnl={int(stats.get('confirmed_realized_pnl_krw', 0))}/"
                    f"{int(stats.get('estimated_realized_pnl_krw', 0))})"
                )
            )
        return "; ".join(chunks)

    def _finalize_session_summary(self, *, command: str) -> list[str]:
        ended_at = datetime.now(timezone.utc)
        started_at_iso = (
            self.session_performance.started_at.isoformat()
            if self.session_performance.started_at is not None
            else ""
        )
        summary = format_display_times(
            {
                "command": command,
                "profile": self.config.credentials.profile_name,
                "started_at": format_kst(self.session_performance.started_at),
                "ended_at": format_kst(ended_at),
                **self.session_performance.to_dict(),
                "last_error": self.last_error,
            }
        )
        record_id = self.repository.save_telegram_control_session(
            command=command,
            profile=self.config.credentials.profile_name,
            started_at=summary.get("started_at"),
            cycles_completed=int(summary.get("cycles_completed", 0) or 0),
            domestic_paper_runs=int(summary.get("domestic_paper_runs", 0) or 0),
            domestic_paper_realized_pnl_krw=int(summary.get("domestic_paper_realized_pnl_krw", 0) or 0),
            domestic_orders_submitted=int(summary.get("domestic_orders_submitted", 0) or 0),
            overseas_orders_submitted=int(summary.get("overseas_orders_submitted", 0) or 0),
            domestic_orders_failed=int(summary.get("domestic_orders_failed", 0) or 0),
            overseas_orders_failed=int(summary.get("overseas_orders_failed", 0) or 0),
            summary_json=summary,
        )
        self.repository.save_heartbeat(
            "TELEGRAM_CONTROL_SESSION_SUMMARY",
            (
                f"record_id={record_id} command={command} "
                f"cycles_completed={summary['cycles_completed']} "
                f"domestic_paper_realized_pnl_krw={summary['domestic_paper_realized_pnl_krw']}"
            ),
        )
        self.repository.save_risk_event(
            event_type="TELEGRAM_CONTROL_SESSION_SUMMARY",
            severity="INFO",
            message=(
                f"telegram control session finalized via {command}; "
                f"realized_pnl_krw={summary['domestic_paper_realized_pnl_krw']}"
            ),
            raw_payload=summary,
        )
        lines = [
            "[KIS][TELEGRAM_CONTROL_SESSION_SUMMARY]",
            f"기록={record_id}",
            "모드=stopped",
            f"명령={command}",
            f"시작={summary['started_at']}",
            f"종료={summary['ended_at']}",
            f"사이클={summary['cycles_completed']}",
            f"확정손익={int(summary['domestic_paper_realized_pnl_krw']):,}원",
            f"추정청산손익={int(summary['estimated_overseas_realized_pnl_krw']):,}원",
            (
                "주문=국내 "
                f"{summary['domestic_orders_submitted']}/{summary['domestic_orders_failed']}, "
                "해외 "
                f"{summary['overseas_orders_submitted']}/{summary['overseas_orders_failed']}"
            ),
            f"종목통계={self._format_symbol_stats_inline(summary.get('symbol_stats') or {})}",
            f"최근오류={summary['last_error'] or '-'}",
        ]
        pnl_message = self._build_session_pnl_message(
            started_at=started_at_iso,
            session_id=self.active_session_id,
        )
        self.session_performance = SessionPerformance()
        self.active_session_id = ""
        combined = "\n".join([*lines, "", pnl_message])
        if len(combined) <= 3900:
            return [combined]
        return ["\n".join(lines), pnl_message]

    @staticmethod
    def _summarize_report(report: LiquidityLabReport, cycle_no: int) -> dict:
        return format_display_times(
            {
                "cycle_no": cycle_no,
                "scanned_at": report.scanned_at,
                "primary_market": report.primary_market,
                "primary_target": report.primary_target,
                "primary_selection_reason": report.primary_selection_reason,
                "domestic_order": report.domestic_order,
                "overseas_order": report.overseas_order,
                "watch_targets": report.to_dict().get("watch_targets", []),
                "domestic_positions": [
                    {
                        "market": "domestic",
                        "stock_code": pos.stock_code,
                        "quantity": pos.quantity,
                        "avg_price": pos.avg_price,
                        "current_price": pos.current_price,
                        "pnl_pct": pos.pnl_pct,
                        "currency": "KRW",
                    }
                    for pos in report.domestic_positions
                ],
                "overseas_positions": [
                    {
                        "market": "overseas",
                        "symbol": pos.symbol,
                        "quantity": pos.quantity,
                        "avg_price": pos.avg_price,
                        "current_price": pos.current_price,
                        "pnl_pct": pos.pnl_pct,
                        "exchange_code": pos.exchange_code,
                        "currency": "USD",
                    }
                    for pos in report.overseas_positions
                ],
                "estimated_api_calls_per_cycle": report.estimated_api_calls_per_cycle,
                "market_closed": (
                    not report.krx_market_open and not report.us_market_open
                ),
            }
        )

    @staticmethod
    def parse_command(text: str) -> ParsedCommand | None:
        stripped = text.strip()
        if not stripped:
            return None
        if stripped.lower().startswith("/lab_paper_test"):
            parts = stripped.split(maxsplit=1)
            return ("paper_test", parts[1].strip() if len(parts) > 1 else None)
        if stripped.lower().startswith("/lab_performance"):
            parts = stripped.split(maxsplit=1)
            return ("performance", parts[1].strip() if len(parts) > 1 else None)
        if stripped.lower().startswith("/lab_report"):
            parts = stripped.split(maxsplit=1)
            return ("report", parts[1].strip() if len(parts) > 1 else None)
        if stripped.lower().startswith("/lab_relist_schedule"):
            return "relist_schedule"
        if stripped.lower().startswith("/lab_gitlog"):
            parts = stripped.split(maxsplit=1)
            return ("gitlog", parts[1].strip() if len(parts) > 1 else None)
        if stripped.lower().startswith("/lab_relist"):
            parts = stripped.split(maxsplit=1)
            return ("relist", parts[1].strip() if len(parts) > 1 else None)

        normalized = stripped.split()[0].lower()
        mapping = {
            "/lab_start": "start",
            "/lab_pause": "pause",
            "/lab_resume": "resume",
            "/lab_stop": "stop",
            "/lab_terminate": "terminate",
            "/lab_service_restart": "service_restart",
            "/lab_status": "status",
            "/lab_watchlist": "watchlist",
            "/lab_log": "log",
            "/lab_report": "report",
            "/lab_guard": "guard",
            "/lab_orders": "orders",
            "/lab_portfolio": "portfolio",
            "/lab_trim_virtual": "trim_virtual",
            "/lab_trim_virtual_confirm": "trim_virtual_confirm",
            "/lab_reset": "reset_virtual",
            "/lab_reset_confirm": "reset_virtual_confirm",
            "/lab_reset_all": "reset_all",
            "/lab_reset_all_confirm": "reset_all_confirm",
            "/lab_relist_schedule": "relist_schedule",
            "/lab_cb_reset": "cb_reset",
            "/lab_gitlog": "gitlog",
            "/lab_menu": "menu",
            "/lab_help": "help",
            "/start": "help",
            "/help": "help",
        }
        return mapping.get(normalized)

    def _domestic_name_map(self, last_report: dict | None = None) -> dict[str, str]:
        result: dict[str, str] = {}
        lab = getattr(self, "lab_service", None)
        if lab is not None:
            for code, name in getattr(lab, "_dynamic_domestic_names", {}).items():
                code_text = str(code).strip().upper()
                name_text = str(name or "").strip()
                if code_text and name_text:
                    result[code_text] = name_text
        report_data = last_report or getattr(self, "last_report_summary", {}) or {}
        for row in report_data.get("domestic_ranked", []) or []:
            code = str(row.get("stock_code", "") or "").strip().upper()
            name = str(row.get("stock_name", "") or row.get("name", "") or "").strip()
            if code and name and code not in result:
                result[code] = name
        return result

    def _format_symbol_label(
        self,
        market: str,
        code: str,
        *,
        last_report: dict | None = None,
    ) -> str:
        code_text = str(code or "").strip().upper()
        if str(market).strip().lower() == "domestic":
            name = self._domestic_name_map(last_report=last_report).get(code_text, "")
            return f"{code_text}({name})" if code_text and name else code_text
        return code_text or "-"

    @staticmethod
    def _format_watch_target_line(
        watch_target: dict,
        pnl_pct: float | None = None,
        *,
        symbol_label: str | None = None,
    ) -> str:
        market_raw = str(watch_target.get("market", "overseas")).strip().lower()
        market = format_market_korean(market_raw)
        code = str(symbol_label or watch_target.get("code", "-"))
        action_bias = str(watch_target.get("action_bias", "WAIT")).upper()
        strategy_flag = str(watch_target.get("strategy_flag", "") or "")
        note_raw = str(watch_target.get("note", "-"))
        note = format_reason_korean(note_raw)
        price = watch_target.get("price", "-")
        if isinstance(price, (int, float)):
            if market_raw == "domestic":
                price_text = f"{int(price):,}원"
            else:
                price_text = f"${float(price):.4f}"
        else:
            price_text = str(price)
        holding_qty = int(watch_target.get("holding_qty", 0) or 0)

        status_map = {
            "BUY": "매수신호",
            "SELL": "매도신호",
            "HOLD": format_side_korean("HOLD"),
            "WAIT": format_side_korean("WAIT"),
            "READY": "📊진입준비",
            "WARMUP": "⏳준비중",
        }
        parts = [
            f"{market} {code}",
            f"상태={status_map.get(action_bias, action_bias)}",
            f"전략={strategy_flag or '-'}",
            f"가격={price_text}",
        ]
        if holding_qty > 0 and pnl_pct is not None:
            parts.append(f"보유={holding_qty}주")
            parts.append(f"손익={format_pct(pnl_pct)}")
        elif note != note_raw:
            parts.append(f"사유={note}")
        if "stale_signal_cache" in note_raw:
            parts.append("신호=캐시")
        return " ".join(parts)

    @staticmethod
    def _format_price(value: float, currency: str) -> str:
        if currency == "KRW":
            return f"{int(round(value)):,}원"
        return f"${value:.4f}"

    @staticmethod
    def _format_notional_price(value: float, currency: str) -> str:
        if currency == "KRW":
            return f"{int(round(value)):,}원"
        return f"${value:,.2f}"

    def _max_concurrent_overseas_positions(self) -> int:
        return int(
            getattr(self.config.liquidity_lab, "max_concurrent_overseas_orders", 0) or 0
        )

    @staticmethod
    def _group_virtual_positions_by_market_currency(
        rows: list[dict],
    ) -> dict[tuple[str, str], dict[str, float | int]]:
        by_market_currency: dict[tuple[str, str], dict[str, float | int]] = {}
        for row in rows:
            market = str(row.get("market", "overseas"))
            currency = str(row.get("currency", "USD"))
            qty = int(row.get("qty", 0) or 0)
            avg_price = float(row.get("avg_price", 0.0) or 0.0)
            if qty <= 0 or avg_price <= 0:
                continue
            key = (market, currency)
            item = by_market_currency.setdefault(key, {"count": 0, "notional": 0.0})
            item["count"] = int(item["count"]) + 1
            item["notional"] = float(item["notional"]) + qty * avg_price
        return by_market_currency

    @staticmethod
    def _short_time(value: str | None) -> str:
        if not value:
            return "-"
        parts = value.split()
        if len(parts) < 2:
            return value
        date_part = parts[0]
        time_part = parts[1]
        try:
            _, month, day = [int(chunk) for chunk in date_part.split("-")]
            hour, minute, _ = [int(chunk) for chunk in time_part.split(":")]
        except ValueError:
            return value
        return f"{month}월 {day}일 {hour:02d}:{minute:02d}"

    @staticmethod
    def _combined_positions(last_report: dict) -> list[dict]:
        return [
            *(last_report.get("domestic_positions") or []),
            *(last_report.get("overseas_positions") or []),
        ]

    def _build_effective_positions(
        self,
        last_report: dict,
        *,
        real_positions_override: list[dict] | None = None,
    ) -> list[dict[str, object]]:
        positions = (
            real_positions_override
            if real_positions_override is not None
            else self._combined_positions(last_report)
        )
        effective: dict[tuple[str, str], dict[str, object]] = {}

        for pos in positions:
            market = str(
                pos.get(
                    "market",
                    "domestic" if pos.get("stock_code") else "overseas",
                )
            )
            symbol = str(pos.get("symbol") or pos.get("stock_code") or "-").upper()
            currency = str(pos.get("currency", "USD"))
            key = (market, symbol)
            effective[key] = {
                "market": market,
                "symbol": symbol,
                "qty": int(pos.get("quantity", 0) or 0),
                "avg_price": float(pos.get("avg_price", 0) or 0.0),
                "currency": currency,
            }

        manager = VirtualTradeManager(self.repository)
        for position in manager.list_positions():
            key = (position.market, position.symbol.upper())
            existing = effective.get(key)
            if existing is None:
                effective[key] = {
                    "market": position.market,
                    "symbol": position.symbol.upper(),
                    "qty": position.qty,
                    "avg_price": position.avg_price,
                    "currency": position.currency,
                }
                continue
            base_qty = int(existing["qty"])
            base_avg = float(existing["avg_price"])
            next_qty = base_qty + position.qty
            next_avg = position.avg_price
            if next_qty > 0:
                next_avg = ((base_avg * base_qty) + (position.avg_price * position.qty)) / next_qty
            existing["qty"] = next_qty
            existing["avg_price"] = next_avg

        for row in self.repository.list_virtual_sell_pending(market="overseas"):
            market = str(row.get("market", "overseas"))
            symbol = str(row.get("symbol", "-")).upper()
            key = (market, symbol)
            existing = effective.get(key)
            if existing is None:
                continue
            existing["qty"] = int(existing["qty"]) - int(row.get("qty", 0) or 0)

        result = [item for item in effective.values() if int(item["qty"]) > 0]
        result.sort(key=lambda item: (str(item["market"]), str(item["symbol"])))
        return result

    @staticmethod
    def _parse_float(value: object) -> float:
        if value is None:
            return 0.0
        text = str(value).strip().replace(",", "")
        if not text:
            return 0.0
        return float(text)
