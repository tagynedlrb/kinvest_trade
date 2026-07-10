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
from .time_utils import (
    KST,
    ensure_timezone,
    format_display_times,
    format_kst,
    format_kst_korean,
    parse_datetime,
)
from .trade_analysis import compare_before_after


HELP_MESSAGE = "\n".join(
    [
        "[KIS][TELEGRAM_CONTROL_HELP]",
        "/lab_start - 거래 루프 시작",
        "/lab_pause - 현재 사이클 종료 후 일시정지",
        "/lab_resume - 일시정지 해제",
        "/lab_stop - 즉시 중지 후 세션 요약",
        "/lab_terminate - 강제 종료 후 대기",
        "/lab_service_restart - 텔레그램 제어 서비스 재시작",
        "/lab_status - 현재 상태",
        "/lab_watchlist - 감시 종목 요약",
        "/lab_log - 최근 매매 내역 조회",
        "/lab_performance [시간] - 최근 실주문접수 전략 성과",
        "/lab_report compare <YYYY-MM-DD> - 기준일 전후 전략 성과 비교",
        "/lab_guard - 현재 성과 기반 전략 차단 상태",
        "/lab_orders - 최근 주문 접수/취소 기록",
        "/lab_cancel_stale_domestic - 30분 이상 국내 미체결 취소 확인",
        "/lab_cancel_stale_overseas - 30분 이상 해외 미체결 취소 확인",
        "/lab_portfolio - 보유현황 통합 (실보유·가상·성과)",
        "/lab_trim_virtual - 가상보유 초과분만 성과 제외 정리",
        "/lab_reset - 가상거래 초기화 (DB 백업 후 virtual 테이블 삭제)",
        "/lab_relist <심볼...> - 해외 감시 풀 수동 교체",
        "/lab_relist_schedule - 해외 relist 알림 시간 확인",
        "/lab_cb_reset - 서킷브레이커 강제 해제 (연속손절 카운터 초기화)",
        "/lab_gitlog [날짜] - 오늘(또는 지정 날짜) 거래 로그를 GitHub에 업로드",
        "/lab_paper_test <종목코드> - 수동 페이퍼 테스트",
        "/lab_help - 명령 목록",
    ]
)

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
    {"command": "lab_cancel_stale_domestic", "description": "장기 국내 미체결 취소 확인"},
    {"command": "lab_cancel_stale_overseas", "description": "장기 해외 미체결 취소 확인"},
    {"command": "lab_portfolio", "description": "보유현황 통합 보기"},
    {"command": "lab_trim_virtual", "description": "가상보유 초과분 정리"},
    {"command": "lab_reset", "description": "가상거래 초기화 (백업 후)"},
    {"command": "lab_relist", "description": "해외 감시 풀 수동 교체"},
    {"command": "lab_relist_schedule", "description": "해외 relist 알림 시간"},
    {"command": "lab_cb_reset", "description": "서킷브레이커 강제 해제"},
    {"command": "lab_gitlog", "description": "거래 로그 GitHub 업로드"},
    {"command": "lab_paper_test", "description": "페이퍼 테스트(종목코드 필요)"},
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
                self.mode = "stopped"
                self._write_runtime_state()
            else:
                await asyncio.gather(scheduler, command_loop)
        except asyncio.CancelledError:
            self.mode = "stopped"
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

    async def _command_loop(self) -> None:
        while True:
            updates = await self.notifier.get_updates(offset=self.update_offset)
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    self.update_offset = update_id + 1
                await self._handle_update(update)
                self._write_runtime_state()
            await asyncio.sleep(0.2)

    async def _handle_update(self, update: dict) -> None:
        message = update.get("message", {}) if isinstance(update, dict) else {}
        chat = message.get("chat", {}) if isinstance(message, dict) else {}
        chat_id = chat.get("id")
        text = str(message.get("text", "") or "").strip()
        if not text:
            return
        if not self.notifier.is_authorized_chat(chat_id):
            return

        parsed_command = self.parse_command(text)
        if parsed_command is None:
            return

        command_name = parsed_command if isinstance(parsed_command, str) else parsed_command[0]
        self.last_command = command_name
        self.last_command_at = datetime.now(timezone.utc)

        if command_name == "help":
            await self.notifier.send(HELP_MESSAGE)
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
        if command_name == "cancel_stale_domestic":
            await self._send_cancel_stale_domestic_prompt()
            return
        if command_name == "cancel_stale_domestic_confirm":
            await self._execute_cancel_stale_domestic_orders()
            return
        if command_name == "cancel_stale_overseas":
            await self._send_cancel_stale_overseas_prompt()
            return
        if command_name == "cancel_stale_overseas_confirm":
            await self._execute_cancel_stale_overseas_orders(source="manual")
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

        by_market_currency: dict[tuple[str, str], dict[str, float | int]] = {}
        for row in positions:
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

        if not by_market_currency and not pending_sells:
            return ["현재상태=가상보유/정산대기 없음"]

        max_overseas_positions = int(
            getattr(self.config.liquidity_lab, "max_concurrent_overseas_orders", 0) or 0
        )
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

    def _select_virtual_trim_candidates(self) -> tuple[list[dict[str, object]], int, int]:
        max_overseas_positions = int(
            getattr(self.config.liquidity_lab, "max_concurrent_overseas_orders", 0) or 0
        )
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
            state = self.repository.get_lab_symbol_state(market, symbol)
            current_price = 0.0
            if state is not None:
                current_price = float(state.get("last_price") or 0.0)
            price_missing = current_price <= 0
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
        price_note = " 현재가없음" if bool(item.get("price_missing")) else ""
        return (
            f"해외 {symbol} 수량={qty} "
            f"매입={self._format_price(avg_price, currency)} "
            f"정리가={self._format_price(current_price, currency)} "
            f"손익={format_pct(pnl_pct)}{price_note}"
        )

    async def _send_trim_virtual_prompt(self) -> None:
        candidates, total, max_positions = self._select_virtual_trim_candidates()
        if not candidates:
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

        lines = [
            "⚠️ [가상보유 초과분 정리]",
            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
            f"포지션={total}/{max_positions} 초과={len(candidates)}종목",
            "방식=성과 제외 가상매도 기록 후 초과분 삭제",
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
        candidates, total, max_positions = self._select_virtual_trim_candidates()
        if not candidates:
            await self.notifier.send(
                "\n".join(
                    [
                        "[KIS][가상보유 정리]",
                        f"시각={format_kst_korean(now)}",
                        f"상태=정리불필요 ({total}/{max_positions})",
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
            for key in ("trades", "events"):
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
        lines.extend(await self._build_start_resume_open_order_notice_lines())
        await self.notifier.send("\n".join(lines))

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
        ]
        if domestic_count:
            lines.append("국내장기취소=/lab_cancel_stale_domestic")
        if overseas_count:
            lines.append("해외장기취소=/lab_cancel_stale_overseas")
        return lines

    async def _handle_cb_reset(self) -> None:
        if self.lab_service is None:
            await self.notifier.send("⚠️ lab 인스턴스에 접근할 수 없습니다.")
            return
        previous = int(getattr(self.lab_service, "_consecutive_losses", 0) or 0)
        setattr(self.lab_service, "_consecutive_losses", 0)
        setattr(self.lab_service, "_halted_at", None)
        await self.notifier.send(
            f"✅ 서킷브레이커 수동 해제\n"
            f"연속손절 카운터: {previous} → 0\n"
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
            async with KisRestClient(self.config.credentials) as client:
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
        if self.mode == "running":
            return "실행중"
        if self.mode == "paused":
            return "일시정지됨 (/lab_resume 가능)"
        return "중지됨 (/lab_start 필요)"

    def _report_freshness_notice(self, now: datetime | None = None) -> str:
        if not self.last_report_summary:
            return "감시데이터=없음 (/lab_start 후 생성)"

        age_min = self._last_report_age_minutes(now)
        if age_min is None:
            return "감시데이터=저장상태(시각불명)"

        age_text = "방금" if age_min <= 0 else f"{age_min}분 전"
        if self.mode != "running":
            mode_text = "일시정지" if self.mode == "paused" else "중지"
            return f"감시데이터={age_text} (저장값·루프 {mode_text})"

        if age_min >= self._status_stale_threshold_min():
            return f"감시데이터={age_text} (지연)"
        return f"감시데이터={age_text}"

    def _last_report_age_minutes(self, now: datetime | None = None) -> int | None:
        if not self.last_report_summary:
            return None
        ref_time = getattr(self, "last_completed_at", None)
        if ref_time is None:
            ref_time = parse_datetime(str(self.last_report_summary.get("scanned_at") or ""))
        if ref_time is None:
            return None

        current = now or datetime.now(timezone.utc)
        return int(max((current - ensure_timezone(ref_time)).total_seconds(), 0.0) // 60)

    def _status_stale_threshold_min(self) -> int:
        config = getattr(self, "config", None)
        liquidity_lab = getattr(config, "liquidity_lab", None)
        interval_sec = max(int(getattr(liquidity_lab, "loop_interval_sec", 20) or 20), 1)
        return max(5, int((interval_sec * 6) // 60))

    def _estimated_pnl_suffix(self, now: datetime | None = None) -> str:
        if not self.last_report_summary:
            return ""
        if self.mode != "running":
            return " (저장값)"
        age_min = self._last_report_age_minutes(now)
        if age_min is None:
            return " (저장값)"
        if age_min >= self._status_stale_threshold_min():
            return " (지연값)"
        return ""

    async def _send_status_message(self) -> None:
        domestic_open_count: int | None = None
        overseas_open_count: int | None = None
        open_order_error = ""
        try:
            domestic_orders, overseas_orders = await asyncio.wait_for(
                asyncio.gather(
                    self._load_live_open_domestic_orders(limit=20),
                    self._load_live_open_overseas_orders(limit=20),
                ),
                timeout=8.0,
            )
            domestic_open_count = len(domestic_orders)
            overseas_open_count = len(overseas_orders)
        except Exception as exc:  # noqa: BLE001
            open_order_error = str(exc)[:80]
        await self.notifier.send(
            self._build_status_message(
                domestic_open_count=domestic_open_count,
                overseas_open_count=overseas_open_count,
                open_order_error=open_order_error,
            )
        )

    def _build_status_message(
        self,
        *,
        domestic_open_count: int | None = None,
        overseas_open_count: int | None = None,
        open_order_error: str = "",
    ) -> str:
        snapshot = self._snapshot()
        session = snapshot.session_performance or {}
        last_report = snapshot.last_report_summary or {}
        now = datetime.now(timezone.utc)
        krx_holiday = bool(
            getattr(self.config, "skip_holiday_domestic", True)
            and is_krx_holiday(now.astimezone(KST).date())
        )
        nyse_holiday = bool(
            getattr(self.config, "skip_holiday_overseas", True)
            and is_nyse_holiday(us_holiday_date_for_kis_session(now))
        )
        krx_open = is_krx_regular_session(now) and not krx_holiday
        us_session = get_us_trading_session(now)
        us_tradeable = is_us_orderable_session_for_env(now, self.config.credentials.env) and not nyse_holiday
        us_watchable = us_session != "closed" and not nyse_holiday
        if krx_open:
            market_status = "KRX 정규장 ✓"
        elif us_tradeable:
            market_status = f"US {us_session} ✓"
        elif us_watchable and us_session in {"daytime", "premarket", "aftermarket"}:
            env = str(getattr(self.config.credentials, "env", "vps") or "vps")
            if env == "prod":
                market_status = f"US {us_session} (감시중)"
            else:
                market_status = f"US {us_session} (모의 주문불가·감시만)"
        elif krx_holiday and nyse_holiday:
            market_status = "KRX/US 휴장"
        elif krx_holiday:
            market_status = "KRX 휴장"
        elif nyse_holiday:
            market_status = "US 휴장"
        else:
            mins = minutes_until_next_tradeable_session(now, self.config.credentials.env)
            hours, minutes = divmod(mins, 60)
            market_status = f"양쪽 장 닫힘 — 다음 개장까지 {hours}h{minutes:02d}m"

        next_interval = determine_loop_interval_sec(
            now,
            self.config.credentials.env,
            self._consecutive_errors,
        )
        next_run_text = "-" if snapshot.mode != "running" else self._short_time(snapshot.next_run_at)
        next_interval_text = "-" if snapshot.mode != "running" else f"{next_interval}초"
        watch_count_text = self._watch_target_count_text(last_report)
        lines = [
            "[KIS][TELEGRAM_CONTROL_STATUS]",
            f"시각={format_kst_korean(now)}",
            f"모드={snapshot.mode}",
            f"거래루프={self._loop_mode_notice()}",
            f"사이클={snapshot.current_cycle_no}",
            f"시장상태={market_status}",
            self._report_freshness_notice(now),
            f"다음실행={next_run_text}",
            f"다음간격={next_interval_text}",
            f"최근명령={snapshot.last_command or '-'}",
            f"최근완료={self._short_time(snapshot.last_completed_at)}",
            f"최근타겟={last_report.get('primary_target') or '-'}",
            f"확정손익={int(session.get('domestic_paper_realized_pnl_krw', 0) or 0):,}원",
            "추정청산손익="
            f"{int(session.get('estimated_overseas_realized_pnl_krw', 0) or 0):,}원"
            f"{self._estimated_pnl_suffix(now)}",
            f"감시수={watch_count_text}",
        ]
        signal_cache_status = self._build_signal_cache_status_line(last_report)
        if signal_cache_status:
            lines.append(signal_cache_status)
        virtual_exposure_status = self._build_virtual_exposure_status_line()
        if virtual_exposure_status:
            lines.append(virtual_exposure_status)
        sell_block_status = self._build_recent_sell_block_status_line()
        if sell_block_status:
            lines.append(sell_block_status)
        if open_order_error:
            lines.append(f"미체결=조회실패 ({open_order_error})")
        elif domestic_open_count is not None or overseas_open_count is not None:
            domestic_count = 0 if domestic_open_count is None else domestic_open_count
            overseas_count = 0 if overseas_open_count is None else overseas_open_count
            lines.append(f"미체결=국내 {domestic_count} / 해외 {overseas_count}")
            if domestic_count or overseas_count:
                lines.append("미체결확인=/lab_orders")
            if domestic_count:
                lines.append("국내장기취소=/lab_cancel_stale_domestic")
            if overseas_count:
                lines.append("해외장기취소=/lab_cancel_stale_overseas")
        lines.extend(
            [
                f"오류연속={self._consecutive_errors}",
                f"최근오류={snapshot.last_error or '-'}",
            ]
        )
        return "\n".join(lines)

    def _build_virtual_exposure_status_line(self) -> str:
        repository = getattr(self, "repository", None)
        if repository is None or not hasattr(repository, "list_virtual_positions"):
            return ""
        try:
            rows = repository.list_virtual_positions()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("status_virtual_exposure_failed error=%s", exc)
            return ""

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
        if not by_market_currency:
            return ""

        lab = self.lab_service
        last_available_usd = (
            None
            if lab is None
            else getattr(lab, "_last_overseas_available_usd", None)
        )
        max_pct = float(
            getattr(self.config.liquidity_lab, "max_virtual_exposure_pct", 1.0) or 1.0
        )
        max_overseas_positions = int(
            getattr(self.config.liquidity_lab, "max_concurrent_overseas_orders", 0) or 0
        )
        parts: list[str] = []
        status = ""
        for (market, currency), item in sorted(by_market_currency.items()):
            count = int(item["count"])
            notional = float(item["notional"])
            parts.append(
                f"{format_market_korean(market)} "
                f"{self._format_notional_price(notional, currency)} "
                f"{count}종목"
            )
            if (
                not status
                and market == "overseas"
                and currency == "USD"
                and last_available_usd is not None
                and float(last_available_usd) > 0
            ):
                limit = float(last_available_usd) * max_pct
                status = "초과" if notional > limit else "정상"

        suffix: list[str] = []
        position_cap_exceeded = False
        if status:
            suffix.append(f"상태={status}")
            if status == "초과" and self.mode != "running":
                suffix.append("감시=중지")
        if max_overseas_positions > 0:
            overseas_count = sum(
                int(item["count"])
                for (market, currency), item in by_market_currency.items()
                if market == "overseas" and currency == "USD"
            )
            if overseas_count > 0:
                position_cap_exceeded = overseas_count > max_overseas_positions
                cap_status = "초과" if position_cap_exceeded else "정상"
                suffix.append(
                    f"포지션한도={overseas_count}/{max_overseas_positions} {cap_status}"
                )
        if position_cap_exceeded and self.mode != "running":
            if "감시=중지" not in suffix:
                suffix.append("감시=중지")
            suffix.append("조치=/lab_trim_virtual 또는 /lab_start")
        suffix.append("확인=/lab_portfolio")
        return f"가상노출={' / '.join(parts)} {' '.join(suffix)}"

    def _build_signal_cache_status_line(self, last_report: dict) -> str:
        raw_watch_targets = last_report.get("watch_targets") or []
        watch_targets = [
            target
            for target in raw_watch_targets
            if not self._is_closed_stale_watch_target(target)
        ]
        hidden_count = len(raw_watch_targets) - len(watch_targets)
        if not watch_targets:
            if hidden_count > 0:
                return f"신호캐시=숨김 정리잔상{hidden_count} 확인=/lab_watchlist"
            return ""
        stale_count = sum(
            1
            for target in watch_targets
            if "stale_signal_cache" in str(target.get("note", ""))
        )
        if stale_count <= 0:
            return ""
        total = len(watch_targets)
        hidden_text = f" 숨김=정리잔상{hidden_count}" if hidden_count > 0 else ""
        if stale_count == total:
            return f"신호캐시={stale_count}/{total} 전체 캐시{hidden_text} 확인=/lab_watchlist"
        return f"신호캐시={stale_count}/{total} 일부 캐시{hidden_text} 확인=/lab_watchlist"

    def _watch_target_count_text(self, last_report: dict) -> str:
        raw_watch_targets = last_report.get("watch_targets") or []
        hidden_count = sum(
            1 for target in raw_watch_targets if self._is_closed_stale_watch_target(target)
        )
        visible_count = max(0, len(raw_watch_targets) - hidden_count)
        if hidden_count <= 0:
            return str(visible_count)
        return f"{visible_count} (숨김 {hidden_count})"

    def _build_recent_sell_block_status_line(self, *, lookback_hours: int = 12) -> str:
        repository = getattr(self, "repository", None)
        if repository is None or not hasattr(repository, "list_event_log"):
            return ""
        try:
            rows = repository.list_event_log(event_type="trade_skip", limit=300)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("status_sell_block_summary_failed error=%s", exc)
            return ""

        cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))
        stats: dict[tuple[str, str, str], dict[str, int | datetime | None]] = {}
        for row in rows:
            logged_at = parse_datetime(str(row.get("logged_at") or ""))
            if logged_at is not None and logged_at < cutoff:
                continue
            detail_raw = row.get("detail", "")
            try:
                detail = json.loads(detail_raw) if isinstance(detail_raw, str) else {}
            except json.JSONDecodeError:
                detail = {}
            reason = str(detail.get("reason") or "")
            if reason not in {"no_orderable_qty", "order_rejected"}:
                continue
            side = str(detail.get("side") or "").lower()
            if side and side != "sell":
                continue
            market = str(row.get("market") or "")
            symbol = str(row.get("symbol") or "").strip().upper()
            if not market or not symbol:
                continue
            key = (market, symbol, reason)
            item = stats.setdefault(key, {"count": 0, "latest": None})
            item["count"] = int(item["count"]) + 1
            if logged_at is not None:
                latest = item.get("latest")
                if latest is None or logged_at > latest:
                    item["latest"] = logged_at

        if not stats:
            return ""

        reason_labels = {
            "no_orderable_qty": "매도가능0",
            "order_rejected": "주문거부",
        }
        ranked = sorted(
            stats.items(),
            key=lambda item: (
                -int(item[1]["count"]),
                item[1]["latest"] or datetime.min.replace(tzinfo=timezone.utc),
            ),
        )
        parts: list[str] = []
        for (market, symbol, reason), item in ranked[:3]:
            count = int(item["count"])
            parts.append(
                f"{format_market_korean(market)} {symbol} "
                f"{reason_labels.get(reason, reason)} {count}회"
            )
        return f"매도장애({lookback_hours}h)={' / '.join(parts)} 확인=/lab_orders"

    def _build_watchlist_message(self) -> str:
        last_report = self.last_report_summary or {}
        watch_targets = last_report.get("watch_targets") or []
        positions = self._combined_positions(last_report)
        pnl_map: dict[tuple[str, str], float] = {}
        for pos in positions:
            market = str(
                pos.get(
                    "market",
                    "domestic" if pos.get("stock_code") else "overseas",
                )
            )
            code = str(pos.get("symbol") or pos.get("stock_code") or "").upper()
            if code:
                pnl_map[(market, code)] = float(pos.get("pnl_pct", 0) or 0)
        lab = self.lab_service
        if lab is not None:
            balance_cache = getattr(lab, "_overseas_balance_cache", {})
            for balance in balance_cache.get("data", {}).values():
                for row in balance.get("positions", []):
                    qty_raw = row.get("ovrs_cblc_qty") or "0"
                    try:
                        qty = int(float(str(qty_raw).replace(",", "")))
                    except (ValueError, TypeError):
                        qty = 0
                    if qty <= 0:
                        continue
                    sym = str(row.get("ovrs_pdno", "")).strip().upper()
                    key = ("overseas", sym)
                    if sym and key not in pnl_map:
                        try:
                            avg = float(str(row.get("pchs_avg_pric", "0") or "0").replace(",", ""))
                            cur = float(str(row.get("now_pric2", "0") or "0").replace(",", ""))
                            pnl_map[key] = (cur - avg) / avg if avg > 0 else 0.0
                        except (ValueError, TypeError):
                            pass
        repository = getattr(self, "repository", None)
        if repository is not None:
            for row in repository.list_lab_symbol_states(only_positions=True):
                market = str(row.get("market", "overseas"))
                sym = str(row.get("symbol", "")).strip().upper()
                if not sym:
                    continue
                pnl_map[(market, sym)] = float(row.get("pnl_pct", 0) or 0)
        lines = [
            "[KIS][TELEGRAM_CONTROL_WATCHLIST]",
            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
            f"모드={self.mode}",
            f"사이클={self.current_cycle_no}",
            self._report_freshness_notice(),
            f"예상호출={last_report.get('estimated_api_calls_per_cycle', '-')}",
        ]
        if self.mode != "running":
            lines.append("주의=루프가 실행 중이 아니므로 아래 목록은 마지막 저장 감시데이터")
        if not watch_targets:
            lines.append("감시종목=없음")
            if positions:
                lines.append(self._build_positions_message())
            return "\n".join(lines)

        hidden_closed_count = 0
        visible_count = 0
        for watch_target in watch_targets:
            if self._is_closed_stale_watch_target(watch_target):
                hidden_closed_count += 1
                continue
            display_target = self._watch_target_with_persisted_position(watch_target)
            market = str(display_target.get("market", "overseas"))
            symbol = str(display_target.get("code", "")).upper()
            lines.append(
                self._format_watch_target_line(
                    display_target,
                    pnl_pct=pnl_map.get((market, symbol)),
                    symbol_label=self._format_symbol_label(
                        market,
                        symbol,
                        last_report=last_report,
                    ),
                )
            )
            visible_count += 1
        if visible_count <= 0:
            lines.append("감시종목=없음")
        if hidden_closed_count > 0:
            lines.append(f"숨김=정리된 보유잔상 {hidden_closed_count}개")
        return "\n".join(lines)

    def _watch_target_with_persisted_position(self, watch_target: dict) -> dict:
        repository = getattr(self, "repository", None)
        if repository is None or not hasattr(repository, "get_lab_symbol_state"):
            return watch_target
        try:
            holding_qty = int(float(str(watch_target.get("holding_qty", 0) or 0)))
        except (TypeError, ValueError):
            holding_qty = 0
        if holding_qty <= 0:
            return watch_target
        market = str(watch_target.get("market", "overseas") or "overseas").strip().lower()
        symbol = str(watch_target.get("code", "") or "").strip().upper()
        if not market or not symbol:
            return watch_target
        state = repository.get_lab_symbol_state(market, symbol)
        if state is None:
            return watch_target
        try:
            has_position = int(state.get("has_position", 0) or 0)
        except (TypeError, ValueError):
            has_position = 0
        if has_position <= 0:
            return watch_target
        display_target = dict(watch_target)
        try:
            state_qty = int(float(str(state.get("holding_qty", 0) or 0)))
        except (TypeError, ValueError):
            state_qty = 0
        if state_qty > 0:
            display_target["holding_qty"] = state_qty
        try:
            last_price = float(state.get("last_price", 0) or 0)
        except (TypeError, ValueError):
            last_price = 0.0
        if last_price > 0:
            display_target["price"] = last_price
        return display_target

    def _is_closed_stale_watch_target(self, watch_target: dict) -> bool:
        try:
            holding_qty = int(float(str(watch_target.get("holding_qty", 0) or 0)))
        except (TypeError, ValueError):
            holding_qty = 0
        if holding_qty <= 0:
            return False
        repository = getattr(self, "repository", None)
        if repository is None or not hasattr(repository, "get_lab_symbol_state"):
            return False
        market = str(watch_target.get("market", "overseas") or "overseas").strip().lower()
        symbol = str(watch_target.get("code", "") or "").strip().upper()
        if not market or not symbol:
            return False
        state = repository.get_lab_symbol_state(market, symbol)
        if state is None:
            return False
        try:
            has_position = int(state.get("has_position", 0) or 0)
        except (TypeError, ValueError):
            has_position = 0
        return has_position <= 0

    def _build_positions_message(self) -> str:
        last_report = self.last_report_summary or {}
        positions = self._combined_positions(last_report)

        lines = [
            "[KIS][TELEGRAM_CONTROL_POSITIONS]",
            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
            f"사이클={self.current_cycle_no}",
        ]

        if not positions:
            lines.append("보유종목=없음")
            return "\n".join(lines)

        total_pnl_pct_sum = 0.0
        for pos in positions:
            market_key = str(pos.get("market", "overseas"))
            symbol = self._format_symbol_label(
                market_key,
                str(pos.get("symbol") or pos.get("stock_code") or "-"),
                last_report=last_report,
            )
            market = format_market_korean(market_key)
            qty = int(pos.get("quantity", 0) or 0)
            avg_price = float(pos.get("avg_price", 0) or 0)
            current_price = float(pos.get("current_price", 0) or 0)
            pnl_pct = float(pos.get("pnl_pct", 0) or 0)
            total_pnl_pct_sum += pnl_pct
            currency = str(pos.get("currency", "USD"))
            pnl_text = format_pct(pnl_pct)
            price_text = self._format_price(current_price, currency)
            avg_text = self._format_price(avg_price, currency)
            lines.append(
                f"{market} {symbol} 수량={qty} 매입={avg_text} 현재={price_text} 손익={pnl_text}"
            )

        avg_pnl = total_pnl_pct_sum / len(positions)
        lines.append(f"평균손익={format_pct(avg_pnl)}")
        return "\n".join(lines)

    async def _send_positions_message(self) -> None:
        await self.notifier.send(self._build_positions_message())

    def _build_virtual_portfolio_message(self) -> str:
        manager = VirtualTradeManager(self.repository)
        positions = manager.list_positions()
        pending_sells = self.repository.list_virtual_sell_pending(market="overseas")
        summary = manager.performance_summary()
        now = datetime.now(timezone.utc)
        lines = [
            "[KIS][VIRTUAL_PORTFOLIO]",
            f"시각={format_kst_korean(now)}",
        ]

        lines.append("--- 보유 종목 (virtual) ---")
        if not positions:
            lines.append("보유종목=없음")
        else:
            for position in positions:
                market = format_market_korean(position.market)
                price_text = self._format_price(position.avg_price, position.currency)
                symbol_label = f"{position.symbol} (virtual)"
                lines.append(
                    f"{market} {symbol_label} 수량={position.qty} 평균단가={price_text}"
                )

        lines.append("--- 정산 대기 매도 (virtual) ---")
        if not pending_sells:
            lines.append("정산대기=없음")
        else:
            for row in pending_sells:
                market = format_market_korean(str(row.get("market", "overseas")))
                symbol = str(row.get("symbol", "-"))
                qty = int(row.get("qty", 0) or 0)
                avg_sell_price = float(row.get("avg_sell_price", 0.0) or 0.0)
                currency = str(row.get("currency", "USD"))
                lines.append(
                    f"{market} {symbol} (virtual) 수량=-{qty} 가상매도가={self._format_price(avg_sell_price, currency)}"
                )

        lines.append("--- 누적 성과 (virtual) ---")
        if not summary:
            lines.append("성과=없음")
        else:
            for key in sorted(summary):
                item = summary[key]
                market = format_market_korean(str(item.get("market", "overseas")))
                currency = str(item.get("currency", "USD"))
                trade_count = int(item.get("trade_count", 0) or 0)
                win_count = int(item.get("win_count", 0) or 0)
                total_pnl = float(item.get("total_pnl", 0.0) or 0.0)
                win_rate = (win_count / trade_count) if trade_count > 0 else 0.0
                pnl_text = self._format_price(total_pnl, currency)
                lines.append(
                    f"{market} 체결={trade_count} 승률={format_pct(win_rate)} 실현손익={pnl_text}"
                )
        return "\n".join(lines)

    async def _send_virtual_portfolio_message(self) -> None:
        await self.notifier.send(self._build_virtual_portfolio_message())

    def _build_portfolio_message(
        self,
        real_positions_override: list[dict] | None = None,
        price_lookup_override: dict[tuple[str, str], float] | None = None,
        virtual_exposure_available_usd: float | None = None,
    ) -> str:
        now = datetime.now(timezone.utc)
        lines = [
            "[KIS][포트폴리오]",
            f"시각={format_kst_korean(now)}",
        ]
        if self.mode != "running":
            lines.append(f"거래루프={self._loop_mode_notice()}")

        last_report = self.last_report_summary or {}
        real_positions = (
            real_positions_override
            if real_positions_override is not None
            else self._combined_positions(last_report)
        )
        price_lookup: dict[tuple[str, str], float] = {}
        for wt in last_report.get("watch_targets", []):
            market = str(wt.get("market", "overseas"))
            code = str(wt.get("code", "")).upper()
            price = float(wt.get("price", 0) or 0)
            if code and price > 0:
                price_lookup[(market, code)] = price
        for pos in real_positions:
            market = str(
                pos.get(
                    "market",
                    "domestic" if pos.get("stock_code") else "overseas",
                )
            )
            code = str(pos.get("symbol") or pos.get("stock_code") or "").upper()
            current_price = float(pos.get("current_price", 0) or 0)
            key = (market, code)
            if code and current_price > 0 and key not in price_lookup:
                price_lookup[key] = current_price
        lab = self.lab_service
        if lab is not None:
            balance_cache = getattr(lab, "_overseas_balance_cache", {})
            for balance in balance_cache.get("data", {}).values():
                for row in balance.get("positions", []):
                    sym = str(row.get("ovrs_pdno", "")).strip().upper()
                    key = ("overseas", sym)
                    if sym:
                        try:
                            cur = float(str(row.get("now_pric2", "0") or "0").replace(",", ""))
                            if cur > 0:
                                price_lookup[key] = cur
                        except (ValueError, TypeError):
                            pass
        repository = getattr(self, "repository", None)
        if repository is not None:
            for row in repository.list_lab_symbol_states(only_positions=True):
                market = str(row.get("market", "overseas"))
                symbol = str(row.get("symbol", "")).strip().upper()
                key = (market, symbol)
                if not symbol:
                    continue
                last_price = float(row.get("last_price", 0) or 0)
                if last_price > 0:
                    price_lookup[key] = last_price
        if price_lookup_override:
            price_lookup.update(
                {
                    (market, symbol): price
                    for (market, symbol), price in price_lookup_override.items()
                    if symbol and price > 0
                }
            )
        lines.append("─── 실보유 종목 ───")
        if not real_positions:
            lines.append("보유종목=없음")
        else:
            for pos in real_positions:
                market_key = str(
                    pos.get(
                        "market",
                        "domestic" if pos.get("stock_code") else "overseas",
                    )
                )
                raw_symbol = str(pos.get("symbol") or pos.get("stock_code") or "-").upper()
                symbol = self._format_symbol_label(
                    market_key,
                    raw_symbol,
                    last_report=last_report,
                )
                market = format_market_korean(market_key)
                qty = int(pos.get("quantity", 0) or 0)
                avg_price = float(pos.get("avg_price", 0) or 0)
                current_price = price_lookup.get(
                    (market_key, raw_symbol),
                    float(pos.get("current_price", 0) or 0),
                )
                pnl_pct = (
                    (current_price - avg_price) / avg_price
                    if avg_price > 0 and current_price > 0
                    else float(pos.get("pnl_pct", 0) or 0)
                )
                currency = str(pos.get("currency", "USD"))
                lines.append(
                    f"{market} {symbol} "
                    f"수량={qty} "
                    f"매입={self._format_price(avg_price, currency)} "
                    f"현재={self._format_price(current_price, currency)} "
                    f"손익={format_pct(pnl_pct)}"
                )
            risk_lines = self._build_real_position_risk_lines(
                real_positions,
                last_report=last_report,
            )
            if risk_lines:
                lines.append("─── 실보유 리스크 ───")
                lines.extend(risk_lines)

        manager = VirtualTradeManager(self.repository)
        effective_positions = self._build_effective_positions(
            last_report,
            real_positions_override=real_positions,
        )
        lines.append("─── 가상보유 종목 ───")
        if not effective_positions:
            lines.append("가상보유=없음")
        else:
            for position in effective_positions:
                market_key = str(position["market"])
                market = format_market_korean(market_key)
                symbol = self._format_symbol_label(
                    market_key,
                    str(position["symbol"]).upper(),
                    last_report=last_report,
                )
                currency = str(position["currency"])
                avg_price = float(position["avg_price"])
                qty = int(position["qty"])
                cur_price = price_lookup.get(
                    (market_key, str(position["symbol"]).upper()),
                    0.0,
                )

                avg_text = self._format_price(avg_price, currency)
                if cur_price > 0 and avg_price > 0:
                    pnl_pct = (cur_price - avg_price) / avg_price
                    cur_text = self._format_price(cur_price, currency)
                    lines.append(
                        f"{market} {symbol} "
                        f"수량={qty} "
                        f"매입={avg_text} "
                        f"현재={cur_text} "
                        f"손익={format_pct(pnl_pct)}"
                    )
                else:
                    lines.append(
                        f"{market} {symbol} "
                        f"수량={qty} "
                        f"평균단가={avg_text} "
                        f"(현재가 없음)"
                    )

            virtual_risk_lines = self._build_virtual_position_risk_lines(
                effective_positions,
                price_lookup,
                last_report=last_report,
            )
            if virtual_risk_lines:
                lines.append("─── 가상보유 리스크 ───")
                lines.extend(virtual_risk_lines)
            cleanup_lines = self._build_virtual_position_cleanup_lines(
                effective_positions,
                price_lookup,
                last_report=last_report,
            )
            if cleanup_lines:
                lines.append("─── 가상보유 정리 후보 ───")
                lines.extend(cleanup_lines)

        exposure_lines = self._build_virtual_exposure_lines(
            available_usd_override=virtual_exposure_available_usd
        )
        if exposure_lines:
            lines.append("─── 가상 노출 ───")
            lines.extend(exposure_lines)

        pending_sells = self.repository.list_virtual_sell_pending(market="overseas")
        lines.append("─── 정산 대기 매도 ───")
        if not pending_sells:
            lines.append("정산대기=없음")
        else:
            for row in pending_sells:
                market = format_market_korean(str(row.get("market", "overseas")))
                symbol = str(row.get("symbol", "-"))
                qty = int(row.get("qty", 0) or 0)
                avg_sell_price = float(row.get("avg_sell_price", 0.0) or 0.0)
                currency = str(row.get("currency", "USD"))
                lines.append(
                    f"{market} {symbol}(v) "
                    f"수량=-{qty} "
                    f"가상매도가={self._format_price(avg_sell_price, currency)}"
                )

        summary = manager.performance_summary()
        lines.append("─── 누적 성과 (virtual) ───")
        if not summary:
            lines.append("성과=없음")
        else:
            for key in sorted(summary):
                item = summary[key]
                market = format_market_korean(str(item.get("market", "overseas")))
                currency = str(item.get("currency", "USD"))
                trade_count = int(item.get("trade_count", 0) or 0)
                win_count = int(item.get("win_count", 0) or 0)
                total_pnl = float(item.get("total_pnl", 0.0) or 0.0)
                win_rate = (win_count / trade_count) if trade_count > 0 else 0.0
                pnl_text = self._format_price(total_pnl, currency)
                lines.append(
                    f"{market} 체결={trade_count} "
                    f"승률={format_pct(win_rate)} "
                    f"실현손익={pnl_text}"
                )

        return "\n".join(lines)

    def _build_real_position_risk_lines(
        self,
        real_positions: list[dict],
        *,
        last_report: dict,
    ) -> list[str]:
        if not real_positions:
            return []
        domestic_threshold = float(
            getattr(getattr(self.config, "auto_trade", None), "hard_stop_loss_pct", 0.01)
            or 0.01
        )
        overseas_threshold = float(
            getattr(getattr(self.config, "liquidity_lab", None), "overseas_stop_loss_pct", 0.01)
            or 0.01
        )
        risk_lines: list[str] = []
        for pos in real_positions:
            market_key = str(
                pos.get(
                    "market",
                    "domestic" if pos.get("stock_code") else "overseas",
                )
            )
            threshold = domestic_threshold if market_key == "domestic" else overseas_threshold
            pnl_pct = float(pos.get("pnl_pct", 0) or 0)
            if pnl_pct > -threshold:
                continue
            symbol = self._format_symbol_label(
                market_key,
                str(pos.get("symbol") or pos.get("stock_code") or "-"),
                last_report=last_report,
            )
            qty = int(pos.get("quantity", 0) or 0)
            market = format_market_korean(market_key)
            state = "감시중" if self.mode == "running" else "감시중지"
            risk_lines.append(
                f"{market} {symbol} 손익={format_pct(pnl_pct)} "
                f"기준={format_pct(-threshold)} 수량={qty} 상태={state}"
            )
        if risk_lines and self.mode != "running":
            risk_lines.append("주의=거래루프가 중지되어 자동 청산 감시가 동작하지 않습니다")
        return risk_lines

    def _build_virtual_position_risk_lines(
        self,
        effective_positions: list[dict[str, object]],
        price_lookup: dict[tuple[str, str], float],
        *,
        last_report: dict,
    ) -> list[str]:
        if not effective_positions:
            return []
        domestic_threshold = float(
            getattr(getattr(self.config, "auto_trade", None), "hard_stop_loss_pct", 0.01)
            or 0.01
        )
        overseas_threshold = float(
            getattr(getattr(self.config, "liquidity_lab", None), "overseas_stop_loss_pct", 0.01)
            or 0.01
        )
        risk_lines: list[str] = []
        for position in effective_positions:
            market_key = str(position["market"])
            symbol_raw = str(position["symbol"]).upper()
            avg_price = float(position["avg_price"])
            qty = int(position["qty"])
            current_price = float(price_lookup.get((market_key, symbol_raw), 0.0) or 0.0)
            threshold = domestic_threshold if market_key == "domestic" else overseas_threshold
            if avg_price <= 0 or current_price <= 0:
                continue
            pnl_pct = (current_price - avg_price) / avg_price
            if pnl_pct > -threshold:
                continue
            symbol = self._format_symbol_label(
                market_key,
                symbol_raw,
                last_report=last_report,
            )
            market = format_market_korean(market_key)
            state = "감시중" if self.mode == "running" else "감시중지"
            risk_lines.append(
                f"{market} {symbol} 손익={format_pct(pnl_pct)} "
                f"기준={format_pct(-threshold)} 수량={qty} 상태={state}"
            )
        if risk_lines and self.mode != "running":
            risk_lines.append("주의=거래루프가 중지되어 가상 포지션 청산 감시가 동작하지 않습니다")
        return risk_lines

    def _build_virtual_position_cleanup_lines(
        self,
        effective_positions: list[dict[str, object]],
        price_lookup: dict[tuple[str, str], float],
        *,
        last_report: dict,
    ) -> list[str]:
        max_overseas_positions = int(
            getattr(self.config.liquidity_lab, "max_concurrent_overseas_orders", 0) or 0
        )
        if max_overseas_positions <= 0:
            return []
        overseas_positions = [
            position
            for position in effective_positions
            if str(position.get("market")) == "overseas" and int(position.get("qty", 0) or 0) > 0
        ]
        excess_count = len(overseas_positions) - max_overseas_positions
        if excess_count <= 0:
            return []

        opened_lookup: dict[tuple[str, str], datetime] = {}
        for row in self.repository.list_virtual_positions():
            market = str(row.get("market", "overseas"))
            symbol = str(row.get("symbol", "")).strip().upper()
            parsed = parse_datetime(row.get("opened_at"))
            if market and symbol and parsed is not None:
                opened_lookup[(market, symbol)] = ensure_timezone(parsed)

        now = datetime.now(timezone.utc)
        candidates: list[dict[str, object]] = []
        for position in overseas_positions:
            market_key = str(position["market"])
            symbol_raw = str(position["symbol"]).strip().upper()
            qty = int(position["qty"])
            avg_price = float(position["avg_price"])
            currency = str(position["currency"])
            current_price = float(price_lookup.get((market_key, symbol_raw), 0.0) or 0.0)
            pnl_pct = (
                (current_price - avg_price) / avg_price
                if avg_price > 0 and current_price > 0
                else None
            )
            opened_at = opened_lookup.get((market_key, symbol_raw))
            age_hours = (
                max(0.0, (now - opened_at).total_seconds() / 3600)
                if opened_at is not None
                else 0.0
            )
            candidates.append(
                {
                    "market": market_key,
                    "symbol": symbol_raw,
                    "label": self._format_symbol_label(
                        market_key,
                        symbol_raw,
                        last_report=last_report,
                    ),
                    "qty": qty,
                    "currency": currency,
                    "notional": max(0.0, qty * avg_price),
                    "pnl_pct": pnl_pct,
                    "age_hours": age_hours,
                }
            )

        candidates.sort(
            key=lambda item: (
                float(item["pnl_pct"]) if item["pnl_pct"] is not None else 0.0,
                -float(item["age_hours"]),
                -float(item["notional"]),
            )
        )
        lines = [
            f"초과={len(overseas_positions)}/{max_overseas_positions} "
            f"정리필요={excess_count}종목",
        ]
        for item in candidates[: min(3, len(candidates))]:
            pnl_text = (
                format_pct(float(item["pnl_pct"]))
                if item["pnl_pct"] is not None
                else "현재가없음"
            )
            age_hours = float(item["age_hours"])
            age_text = f"{age_hours:.1f}h" if age_hours < 48 else f"{age_hours / 24:.1f}d"
            lines.append(
                f"{format_market_korean(str(item['market']))} {item['label']} "
                f"손익={pnl_text} "
                f"노출={self._format_notional_price(float(item['notional']), str(item['currency']))} "
                f"보유={age_text}"
            )
        return lines

    def _build_virtual_exposure_lines(
        self,
        *,
        available_usd_override: float | None = None,
    ) -> list[str]:
        rows = self.repository.list_virtual_positions()
        if not rows:
            return []
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

        if not by_market_currency:
            return []

        max_pct = float(
            getattr(self.config.liquidity_lab, "max_virtual_exposure_pct", 1.0) or 1.0
        )
        max_overseas_positions = int(
            getattr(self.config.liquidity_lab, "max_concurrent_overseas_orders", 0) or 0
        )
        lab = self.lab_service
        last_available_usd = available_usd_override
        if last_available_usd is None:
            last_available_usd = (
                None
                if lab is None
                else getattr(lab, "_last_overseas_available_usd", None)
            )
        lines: list[str] = []
        position_cap_exceeded = False
        for (market, currency), item in sorted(by_market_currency.items()):
            count = int(item["count"])
            notional = float(item["notional"])
            parts = [
                f"{format_market_korean(market)} 가상매수노출={self._format_notional_price(notional, currency)}",
                f"{count}종목",
            ]
            if market == "overseas" and currency == "USD":
                parts.append(f"한도=주문가능USD x{max_pct * 100:.0f}%")
                if last_available_usd is not None and float(last_available_usd) > 0:
                    limit = float(last_available_usd) * max_pct
                    status = "초과" if notional > limit else "정상"
                    parts.append(f"최근한도={self._format_notional_price(limit, currency)}")
                    parts.append(f"상태={status}")
                    if status == "초과" and self.mode != "running":
                        parts.append("감시=중지")
                if max_overseas_positions > 0:
                    cap_exceeded = count > max_overseas_positions
                    if cap_exceeded:
                        position_cap_exceeded = True
                    cap_status = "초과" if cap_exceeded else "정상"
                    parts.append(f"포지션한도={count}/{max_overseas_positions} {cap_status}")
                    if cap_exceeded and self.mode != "running":
                        parts.append("감시=중지")
            lines.append(" ".join(parts))
        if any("상태=초과 감시=중지" in line for line in lines):
            lines.append("주의=가상 노출 한도 초과 상태에서 거래루프가 중지되어 있습니다")
        if position_cap_exceeded and self.mode != "running":
            lines.append("주의=가상 포지션 한도 초과 상태에서 거래루프가 중지되어 있습니다")
            lines.append("조치=/lab_trim_virtual 초과분 정리 또는 /lab_start 재개")
        return lines

    async def _send_portfolio_message(self) -> None:
        live_real_positions = None
        live_virtual_prices: dict[tuple[str, str], float] = {}
        live_available_usd = None
        try:
            async with KisRestClient(self.config.credentials) as client:
                portfolio_lab = self._build_portfolio_lab_service(client)
                live_real_positions = await self._load_live_portfolio_positions(portfolio_lab)
                live_virtual_prices = await self._load_live_virtual_price_lookup(portfolio_lab)
                live_available_usd = await self._load_live_overseas_available_usd(
                    portfolio_lab,
                    real_positions=live_real_positions or [],
                    price_lookup=live_virtual_prices,
                )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("portfolio_live_refresh_failed error=%s", exc)
        await self.notifier.send(
            self._build_portfolio_message(
                real_positions_override=live_real_positions,
                price_lookup_override=live_virtual_prices,
                virtual_exposure_available_usd=live_available_usd,
            )
        )

    def _build_portfolio_lab_service(self, client: KisRestClient) -> LiquidityLabService:
        service = LiquidityLabService(self.config, client, self.repository, self.notifier)
        existing = self.lab_service
        if existing is not None:
            for attr in (
                "_dynamic_domestic_names",
                "_dynamic_overseas_pool",
                "_manual_overseas_pool",
                "_last_overseas_available_usd",
            ):
                if hasattr(existing, attr):
                    setattr(service, attr, getattr(existing, attr))
        return service

    async def _load_live_overseas_available_usd(
        self,
        lab: LiquidityLabService,
        *,
        real_positions: list[dict],
        price_lookup: dict[tuple[str, str], float],
    ) -> float | None:
        candidates: list[tuple[str, str, float]] = []
        for position in real_positions:
            if str(position.get("market", "")).lower() != "overseas":
                continue
            symbol = str(position.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            exchange_code = str(position.get("exchange_code") or "NASD").strip().upper()
            price = self._parse_float(position.get("current_price"))
            if price > 0:
                candidates.append((symbol, exchange_code, price))

        if not candidates:
            manager = VirtualTradeManager(self.repository)
            for position in manager.list_positions("overseas"):
                if int(position.qty) <= 0:
                    continue
                symbol = position.symbol.upper()
                price = price_lookup.get(("overseas", symbol), float(position.avg_price))
                if price > 0:
                    candidates.append((symbol, str(position.exchange_code or "NASD").upper(), price))
                if len(candidates) >= 3:
                    break

        for symbol, exchange_code, price in candidates[:3]:
            try:
                return await asyncio.wait_for(
                    lab._get_overseas_available_usd(
                        symbol=symbol,
                        exchange_code=exchange_code,
                        price=price,
                    ),
                    timeout=6.0,
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "portfolio_live_available_usd_failed symbol=%s error=%s",
                    symbol,
                    exc,
                )
        return None

    async def _load_live_virtual_price_lookup(
        self,
        lab: LiquidityLabService | None = None,
    ) -> dict[tuple[str, str], float]:
        lab = lab or self.lab_service
        if lab is None:
            return {}

        result: dict[tuple[str, str], float] = {}
        manager = VirtualTradeManager(self.repository)
        positions = [position for position in manager.list_positions("overseas") if position.qty > 0]

        async def fetch_price(position) -> tuple[tuple[str, str], float] | None:
            symbol = position.symbol.upper()
            exchange_code = str(position.exchange_code or "NASD").upper()
            try:
                quote = await asyncio.wait_for(
                    lab.client.get_overseas_price(symbol, exchange_code),
                    timeout=6.0,
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "portfolio_live_virtual_quote_failed symbol=%s error=%s",
                    symbol,
                    exc,
                )
                return None
            last_price = self._parse_float(quote.get("last_price"))
            if last_price <= 0:
                bid = self._parse_float(quote.get("bid"))
                ask = self._parse_float(quote.get("ask"))
                if bid > 0 and ask > 0:
                    last_price = (bid + ask) / 2.0
                else:
                    last_price = max(bid, ask)
            if last_price > 0:
                return ("overseas", symbol), last_price
            return None

        fetched = []
        limited_positions = positions[:25]
        batch_size = 2
        for start in range(0, len(limited_positions), batch_size):
            if start > 0:
                await asyncio.sleep(1.05)
            batch = limited_positions[start : start + batch_size]
            fetched.extend(await asyncio.gather(*(fetch_price(position) for position in batch)))
        for item in fetched:
            if item is None:
                continue
            key, price = item
            result[key] = price
        return result

    async def _load_live_portfolio_positions(
        self,
        lab: LiquidityLabService | None = None,
    ) -> list[dict] | None:
        lab = lab or self.lab_service
        if lab is None:
            return None

        positions: list[dict] = []
        loaded_any = False

        try:
            balance = await lab.client.get_balance()
            loaded_any = True
            for row in balance.get("positions", []) or []:
                qty = int(parse_kis_number(row.get("hldg_qty")))
                if qty <= 0:
                    continue
                stock_code = str(row.get("pdno", "")).strip()
                if not stock_code:
                    continue
                avg_price = self._parse_float(row.get("pchs_avg_pric"))
                current_price = (
                    self._parse_float(row.get("prpr"))
                    or self._parse_float(row.get("stck_prpr"))
                    or self._parse_float(row.get("now_pric"))
                    or self._parse_float(row.get("last_price"))
                    or avg_price
                )
                pnl_pct = (current_price - avg_price) / avg_price if avg_price > 0 else 0.0
                positions.append(
                    {
                        "market": "domestic",
                        "stock_code": stock_code,
                        "quantity": qty,
                        "orderable_qty": int(parse_kis_number(row.get("ord_psbl_qty")) or qty),
                        "avg_price": avg_price,
                        "current_price": current_price,
                        "pnl_pct": pnl_pct,
                        "currency": "KRW",
                    }
                )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("portfolio_live_domestic_balance_failed error=%s", exc)

        try:
            overseas_positions = await lab._load_overseas_positions([])
            loaded_any = True
            for position in overseas_positions:
                item = asdict(position)
                item["market"] = "overseas"
                item["currency"] = "USD"
                positions.append(item)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("portfolio_live_overseas_balance_failed error=%s", exc)

        if not loaded_any:
            return None
        return positions

    async def _send_recent_trade_log(self) -> None:
        started_at = (
            self.session_performance.started_at.isoformat()
            if getattr(self.session_performance, "started_at", None)
            else ""
        )
        await self.notifier.send(
            self._build_session_pnl_message(
                started_at=started_at,
                session_id=self.active_session_id,
            )
        )

    async def _send_performance_message(self, hours_text: str | None = None) -> None:
        await self.notifier.send(self._build_performance_message(hours_text))

    async def _send_report_message(self, report_args: str | None = None) -> None:
        await self.notifier.send(self._build_report_message(report_args))

    async def _send_guard_message(self) -> None:
        await self.notifier.send(self._build_guard_message())

    def _build_report_message(self, report_args: str | None = None) -> str:
        now = datetime.now(timezone.utc)
        args = str(report_args or "").strip().split()
        usage = "사용법=/lab_report compare 2026-07-10"
        if len(args) != 2 or args[0].lower() != "compare":
            return "\n".join(
                [
                    "[KIS][전략리포트]",
                    f"시각={format_kst_korean(now)}",
                    "실행실패=지원하지 않는 리포트 명령",
                    usage,
                ]
            )
        cutoff_date = args[1]
        try:
            comparison = compare_before_after(self.repository.db_path, cutoff_date)
        except Exception as exc:  # noqa: BLE001
            return "\n".join(
                [
                    "[KIS][전략리포트]",
                    f"시각={format_kst_korean(now)}",
                    "실행실패=전략 비교 생성 실패",
                    f"오류={str(exc)[:120]}",
                    usage,
                ]
            )
        return "\n".join(
            [
                "[KIS][전략리포트]",
                f"시각={format_kst_korean(now)}",
                "기준=실주문접수 SELL_REAL",
                "주의=net은 평균 손익률에서 0.5% 비용을 차감한 추정치",
                comparison,
            ]
        )

    def _build_guard_message(self) -> str:
        now = datetime.now(timezone.utc)
        config = getattr(self.config, "liquidity_lab", object())
        auto_trade = getattr(self.config, "auto_trade", object())
        enabled = bool(getattr(config, "strategy_guard_enabled", False))
        lookback_hours = max(1, int(getattr(config, "strategy_guard_lookback_hours", 48) or 48))
        min_trades = max(1, int(getattr(config, "strategy_guard_min_trades", 3) or 3))
        max_avg_net = float(getattr(config, "strategy_guard_max_avg_net_pnl_pct", -0.003) or -0.003)
        guard_markets = {
            str(market).strip().lower()
            for market in getattr(config, "strategy_guard_markets", ["overseas"])
            if str(market).strip()
        }
        guard_flags = {
            str(flag).strip().upper()
            for flag in getattr(config, "strategy_guard_strategy_flags", ["VWAP", "RSI", "VOL"])
            if str(flag).strip()
        }
        cost_pct = max(
            0.005,
            float(getattr(auto_trade, "overseas_commission_rate", 0.0025) or 0.0025) * 2,
        )
        after_logged_at = (now - timedelta(hours=lookback_hours)).isoformat()
        lines = [
            "[KIS][전략가드]",
            f"시각={format_kst_korean(now)}",
            f"상태={'활성' if enabled else '비활성'}",
            f"범위=최근 {lookback_hours}시간",
            (
                f"차단조건={min_trades}건 이상, 평균순손익 "
                f"{format_pct(max_avg_net)} 이하"
            ),
            f"감시대상={','.join(sorted(guard_markets))}:{','.join(sorted(guard_flags))}",
            "주의=실주문접수 SELL_REAL 기준, 체결확정은 /lab_orders 확인",
        ]
        hard_blocks: list[str] = []
        if bool(getattr(config, "overseas_block_standalone_vwap", False)):
            hard_blocks.append("해외 VWAP단독")
        if bool(getattr(config, "overseas_block_standalone_rsi", False)):
            hard_blocks.append("해외 RSI단독")
        if bool(getattr(config, "overseas_block_standalone_vol", False)):
            hard_blocks.append("해외 VOL단독")
        if hard_blocks:
            lines.insert(6, f"고정차단={','.join(hard_blocks)}")
        if not enabled:
            return "\n".join(lines)
        if not hasattr(self.repository, "get_recent_strategy_guard_performance"):
            lines.append("성과=조회불가")
            return "\n".join(lines)

        rows = self.repository.get_recent_strategy_guard_performance(
            after_logged_at=after_logged_at,
            cost_pct=cost_pct,
        )
        if not rows:
            lines.append("성과=없음")
            return "\n".join(lines)

        for row in rows[:10]:
            market = str(row.get("market") or "").strip().lower()
            strategy = str(row.get("strategy_flag") or "").strip().upper()
            trade_count = int(row.get("trade_count") or 0)
            win_count = int(row.get("win_count") or 0)
            avg_net = float(row.get("avg_net_pnl_pct") or 0.0)
            win_rate = (win_count / trade_count) if trade_count else 0.0
            monitored = (not guard_markets or market in guard_markets) and (
                not guard_flags or strategy in guard_flags
            )
            blocked = monitored and trade_count >= min_trades and avg_net <= max_avg_net
            if blocked:
                state = "차단"
            elif monitored:
                state = "감시"
            else:
                state = "참고"
            lines.append(
                f"{format_market_korean(market)} {strategy or '-'} "
                f"상태={state} {trade_count}건 승률={win_rate * 100:.0f}% "
                f"평균순={format_pct(avg_net)}"
            )
        return "\n".join(lines)

    @staticmethod
    def _parse_performance_hours(hours_text: str | None) -> int:
        try:
            hours = int(float(str(hours_text or "24").strip()))
        except (TypeError, ValueError):
            hours = 24
        return min(max(hours, 1), 720)

    @staticmethod
    def _format_mixed_pnl(*, usd: float, krw: float) -> str:
        parts: list[str] = []
        if abs(usd) > 1e-9:
            parts.append(format_usd(usd))
        if abs(krw) > 0.5:
            parts.append(format_krw(krw))
        return "/".join(parts) if parts else "0"

    @staticmethod
    def _performance_row_score(row: dict) -> tuple[float, float]:
        return (
            float(row.get("total_net_pnl_krw") or 0.0),
            float(row.get("total_net_pnl_usd") or 0.0),
        )

    def _format_performance_row(self, row: dict) -> str:
        market = format_market_korean(str(row.get("market") or "-"))
        strategy = str(row.get("strategy_flag") or "-")
        entry_by = str(row.get("entry_by") or "-")
        exit_by = str(row.get("exit_by") or "-")
        trade_count = int(row.get("trade_count") or 0)
        win_rate = float(row.get("win_rate") or 0.0)
        avg_pnl = float(row.get("avg_pnl_pct") or 0.0)
        pnl_label = self._format_mixed_pnl(
            usd=float(row.get("total_net_pnl_usd") or 0.0),
            krw=float(row.get("total_net_pnl_krw") or 0.0),
        )
        return (
            f"{market} {strategy} "
            f"진입={entry_by} 청산={format_reason_korean(exit_by)} "
            f"{trade_count}건 승률={win_rate * 100:.0f}% "
            f"평균={format_pct(avg_pnl)} 손익={pnl_label}"
        )

    def _build_performance_message(self, hours_text: str | None = None) -> str:
        hours = self._parse_performance_hours(hours_text)
        now = datetime.now(timezone.utc)
        after_logged_at = (now - timedelta(hours=hours)).isoformat()
        rows = self.repository.get_realized_strategy_performance(
            after_logged_at=after_logged_at,
            limit=200,
        )
        lines = [
            "[KIS][전략성과]",
            f"시각={format_kst_korean(now)}",
            f"범위=최근 {hours}시간",
            "기준=실주문접수 SELL_REAL만 집계",
            "제외=감시 신호 BUY/SELL/HOLD",
            "주의=체결확정은 MTS/잔고 기준 확인",
        ]
        if not rows:
            lines.append("성과=없음")
            return "\n".join(lines)

        total_trades = sum(int(row.get("trade_count") or 0) for row in rows)
        total_wins = sum(int(row.get("win_count") or 0) for row in rows)
        total_usd = sum(float(row.get("total_net_pnl_usd") or 0.0) for row in rows)
        total_krw = sum(float(row.get("total_net_pnl_krw") or 0.0) for row in rows)
        total_win_rate = (total_wins / total_trades) if total_trades else 0.0
        lines.append(
            "전체="
            f"{total_trades}건 승률={total_win_rate * 100:.0f}% "
            f"손익={self._format_mixed_pnl(usd=total_usd, krw=total_krw)}"
        )
        best_rows = rows[:5]
        worst_rows = sorted(rows, key=self._performance_row_score)[:5]
        lines.append("─── 상위 전략 ───")
        for row in best_rows:
            lines.append(self._format_performance_row(row))
        lines.append("─── 하위 전략 ───")
        for row in worst_rows:
            lines.append(self._format_performance_row(row))
        return "\n".join(lines)

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

    async def _send_cancel_stale_domestic_prompt(self) -> None:
        try:
            live_open_orders = await self._load_live_open_domestic_orders()
        except Exception as exc:  # noqa: BLE001
            await self.notifier.send(
                "\n".join(
                    [
                        "[KIS][국내미체결취소]",
                        f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                        "상태=조회실패",
                        f"사유={str(exc)[:120]}",
                    ]
                )
            )
            return

        stale_orders = self._filter_stale_live_open_orders(live_open_orders)
        lines = [
            "[KIS][국내미체결취소]",
            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
            "동작=확인",
        ]
        if not stale_orders:
            lines.append("대상=없음 (30분 이상 국내 미체결 없음)")
            await self.notifier.send("\n".join(lines))
            return
        lines.append(f"대상={len(stale_orders)}건")
        for row in stale_orders[:8]:
            lines.append(self._format_live_open_domestic_order_line(row))
        if len(stale_orders) > 8:
            lines.append(f"외 {len(stale_orders) - 8}건")
        lines.extend(
            [
                "주의=확정 명령을 보내면 위 국내 미체결 주문을 KIS에 취소 요청합니다.",
                "실행=/lab_cancel_stale_domestic_confirm",
            ]
        )
        await self.notifier.send("\n".join(lines))

    async def _execute_cancel_stale_domestic_orders(
        self,
        *,
        source: str = "manual",
        candidate_orders: list[dict] | None = None,
        now: datetime | None = None,
    ) -> None:
        try:
            live_open_orders = (
                candidate_orders
                if candidate_orders is not None
                else await self._load_live_open_domestic_orders()
            )
        except Exception as exc:  # noqa: BLE001
            await self.notifier.send(
                "\n".join(
                    [
                        "[KIS][국내미체결취소]",
                        f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                        "상태=조회실패",
                        f"사유={str(exc)[:120]}",
                    ]
                )
            )
            return

        stale_orders = self._filter_stale_live_open_orders(live_open_orders)
        if not stale_orders:
            await self.notifier.send(
                "\n".join(
                    [
                        "[KIS][국내미체결취소]",
                        f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                        "상태=취소대상없음",
                    ]
                )
            )
            return

        current = now or datetime.now(timezone.utc)
        if not is_krx_regular_session(current) or is_krx_holiday(current.astimezone(KST).date()):
            await self.notifier.send(
                "\n".join(
                    [
                        "[KIS][국내미체결취소]",
                        f"시각={format_kst_korean(current)}",
                        "상태=장외취소보류",
                        f"대상={len(stale_orders)}건",
                        "안내=국내 정규장 중에 /lab_cancel_stale_domestic_confirm 재시도",
                    ]
                )
            )
            self.repository.save_event(
                event_type="maintenance_skip",
                market="domestic",
                symbol="",
                detail={
                    "reason": "domestic_cancel_outside_regular_session",
                    "stale_order_count": len(stale_orders),
                    "source": source,
                },
                cycle_no=getattr(self, "current_cycle_no", 0),
                session_id=getattr(self, "active_session_id", ""),
            )
            return

        lines = [
            "[KIS][국내미체결취소]",
            f"시각={format_kst_korean(current)}",
            f"동작={'자동취소' if source == 'auto' else '확정취소'}",
            f"요청={len(stale_orders)}건",
        ]
        async with KisRestClient(self.config.credentials) as client:
            for row in stale_orders[:10]:
                symbol = str(row.get("symbol") or row.get("pdno") or "").strip().upper()
                order_no = str(row.get("order_no") or row.get("odno") or "").strip()
                orgno = str(row.get("ord_gno_brno") or row.get("krx_fwdg_ord_orgno") or "").strip()
                order_division = str(row.get("ord_dvsn_cd") or "00").strip() or "00"
                exchange_code = str(
                    row.get("excg_id_dvsn_cd")
                    or row.get("excg_id_dvsn_Cd")
                    or row.get("EXCG_ID_DVSN_CD")
                    or "KRX"
                ).strip() or "KRX"
                open_qty = int(row.get("open_qty") or parse_kis_number(row.get("rmn_qty")))
                price = int(round(float(row.get("order_price") or self._parse_float(row.get("ord_unpr")))))
                side = self._domestic_order_side(row)
                if not symbol or not order_no or not orgno:
                    lines.append(f"{symbol or '-'} 취소실패=필수 주문정보 부족")
                    continue
                try:
                    response = await client.revise_or_cancel_domestic_order(
                        krx_order_orgno=orgno,
                        original_order_no=order_no,
                        order_division=order_division,
                        rvse_cncl_dvsn_cd="02",
                        qty=0,
                        price=0,
                        qty_all_order_yn="Y",
                        exchange_code=exchange_code,
                    )
                except Exception as exc:  # noqa: BLE001
                    error_text = str(exc)[:80]
                    if "장종료" in error_text:
                        error_text = "장종료(국내장중 재시도 필요)"
                    self.repository.save_broker_order_event(
                        created_at=datetime.now(timezone.utc).isoformat(),
                        market="domestic",
                        symbol=symbol,
                        exchange_code=exchange_code,
                        side=side,
                        order_kind="cancel",
                        requested_qty=open_qty,
                        requested_price=price,
                        status="REJECTED",
                        reason="stale_live_order_cancel_failed",
                        broker_order_no=order_no,
                        is_virtual=0,
                        payload={
                            "original_order_no": order_no,
                            "original_order_orgno": orgno,
                            "order_division": order_division,
                            "original_order_price": price,
                            "reference_price": price,
                            "open_qty": open_qty,
                            "error": str(exc),
                        },
                    )
                    lines.append(f"{symbol} 취소실패={error_text}")
                    continue

                output = response.get("output") if isinstance(response, dict) else {}
                if not isinstance(output, dict):
                    output = {}
                cancel_order_no = str(output.get("ODNO") or output.get("odno") or order_no).strip()
                self.repository.save_broker_order_event(
                    created_at=datetime.now(timezone.utc).isoformat(),
                    market="domestic",
                    symbol=symbol,
                    exchange_code=exchange_code,
                    side=side,
                    order_kind="cancel",
                    requested_qty=open_qty,
                    requested_price=price,
                    status="CANCELED",
                    reason="stale_live_order_cancel",
                    broker_order_no=cancel_order_no,
                    is_virtual=0,
                    payload={
                        "original_order_no": order_no,
                        "original_order_orgno": orgno,
                        "order_division": order_division,
                        "original_order_price": price,
                        "reference_price": price,
                        "open_qty": open_qty,
                        "response": response,
                    },
                )
                name = str(row.get("name") or row.get("prdt_name") or "").strip()
                symbol_text = f"{symbol}({name})" if name else symbol
                lines.append(f"{symbol_text} 취소요청 x{open_qty} 원주문={order_no} 취소주문={cancel_order_no}")
        await self.notifier.send("\n".join(lines))

    async def _send_cancel_stale_overseas_prompt(self) -> None:
        try:
            live_open_orders = await self._load_live_open_overseas_orders()
        except Exception as exc:  # noqa: BLE001
            await self.notifier.send(
                "\n".join(
                    [
                        "[KIS][해외미체결취소]",
                        f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                        "상태=조회실패",
                        f"사유={str(exc)[:120]}",
                    ]
                )
            )
            return

        stale_orders = self._filter_stale_live_open_orders(live_open_orders)
        lines = [
            "[KIS][해외미체결취소]",
            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
            "동작=확인",
        ]
        if not stale_orders:
            lines.append("대상=없음 (30분 이상 해외 미체결 없음)")
            await self.notifier.send("\n".join(lines))
            return
        lines.append(f"대상={len(stale_orders)}건")
        for row in stale_orders[:8]:
            lines.append(self._format_live_open_overseas_order_line(row))
        if len(stale_orders) > 8:
            lines.append(f"외 {len(stale_orders) - 8}건")
        lines.extend(
            [
                "주의=확정 명령을 보내면 위 해외 미체결 주문을 KIS에 취소 요청합니다.",
                "실행=/lab_cancel_stale_overseas_confirm",
            ]
        )
        await self.notifier.send("\n".join(lines))

    async def _maybe_auto_cancel_stale_domestic_orders(
        self,
        *,
        now: datetime | None = None,
    ) -> bool:
        current = now or datetime.now(timezone.utc)
        if not is_krx_regular_session(current) or is_krx_holiday(current.astimezone(KST).date()):
            return False
        last_run = self._last_auto_stale_domestic_cancel_at
        if last_run is not None:
            elapsed_min = (current - last_run).total_seconds() / 60
            if elapsed_min < 10:
                return False
        self._last_auto_stale_domestic_cancel_at = current
        try:
            live_open_orders = await self._load_live_open_domestic_orders()
        except Exception as exc:  # noqa: BLE001
            self.repository.save_event(
                event_type="maintenance_skip",
                market="domestic",
                symbol="",
                detail={
                    "reason": "auto_stale_domestic_cancel_lookup_failed",
                    "error": str(exc)[:120],
                },
                cycle_no=self.current_cycle_no,
                session_id=self.active_session_id,
            )
            return False

        stale_orders = self._filter_stale_live_open_orders(live_open_orders, now=current)
        bot_owned_stale_orders = self._filter_bot_submitted_domestic_orders(stale_orders)
        if not bot_owned_stale_orders:
            return False
        await self._execute_cancel_stale_domestic_orders(
            source="auto",
            candidate_orders=bot_owned_stale_orders,
            now=current,
        )
        return True

    def _filter_bot_submitted_domestic_orders(self, rows: list[dict]) -> list[dict]:
        if not rows:
            return []
        submitted_order_numbers = {
            str(event.get("broker_order_no", "") or "").strip()
            for event in self.repository.list_broker_order_events(limit=500)
            if str(event.get("market", "") or "").lower() == "domestic"
            and str(event.get("status", "") or "").upper() == "SUBMITTED"
            and str(event.get("order_kind", "") or "").lower() != "cancel"
        }
        if not submitted_order_numbers:
            return []
        result: list[dict] = []
        for row in rows:
            order_no = str(row.get("order_no") or row.get("odno") or "").strip()
            if order_no and order_no in submitted_order_numbers:
                result.append(row)
        return result

    async def _maybe_auto_cancel_stale_overseas_orders(
        self,
        *,
        now: datetime | None = None,
    ) -> bool:
        current = now or datetime.now(timezone.utc)
        env = str(getattr(self.config.credentials, "env", "vps") or "vps")
        if (
            not is_us_orderable_session_for_env(current, env)
            or is_nyse_holiday(us_holiday_date_for_kis_session(current))
        ):
            return False
        last_run = self._last_auto_stale_overseas_cancel_at
        if last_run is not None:
            elapsed_min = (current - last_run).total_seconds() / 60
            if elapsed_min < 10:
                return False
        self._last_auto_stale_overseas_cancel_at = current
        try:
            live_open_orders = await self._load_live_open_overseas_orders()
        except Exception as exc:  # noqa: BLE001
            self.repository.save_event(
                event_type="maintenance_skip",
                market="overseas",
                symbol="",
                detail={
                    "reason": "auto_stale_overseas_cancel_lookup_failed",
                    "error": str(exc)[:120],
                },
                cycle_no=self.current_cycle_no,
                session_id=self.active_session_id,
            )
            return False

        stale_orders = self._filter_stale_live_open_orders(live_open_orders, now=current)
        bot_owned_stale_orders = self._filter_bot_submitted_overseas_orders(stale_orders)
        if not bot_owned_stale_orders:
            return False
        await self._execute_cancel_stale_overseas_orders(
            source="auto",
            candidate_orders=bot_owned_stale_orders,
        )
        return True

    def _filter_bot_submitted_overseas_orders(self, rows: list[dict]) -> list[dict]:
        if not rows:
            return []
        submitted_events: dict[str, dict] = {}
        for event in self.repository.list_broker_order_events(limit=500):
            order_no = str(event.get("broker_order_no", "") or "").strip()
            if (
                order_no
                and str(event.get("market", "") or "").lower() == "overseas"
                and str(event.get("status", "") or "").upper() == "SUBMITTED"
                and str(event.get("order_kind", "") or "").lower() != "cancel"
            ):
                submitted_events[order_no] = event
        if not submitted_events:
            return []
        result: list[dict] = []
        for row in rows:
            order_no = str(row.get("order_no") or row.get("odno") or "").strip()
            event = submitted_events.get(order_no)
            if event is None:
                continue
            item = dict(row)
            if not str(item.get("exchange_code") or "").strip():
                item["exchange_code"] = str(event.get("exchange_code") or "NASD").strip().upper()
            if not str(item.get("side") or "").strip():
                item["side"] = str(event.get("side") or "").strip().upper()
            result.append(item)
        return result

    async def _execute_cancel_stale_overseas_orders(
        self,
        *,
        source: str = "auto",
        candidate_orders: list[dict] | None = None,
    ) -> None:
        try:
            live_open_orders = (
                candidate_orders
                if candidate_orders is not None
                else await self._load_live_open_overseas_orders()
            )
        except Exception as exc:  # noqa: BLE001
            if source != "auto":
                await self.notifier.send(
                    "\n".join(
                        [
                            "[KIS][해외미체결취소]",
                            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                            "상태=조회실패",
                            f"사유={str(exc)[:120]}",
                        ]
                    )
                )
            return
        stale_orders = self._filter_stale_live_open_orders(live_open_orders)
        if not stale_orders:
            if source != "auto":
                await self.notifier.send(
                    "\n".join(
                        [
                            "[KIS][해외미체결취소]",
                            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
                            "대상=없음 (30분 이상 해외 미체결 없음)",
                        ]
                    )
                )
            return

        lines = [
            "[KIS][해외미체결취소]",
            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
            f"동작={'자동취소' if source == 'auto' else '확정취소'}",
            f"요청={len(stale_orders)}건",
        ]
        async with KisRestClient(self.config.credentials) as client:
            lab = LiquidityLabService(self.config, client, self.repository, self.notifier)
            for row in stale_orders[:10]:
                symbol = str(row.get("symbol") or row.get("pdno") or row.get("ovrs_pdno") or "").strip().upper()
                exchange_code = str(
                    row.get("exchange_code")
                    or row.get("ovrs_excg_cd")
                    or "NASD"
                ).strip().upper()
                order_no = str(row.get("order_no") or row.get("odno") or "").strip()
                open_qty = int(row.get("open_qty") or parse_kis_number(row.get("nccs_qty")))
                price = self._parse_float(row.get("order_price") or row.get("ft_ord_unpr3"))
                side = self._overseas_order_side(row)
                if not symbol or not order_no or open_qty <= 0:
                    lines.append(f"{symbol or '-'} 취소실패=필수 주문정보 부족")
                    continue
                try:
                    response = await lab._cancel_open_overseas_order(
                        symbol=symbol,
                        exchange_code=exchange_code,
                        pending_order={**row, "order_no": order_no, "open_qty": open_qty},
                    )
                except Exception as exc:  # noqa: BLE001
                    self.repository.save_broker_order_event(
                        created_at=datetime.now(timezone.utc).isoformat(),
                        market="overseas",
                        symbol=symbol,
                        exchange_code=exchange_code,
                        side=side,
                        order_kind="cancel",
                        requested_qty=open_qty,
                        requested_price=price,
                        status="REJECTED",
                        reason="stale_live_overseas_order_cancel_failed",
                        broker_order_no=order_no,
                        is_virtual=0,
                        payload={
                            "original_order_no": order_no,
                            "order_division": str(row.get("ord_dvsn_cd") or "00").strip() or "00",
                            "original_order_price": price,
                            "reference_price": price,
                            "open_qty": open_qty,
                            "error": str(exc),
                        },
                    )
                    lines.append(f"{symbol} 취소실패={str(exc)[:80]}")
                    continue

                output = response.get("output") if isinstance(response, dict) else {}
                if not isinstance(output, dict):
                    output = {}
                cancel_order_no = str(output.get("ODNO") or output.get("odno") or order_no).strip()
                self.repository.save_broker_order_event(
                    created_at=datetime.now(timezone.utc).isoformat(),
                    market="overseas",
                    symbol=symbol,
                    exchange_code=exchange_code,
                    side=side,
                    order_kind="cancel",
                    requested_qty=open_qty,
                    requested_price=price,
                    status="CANCELED",
                    reason="stale_live_overseas_order_cancel",
                    broker_order_no=cancel_order_no,
                    is_virtual=0,
                    payload={
                        "original_order_no": order_no,
                        "order_division": str(row.get("ord_dvsn_cd") or "00").strip() or "00",
                        "original_order_price": price,
                        "reference_price": price,
                        "open_qty": open_qty,
                        "response": response,
                    },
                )
                lines.append(f"{symbol} 취소요청 x{open_qty} 원주문={order_no} 취소주문={cancel_order_no}")
        await self.notifier.send("\n".join(lines))

    def _build_recent_order_events_message(
        self,
        *,
        limit: int = 12,
        live_open_domestic_orders: list[dict] | None = None,
        live_open_domestic_error: str = "",
        live_open_orders: list[dict] | None = None,
        live_open_error: str = "",
    ) -> str:
        rows = self.repository.list_broker_order_events(limit=limit)
        audit_rows = self.repository.list_submitted_order_audit_rows(limit=5, source_limit=500)
        live_open_order_keys: set[tuple[str, str]] = set()
        live_checked_markets: set[str] = set()
        if live_open_domestic_orders is not None and not live_open_domestic_error:
            live_checked_markets.add("domestic")
            live_open_order_keys.update(
                self._live_open_order_keys("domestic", live_open_domestic_orders)
            )
        if live_open_orders is not None and not live_open_error:
            live_checked_markets.add("overseas")
            live_open_order_keys.update(
                self._live_open_order_keys("overseas", live_open_orders)
            )
        lines = [
            "[KIS][주문기록]",
            f"시각={format_kst_korean(datetime.now(timezone.utc))}",
            "기준=주문 접수/취소/가상기록 (체결확정 아님)",
        ]
        if live_open_domestic_orders is not None or live_open_domestic_error:
            lines.append("─── live 국내 미체결 ───")
            if live_open_domestic_error:
                lines.append(f"조회실패={live_open_domestic_error[:80]}")
            elif not live_open_domestic_orders:
                lines.append("미체결=없음")
            else:
                for row in live_open_domestic_orders[:8]:
                    lines.append(self._format_live_open_domestic_order_line(row))
        if live_open_orders is not None or live_open_error:
            lines.append("─── live 해외 미체결 ───")
            if live_open_error:
                lines.append(f"조회실패={live_open_error[:80]}")
            elif not live_open_orders:
                lines.append("미체결=없음")
            else:
                for row in live_open_orders[:8]:
                    lines.append(self._format_live_open_overseas_order_line(row))
        if audit_rows:
            lines.append("─── 접수 후 체결확정 추적 필요 ───")
            lines.append("기준=실주문 SUBMITTED, DB상 체결확정 이벤트 없음")
            for row in audit_rows:
                lines.append(
                    self._format_submitted_order_audit_line(
                        row,
                        live_open_order_keys=live_open_order_keys,
                        live_checked_markets=live_checked_markets,
                    )
                )
        if rows:
            lines.append("─── 내부 주문 이벤트 ───")
        if not rows:
            lines.append("주문기록=없음")
            return "\n".join(lines)

        for row in rows:
            created_at = parse_datetime(row.get("created_at"))
            time_text = format_kst_korean(created_at) if created_at else "-"
            market = str(row.get("market", "overseas"))
            symbol = str(row.get("symbol", "-")).upper()
            side = str(row.get("side", "")).upper()
            status = str(row.get("status", "") or "-").upper()
            action = self._format_order_event_action(row)
            qty = int(row.get("requested_qty", 0) or 0)
            price = float(row.get("requested_price", 0.0) or 0.0)
            currency = "KRW" if market == "domestic" else "USD"
            price_text = "-" if price <= 0 else self._format_price(price, currency)
            reason = format_reason_korean(str(row.get("reason", "") or "-"))
            order_no = str(row.get("broker_order_no", "") or "").strip()
            virtual_note = " virtual" if int(row.get("is_virtual", 0) or 0) else ""
            parts = [
                f"{time_text} {format_market_korean(market)} {symbol}{virtual_note}",
                action,
                price_text,
                f"x{qty}",
                f"상태={status}",
                f"사유={reason}",
            ]
            if order_no:
                parts.append(f"주문번호={order_no}")
            payload = row.get("payload_json") or {}
            if status == "REJECTED" and isinstance(payload, dict):
                error_text = str(payload.get("error") or "").strip()
                if error_text:
                    parts.append(f"오류={error_text[:80]}")
            if side and side not in {"BUY", "SELL"}:
                parts.append(f"원시구분={side}")
            lines.append(" ".join(parts))
        return "\n".join(lines)

    @staticmethod
    def _live_open_order_keys(market: str, rows: list[dict]) -> set[tuple[str, str]]:
        result: set[tuple[str, str]] = set()
        for row in rows:
            order_no = str(row.get("order_no") or row.get("odno") or "").strip()
            if order_no:
                result.add((market, order_no))
        return result

    async def _load_live_open_domestic_orders(self, *, limit: int = 12) -> list[dict]:
        now_kst = datetime.now(timezone.utc).astimezone(KST)
        trade_date = now_kst.strftime("%Y%m%d")
        async with KisRestClient(self.config.credentials) as client:
            history = await client.get_domestic_order_history(
                symbol="",
                start_date=trade_date,
                end_date=trade_date,
                side_filter="00",
                fill_filter="02",
                query_order="00",
                query_type="00",
                exchange_code="KRX",
            )
        return self._parse_live_open_domestic_order_rows(history.get("orders", []), limit=limit)

    def _parse_live_open_domestic_order_rows(self, rows: list[dict], *, limit: int = 12) -> list[dict]:
        parsed: list[dict] = []
        for row in rows:
            open_qty = parse_kis_number(row.get("rmn_qty"))
            if open_qty <= 0:
                order_qty = parse_kis_number(row.get("ord_qty"))
                filled_qty = parse_kis_number(row.get("tot_ccld_qty"))
                canceled_qty = parse_kis_number(row.get("cncl_cfrm_qty"))
                rejected_qty = parse_kis_number(row.get("rjct_qty"))
                open_qty = max(0, order_qty - filled_qty - canceled_qty - rejected_qty)
            if open_qty <= 0:
                continue
            if str(row.get("cncl_yn", "") or "").strip().upper() == "Y":
                continue
            item = dict(row)
            item["open_qty"] = open_qty
            item["symbol"] = str(row.get("pdno") or "").strip().upper()
            item["name"] = str(row.get("prdt_name") or "").strip()
            item["order_no"] = str(row.get("odno") or "").strip()
            item["order_price"] = self._parse_float(row.get("ord_unpr"))
            item["created_at"] = self._parse_domestic_order_history_timestamp(row)
            parsed.append(item)
        parsed.sort(
            key=lambda item: item.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return parsed[:limit]

    @staticmethod
    def _parse_domestic_order_history_timestamp(row: dict) -> datetime | None:
        ord_dt = str(row.get("ord_dt") or "").strip()
        ord_tmd = str(row.get("ord_tmd") or "").strip()
        if not ord_dt or not ord_tmd:
            return None
        ord_tmd = ord_tmd.zfill(6)[:6]
        try:
            parsed = datetime.strptime(f"{ord_dt}{ord_tmd}", "%Y%m%d%H%M%S")
        except ValueError:
            return None
        return parsed.replace(tzinfo=KST).astimezone(timezone.utc)

    def _format_live_open_domestic_order_line(
        self,
        row: dict,
        *,
        now: datetime | None = None,
    ) -> str:
        created_at = row.get("created_at")
        time_text = format_kst_korean(created_at) if isinstance(created_at, datetime) else "-"
        symbol = str(row.get("symbol") or row.get("pdno") or "-").upper()
        name = str(row.get("name") or row.get("prdt_name") or "").strip()
        symbol_text = f"{symbol}({name})" if name else symbol
        side_code = str(row.get("sll_buy_dvsn_cd") or "").strip()
        side_name = str(row.get("sll_buy_dvsn_cd_name") or "").strip()
        if side_code == "01" or side_name == "매도":
            side_text = "매도미체결"
        elif side_code == "02" or side_name == "매수":
            side_text = "매수미체결"
        else:
            side_text = "미체결"
        qty = int(row.get("open_qty") or parse_kis_number(row.get("rmn_qty")))
        price = self._parse_float(row.get("order_price") or row.get("ord_unpr"))
        price_text = "-" if price <= 0 else self._format_price(price, "KRW")
        order_no = str(row.get("order_no") or row.get("odno") or "").strip()
        parts = [
            f"{time_text} 국내 {symbol_text}",
            side_text,
            price_text,
            f"x{qty}",
        ]
        if order_no:
            parts.append(f"주문번호={order_no}")
        current = now or datetime.now(timezone.utc)
        age_parts = self._format_open_order_age_parts(created_at, now=current)
        parts.extend(age_parts)
        if "주의=장기미체결" in age_parts and not is_krx_regular_session(current):
            parts.append("취소가능=국내장중")
        return " ".join(parts)

    async def _load_live_open_overseas_orders(self, *, limit: int = 12) -> list[dict]:
        now_kst = datetime.now(timezone.utc).astimezone(KST)
        start_date = (now_kst - timedelta(days=1)).strftime("%Y%m%d")
        end_date = now_kst.strftime("%Y%m%d")
        env = str(getattr(self.config.credentials, "env", "vps") or "vps")
        async with KisRestClient(self.config.credentials) as client:
            if env != "prod":
                history = await client.get_overseas_order_history(
                    symbol="",
                    start_date=start_date,
                    end_date=end_date,
                    side_filter="00",
                    fill_filter="00",
                    exchange_code="",
                    sort_sqn="DS",
                )
                return self._parse_live_open_overseas_order_rows(history.get("orders", []), limit=limit)

            service = LiquidityLabService(self.config, client, self.repository, self.notifier)
            results: list[dict] = []
            seen: set[tuple[str, str]] = set()
            for event in self.repository.list_broker_order_events(limit=200):
                if str(event.get("market", "")).lower() != "overseas":
                    continue
                if int(event.get("is_virtual", 0) or 0):
                    continue
                symbol = str(event.get("symbol", "") or "").strip().upper()
                exchange_code = str(event.get("exchange_code") or "NASD").strip().upper()
                key = (symbol, exchange_code)
                if not symbol or key in seen:
                    continue
                seen.add(key)
                results.extend(
                    await service._list_open_overseas_orders(
                        symbol=symbol,
                        exchange_code=exchange_code,
                    )
                )
                if len(seen) >= 10 or len(results) >= limit:
                    break
            return sorted(
                results,
                key=lambda item: item.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )[:limit]

    def _parse_live_open_overseas_order_rows(self, rows: list[dict], *, limit: int = 12) -> list[dict]:
        parsed: list[dict] = []
        for row in rows:
            open_qty = parse_kis_number(row.get("nccs_qty"))
            if open_qty <= 0:
                continue
            item = dict(row)
            item["open_qty"] = open_qty
            item["symbol"] = str(row.get("pdno") or row.get("ovrs_pdno") or "").strip().upper()
            item["order_no"] = str(row.get("odno") or "").strip()
            item["order_price"] = self._parse_float(row.get("ft_ord_unpr3"))
            item["created_at"] = LiquidityLabService._parse_overseas_order_history_timestamp(row)
            parsed.append(item)
        parsed.sort(
            key=lambda item: item.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return parsed[:limit]

    def _format_live_open_overseas_order_line(self, row: dict) -> str:
        created_at = row.get("created_at")
        time_text = format_kst_korean(created_at) if isinstance(created_at, datetime) else "-"
        symbol = str(row.get("symbol") or row.get("pdno") or row.get("ovrs_pdno") or "-").upper()
        side_code = str(row.get("sll_buy_dvsn_cd") or "").strip()
        side_text = "매도미체결" if side_code == "01" else "매수미체결" if side_code == "02" else "미체결"
        qty = int(row.get("open_qty") or parse_kis_number(row.get("nccs_qty")))
        price = self._parse_float(row.get("order_price") or row.get("ft_ord_unpr3"))
        price_text = "-" if price <= 0 else self._format_price(price, "USD")
        order_no = str(row.get("order_no") or row.get("odno") or "").strip()
        parts = [
            f"{time_text} 해외 {symbol}",
            side_text,
            price_text,
            f"x{qty}",
        ]
        if order_no:
            parts.append(f"주문번호={order_no}")
        age_parts = self._format_open_order_age_parts(created_at)
        parts.extend(age_parts)
        return " ".join(parts)

    def _filter_stale_live_open_orders(
        self,
        rows: list[dict],
        *,
        stale_threshold_min: int = 30,
        now: datetime | None = None,
    ) -> list[dict]:
        current = now or datetime.now(timezone.utc)
        result: list[dict] = []
        for row in rows:
            created_at = row.get("created_at")
            if not isinstance(created_at, datetime):
                continue
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            age_min = int(max((current - created_at).total_seconds(), 0.0) // 60)
            if age_min >= stale_threshold_min:
                result.append(row)
        return result

    @staticmethod
    def _domestic_order_side(row: dict) -> str:
        side_code = str(row.get("sll_buy_dvsn_cd") or "").strip()
        side_name = str(row.get("sll_buy_dvsn_cd_name") or "").strip()
        if side_code == "01" or side_name == "매도":
            return "SELL"
        if side_code == "02" or side_name == "매수":
            return "BUY"
        return ""

    @staticmethod
    def _overseas_order_side(row: dict) -> str:
        side_code = str(row.get("sll_buy_dvsn_cd") or "").strip()
        side_name = str(row.get("sll_buy_dvsn_cd_name") or row.get("sll_buy_dvsn_name") or "").strip()
        raw_side = str(row.get("side") or "").strip().upper()
        if side_code == "01" or side_name == "매도" or raw_side == "SELL":
            return "SELL"
        if side_code == "02" or side_name == "매수" or raw_side == "BUY":
            return "BUY"
        return ""

    @staticmethod
    def _format_open_order_age_parts(
        created_at: object,
        *,
        stale_threshold_min: int = 30,
        now: datetime | None = None,
    ) -> list[str]:
        if not isinstance(created_at, datetime):
            return []
        current = now or datetime.now(timezone.utc)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        age_min = int(max((current - created_at).total_seconds(), 0.0) // 60)
        if age_min < 60:
            age_text = f"{age_min}분"
        else:
            hours, minutes = divmod(age_min, 60)
            age_text = f"{hours}시간{minutes:02d}분"
        parts = [f"경과={age_text}"]
        if age_min >= stale_threshold_min:
            parts.append("주의=장기미체결")
        return parts

    @staticmethod
    def _format_order_event_action(row: dict) -> str:
        status = str(row.get("status", "") or "").upper()
        side = str(row.get("side", "") or "").upper()
        order_kind = str(row.get("order_kind", "") or "").lower()
        is_virtual = bool(int(row.get("is_virtual", 0) or 0))
        if status == "CANCELED":
            return "취소"
        if status in {"REJECTED", "FAILED"}:
            if order_kind == "cancel":
                return "취소거부"
            return "주문거부"
        if status == "RECORDED":
            if is_virtual and side == "BUY":
                return "가상매수기록"
            if is_virtual and side == "SELL":
                return "가상매도기록"
            return "기록"
        if side == "BUY":
            return "매수접수"
        if side == "SELL":
            return "매도접수"
        return status or "-"

    def _format_submitted_order_audit_line(
        self,
        row: dict,
        *,
        live_open_order_keys: set[tuple[str, str]] | None = None,
        live_checked_markets: set[str] | None = None,
    ) -> str:
        created_at = parse_datetime(row.get("created_at"))
        time_text = format_kst_korean(created_at) if created_at else "-"
        market = str(row.get("market", "overseas"))
        symbol = str(row.get("symbol", "-")).upper()
        side = str(row.get("side", "")).upper()
        side_text = "매수접수" if side == "BUY" else "매도접수" if side == "SELL" else "접수"
        qty = int(row.get("requested_qty", 0) or 0)
        price = float(row.get("requested_price", 0.0) or 0.0)
        currency = "KRW" if market == "domestic" else "USD"
        price_text = "-" if price <= 0 else self._format_price(price, currency)
        order_no = str(row.get("broker_order_no", "") or "").strip()
        parts = [
            f"{time_text} {format_market_korean(market)} {symbol}",
            side_text,
            price_text,
            f"x{qty}",
            "확인필요=MTS/잔고",
        ]
        if order_no:
            parts.append(f"주문번호={order_no}")
            live_key = (market, order_no)
            if live_key in (live_open_order_keys or set()):
                parts.append("브로커상태=미체결")
            elif market in (live_checked_markets or set()):
                parts.append("브로커상태=미체결목록없음")
        followup_status = str(row.get("followup_status") or "").strip().upper()
        if followup_status:
            followup_reason = format_reason_korean(str(row.get("followup_reason") or "-"))
            followup_action = self._format_order_event_action(
                {
                    "status": followup_status,
                    "order_kind": "cancel",
                    "side": side,
                    "is_virtual": 0,
                }
            )
            parts.append(f"후속={followup_action}")
            parts.append(f"후속사유={followup_reason}")
        return " ".join(parts)

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
            "/lab_cancel_stale_domestic": "cancel_stale_domestic",
            "/lab_cancel_stale_domestic_confirm": "cancel_stale_domestic_confirm",
            "/lab_cancel_stale_overseas": "cancel_stale_overseas",
            "/lab_cancel_stale_overseas_confirm": "cancel_stale_overseas_confirm",
            "/lab_portfolio": "portfolio",
            "/lab_trim_virtual": "trim_virtual",
            "/lab_trim_virtual_confirm": "trim_virtual_confirm",
            "/lab_reset": "reset_virtual",
            "/lab_reset_confirm": "reset_virtual_confirm",
            "/lab_relist_schedule": "relist_schedule",
            "/lab_cb_reset": "cb_reset",
            "/lab_gitlog": "gitlog",
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
        market = format_market_korean(str(watch_target.get("market", "overseas")))
        code = str(symbol_label or watch_target.get("code", "-"))
        action_bias = str(watch_target.get("action_bias", "WAIT")).upper()
        strategy_flag = str(watch_target.get("strategy_flag", "") or "")
        note_raw = str(watch_target.get("note", "-"))
        note = format_reason_korean(note_raw)
        price = watch_target.get("price", "-")
        if isinstance(price, (int, float)):
            if float(price) >= 1000:
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
